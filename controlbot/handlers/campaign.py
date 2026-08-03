"""إنشاء حملة جديدة عبر تدفق خطوات (FSM) داخل بوت التحكم.

الحالة تُحفظ في الذاكرة لكل مستخدم؛ الحملة نفسها تُكتب في MongoDB فقط
عند التأكيد النهائي.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pyrogram import Client
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from controlbot.context import RuntimeNotReady, runtime
from controlbot.handlers.base import Route, edit, route
from controlbot.keyboards import CB_CAMPAIGN, CB_MENU, cb
from db import CampaignsRepo, CampaignStatus, ContentKind, TargetsRepo, build_content
from userbot.sender import MIN_INTERVAL_BETWEEN_MESSAGES_SEC, send_campaign

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ حدود الإدخال

MAX_REPEAT_COUNT = 50
MAX_INTERVAL_SEC = 86_400  # 24 ساعة
DRAFT_TTL_SEC = 900  # مسوّدة مهملة 15 دقيقة تُلغى تلقائياً
PREVIEW_MAX_LEN = 300

# الوسائط التي نقرأ منها file_id، بالترتيب
MEDIA_ATTRS = (
    "photo",
    "video",
    "animation",
    "document",
    "audio",
    "voice",
    "video_note",
    "sticker",
)

MEDIA_LABELS = {
    "photo": "صورة",
    "video": "فيديو",
    "animation": "GIF",
    "document": "ملف",
    "audio": "صوت",
    "voice": "بصمة صوتية",
    "video_note": "فيديو دائري",
    "sticker": "ملصق",
}

# تحويل الأرقام العربية-الهندية إلى لاتينية قبل التحقق
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


# ------------------------------------------------------------------ الحالة

class Step(str, Enum):
    CONTENT = "content"
    REPEAT = "repeat"
    MSG_INTERVAL = "msg_interval"
    TARGET_INTERVAL = "target_interval"
    CONFIRM = "confirm"


@dataclass
class Draft:
    user_id: int
    step: Step = Step.CONTENT
    content: dict[str, Any] | None = None
    content_label: str = ""
    content_preview: str = ""
    repeat_count: int = 1
    msg_interval: int = 0
    target_interval: int = 0
    updated_at: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.updated_at = time.monotonic()

    @property
    def is_expired(self) -> bool:
        return time.monotonic() - self.updated_at > DRAFT_TTL_SEC


class DraftStore:
    """تخزين المسوّدات في الذاكرة. تُفقد عند إعادة تشغيل العملية."""

    def __init__(self) -> None:
        self._drafts: dict[int, Draft] = {}

    def start(self, user_id: int) -> Draft:
        draft = Draft(user_id=user_id)
        self._drafts[user_id] = draft
        return draft

    def get(self, user_id: int) -> Draft | None:
        draft = self._drafts.get(user_id)
        if draft is None:
            return None
        if draft.is_expired:
            self._drafts.pop(user_id, None)
            log.info("انتهت صلاحية مسوّدة المستخدم %s.", user_id)
            return None
        return draft

    def clear(self, user_id: int) -> None:
        self._drafts.pop(user_id, None)


drafts = DraftStore()


# ------------------------------------------------------------------ مهام الحملات

_tasks: dict[str, asyncio.Task] = {}


def running_campaign_ids() -> list[str]:
    """معرّفات الحملات التي لها مهمة تنفيذ حيّة في هذه العملية."""
    return [cid for cid, task in _tasks.items() if not task.done()]


def cancel_campaign_task(campaign_id: str) -> bool:
    task = _tasks.get(campaign_id)
    if task is None or task.done():
        return False
    task.cancel()
    return True


async def _run_campaign(
    client: Client, campaign_id: str, notify_chat_id: int, resume: bool = False
) -> None:
    """يشغّل الحملة ويبلّغ المالك بالنتيجة."""
    try:
        result = await send_campaign(
            runtime.userbot, campaign_id, db=runtime.db, resume=resume
        )
        text = (
            f"**انتهت الحملة** `{campaign_id}`\n\n"
            f"الحالة: **{result.final_status.value}**\n"
            f"الأهداف: **{result.targets_processed}/{result.targets_total}**\n"
            f"نجاح: **{result.sent}**  •  فشل: **{result.failed}**"
        )
        if result.skipped:
            text += f"  •  تخطي: **{result.skipped}**"
        if result.deactivated:
            text += f"\nعُطّلت **{len(result.deactivated)}** محادثة تلقائياً."
        if result.abort_reason:
            text += f"\n\n⚠️ {result.abort_reason}"
        await client.send_message(notify_chat_id, text)

    except asyncio.CancelledError:
        log.warning("أُلغيت مهمة الحملة %s.", campaign_id)
        raise

    except Exception as exc:  # noqa: BLE001
        log.exception("فشل تنفيذ الحملة %s.", campaign_id)
        try:
            await client.send_message(
                notify_chat_id,
                f"⚠️ توقفت الحملة `{campaign_id}` بخطأ:\n`{type(exc).__name__}: {exc}`",
            )
        except Exception:  # noqa: BLE001
            pass


def spawn_campaign(
    client: Client, campaign_id: str, notify_chat_id: int, *, resume: bool = False
) -> bool:
    """يشغّل الحملة في مهمة منفصلة. يعيد False إن كانت هناك مهمة حيّة لها.

    الاحتفاظ بمرجع المهمة ضروري؛ بدونه قد يجمعها جامع القمامة أثناء التنفيذ.
    """
    existing = _tasks.get(campaign_id)
    if existing is not None and not existing.done():
        return False

    task = asyncio.create_task(
        _run_campaign(client, campaign_id, notify_chat_id, resume=resume),
        name=f"campaign:{campaign_id}",
    )
    _tasks[campaign_id] = task
    task.add_done_callback(lambda _t: _tasks.pop(campaign_id, None))
    return True


async def shutdown_campaign_tasks(timeout: float = 20.0) -> int:
    """يلغي مهام الحملات الجارية وينتظر كتابتها لحالة paused في قاعدة البيانات."""
    tasks = [task for task in list(_tasks.values()) if not task.done()]
    if not tasks:
        return 0

    log.info("إلغاء %d مهمة حملة جارية…", len(tasks))
    for task in tasks:
        task.cancel()

    done, pending = await asyncio.wait(tasks, timeout=timeout)
    if pending:
        log.warning("%d مهمة لم تستجب للإلغاء خلال %.0f ثانية.", len(pending), timeout)
    return len(tasks)


# ------------------------------------------------------------------ أدوات

def _code(text: str) -> str:
    """يجعل النص آمناً داخل كتلة كود Markdown."""
    return text.replace("```", "'''").replace("`", "'")


def _parse_int(raw: str) -> int | None:
    cleaned = raw.strip().translate(_ARABIC_DIGITS)
    if not cleaned.isdigit():
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ إلغاء", callback_data=cb(CB_CAMPAIGN, "cancel"))]]
    )


def _confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ تأكيد وبدء", callback_data=cb(CB_CAMPAIGN, "confirm")
                ),
                InlineKeyboardButton("❌ إلغاء", callback_data=cb(CB_CAMPAIGN, "cancel")),
            ]
        ]
    )


def _menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("◀️ القائمة الرئيسية", callback_data=cb(CB_MENU, "home"))]]
    )


PROMPTS: dict[Step, str] = {
    Step.CONTENT: (
        "**📝 حملة جديدة — 1/4**\n\n"
        "أرسل الآن المحتوى المطلوب بثه:\n"
        "• نص عادي\n"
        "• صورة أو فيديو أو ملف (مع تعليق اختياري)\n"
        "• أو وجّه (forward) رسالة من قناة\n\n"
        "للإلغاء في أي وقت: /cancel"
    ),
    Step.REPEAT: (
        "**📝 حملة جديدة — 2/4**\n\n"
        "كم عدد مرات الإرسال لكل محادثة؟\n"
        f"اكتب رقماً بين **1** و **{MAX_REPEAT_COUNT}**."
    ),
    Step.MSG_INTERVAL: (
        "**📝 حملة جديدة — 3/4**\n\n"
        "الفاصل الزمني بين كل رسالة وأخرى لنفس المحادثة (بالثواني)؟\n"
        f"الحد الأدنى المفروض **{MIN_INTERVAL_BETWEEN_MESSAGES_SEC}** ثانية."
    ),
    Step.TARGET_INTERVAL: (
        "**📝 حملة جديدة — 4/4**\n\n"
        "الفاصل الزمني بين كل محادثة وأخرى (بالثواني)؟\n"
        "يُنصح بـ **15** ثانية أو أكثر لتقليل خطر التقييد."
    ),
}


# ------------------------------------------------------------------ استخراج المحتوى

def _extract_content(message: Message) -> tuple[dict[str, Any], str, str]:
    """يحوّل رسالة المالك إلى content + تسمية + معاينة.

    يرفع ValueError إذا لم تحتوِ الرسالة على محتوى قابل للبث.
    """
    caption = message.caption or None

    # 1) توجيه من قناة: أدق طريقة لأن الـ userbot يعيد التوجيه من المصدر نفسه
    fwd_chat = getattr(message, "forward_from_chat", None)
    fwd_msg_id = getattr(message, "forward_from_message_id", None)
    if fwd_chat is not None and fwd_msg_id:
        content = build_content(
            kind=ContentKind.FORWARD,
            from_chat_id=fwd_chat.id,
            message_id=int(fwd_msg_id),
        )
        source = fwd_chat.title or (
            f"@{fwd_chat.username}" if fwd_chat.username else fwd_chat.id
        )
        return content, "توجيه (forward)", f"من: {source}\nرقم الرسالة: {fwd_msg_id}"

    # 2) وسائط
    for attr in MEDIA_ATTRS:
        media = getattr(message, attr, None)
        file_id = getattr(media, "file_id", None) if media is not None else None
        if file_id:
            content = build_content(kind=ContentKind.MEDIA, file_id=file_id, text=caption)
            label = MEDIA_LABELS.get(attr, attr)
            preview = f"تعليق: {caption[:PREVIEW_MAX_LEN]}" if caption else "بدون تعليق"
            return content, label, preview

    # 3) نص
    text = message.text or caption
    if text and text.strip():
        content = build_content(kind=ContentKind.TEXT, text=text)
        preview = text[:PREVIEW_MAX_LEN] + ("…" if len(text) > PREVIEW_MAX_LEN else "")
        return content, "نص", preview

    raise ValueError("لا يوجد محتوى قابل للبث في هذه الرسالة.")


# ------------------------------------------------------------------ الملخص

async def _summary_text(draft: Draft) -> str:
    active_targets = await TargetsRepo(runtime.db).count_active()

    effective_msg_interval = max(draft.msg_interval, MIN_INTERVAL_BETWEEN_MESSAGES_SEC)
    total_messages = active_targets * draft.repeat_count

    lines = [
        "**📋 ملخص الحملة**",
        "",
        f"نوع المحتوى: **{draft.content_label}**",
        "```",
        _code(draft.content_preview or "-"),
        "```",
        f"عدد مرات الإرسال لكل محادثة: **{draft.repeat_count}**",
        f"الفاصل بين الرسائل: **{effective_msg_interval}** ثانية",
    ]
    if draft.msg_interval < MIN_INTERVAL_BETWEEN_MESSAGES_SEC:
        lines.append(
            f"  ↳ أدخلت **{draft.msg_interval}**، ورُفع إلى الحد الأدنى "
            f"**{MIN_INTERVAL_BETWEEN_MESSAGES_SEC}**."
        )
    lines += [
        f"الفاصل بين المحادثات: **{draft.target_interval}** ثانية",
        "",
        f"المحادثات المستهدفة المفعّلة: **{active_targets}**",
        f"إجمالي الرسائل المتوقع: **{total_messages}**",
    ]

    if active_targets == 0:
        lines += [
            "",
            "⚠️ لا توجد محادثات مفعّلة. فعّل محادثات من قسم "
            "«إدارة المحادثات المستهدفة» قبل البدء.",
        ]

    return "\n".join(lines)


# ------------------------------------------------------------------ معالج الأزرار

@route(CB_CAMPAIGN)
async def handle_campaign(client: Client, query: CallbackQuery, route_obj: Route) -> None:
    action = route_obj.action
    user_id = query.from_user.id

    try:
        if action == "new":
            drafts.start(user_id)
            await edit(query, PROMPTS[Step.CONTENT], _cancel_kb())

        elif action == "cancel":
            drafts.clear(user_id)
            await edit(query, "❌ أُلغيت الحملة.", _menu_kb())

        elif action == "confirm":
            await _confirm(client, query)

        else:
            await edit(query, "⚠️ إجراء غير معروف.", _menu_kb())

    except RuntimeNotReady as exc:
        log.error("سياق التشغيل غير مكتمل: %s", exc)
        await edit(query, f"⚠️ {exc}", _menu_kb())


async def _confirm(client: Client, query: CallbackQuery) -> None:
    user_id = query.from_user.id
    draft = drafts.get(user_id)

    if draft is None or draft.content is None or draft.step is not Step.CONFIRM:
        await edit(
            query,
            "⚠️ لا توجد حملة جاهزة للتأكيد (ربما انتهت صلاحية الجلسة).\nابدأ من جديد.",
            _menu_kb(),
        )
        return

    targets_repo = TargetsRepo(runtime.db)
    if await targets_repo.count_active() == 0:
        await edit(
            query,
            "⚠️ لا توجد محادثات مفعّلة؛ لن تُنشأ الحملة.\n"
            "فعّل محادثات أولاً من قسم إدارة المحادثات المستهدفة.",
            _menu_kb(),
        )
        return

    campaigns = CampaignsRepo(runtime.db)
    campaign_id = await campaigns.add(
        content=draft.content,
        repeat_count=draft.repeat_count,
        interval_between_messages_sec=draft.msg_interval,
        interval_between_targets_sec=draft.target_interval,
    )
    drafts.clear(user_id)

    # send_campaign ينقل الحالة pending -> running ذرياً بنفسه
    spawn_campaign(client, campaign_id, query.message.chat.id)
    log.info("أُنشئت الحملة %s وبدأ تنفيذها.", campaign_id)

    await edit(
        query,
        f"✅ **بدأت الحملة**\n\n"
        f"المعرّف: `{campaign_id}`\n"
        f"الحالة: **{CampaignStatus.RUNNING.value}**\n\n"
        "يمكنك إيقافها مؤقتاً أو نهائياً من «الحملات النشطة».\n"
        "سيصلك تقرير عند الانتهاء.",
        _menu_kb(),
    )


# ------------------------------------------------------------------ معالج الرسائل

async def on_cancel_command(client: Client, message: Message) -> None:
    """/cancel — يُسجَّل في bot.py."""
    if drafts.get(message.from_user.id) is None:
        await message.reply_text("لا يوجد شيء لإلغائه.", reply_markup=_menu_kb())
        return
    drafts.clear(message.from_user.id)
    await message.reply_text("❌ أُلغيت الحملة.", reply_markup=_menu_kb())


async def on_flow_message(client: Client, message: Message) -> None:
    """يستقبل رسائل المالك ويوجّهها حسب خطوة المسوّدة. يُسجَّل في bot.py."""
    draft = drafts.get(message.from_user.id)

    if draft is None:
        await message.reply_text(
            "لا يوجد إجراء قيد التنفيذ. استخدم /start لفتح لوحة التحكم.",
            reply_markup=_menu_kb(),
        )
        return

    draft.touch()

    if draft.step is Step.CONTENT:
        await _on_content(message, draft)
    elif draft.step is Step.REPEAT:
        await _on_repeat(message, draft)
    elif draft.step is Step.MSG_INTERVAL:
        await _on_msg_interval(message, draft)
    elif draft.step is Step.TARGET_INTERVAL:
        await _on_target_interval(client, message, draft)
    elif draft.step is Step.CONFIRM:
        await message.reply_text(
            "استخدم الأزرار في رسالة الملخص للتأكيد أو الإلغاء.",
            reply_markup=_confirm_kb(),
        )


async def _on_content(message: Message, draft: Draft) -> None:
    try:
        content, label, preview = _extract_content(message)
    except ValueError as exc:
        await message.reply_text(
            f"⚠️ {exc}\nأرسل نصاً أو صورة أو فيديو أو رسالة موجّهة.",
            reply_markup=_cancel_kb(),
        )
        return

    draft.content = content
    draft.content_label = label
    draft.content_preview = preview
    draft.step = Step.REPEAT

    await message.reply_text(
        f"✅ استُلم المحتوى ({label}).\n\n{PROMPTS[Step.REPEAT]}",
        reply_markup=_cancel_kb(),
    )


async def _on_repeat(message: Message, draft: Draft) -> None:
    value = _parse_int(message.text or "")

    if value is None:
        await message.reply_text(
            "⚠️ هذا ليس رقماً صحيحاً. أعد الإدخال برقم فقط، مثال: `3`",
            reply_markup=_cancel_kb(),
        )
        return
    if not 1 <= value <= MAX_REPEAT_COUNT:
        await message.reply_text(
            f"⚠️ الرقم يجب أن يكون بين **1** و **{MAX_REPEAT_COUNT}**. أعد الإدخال.",
            reply_markup=_cancel_kb(),
        )
        return

    draft.repeat_count = value
    draft.step = Step.MSG_INTERVAL
    await message.reply_text(PROMPTS[Step.MSG_INTERVAL], reply_markup=_cancel_kb())


async def _on_msg_interval(message: Message, draft: Draft) -> None:
    value = _parse_int(message.text or "")

    if value is None:
        await message.reply_text(
            "⚠️ هذا ليس رقماً صحيحاً. أدخل عدد الثواني كرقم، مثال: `5`",
            reply_markup=_cancel_kb(),
        )
        return
    if value > MAX_INTERVAL_SEC:
        await message.reply_text(
            f"⚠️ الحد الأقصى **{MAX_INTERVAL_SEC}** ثانية. أعد الإدخال.",
            reply_markup=_cancel_kb(),
        )
        return

    draft.msg_interval = value
    draft.step = Step.TARGET_INTERVAL

    note = ""
    if value < MIN_INTERVAL_BETWEEN_MESSAGES_SEC:
        note = (
            f"ℹ️ سيُرفع إلى الحد الأدنى **{MIN_INTERVAL_BETWEEN_MESSAGES_SEC}** ثانية.\n\n"
        )
    await message.reply_text(note + PROMPTS[Step.TARGET_INTERVAL], reply_markup=_cancel_kb())


async def _on_target_interval(client: Client, message: Message, draft: Draft) -> None:
    value = _parse_int(message.text or "")

    if value is None:
        await message.reply_text(
            "⚠️ هذا ليس رقماً صحيحاً. أدخل عدد الثواني كرقم، مثال: `15`",
            reply_markup=_cancel_kb(),
        )
        return
    if value > MAX_INTERVAL_SEC:
        await message.reply_text(
            f"⚠️ الحد الأقصى **{MAX_INTERVAL_SEC}** ثانية. أعد الإدخال.",
            reply_markup=_cancel_kb(),
        )
        return

    draft.target_interval = value
    draft.step = Step.CONFIRM

    try:
        summary = await _summary_text(draft)
    except RuntimeNotReady as exc:
        await message.reply_text(f"⚠️ {exc}", reply_markup=_menu_kb())
        return

    await message.reply_text(summary, reply_markup=_confirm_kb())
