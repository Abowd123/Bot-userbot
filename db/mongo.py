"""تهيئة اتصال MongoDB باستخدام motor."""

from __future__ import annotations

import logging

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo.errors import ConfigurationError, PyMongoError, ServerSelectionTimeoutError

log = logging.getLogger(__name__)

# مهلة قصيرة حتى يفشل التشغيل سريعاً بدل التعليق دقيقة كاملة
SERVER_SELECTION_TIMEOUT_MS = 8000


class DbError(Exception):
    """خطأ على مستوى طبقة قاعدة البيانات."""


class MongoConnection:
    """يحفظ العميل وقاعدة البيانات لكل عملية تشغيل واحدة."""

    def __init__(self) -> None:
        self._client: AsyncIOMotorClient | None = None
        self._db: AsyncIOMotorDatabase | None = None

    @property
    def db(self) -> AsyncIOMotorDatabase:
        if self._db is None:
            raise DbError("لم يتم الاتصال بقاعدة البيانات بعد. نادِ connect() أولاً.")
        return self._db

    @property
    def is_connected(self) -> bool:
        return self._db is not None

    async def connect(self, uri: str, db_name: str) -> AsyncIOMotorDatabase:
        if self._db is not None:
            return self._db

        try:
            self._client = AsyncIOMotorClient(
                uri,
                serverSelectionTimeoutMS=SERVER_SELECTION_TIMEOUT_MS,
                tz_aware=True,
                appname="broadcast-bot",
            )
            # ping يجبر الاتصال الفعلي؛ بدونه لا يظهر الخطأ إلا عند أول استعلام
            await self._client.admin.command("ping")
        except ConfigurationError as exc:
            self._client = None
            raise DbError(
                f"صيغة MONGO_URI غير صحيحة: {exc}. "
                "لروابط mongodb+srv تأكد من تثبيت dnspython."
            ) from exc
        except ServerSelectionTimeoutError as exc:
            self._client = None
            raise DbError(
                "تعذّر الوصول إلى سيرفر MongoDB. تحقق من الرابط وكلمة المرور "
                f"ومن إضافة عنوان IP إلى قائمة السماح (Network Access). التفصيل: {exc}"
            ) from exc
        except PyMongoError as exc:
            self._client = None
            raise DbError(f"فشل الاتصال بـ MongoDB: {exc}") from exc

        self._db = self._client[db_name]
        log.info("تم الاتصال بقاعدة البيانات '%s'.", db_name)
        return self._db

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            log.info("تم إغلاق اتصال MongoDB.")
        self._client = None
        self._db = None


# نسخة واحدة مشتركة على مستوى التطبيق
mongo = MongoConnection()


def get_db() -> AsyncIOMotorDatabase:
    """اختصار للوصول إلى قاعدة البيانات من أي وحدة."""
    return mongo.db
