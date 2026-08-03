"""متابعة الحملات النشطة والتحكم فيها: إيقاف مؤقت، استئناف، إيقاف نهائي.

الرسالة تُحدَّث تلقائياً كل REFRESH_INTERVAL_SEC ثانية طالما الحملة running،
عبر مهمة مراقبة واحدة مرتبطة بكل رسالة.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from pyrogram import Client
from pyrogram.errors import FloodWait, MessageIdInvalid, MessageNotModified, RPCError
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from controlbot.context import RuntimeNotReady, runtime
from controlbot.handlers.base import Route, edit, on_before_dispatch, route
from controlbot.handlers.campaign import running_campaign_ids, spawn_campaign
from controlbot.keyboards import CB_CONTROL, CB_MENU, cb
from db import CampaignsRepo, CampaignStatus, LogsRepo

log = logging.getLogger(__name__)

# كل كم ثانية تُعاد قراءة التقدّم وتُعدَّل الرسالة
REFRESH_INTERVAL_SEC = 5

# سقف عمر مهمة المراقبة، حماية من تسريب مهام تعمل للأبد
MAX_WATCH_DURATION_SEC = 6 * 3600

LIST_LIMIT = 20
BAR_WIDTH = 12

STATUS_EMOJI = {
    CampaignStatus.PENDING: "🕐",
    CampaignStatus.RUNNING: "🟢",
    CampaignStatus.PAUSED: "⏸️",
    CampaignStatus.STOPPED: "⏹️",
    CampaignStatus.DONE: "✅",
}

STATUS_LABEL = {
    CampaignStatus.PENDING: "في الانتظار",
    CampaignStatus.RUNNING: "قيد التنفيذ",
    CampaignStatus.PAUSED: "موقوفة مؤقتاً",
    CampaignStatus.STOPPED: "موقوفة نهائياً",
    CampaignStatus.DONE: "مكتملة",
}

TERMINAL = (CampaignStatus.STOPPED, CampaignStatus.DONE)


class MessageGone(Exception):
    """الرسالة المراقَبة حُذفت أو صارت غير قابلة للتعديل."""


# ------------------------------------------------------------------ التقدّم

@dataclass(frozen=True)
class Progress:
    status: CampaignStatus
    sent: int
    failed: int
    expected: int          # إجمالي الرسائل المتوقَّع (أهداف × تكرار)
    targets_total: int
    targets_done: int      # أهداف ظهرت في السجل (نجحت أو فشلت)
    repeat_count: int
    started_at: datetime | None
    finished_at: datetime | None

    @property
    def processed(self) -> int:
        return self.sent + self.failed

    @property
    def percent(self) -> int:
        if self.expected <= 0:
            return 0
        return min(100, int(self.processed * 100 / self.expected))

    @property
    def looks_complete(self) -> bool:
        """كل الأهداف تمت معالجتها وبلغ عدد الرسائل الحد المتوقَّع."""
        return (
            self.targets_total > 0
            and self.expected > 0
            and self.targets_done >= self.targets_total
            and self.processed >= self.expected
        )


async def _progress(campaign: dict) -> Progress:
    logs = LogsRepo(runtime.db)
    campaign_id = campaign["campaign_id"]

    stats, targets_done = await asyncio.gather(
        logs.stats(campaign_id),
        logs.distinct_targets(campaign_id),
    )

    repeat_count = max(1, int(campaign.get("repeat_count", 1)))
    targets_total = int(campaign.get("targets_total") or 0)
    expected = int(campaign.get("expected_messages") or 0)
    # حملات أُنشئت قبل إضافة الحقلين
    if expected <= 0 and targets_total > 0:
        expected = targets_total * repeat_count

    return Progress(
        status=CampaignStatus(campaign["status"]),
        sent=stats.get("success", 0),
        failed=stats.get("failed", 0),
        expected=expected,
        targets_total=targets_total,
        targets_done=targets_done,
        repeat_count=repeat_count,
        started_at=campaign.get("started_at"),
        finished_at=campaign.get("finished_at"),
    )


# ------------------------------------------------------------------ نصوص

def _bar(done: int, total: int) -> str:
    if total <= 0:
        return "▱" * BAR_WIDTH
    filled = int(BAR_WIDTH * min(done, total) / total)
    return "▰" * filled + "▱" * (BAR_WIDTH - filled)


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}س {m}د"
    if m:
        return f"{m}د {s}ث"
    return f"{s}ث"


def _elapsed(prog: Progress) -> str | None:
    if prog.started_at is None:
        return None
    end = prog.finished_at or datetime.now(timezone.utc)
    return _fmt_duration((end - prog.started_at).total_seconds())


def _detail_text(campaign: dict, prog: Progress, notice: str | None = None) -> str:
    campaign_id = campaign["campaign_id"]
    emoji = STATUS_EMOJI.get(prog.status, "•")

    lines = [
        f"{emoji} **الحملة** `{campaign_id}`",
        "",
        f"الحالة: **{STATUS_LABEL.get(prog.status, prog.status.value)}**",
        f"نوع المحتوى: **{campaign.get('content', {}).get('kind', '-')}**",
        "",
        f"`{_bar(prog.processed, prog.expected)}`  **{prog.percent}%**",
        f"الرسائل: **{prog.processed}** من **{prog.expected or '?'}**",
        f"نجاح: **{prog.sent}**  •  فشل: **{prog.failed}**",
        f"المحادثات: **{prog.targets_done}** من **{prog.targets_total or '?'}**",
        f"التكرار لكل محادثة: **{prog.repeat_count}**",
    ]

    elapsed = _elapsed(prog)
    if elapsed:
        label = "المدة" if prog.status in TERMINAL else "منقضٍ"
        lines.append(f"{label}: **{elapsed}**")

    if prog.status is CampaignStatus.RUNNING:
        lines += ["", f"🔄 يُحدَّث تلقائياً كل {REFRESH_INTERVAL_SEC} ثوانٍ."]
    elif prog.status is CampaignStatus.PAUSED:
        lines += ["", "⏸️ التحديث التلقائي متوقف. اضغط استئناف للمتابعة."]

    if notice:
        lines += ["", notice]

    return "\n".join(lines)


def _final_report(campaign_id: str, prog: Progress) -> str:
    lines = [
        f"{STATUS_EMOJI[prog.status]} "
        f"**{'اكتملت' if prog.status is CampaignStatus.DONE else 'أُوقفت'} الحملة** "
        f"`{campaign_id}`",
        "",
        f"الرسائل المرسلة بنجاح: **{prog.sent}**",
        f"الفاشلة: **{prog.failed}**",
        f"المحادثات المعالَجة: **{prog.targets_done}** من **{prog.targets_total or '?'}**",
    ]
    elapsed = _elapsed(prog)
    if elapsed:
        lines.append(f"المدة الكلية: **{elapsed}**")
    if prog.failed:
        lines += ["", "راجع تفاصيل الأخطاء من قسم 📊 السجل."]
    return "\n".join(lines)


# ------------------------------------------------------------------ لوحات

def _list_kb(campaigns: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    for doc in campaigns:
        status = CampaignStatus(doc["status"])
        emoji = STATUS_EMOJI.get(status, "•")
        kind = doc.get("content", {}).get("kind", "-")
        rows.append(
            [
                InlineKeyboardButton(
                    f"{emoji} {doc['campaign_id']} • {kind}",
                    callback_data=cb(CB_CONTROL, "view", doc["campaign_id"]),
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton("🔄 تحديث", callback_data=cb(CB_CONTROL, "active")),
            InlineKeyboardButton("◀️ رجوع", callback_data=cb(CB_MENU, "home")),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _detail_kb(campaign_id: str, status: CampaignStatus) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if status is CampaignStatus.RUNNING:
        rows.append(
            [
                InlineKeyboardButton(
                    "⏸️ إيقاف مؤقت", callback_data=cb(CB_CONTROL, "pause", campaign_id)
                ),
                InlineKeyboardButton(
                    "⏹️ إيقاف نهائي", callback_data=cb(CB_CONTROL, "stop", campaign_id)
                ),
            ]
        )
    elif status in (CampaignStatus.PAUSED, CampaignStatus.PENDING):
        rows.append(
            [
                InlineKeyboardButton(
                    "▶️ استئناف", callback_data=cb(CB_CONTROL, "resume", campaign_id)
                ),
                InlineKeyboardButton(
                    "⏹️ إيقاف نهائي", callback_data=cb(CB_CONTROL, "stop", campaign_id)
                ),
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                "🔄 تحديث", callback_data=cb(CB_CONTROL, "view", campaign_id)
            ),
            InlineKeyboardButton("◀️ القائمة", callback_data=cb(CB_CONTROL, "active")),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _stop_confirm_kb(campaign_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⏹️ نعم، أوقفها نهائياً",
                    callback_data=cb(CB_CONTROL, "stopc", campaign_id),
                )
            ],
            [
                InlineKeyboardButton(
                    "◀️ تراجع", callback_data=cb(CB_CONTROL, "view", campaign_id)
                )
            ],
        ]
    )


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("◀️ القائمة", callback_data=cb(CB_CONTROL, "active")),
                InlineKeyboardButton("🏠 الرئيسية", callback_data=cb(CB_MENU, "home")),
            ]
        ]
    )


# ------------------------------------------------------------------ الإنهاء التلقائي

async def _finalize_if_complete(
    client: Client, campaign: dict, prog: Progress, notify_chat_id: int
) -> bool:
    """يضع الحملة على done عند اكتمالها ويرسل تقريراً نهائياً.

    شبكة أمان: send_campaign هو المسؤول الأساسي عن الانتقال إلى done. هذا
    يتدخّل فقط عندما لا توجد مهمة تنفيذ حيّة (مثلاً بعد إعادة تشغيل الحاوية
    وقد كانت الحملة قد أنهت كل أهدافها فعلياً).
    """
    campaign_id = campaign["campaign_id"]

    if prog.status is not CampaignStatus.RUNNING or not prog.looks_complete:
        return False
    if campaign_id in running_campaign_ids():
        # المُنفِّذ يعمل وسيتولّى الإنهاء والتقرير بنفسه
        return False

    if not await CampaignsRepo(runtime.db).update_status(
        campaign_id, CampaignStatus.DONE, expected_status=CampaignStatus.RUNNING
    ):
        return False

    log.info("أُنهيت الحملة %s تلقائياً (كل الأهداف معالَجة).", campaign_id)
    try:
        await client.send_message(
            notify_chat_id,
            _final_report(campaign_id, prog).replace(
                STATUS_EMOJI[prog.status], STATUS_EMOJI[CampaignStatus.DONE], 1
            ),
        )
    except RPCError:
        log.warning("تعذّر إرسال التقرير النهائي للحملة %s.", campaign_id)
    return True


# ------------------------------------------------------------------ التحديث التلقائي

async def _safe_edit(
    client: Client,
    chat_id: int,
    message_id: int,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    try:
        await client.edit_message_text(chat_id, message_id, text, reply_markup=keyboard)
    except MessageNotModified:
        pass
    except MessageIdInvalid as exc:
        raise MessageGone("الرسالة لم تعد موجودة.") from exc
    except FloodWait as exc:
        wait_for = int(getattr(exc, "value", 0) or 0)
        log.warning("FloodWait %ds أثناء تحديث رسالة المتابعة.", wait_for)
        await asyncio.sleep(wait_for + 2)
    except RPCError as exc:
        log.warning("تعذّر تحديث رسالة المتابعة: %s", exc)


class WatcherRegistry:
    """مهمة مراقبة واحدة لكل رسالة، مفتاحها (chat_id, message_id)."""

    def __init__(self) -> None:
        self._tasks: dict[tuple[int, int], asyncio.Task] = {}

    def start(
        self, client: Client, chat_id: int, message_id: int, campaign_id: str
    ) -> None:
        key = (chat_id, message_id)
        self.stop(chat_id, message_id)
        task = asyncio.create_task(
            _watch(client, chat_id, message_id, campaign_id),
            name=f"watch:{campaign_id}:{message_id}",
        )
        self._tasks[key] = task
        task.add_done_callback(lambda _t: self._tasks.pop(key, None))

    def stop(self, chat_id: int, message_id: int) -> None:
        task = self._tasks.pop((chat_id, message_id), None)
        if task is not None and not task.done():
            task.cancel()

    def stop_all(self) -> None:
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        self._tasks.clear()


watchers = WatcherRegistry()


async def _watch(client: Client, chat_id: int, message_id: int, campaign_id: str) -> None:
    """يعدّل نفس الرسالة كل REFRESH_INTERVAL_SEC ثانية حتى تخرج الحملة من running."""
    repo = CampaignsRepo(runtime.db)
    deadline = time.monotonic() + MAX_WATCH_DURATION_SEC
    last_text: str | None = None

    try:
        while True:
            await asyncio.sleep(REFRESH_INTERVAL_SEC)

            if time.monotonic() > deadline:
                log.info("انتهى سقف مراقبة الحملة %s.", campaign_id)
                await _safe_edit(
                    client,
                    chat_id,
                    message_id,
                    "⏳ توقف التحديث التلقائي (تجاوز المدة القصوى).\n"
                    "اضغط تحديث لعرض الحالة الحالية.",
                    _detail_kb(campaign_id, CampaignStatus.RUNNING),
                )
                return

            campaign = await repo.get(campaign_id)
            if campaign is None:
                await _safe_edit(
                    client, chat_id, message_id, "⚠️ حُذفت هذه الحملة.", _back_kb()
                )
                return

            prog = await _progress(campaign)

            if await _finalize_if_complete(client, campaign, prog, chat_id):
                campaign = await repo.get(campaign_id) or campaign
                prog = await _progress(campaign)

            text = _detail_text(campaign, prog)
            if text != last_text:
                await _safe_edit(
                    client,
                    chat_id,
                    message_id,
                    text,
                    _detail_kb(campaign_id, prog.status),
                )
                last_text = text

            # التحديث التلقائي مقصور على running حسب المطلوب
            if prog.status is not CampaignStatus.RUNNING:
                return

    except MessageGone:
        log.debug("توقفت مراقبة %s: الرسالة غير موجودة.", campaign_id)
    except asyncio.CancelledError:
        raise
    except RuntimeNotReady as exc:
        log.error("توقفت مراقبة %s: %s", campaign_id, exc)
    except Exception:  # noqa: BLE001
        log.exception("خطأ في مراقبة الحملة %s.", campaign_id)


@on_before_dispatch
async def _stop_watcher_on_any_press(
    client: Client, query: CallbackQuery, route_obj: Route
) -> None:
    """يوقف مراقبة الرسالة قبل تنفيذ أي معالج.

    بدون هذا، ستكتب مهمة المراقبة تفاصيل الحملة فوق أي شاشة أخرى يفتحها
    المالك في نفس الرسالة. المعالج أدناه يعيد تشغيلها عند الحاجة.
    """
    message = query.message
    if message is not None:
        watchers.stop(message.chat.id, message.id)


# ------------------------------------------------------------------ المعالج

@route(CB_CONTROL)
async def handle_control(client: Client, query: CallbackQuery, route_obj: Route) -> None:
    action = route_obj.action

    if action == "noop":
        return

    try:
        if action == "active":
            await _render_list(query)

        elif action == "view":
            await _render_detail(client, query, route_obj.arg(0, ""))

        elif action == "pause":
            await _change(client, query, route_obj.arg(0, ""), CampaignStatus.PAUSED)

        elif action == "resume":
            await _resume(client, query, route_obj.arg(0, ""))

        elif action == "stop":
            await _confirm_stop(query, route_obj.arg(0, ""))

        elif action == "stopc":
            await _change(client, query, route_obj.arg(0, ""), CampaignStatus.STOPPED)

        else:
            await _render_list(query, notice="⚠️ إجراء غير معروف.")

    except RuntimeNotReady as exc:
        log.error("سياق التشغيل غير مكتمل: %s", exc)
        await edit(query, f"⚠️ {exc}", _back_kb())

    except RPCError as exc:
        log.exception("خطأ تيليجرام في قسم التحكم.")
        await edit(query, f"⚠️ خطأ من تيليجرام: `{type(exc).__name__}`", _back_kb())


async def _render_list(query: CallbackQuery, notice: str | None = None) -> None:
    campaigns = await CampaignsRepo(runtime.db).list_running_or_paused(limit=LIST_LIMIT)

    if not campaigns:
        text = "**▶️ الحملات النشطة**\n\nلا توجد حملات قيد التنفيذ أو موقوفة مؤقتاً."
        if notice:
            text += f"\n\n{notice}"
        await edit(query, text, _list_kb([]))
        return

    running = sum(1 for c in campaigns if c["status"] == CampaignStatus.RUNNING.value)
    paused = len(campaigns) - running

    lines = [
        "**▶️ الحملات النشطة**",
        "",
        f"🟢 قيد التنفيذ: **{running}**  •  ⏸️ موقوفة مؤقتاً: **{paused}**",
        "",
        "اختر حملة لعرض تفاصيلها والتحكم بها.",
    ]
    if notice:
        lines += ["", notice]

    await edit(query, "\n".join(lines), _list_kb(campaigns))


async def _render_detail(
    client: Client,
    query: CallbackQuery,
    campaign_id: str,
    notice: str | None = None,
) -> None:
    if not campaign_id:
        await _render_list(query, notice="⚠️ معرّف حملة غير صالح.")
        return

    campaign = await CampaignsRepo(runtime.db).get(campaign_id)
    if campaign is None:
        await _render_list(query, notice=f"⚠️ لا توجد حملة بالمعرّف `{campaign_id}`.")
        return

    prog = await _progress(campaign)
    chat_id = query.message.chat.id

    if await _finalize_if_complete(client, campaign, prog, chat_id):
        campaign = await CampaignsRepo(runtime.db).get(campaign_id) or campaign
        prog = await _progress(campaign)

    await edit(
        query, _detail_text(campaign, prog, notice), _detail_kb(campaign_id, prog.status)
    )

    if prog.status is CampaignStatus.RUNNING:
        watchers.start(client, chat_id, query.message.id, campaign_id)


async def _confirm_stop(query: CallbackQuery, campaign_id: str) -> None:
    await edit(
        query,
        f"⏹️ **إيقاف نهائي للحملة** `{campaign_id}`\n\n"
        "هذا الإجراء **لا يمكن الرجوع عنه**؛ الحملة الموقوفة نهائياً لا تُستأنف، "
        "وستحتاج إلى إنشاء حملة جديدة.\n"
        "إن كنت تريد التوقف المؤقت فقط، استخدم «إيقاف مؤقت».",
        _stop_confirm_kb(campaign_id),
    )


async def _change(
    client: Client, query: CallbackQuery, campaign_id: str, target: CampaignStatus
) -> None:
    """يغيّر حالة الحملة انتقالاً ذرياً، ثم يعيد العرض."""
    repo = CampaignsRepo(runtime.db)

    expected = CampaignStatus.RUNNING if target is CampaignStatus.PAUSED else None
    changed = await repo.update_status(campaign_id, target, expected_status=expected)

    if not changed:
        fresh = await repo.get(campaign_id)
        current = CampaignStatus(fresh["status"]) if fresh else None
        notice = (
            f"⚠️ تعذّر التغيير؛ الحالة الحالية "
            f"**{STATUS_LABEL.get(current, 'غير معروفة')}**."
            if current
            else "⚠️ الحملة غير موجودة."
        )
        await _render_detail(client, query, campaign_id, notice=notice)
        return

    log.info("تغيير حالة الحملة %s إلى %s بأمر من المالك.", campaign_id, target.value)

    if target is CampaignStatus.PAUSED:
        notice = (
            "⏸️ أُوقفت مؤقتاً. المُنفِّذ يتوقف عند أقرب نقطة تحكم "
            f"(خلال {REFRESH_INTERVAL_SEC} ثوانٍ تقريباً)."
        )
    else:
        notice = "⏹️ أُوقفت نهائياً."

    await _render_detail(client, query, campaign_id, notice=notice)

    if target is CampaignStatus.STOPPED:
        campaign = await repo.get(campaign_id)
        if campaign is not None:
            try:
                await client.send_message(
                    query.message.chat.id,
                    _final_report(campaign_id, await _progress(campaign)),
                )
            except RPCError:
                log.warning("تعذّر إرسال تقرير الإيقاف للحملة %s.", campaign_id)


async def _resume(client: Client, query: CallbackQuery, campaign_id: str) -> None:
    """يستأنف حملة موقوفة مؤقتاً.

    إن كان المُنفِّذ ما زال حياً فتغيير الحالة كافٍ: حلقة الانتظار في
    send_campaign تلتقطه. وإن كان قد مات (إعادة تشغيل الحاوية) فلا بد من
    مهمة جديدة بـ resume=True لتتخطى ما أُرسل فعلاً.
    """
    repo = CampaignsRepo(runtime.db)
    campaign = await repo.get(campaign_id)

    if campaign is None:
        await _render_list(query, notice=f"⚠️ لا توجد حملة بالمعرّف `{campaign_id}`.")
        return

    status = CampaignStatus(campaign["status"])
    if status in TERMINAL:
        await _render_detail(
            client, query, campaign_id, notice="⚠️ الحملة منتهية؛ لا يمكن استئنافها."
        )
        return

    executor_alive = campaign_id in running_campaign_ids()

    if not await repo.update_status(
        campaign_id, CampaignStatus.RUNNING, expected_status=status
    ):
        await _render_detail(client, query, campaign_id, notice="⚠️ تعذّر الاستئناف.")
        return

    if executor_alive:
        notice = "▶️ استُؤنفت الحملة."
    else:
        spawned = spawn_campaign(client, campaign_id, query.message.chat.id, resume=True)
        notice = (
            "▶️ استُؤنفت الحملة بمُنفِّذ جديد (يتخطى ما أُرسل مسبقاً)."
            if spawned
            else "▶️ الحملة تعمل بالفعل."
        )

    log.info("استئناف الحملة %s | executor_alive=%s", campaign_id, executor_alive)
    await _render_detail(client, query, campaign_id, notice=notice)
