"""تحميل وتحقق من متغيرات البيئة الخاصة بالمشروع."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# يقرأ ملف .env إن وُجد (محلياً في Termux).
# على Railway لا يوجد .env، والمتغيرات تُقرأ من بيئة النظام مباشرة.
load_dotenv(override=False)


class ConfigError(Exception):
    """يُرفع عند غياب متغير بيئة مطلوب أو كون قيمته غير صالحة."""


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(
            f"المتغير المطلوب '{name}' غير موجود أو فارغ. راجع ملف .env.example."
        )
    return value


def _require_int(name: str) -> int:
    raw = _require(name)
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} يجب أن يكون رقماً صحيحاً، وليس '{raw}'.") from exc


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    session_string: str
    bot_token: str
    owner_id: int
    mongo_uri: str
    mongo_db_name: str


def load_config() -> Config:
    return Config(
        api_id=_require_int("API_ID"),
        api_hash=_require("API_HASH"),
        session_string=_require("SESSION_STRING"),
        bot_token=_require("BOT_TOKEN"),
        owner_id=_require_int("OWNER_ID"),
        mongo_uri=_require("MONGO_URI"),
        mongo_db_name=os.getenv("MONGO_DB_NAME", "broadcast_bot").strip()
        or "broadcast_bot",
    )
