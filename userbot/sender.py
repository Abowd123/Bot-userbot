"""تنفيذ حملات البث: الإرسال، الفواصل الزمنية، التحكم، وتسجيل النتائج.

لا يحتوي على أي منطق أوامر أو واجهة؛ يُستدعى send_campaign من الخارج.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from pyrogram import Client
from pyrogram.errors import (
    ChannelPrivate,
    ChatWriteForbidden,
    FloodWait,
    InputUserDeactivated,
    PeerFlood,
    PeerIdInvalid,
    RPCError,
    SlowmodeWait,
    UserIsBlocked,
    UserIsBot,
)

from db import (
    CampaignsRepo,
    CampaignStatus,
    ContentKind,
    LogsRepo,
    LogStatus,
    TargetsRepo,
    get_db,
)

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ ثوابت الضبط

# حد أدنى مفروض للفاصل بين رسائل نفس الهدف، حتى لو طلب المستخدم أقل
MIN_INTERVAL_BETWEEN_MESSAGES_SEC = 3

# كل كم ثانية نعيد قراءة حالة الحملة أثناء التوقف المؤقت أو أثناء النوم الطويل
STATUS_POLL_INTERVAL_SEC = 5

# ثانيتان إضافيتان فوق مدة FloodWait؛ العودة في اللحظة نفسها تعيد الخطأ غالباً
FLOOD_WAIT_BUFFER_SEC = 2

# أقصى عدد محاولات لنفس الرسالة عند تكرار FloodWait
MAX_FLOOD_RETRIES = 5

# FloodWait أطول من هذا لا ننتظره داخل الحملة؛ نوقفها مؤقتاً بدل تعليق المُنفِّذ
MAX_FLOOD_WAIT_SEC = 3600

# PeerFlood خطر على مستوى الحساب لا على مستوى الهدف
PEER_FLOOD_COOLDOWN_SEC = 60
MAX_CONSECUTIVE_PEER_FLOOD = 3

# أخطاء تعني أن هذا الهدف تحديداً لم يعد قابلاً للمراسلة
DEAD_TARGET_ERRORS = (
    UserIsBlocked,
    ChatWriteForbidden,
    PeerIdInvalid,
    InputUserDeactivated,
    ChannelPrivate,
    UserIsBot,
)

MAX_ERROR_REASON_LEN = 300


class FloodRetriesExhausted(Exception):
    """تكرر FloodWait لنفس الرسالة أكثر من MAX_FLOOD_RETRIES."""


class FloodWaitTooLong(Exception):
    """مدة FloodWait تجاوزت MAX_FLOOD_WAIT_SEC."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"FloodWait {seconds}s يتجاوز الحد المسموح {MAX_FLOOD_WAIT_SEC}s")
        self.seconds = seconds


@dataclass
class CampaignResult:
    campaign_id: str
    final_status: CampaignStatus
    targets_total: int = 0
    targets_processed: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    deactivated: list[int] = field(default_factory=list)
    abort_reason: str | None = None

    def summary(self) -> str:
        return (
            f"campaign={self.campaign_id} status={self.final_status.value} "
            f"targets={self.targets_processed}/{self.targets_total} "
            f"sent={self.sent} failed={self.failed} skipped={self.skipped}"
        )


# ------------------------------------------------------------------ أدوات مساعدة

def _reason(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".strip()
    return text[:MAX_ERROR_REASON_LEN]


def _target_label(target: dict[str, Any]) -> str:
    return target.get("title") or str(target.get("chat_id"))


async def _resolve_control(
    campaigns: CampaignsRepo, campaign_id: str
) -> CampaignStatus | None:
    """يعيد الحالة التي يجب التصرف عليها، بعد استيعاب التوقف المؤقت.

    - running / pending  -> أكمل
    - paused             -> يبقى في حلقة انتظار حتى يتغير الوضع
    - stopped / done     -> اخرج
    - None               -> الحملة حُذفت من قاعدة البيانات
    """
    announced_pause = False

    while True:
        doc = await campaigns.get(campaign_id)
        if doc is None:
            return None

        status = CampaignStatus(doc["status"])

        if status in (CampaignStatus.RUNNING, CampaignStatus.PENDING):
            if announced_pause:
                log.info("الحملة %s استُؤنفت.", campaign_id)
            return status

        if status is CampaignStatus.PAUSED:
            if not announced_pause:
                log.info(
                    "الحملة %s موقوفة مؤقتاً؛ في انتظار الاستئناف أو الإيقاف.",
                    campaign_id,
                )
                announced_pause = True
            await asyncio.sleep(STATUS_POLL_INTERVAL_SEC)
            continue

        # stopped أو done
        return status


async def _sleep_checked(
    seconds: float, campaigns: CampaignsRepo, campaign_id: str
) -> bool:
    """ينام على دفعات ويتحقق من الإيقاف بينها.

    النوم المتصل يجعل أمر stop بلا تأثير حتى انتهاء الفاصل، وهو أمر ملحوظ
    عندما تكون الفواصل بالدقائق. يعيد False إذا صارت الحملة stopped/done/محذوفة.
    """
    remaining = float(seconds)
    while remaining > 0:
        chunk = min(remaining, STATUS_POLL_INTERVAL_SEC)
        await asyncio.sleep(chunk)
        remaining -= chunk

        if remaining > 0:
            doc = await campaigns.get(campaign_id)
            if doc is None:
                return False
            if CampaignStatus(doc["status"]) in (
                CampaignStatus.STOPPED,
                CampaignStatus.DONE,
            ):
                return False
    return True


# ------------------------------------------------------------------ الإرسال الفعلي

async def _dispatch(client: Client, chat_id: int, content: dict[str, Any]) -> Any:
    """ينفّذ استدعاء Pyrogram المناسب لنوع المحتوى. لا يعالج أي خطأ."""
    kind = ContentKind(content["kind"])

    if kind is ContentKind.TEXT:
        return await client.send_message(
            chat_id, content["text"], disable_web_page_preview=True
        )

    if kind is ContentKind.MEDIA:
        # send_cached_media يقبل أي file_id مخزَّن دون معرفة نوع الوسيط مسبقاً
        return await client.send_cached_media(
            chat_id, content["file_id"], caption=content.get("caption") or None
        )

    if kind is ContentKind.FORWARD:
        return await client.forward_messages(
            chat_id=chat_id,
            from_chat_id=content["from_chat_id"],
            message_ids=content["message_id"],
        )

    raise ValueError(f"نوع محتوى غير مدعوم: {content.get('kind')}")


async def _send_with_flood_retry(
    client: Client, chat_id: int, content: dict[str, Any], campaign_id: str
) -> Any:
    """يرسل رسالة واحدة، ويعيد المحاولة على نفس الرسالة عند FloodWait.

    FloodWait و SlowmodeWait كلاهما يطلب انتظاراً محدداً من تيليجرام،
    فيُعالجان بالطريقة نفسها. بقية الأخطاء تُمرَّر للمستدعي ليصنّفها.
    """
    for attempt in range(1, MAX_FLOOD_RETRIES + 1):
        try:
            return await _dispatch(client, chat_id, content)

        except (FloodWait, SlowmodeWait) as exc:
            wait_for = int(getattr(exc, "value", 0) or 0)

            if wait_for > MAX_FLOOD_WAIT_SEC:
                raise FloodWaitTooLong(wait_for) from exc

            total = wait_for + FLOOD_WAIT_BUFFER_SEC
            log.warning(
                "%s: تيليجرام يطلب انتظار %ds (chat_id=%s، محاولة %d/%d) "
                "— انتظار %ds ثم إعادة إرسال نفس الرسالة | campaign=%s",
                type(exc).__name__,
                wait_for,
                chat_id,
                attempt,
                MAX_FLOOD_RETRIES,
                total,
                campaign_id,
            )
            await asyncio.sleep(total)

    raise FloodRetriesExhausted(
        f"تكرر FloodWait {MAX_FLOOD_RETRIES} مرات على chat_id={chat_id}"
    )


# ------------------------------------------------------------------ الدالة الرئيسية

async def send_campaign(
    client: Client,
    campaign_id: str,
    *,
    db: AsyncIOMotorDatabase | None = None,
    resume: bool = False,
) -> CampaignResult:
    """ينفّذ حملة بث كاملة على الأهداف النشطة.

    resume=True يتخطى عدد الرسائل الناجحة المسجَّلة مسبقاً لكل هدف، وهو
    المطلوب عند استئناف حملة بعد إعادة تشغيل الحاوية.
    """
    database = db if db is not None else get_db()
    campaigns = CampaignsRepo(database)
    targets_repo = TargetsRepo(database)
    logs = LogsRepo(database)

    campaign = await campaigns.get(campaign_id)
    if campaign is None:
        raise ValueError(f"لا توجد حملة بالمعرف {campaign_id}.")

    current_status = CampaignStatus(campaign["status"])
    if current_status in (CampaignStatus.STOPPED, CampaignStatus.DONE):
        log.warning(
            "الحملة %s حالتها %s؛ لا شيء لتنفيذه.", campaign_id, current_status.value
        )
        return CampaignResult(campaign_id, current_status)

    content: dict[str, Any] = campaign["content"]
    repeat_count = max(1, int(campaign.get("repeat_count", 1)))

    raw_msg_interval = int(campaign.get("interval_between_messages_sec", 0) or 0)
    msg_interval = max(raw_msg_interval, MIN_INTERVAL_BETWEEN_MESSAGES_SEC)
    if raw_msg_interval < MIN_INTERVAL_BETWEEN_MESSAGES_SEC:
        log.info(
            "الفاصل بين الرسائل %ds أقل من الحد الأدنى؛ رُفع إلى %ds | campaign=%s",
            raw_msg_interval,
            msg_interval,
            campaign_id,
        )

    target_interval = max(0, int(campaign.get("interval_between_targets_sec", 0) or 0))

    targets = await targets_repo.list_active()
    result = CampaignResult(
        campaign_id=campaign_id,
        final_status=current_status,
        targets_total=len(targets),
    )

    if not targets:
        log.warning("لا توجد أهداف نشطة؛ تُعتبر الحملة %s منتهية.", campaign_id)
        await campaigns.update_status(campaign_id, CampaignStatus.DONE)
        result.final_status = CampaignStatus.DONE
        return result

    # لقطة الحجم؛ قسم التحكم يحسب منها نسبة التقدّم
    await campaigns.set_totals(campaign_id, len(targets), len(targets) * repeat_count)

    # الانتقال إلى running ذرياً؛ إن فشل فقد سبقنا مُنفِّذ آخر أو صدر أمر إيقاف
    if current_status is CampaignStatus.PENDING:
        if not await campaigns.update_status(
            campaign_id, CampaignStatus.RUNNING, expected_status=CampaignStatus.PENDING
        ):
            fresh = await campaigns.get(campaign_id)
            status = CampaignStatus(fresh["status"]) if fresh else CampaignStatus.STOPPED
            log.warning(
                "تعذّر تشغيل الحملة %s؛ حالتها الآن %s.", campaign_id, status.value
            )
            result.final_status = status
            return result
    else:
        await campaigns.update_status(campaign_id, CampaignStatus.RUNNING)

    log.info(
        "بدء الحملة %s | نوع المحتوى=%s | أهداف=%d | تكرار=%d "
        "| فاصل الرسائل=%ds | فاصل الأهداف=%ds",
        campaign_id,
        content.get("kind"),
        len(targets),
        repeat_count,
        msg_interval,
        target_interval,
    )

    consecutive_peer_flood = 0
    stop_campaign = False
    final_status = CampaignStatus.DONE

    try:
        for index, target in enumerate(targets, start=1):
            chat_id = int(target["chat_id"])
            label = _target_label(target)

            # 1) نقطة تحكم قبل كل هدف
            control = await _resolve_control(campaigns, campaign_id)
            if control is None:
                log.warning("الحملة %s حُذفت أثناء التنفيذ؛ توقف.", campaign_id)
                final_status = CampaignStatus.STOPPED
                break
            if control in (CampaignStatus.STOPPED, CampaignStatus.DONE):
                log.info("أمر %s على الحملة %s؛ خروج فوري.", control.value, campaign_id)
                final_status = control
                break

            # 2) الاستئناف: تخطي ما أُرسل بنجاح سابقاً لهذا الهدف
            start_iteration = 1
            if resume:
                done_before = await logs.success_count(campaign_id, chat_id)
                if done_before >= repeat_count:
                    result.skipped += 1
                    log.info("تخطي %s (%s): مكتمل مسبقاً.", label, chat_id)
                    continue
                start_iteration = done_before + 1

            result.targets_processed += 1
            target_aborted = False

            for iteration in range(start_iteration, repeat_count + 1):
                # 3) نقطة تحكم قبل كل رسالة
                control = await _resolve_control(campaigns, campaign_id)
                if control is None or control in (
                    CampaignStatus.STOPPED,
                    CampaignStatus.DONE,
                ):
                    final_status = control or CampaignStatus.STOPPED
                    stop_campaign = True
                    break

                try:
                    await _send_with_flood_retry(client, chat_id, content, campaign_id)

                except DEAD_TARGET_ERRORS as exc:
                    # الهدف نفسه ميت: نسجّل، نعطّله، ونكمل للهدف التالي
                    await logs.add(campaign_id, chat_id, LogStatus.FAILED, _reason(exc))
                    result.failed += 1
                    await targets_repo.update_status(chat_id, is_active=False)
                    result.deactivated.append(chat_id)
                    log.warning(
                        "الهدف %s (%s) عُطّل: %s", label, chat_id, type(exc).__name__
                    )
                    target_aborted = True
                    break

                except PeerFlood as exc:
                    # خطر على مستوى الحساب لا على مستوى الهدف
                    await logs.add(campaign_id, chat_id, LogStatus.FAILED, _reason(exc))
                    result.failed += 1
                    consecutive_peer_flood += 1
                    log.error(
                        "PeerFlood على %s (%s) — الحساب مقيَّد مؤقتاً "
                        "(متتالية %d/%d). تهدئة %ds ثم متابعة.",
                        label,
                        chat_id,
                        consecutive_peer_flood,
                        MAX_CONSECUTIVE_PEER_FLOOD,
                        PEER_FLOOD_COOLDOWN_SEC,
                    )

                    if consecutive_peer_flood >= MAX_CONSECUTIVE_PEER_FLOOD:
                        result.abort_reason = (
                            f"PeerFlood متتالٍ {consecutive_peer_flood} مرات؛ "
                            "أُوقفت الحملة مؤقتاً لحماية الحساب من الحظر."
                        )
                        log.critical("%s | campaign=%s", result.abort_reason, campaign_id)
                        final_status = CampaignStatus.PAUSED
                        stop_campaign = True
                        break

                    if not await _sleep_checked(
                        PEER_FLOOD_COOLDOWN_SEC, campaigns, campaign_id
                    ):
                        final_status = CampaignStatus.STOPPED
                        stop_campaign = True
                        break

                    target_aborted = True
                    break

                except FloodWaitTooLong as exc:
                    await logs.add(campaign_id, chat_id, LogStatus.FAILED, _reason(exc))
                    result.failed += 1
                    result.abort_reason = str(exc)
                    log.critical(
                        "FloodWait %ds طويل جداً؛ أُوقفت الحملة %s مؤقتاً "
                        "لاستئنافها لاحقاً بـ resume=True.",
                        exc.seconds,
                        campaign_id,
                    )
                    final_status = CampaignStatus.PAUSED
                    stop_campaign = True
                    break

                except FloodRetriesExhausted as exc:
                    await logs.add(campaign_id, chat_id, LogStatus.FAILED, _reason(exc))
                    result.failed += 1
                    log.error("%s | campaign=%s", exc, campaign_id)
                    target_aborted = True
                    break

                except (ValueError, KeyError) as exc:
                    # محتوى الحملة نفسه معطوب؛ تكرار المحاولة بلا جدوى
                    await logs.add(campaign_id, chat_id, LogStatus.FAILED, _reason(exc))
                    result.failed += 1
                    result.abort_reason = f"محتوى الحملة غير صالح: {exc}"
                    log.critical("%s | campaign=%s", result.abort_reason, campaign_id)
                    final_status = CampaignStatus.STOPPED
                    stop_campaign = True
                    break

                except RPCError as exc:
                    # أي خطأ آخر من تيليجرام: يُسجَّل ونتابع للهدف التالي
                    await logs.add(campaign_id, chat_id, LogStatus.FAILED, _reason(exc))
                    result.failed += 1
                    log.warning(
                        "فشل الإرسال إلى %s (%s): %s", label, chat_id, _reason(exc)
                    )
                    target_aborted = True
                    break

                except (OSError, ConnectionError, asyncio.TimeoutError) as exc:
                    await logs.add(campaign_id, chat_id, LogStatus.FAILED, _reason(exc))
                    result.failed += 1
                    log.warning("مشكلة شبكة عند %s (%s): %s", label, chat_id, exc)
                    target_aborted = True
                    break

                else:
                    await logs.add(campaign_id, chat_id, LogStatus.SUCCESS)
                    result.sent += 1
                    consecutive_peer_flood = 0
                    log.info(
                        "أُرسلت %d/%d إلى %s (%s) | الهدف %d/%d",
                        iteration,
                        repeat_count,
                        label,
                        chat_id,
                        index,
                        len(targets),
                    )

                # 4) الفاصل بين رسائل نفس الهدف — لا ينام بعد الرسالة الأخيرة
                if iteration < repeat_count:
                    if not await _sleep_checked(msg_interval, campaigns, campaign_id):
                        final_status = CampaignStatus.STOPPED
                        stop_campaign = True
                        break

            if stop_campaign:
                break

            # 5) الفاصل قبل الانتقال إلى هدف جديد
            if target_interval and index < len(targets):
                if not await _sleep_checked(target_interval, campaigns, campaign_id):
                    final_status = CampaignStatus.STOPPED
                    break

            _ = target_aborted  # الهدف انتهى مبكراً، لكن الحملة تكمل

    except asyncio.CancelledError:
        # إيقاف الحاوية أو إلغاء المهمة: تُترك الحملة paused لتُستأنف لاحقاً
        log.warning("أُلغيت مهمة الحملة %s؛ حالتها paused.", campaign_id)
        await campaigns.update_status(campaign_id, CampaignStatus.PAUSED)
        result.final_status = CampaignStatus.PAUSED
        raise

    except Exception as exc:  # noqa: BLE001
        log.exception("خطأ غير متوقع في الحملة %s؛ أُوقفت مؤقتاً.", campaign_id)
        result.abort_reason = _reason(exc)
        await campaigns.update_status(campaign_id, CampaignStatus.PAUSED)
        result.final_status = CampaignStatus.PAUSED
        return result

    # لا نكتب الحالة النهائية فوق أمر stop صادر من المستخدم
    if final_status is not CampaignStatus.STOPPED:
        await campaigns.update_status(campaign_id, final_status)
    result.final_status = final_status

    log.info("انتهت الحملة | %s", result.summary())
    return result
