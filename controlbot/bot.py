"""بوت التحكم (Bot API) — منفصل عن الـ userbot ويعمل بـ BOT_TOKEN."""

from __future__ import annotations

import logging

from pyrogram import Client, filters
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import CallbackQuery, Message

from config import Config, load_config
from controlbot.handlers.base import MENU_TEXT, dispatch
from controlbot.handlers.campaign import on_cancel_command, on_flow_message
from controlbot.keyboards import main_menu

log = logging.getLogger(__name__)

SESSION_NAME = "control_bot"

UNAUTHORIZED_TEXT = "غير مصرح."


def create_controlbot(config: Config | None = None) -> Client:
    """يبني عميل البوت ويسجّل المعالجات، دون تشغيله."""
    cfg = config or load_config()

    app = Client(
        name=SESSION_NAME,
        api_id=cfg.api_id,
        api_hash=cfg.api_hash,
        bot_token=cfg.bot_token,
        # لا ملف .session على القرص — مناسب لحاويات Railway المؤقتة
        in_memory=True,
        sleep_threshold=60,
    )
    register_handlers(app, cfg.owner_id)
    return app


def register_handlers(app: Client, owner_id: int) -> None:
    """يسجّل معالجات المالك ومعالجات الرفض للبقية.

    البوابة الحقيقية هي filters.user(owner_id) على مستوى التسجيل، لا فحص
    داخل جسم الدالة، فلا يمكن لمعالج جديد أن ينسى التحقق.
    """
    owner = filters.user(owner_id)

    # --- المالك -------------------------------------------------------
    app.add_handler(
        MessageHandler(_on_start, filters.command("start") & filters.private & owner)
    )
    app.add_handler(
        MessageHandler(
            on_cancel_command, filters.command("cancel") & filters.private & owner
        )
    )
    # يجب أن يأتي بعد معالجات الأوامر: Pyrogram ينفّذ أول معالج مطابق فقط
    app.add_handler(
        MessageHandler(
            on_flow_message,
            filters.private & owner & ~filters.command(["start", "cancel"]),
        )
    )
    app.add_handler(CallbackQueryHandler(_on_callback, owner))

    # --- أي شخص آخر ---------------------------------------------------
    app.add_handler(MessageHandler(_on_unauthorized_message, filters.private & ~owner))
    app.add_handler(CallbackQueryHandler(_on_unauthorized_callback, ~owner))

    log.info("سُجِّلت معالجات بوت التحكم | OWNER_ID=%s", owner_id)


# ---------------------------------------------------------------- المالك

async def _on_start(client: Client, message: Message) -> None:
    await message.reply_text(MENU_TEXT, reply_markup=main_menu())


async def _on_callback(client: Client, query: CallbackQuery) -> None:
    await dispatch(client, query)


# ---------------------------------------------------------------- غير المصرح

async def _on_unauthorized_message(client: Client, message: Message) -> None:
    user = message.from_user
    log.warning(
        "محاولة وصول غير مصرح | id=%s username=%s",
        getattr(user, "id", "?"),
        getattr(user, "username", None),
    )
    await message.reply_text(UNAUTHORIZED_TEXT)


async def _on_unauthorized_callback(client: Client, query: CallbackQuery) -> None:
    log.warning(
        "callback غير مصرح | id=%s data=%r",
        getattr(query.from_user, "id", "?"),
        query.data,
    )
    await query.answer(UNAUTHORIZED_TEXT, show_alert=True)
