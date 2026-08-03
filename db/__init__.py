"""طبقة الوصول إلى قاعدة البيانات."""

from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorDatabase

from db.models import (
    CampaignsRepo,
    CampaignStatus,
    ChatType,
    ContentKind,
    LogsRepo,
    LogStatus,
    TargetsRepo,
    build_content,
    ensure_indexes,
)
from db.mongo import DbError, MongoConnection, get_db, mongo

__all__ = [
    "CampaignStatus",
    "CampaignsRepo",
    "ChatType",
    "ContentKind",
    "DbError",
    "LogStatus",
    "LogsRepo",
    "MongoConnection",
    "TargetsRepo",
    "build_content",
    "ensure_indexes",
    "get_db",
    "init_db",
    "mongo",
]


async def init_db(uri: str, db_name: str) -> AsyncIOMotorDatabase:
    """يتصل ثم يجهّز الفهارس. نقطة الدخول الوحيدة للتطبيق."""
    db = await mongo.connect(uri, db_name)
    await ensure_indexes(db)
    return db
