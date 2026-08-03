"""إدارة المحادثات المستهدفة: جلب من الـ userbot، عرض بصفحات، وتبديل التفعيل."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from pyrogram import Client
from pyrogram.enums import ChatType as TgChatType
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from controlbot.context import RuntimeNotReady, runtime
from controlbot.handlers.base import Route, edit, route
from controlbot.keyboards import CB_MENU, CB_TARGETS, cb
from db import ChatType, TargetsRepo

log = logging.getLogger(__name__)

PAGE_SIZE = 8

# جلب الحوارات مكلف على حساب المستخدم؛ نخزّنه مؤقتاً بين الصفحات
DIALOGS_CACHE_TTL_SEC = 300

# سقف احتياطي: حساب بآلاف الحوارات سيجعل الجلب طويلاً جداً
MAX_DIALOGS = 1000

# القنوات مستثناة صراحةً حسب المطلوب
ALLOWED_TG_TYPES = frozenset(
    {TgChatType.PRIVATE, TgChatType.GROUP, TgChatType.SUPERGROUP}
)

MAX_BUTTON_TITLE_LEN = 30

ON = "✅"
OFF = "⬜"


@dataclass(frozen=True)
class DialogEntry:
    chat_id: int
    chat_type: ChatType
    title: str


# --------------------------------------------------------------- الكاش

class _DialogsCache:
    def __init__(self) -> None:
        self._entries: list[DialogEntry] = []
        self._fetched_at: float = 0.0
        # يمنع جلبين متزامنين لو ضغط المالك زرين بسرعة
        self._lock = asyncio.Lock()

    def is_fresh(self) -> bool:
        return bool(self._entries) and (
            time.monotonic() - self._fetched_at < DIALOGS_CACHE_TTL_SEC
        )

    async def get(self, userbot: Client, force: bool = False) -> list[DialogEntry]:
        async with self._lock:
            if not force and self.is_fresh():
                return self._entries
            self._entries = await _fetch_dialogs(userbot)
            self._fetched_at = time.monotonic()
            return self._entries

    def invalidate(self) -> None:
        self._entries = []
        self._fetched_at = 0.0


_cache = _DialogsCache()


# --------------------------------------------------------------- الجلب

def _display_title(chat) -> str:
    if chat.title:
        return chat.title
    name = " ".join(p for p in (chat.first_name, chat.last_name) if p).strip()
    if name:
        return name
    if chat.username:
        return f"@{chat.username}"
    return str(chat.id)


def _map_type(tg_type: TgChatType) -> ChatType:
    return ChatType.PRIVATE if tg_type is TgChatType.PRIVATE else ChatType.GROUP


async def _fetch_dialogs(userbot: Client) -> list[DialogEntry]:
    """يجلب الحوارات من حساب المستخدم، مستثنياً القنوات."""
    if not userbot.is_connected:
        raise RuntimeNotReady("الـ userbot غير متصل حالياً.")

    entries: list[DialogEntry] = []
    seen: set[int] = set()

    async for dialog in userbot.get_dialogs():
        chat = dialog.chat
        if chat is None or chat.type not in ALLOWED_TG_TYPES:
            continue
        # محادثات البوتات تظهر كـ private؛ نستثنيها إن أتاحت النسخة العلم
        if getattr(chat, "is_bot", False):
            continue
        if chat.id in seen:
            continue

        seen.add(chat.id)
        entries.append(
            DialogEntry(
                chat_id=chat.id,
                chat_type=_map_type(chat.type),
                title=_display_title(chat),
            )
        )
        if len(entries) >= MAX_DIALOGS:
            log.warning("بلغ الجلب سقف %d محادثة؛ تم القطع.", MAX_DIALOGS)
            break

    # ترتيب ثابت حتى لا تتبدّل الصفحات بين الضغطات
    entries.sort(
        key=lambda e: (e.chat_type is ChatType.PRIVATE, e.title.lower(), e.chat_id)
    )
    log.info("جُلبت %d محادثة صالحة من الـ userbot.", len(entries))
    return entries


# --------------------------------------------------------------- العرض

def _clamp_page(page: int, total: int) -> int:
    pages = max(1, -(-total // PAGE_SIZE))  # ceil
    return max(0, min(page, pages - 1))


def _page_count(total: int) -> int:
    return max(1, -(-total // PAGE_SIZE))


def _build_keyboard(
    entries: list[DialogEntry], states: dict[int, bool], page: int
) -> InlineKeyboardMarkup:
    total = len(entries)
    pages = _page_count(total)
    start = page * PAGE_SIZE
    chunk = entries[start : start + PAGE_SIZE]

    rows: list[list[InlineKeyboardButton]] = []

    for entry in chunk:
        mark = ON if states.get(entry.chat_id, False) else OFF
        title = entry.title
        if len(title) > MAX_BUTTON_TITLE_LEN:
            title = title[: MAX_BUTTON_TITLE_LEN - 1] + "…"
        rows.append(
            [
                InlineKeyboardButton(
                    f"{mark} {title}",
                    callback_data=cb(CB_TARGETS, "toggle", entry.chat_id, page),
                )
            ]
        )

    # التنقل — يظهر فقط عند وجود أكثر من صفحة
    if pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    "◀️ السابق", callback_data=cb(CB_TARGETS, "page", page - 1)
                )
            )
        nav.append(
            InlineKeyboardButton(
                f"{page + 1}/{pages}", callback_data=cb(CB_TARGETS, "noop")
            )
        )
        if page < pages - 1:
            nav.append(
                InlineKeyboardButton(
                    "التالي ▶️", callback_data=cb(CB_TARGETS, "page", page + 1)
                )
            )
        rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton(
                "✅ تفعيل الكل", callback_data=cb(CB_TARGETS, "all_on", page)
            ),
            InlineKeyboardButton(
                "❌ تعطيل الكل", callback_data=cb(CB_TARGETS, "all_off", page)
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "🔄 تحديث القائمة", callback_data=cb(CB_TARGETS, "refresh", page)
            ),
            InlineKeyboardButton("◀️ رجوع", callback_data=cb(CB_MENU, "home")),
        ]
    )

    return InlineKeyboardMarkup(rows)


def _header(total: int, active: int, page: int, notice: str | None) -> str:
    lines = [
        "**📋 إدارة المحادثات المستهدفة**",
        "",
        f"المحادثات: **{total}**  •  المفعّلة: **{active}**",
        f"الصفحة **{page + 1}** من **{_page_count(total)}**",
    ]
    if notice:
        lines += ["", notice]
    lines += ["", "اضغط على أي محادثة لتبديل حالتها."]
    return "\n".join(lines)


async def _render(
    query: CallbackQuery,
    page: int,
    *,
    force_fetch: bool = False,
    notice: str | None = None,
) -> None:
    """يبني الصفحة كاملة: جلب (أو كاش) + حالات من MongoDB + لوحة أزرار."""
    userbot = runtime.userbot
    repo = TargetsRepo(runtime.db)

    if force_fetch or not _cache.is_fresh():
        await edit(query, "⏳ جارٍ جلب المحادثات من الحساب…", InlineKeyboardMarkup([]))

    entries = await _cache.get(userbot, force=force_fetch)

    if not entries:
        await edit(
            query,
            "**📋 إدارة المحادثات المستهدفة**\n\n"
            "لا توجد محادثات صالحة (خاص أو قروبات) في هذا الحساب.",
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🔄 تحديث", callback_data=cb(CB_TARGETS, "refresh", 0)
                        ),
                        InlineKeyboardButton(
                            "◀️ رجوع", callback_data=cb(CB_MENU, "home")
                        ),
                    ]
                ]
            ),
        )
        return

    page = _clamp_page(page, len(entries))
    start = page * PAGE_SIZE
    page_ids = [e.chat_id for e in entries[start : start + PAGE_SIZE]]

    # استعلام واحد لحالات هذه الصفحة فقط، لا لكل الحوارات
    states = await repo.get_states(page_ids)
    active_total = await repo.count_active()

    await edit(
        query,
        _header(len(entries), active_total, page, notice),
        _build_keyboard(entries, states, page),
    )


# --------------------------------------------------------------- المعالج

@route(CB_TARGETS)
async def handle_targets(client: Client, query: CallbackQuery, route_obj: Route) -> None:
    action = route_obj.action

    if action == "noop":
        return

    try:
        if action in ("menu", "open"):
            await _render(query, 0)

        elif action == "page":
            await _render(query, int(route_obj.arg(0, "0")))

        elif action == "refresh":
            page = int(route_obj.arg(0, "0"))
            await _render(query, page, force_fetch=True, notice="🔄 حُدّثت القائمة.")

        elif action == "toggle":
            await _handle_toggle(query, route_obj)

        elif action in ("all_on", "all_off"):
            await _handle_bulk(query, route_obj, activate=action == "all_on")

        else:
            await _render(query, 0, notice="⚠️ إجراء غير معروف.")

    except RuntimeNotReady as exc:
        log.error("سياق التشغيل غير مكتمل: %s", exc)
        await edit(query, f"⚠️ {exc}\nتأكد من أن الـ userbot يعمل، ثم أعد المحاولة.")

    except FloodWait as exc:
        wait_for = int(getattr(exc, "value", 0) or 0)
        log.warning("FloodWait %ds أثناء جلب الحوارات.", wait_for)
        await edit(
            query,
            f"⏳ تيليجرام يطلب انتظار **{wait_for}** ثانية قبل جلب المحادثات مرة أخرى.",
        )

    except RPCError as exc:
        log.exception("خطأ تيليجرام في إدارة الأهداف.")
        await edit(query, f"⚠️ خطأ من تيليجرام: `{type(exc).__name__}`")

    except ValueError as exc:
        log.warning("callback_data معطوب %r: %s", route_obj.raw, exc)
        await _render(query, 0, notice="⚠️ زر غير صالح.")


async def _handle_toggle(query: CallbackQuery, route_obj: Route) -> None:
    chat_id = int(route_obj.arg(0, ""))
    page = int(route_obj.arg(1, "0"))

    entries = await _cache.get(runtime.userbot)
    entry = next((e for e in entries if e.chat_id == chat_id), None)
    if entry is None:
        # الحوار اختفى من القائمة بعد تحديث الكاش
        await _render(
            query, page, force_fetch=True, notice="⚠️ لم تُعد هذه المحادثة موجودة."
        )
        return

    repo = TargetsRepo(runtime.db)
    new_state = await repo.toggle(entry.chat_id, entry.chat_type, entry.title)

    log.info(
        "تبديل الهدف %s (%s) -> %s",
        entry.chat_id,
        entry.title,
        "active" if new_state else "inactive",
    )
    notice = f"{ON} تم التفعيل." if new_state else f"{OFF} تم التعطيل."
    await _render(query, page, notice=notice)


async def _handle_bulk(query: CallbackQuery, route_obj: Route, *, activate: bool) -> None:
    page = int(route_obj.arg(0, "0"))
    repo = TargetsRepo(runtime.db)

    if activate:
        entries = await _cache.get(runtime.userbot)
        changed = await repo.set_active_many(
            [
                {"chat_id": e.chat_id, "chat_type": e.chat_type, "title": e.title}
                for e in entries
            ],
            is_active=True,
        )
        notice = f"✅ فُعّلت **{changed}** محادثة."
    else:
        # التعطيل يشمل كل ما في قاعدة البيانات، لا الحوارات المعروضة فقط
        changed = await repo.deactivate_all()
        notice = f"❌ عُطّلت **{changed}** محادثة."

    log.info("إجراء جماعي على الأهداف: activate=%s changed=%d", activate, changed)
    await _render(query, page, notice=notice)
