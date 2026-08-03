"""تهيئة عميل Pyrogram (userbot) اعتماداً على SESSION_STRING."""

from __future__ import annotations

from pyrogram import Client

from config import Config, load_config

# اسم منطقي فقط؛ عند تمرير session_string يستخدم Pyrogram تخزيناً في الذاكرة
# ولا يُنشئ ملف .session على القرص، وهو المناسب لبيئة Railway.
SESSION_NAME = "broadcast_userbot"


def create_userbot(config: Config | None = None) -> Client:
    """يبني كائن Client دون تشغيله."""
    cfg = config or load_config()

    return Client(
        name=SESSION_NAME,
        api_id=cfg.api_id,
        api_hash=cfg.api_hash,
        session_string=cfg.session_string,
        # صفر يعني أن Pyrogram يرفع FloodWait بدل النوم صمتاً؛
        # المعالجة والتسجيل يجريان في userbot/sender.py
        sleep_threshold=0,
    )
