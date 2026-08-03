"""استئناف الحملات التي كانت قيد التنفيذ قبل توقف العملية.

بعد إعادة تشغيل الحاوية لا يوجد مُنفِّذ حيّ، فأي حملة بحالة running في
قاعدة البيانات هي حملة معلّقة (stale) بحكم الواقع.
"""

from __future__ import annotations

import logging

from motor.motor_asyncio import AsyncIOMotorDatabase
from pyrogram import Client
from pyrogram.errors import RPCError

from controlbot.handlers.campaign import spawn_campaign
from db import CampaignsRepo, CampaignStatus, LogsRepo

log = logging.getLogger(__name__)

# سقف الحملات المستأنفة معاً؛ ما زاد يُترك paused ليقرره المالك
MAX_AUTO_RESUME = 3


async def auto_resume(
    control: Client, owner_id: int, db: AsyncIOMotorDatabase
) -> list[str]:
    """يستأنف الحملات المعلّقة ويبلّغ المالك. يعيد قائمة المعرّفات المستأنفة."""
    campaigns = CampaignsRepo(db)
    logs = LogsRepo(db)

    stale = await campaigns.list_by_status(CampaignStatus.RUNNING, limit=50)
    if not stale:
        log.info("لا توجد حملات معلّقة تحتاج استئنافاً.")
        return []

    # الأقدم أولاً حتى تُستأنف بترتيب إنشائها
    stale.sort(key=lambda doc: doc.get("created_at") or 0)

    resumed: list[str] = []
    deferred: list[str] = []
    report: list[str] = []

    for doc in stale:
        campaign_id = doc["campaign_id"]

        if len(resumed) >= MAX_AUTO_RESUME:
            if await campaigns.update_status(
                campaign_id, CampaignStatus.PAUSED, expected_status=CampaignStatus.RUNNING
            ):
                deferred.append(campaign_id)
            continue

        stats = await logs.stats(campaign_id)
        sent = stats.get("success", 0)
        failed = stats.get("failed", 0)
        expected = int(doc.get("expected_messages") or 0)

        # resume=True يجعل send_campaign يقرأ logs ويتخطى ما أُرسل بنجاح فعلاً
        if not spawn_campaign(control, campaign_id, owner_id, resume=True):
            log.warning("الحملة %s لها مهمة حيّة بالفعل؛ تُركت كما هي.", campaign_id)
            continue

        resumed.append(campaign_id)
        report.append(
            f"• `{campaign_id}` — أُرسل **{sent}**"
            + (f" من **{expected}**" if expected else "")
            + (f"، فشل **{failed}**" if failed else "")
        )
        log.info(
            "استئناف تلقائي للحملة %s | نجاح سابق=%d فشل=%d متوقَّع=%s",
            campaign_id,
            sent,
            failed,
            expected or "?",
        )

    if not resumed and not deferred:
        return []

    lines = ["♻️ **استئناف تلقائي بعد إعادة التشغيل**", ""]
    if resumed:
        lines += [f"استُؤنفت **{len(resumed)}** حملة من حيث توقفت:", *report]
    if deferred:
        lines += [
            "",
            f"⏸️ أُوقفت مؤقتاً **{len(deferred)}** حملة لتجاوز حد الاستئناف التلقائي "
            f"({MAX_AUTO_RESUME}):",
            *(f"• `{cid}`" for cid in deferred),
            "استأنفها يدوياً من «الحملات النشطة».",
        ]

    try:
        await control.send_message(owner_id, "\n".join(lines))
    except RPCError as exc:
        # يفشل عادة إذا لم يبدأ المالك محادثة مع البوت بعد
        log.warning("تعذّر إبلاغ المالك بالاستئناف التلقائي: %s", exc)

    return resumed
