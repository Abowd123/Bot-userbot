"""توجيه CallbackQuery حسب بادئة callback_data.

كل بادئة لها معالج واحد يستقبل Route مُحلَّلاً، يُسجَّل عبر @route من
الوحدة المسؤولة عنها (targets.py, campaign.py, controls.py, logs.py).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from pyrogram import Client
from pyrogram.errors import MessageNotModified, QueryIdInvalid
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup

from controlbot.keyboards import CB_MENU, SEPARATOR, back_to_menu, main_menu

log = logging.getLogger(__name__)

MENU_TEXT = "**لوحة تحكم البث**\n\nاختر أحد الأقسام:"


@dataclass(frozen=True)
class Route:
    """callback_data مُحلَّلاً: prefix:action[:arg...]"""

    prefix: str
    action: str
    args: tuple[str, ...] = ()

    @property
    def raw(self) -> str:
        return SEPARATOR.join([self.prefix, self.action, *self.args])

    def arg(self, index: int, default: str | None = None) -> str | None:
        return self.args[index] if index < len(self.args) else default


RouteHandler = Callable[[Client, CallbackQuery, "Route"], Awaitable[None]]

# سجل البادئات -> المعالج
ROUTES: dict[str, RouteHandler] = {}

# خطّافات تُنفَّذ قبل أي معالج، أياً كانت البادئة
PreDispatchHook = Callable[[Client, CallbackQuery, "Route"], Awaitable[None]]
PRE_DISPATCH: list[PreDispatchHook] = []


def route(prefix: str) -> Callable[[RouteHandler], RouteHandler]:
    """يسجّل معالجاً لبادئة. يمنع التسجيل المزدوج على نفس البادئة."""

    def decorator(func: RouteHandler) -> RouteHandler:
        if prefix in ROUTES:
            raise RuntimeError(
                f"البادئة '{prefix}' مسجَّلة مسبقاً لـ {ROUTES[prefix].__name__}."
            )
        ROUTES[prefix] = func
        return func

    return decorator


def on_before_dispatch(func: PreDispatchHook) -> PreDispatchHook:
    """يسجّل خطّافاً يُنفَّذ قبل كل معالج، أياً كانت البادئة."""
    PRE_DISPATCH.append(func)
    return func


def parse_route(data: str | None) -> Route | None:
    if not data:
        return None
    parts = data.split(SEPARATOR)
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    return Route(prefix=parts[0], action=parts[1], args=tuple(parts[2:]))


# ---------------------------------------------------------------- أدوات مساعدة

async def edit(
    query: CallbackQuery,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    """يعدّل الرسالة ويتجاهل MessageNotModified.

    الضغط على نفس الزر مرتين يرفع MessageNotModified، وهو ليس خطأً فعلياً.
    """
    try:
        await query.edit_message_text(
            text, reply_markup=keyboard if keyboard is not None else back_to_menu()
        )
    except MessageNotModified:
        pass


# ---------------------------------------------------------------- القائمة الرئيسية

@route(CB_MENU)
async def handle_menu(client: Client, query: CallbackQuery, route_obj: Route) -> None:
    if route_obj.action == "home":
        await edit(query, MENU_TEXT, main_menu())
        return
    await query.answer("إجراء غير معروف.", show_alert=True)


# ---------------------------------------------------------------- نقطة الدخول

async def dispatch(client: Client, query: CallbackQuery) -> None:
    """يوزّع أي CallbackQuery على المعالج المطابق لبادئته."""
    route_obj = parse_route(query.data)

    if route_obj is None:
        log.warning("callback_data غير صالح: %r", query.data)
        await query.answer("زر غير صالح.", show_alert=True)
        return

    handler = ROUTES.get(route_obj.prefix)
    if handler is None:
        log.warning("لا يوجد معالج للبادئة %r", route_obj.prefix)
        await query.answer("هذا الزر لم يُربط بعد.", show_alert=True)
        return

    try:
        # answer أولاً لإيقاف مؤشر التحميل خلال مهلة تيليجرام القصيرة
        await query.answer()
    except QueryIdInvalid:
        # الزر قديم (الرسالة أُعيد تحميلها بعد إعادة تشغيل البوت)
        log.debug("QueryIdInvalid على %r", query.data)

    for hook in PRE_DISPATCH:
        try:
            await hook(client, query, route_obj)
        except Exception:  # noqa: BLE001 - الخطّاف لا يجب أن يُفشل المعالج
            log.exception("فشل خطّاف ما قبل التوجيه.")

    try:
        await handler(client, query, route_obj)
    except Exception:
        log.exception("فشل معالج %r", route_obj.raw)
        try:
            await query.answer("حدث خطأ غير متوقع. راجع السجل.", show_alert=True)
        except Exception:  # noqa: BLE001
            pass
