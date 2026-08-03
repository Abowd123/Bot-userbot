"""المجموعات (collections)، الفهارس، ودوال CRUD الأساسية."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, AsyncIterator

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING, UpdateOne
from pymongo.errors import PyMongoError

from db.mongo import DbError

log = logging.getLogger(__name__)

COL_TARGETS = "targets"
COL_CAMPAIGNS = "campaigns"
COL_LOGS = "logs"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_campaign_id() -> str:
    return uuid.uuid4().hex[:12]


class ChatType(str, Enum):
    PRIVATE = "private"
    GROUP = "group"


class CampaignStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    DONE = "done"


class LogStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"


class ContentKind(str, Enum):
    TEXT = "text"
    MEDIA = "media"       # file_id
    FORWARD = "forward"   # from_chat_id + message_id


# حالات لا يجوز الانتقال منها
TERMINAL_STATUSES = (CampaignStatus.STOPPED, CampaignStatus.DONE)
# الحملات التي تعتبر "حيّة" للجدولة
ACTIVE_STATUSES = (CampaignStatus.PENDING, CampaignStatus.RUNNING, CampaignStatus.PAUSED)


# ---------------------------------------------------------------- الفهارس

async def ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    """ينشئ الفهارس. العملية idempotent، آمنة عند كل إقلاع."""
    try:
        await db[COL_TARGETS].create_index(
            [("chat_id", ASCENDING)], unique=True, name="uniq_chat_id"
        )
        await db[COL_TARGETS].create_index(
            [("is_active", ASCENDING), ("added_at", ASCENDING)], name="active_added"
        )

        await db[COL_CAMPAIGNS].create_index(
            [("campaign_id", ASCENDING)], unique=True, name="uniq_campaign_id"
        )
        await db[COL_CAMPAIGNS].create_index(
            [("status", ASCENDING), ("created_at", DESCENDING)], name="status_created"
        )
        await db[COL_CAMPAIGNS].create_index(
            [("created_at", DESCENDING)], name="created_desc"
        )

        await db[COL_LOGS].create_index(
            [("campaign_id", ASCENDING), ("sent_at", DESCENDING)], name="campaign_sent"
        )
        await db[COL_LOGS].create_index(
            [("campaign_id", ASCENDING), ("status", ASCENDING)], name="campaign_status"
        )
        await db[COL_LOGS].create_index(
            [("campaign_id", ASCENDING), ("target_chat_id", ASCENDING), ("status", ASCENDING)],
            name="campaign_target_status",
        )
        # لحذف السجلات القديمة تلقائياً بعد 30 يوماً، أزل التعليق:
        # await db[COL_LOGS].create_index(
        #     [("sent_at", ASCENDING)], expireAfterSeconds=30 * 24 * 3600, name="ttl_sent_at"
        # )
    except PyMongoError as exc:
        raise DbError(f"فشل إنشاء الفهارس: {exc}") from exc

    log.info("الفهارس جاهزة.")


# ---------------------------------------------------------------- بناء المحتوى

def build_content(
    *,
    kind: ContentKind,
    text: str | None = None,
    file_id: str | None = None,
    from_chat_id: int | None = None,
    message_id: int | None = None,
) -> dict[str, Any]:
    """يبني حقل content ويتحقق من اكتمال الحقول المطلوبة لكل نوع."""
    if kind is ContentKind.TEXT:
        if not text:
            raise ValueError("محتوى نصي بلا نص.")
        return {"kind": kind.value, "text": text}

    if kind is ContentKind.MEDIA:
        if not file_id:
            raise ValueError("محتوى وسائط بلا file_id.")
        return {"kind": kind.value, "file_id": file_id, "caption": text}

    if kind is ContentKind.FORWARD:
        if from_chat_id is None or message_id is None:
            raise ValueError("التوجيه يحتاج from_chat_id و message_id.")
        return {"kind": kind.value, "from_chat_id": from_chat_id, "message_id": message_id}

    raise ValueError(f"نوع محتوى غير معروف: {kind}")


# ---------------------------------------------------------------- targets

class TargetsRepo:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._col = db[COL_TARGETS]

    async def add(self, chat_id: int, chat_type: ChatType, title: str) -> bool:
        """يضيف هدفاً أو يحدّث بياناته. يعيد True إذا كان جديداً.

        الفهرس الفريد على chat_id يمنع التكرار، والـ upsert يتجنب
        DuplicateKeyError عند إعادة الإضافة.
        """
        res = await self._col.update_one(
            {"chat_id": chat_id},
            {
                "$set": {
                    "chat_type": ChatType(chat_type).value,
                    "title": title,
                    "is_active": True,
                },
                "$setOnInsert": {"chat_id": chat_id, "added_at": utcnow()},
            },
            upsert=True,
        )
        return res.upserted_id is not None

    async def add_many(self, targets: list[dict[str, Any]]) -> int:
        """إضافة جماعية. يعيد عدد الأهداف الجديدة فقط."""
        added = 0
        for t in targets:
            if await self.add(t["chat_id"], t["chat_type"], t.get("title", "")):
                added += 1
        return added

    async def get(self, chat_id: int) -> dict[str, Any] | None:
        return await self._col.find_one({"chat_id": chat_id})

    async def update_status(self, chat_id: int, is_active: bool) -> bool:
        """تعطيل/تفعيل هدف. يُستخدم عند حظر الحساب أو حذف المحادثة."""
        res = await self._col.update_one(
            {"chat_id": chat_id},
            {"$set": {"is_active": is_active, "status_changed_at": utcnow()}},
        )
        return res.matched_count > 0

    async def list_active(
        self, chat_type: ChatType | None = None, limit: int = 0
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"is_active": True}
        if chat_type is not None:
            query["chat_type"] = ChatType(chat_type).value
        return await self._col.find(query).sort("added_at", ASCENDING).to_list(
            length=limit or None
        )

    async def iter_active(
        self, chat_type: ChatType | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """للبث على أعداد كبيرة دون تحميل كل شيء في الذاكرة."""
        query: dict[str, Any] = {"is_active": True}
        if chat_type is not None:
            query["chat_type"] = ChatType(chat_type).value
        async for doc in self._col.find(query).sort("added_at", ASCENDING):
            yield doc

    async def count_active(self) -> int:
        return await self._col.count_documents({"is_active": True})

    async def delete(self, chat_id: int) -> bool:
        res = await self._col.delete_one({"chat_id": chat_id})
        return res.deleted_count > 0

    async def get_states(self, chat_ids: list[int]) -> dict[int, bool]:
        """حالة التفعيل لمجموعة معرّفات في استعلام واحد.

        المعرّفات غير الموجودة في المجموعة لا تظهر في النتيجة (تُعتبر غير مفعّلة).
        """
        if not chat_ids:
            return {}
        cursor = self._col.find(
            {"chat_id": {"$in": chat_ids}}, {"chat_id": 1, "is_active": 1}
        )
        return {
            doc["chat_id"]: bool(doc.get("is_active"))
            for doc in await cursor.to_list(None)
        }

    async def get_titles(self, chat_ids: list[int]) -> dict[int, str]:
        """أسماء المحادثات لمجموعة معرّفات، لعرضها في السجل."""
        if not chat_ids:
            return {}
        cursor = self._col.find({"chat_id": {"$in": chat_ids}}, {"chat_id": 1, "title": 1})
        return {
            doc["chat_id"]: doc.get("title") or str(doc["chat_id"])
            for doc in await cursor.to_list(None)
        }

    async def toggle(self, chat_id: int, chat_type: ChatType, title: str) -> bool:
        """يعكس is_active ويعيد الحالة الجديدة. المحادثة غير الموجودة تُضاف مفعّلة."""
        doc = await self._col.find_one({"chat_id": chat_id}, {"is_active": 1})
        if doc is None:
            await self.add(chat_id, chat_type, title)
            return True

        new_state = not bool(doc.get("is_active"))
        await self._col.update_one(
            {"chat_id": chat_id},
            {
                "$set": {
                    "is_active": new_state,
                    "chat_type": ChatType(chat_type).value,
                    "title": title,
                    "status_changed_at": utcnow(),
                }
            },
        )
        return new_state

    async def set_active_many(
        self, targets: list[dict[str, Any]], is_active: bool = True
    ) -> int:
        """إضافة/تحديث جماعي بعملية كتابة واحدة. يعيد عدد المستندات المتأثرة."""
        if not targets:
            return 0

        now = utcnow()
        ops = [
            UpdateOne(
                {"chat_id": int(t["chat_id"])},
                {
                    "$set": {
                        "chat_type": ChatType(t["chat_type"]).value,
                        "title": t.get("title", ""),
                        "is_active": is_active,
                        "status_changed_at": now,
                    },
                    "$setOnInsert": {"chat_id": int(t["chat_id"]), "added_at": now},
                },
                upsert=True,
            )
            for t in targets
        ]
        res = await self._col.bulk_write(ops, ordered=False)
        return res.upserted_count + res.modified_count

    async def deactivate_all(self) -> int:
        res = await self._col.update_many(
            {"is_active": True},
            {"$set": {"is_active": False, "status_changed_at": utcnow()}},
        )
        return res.modified_count


# ---------------------------------------------------------------- campaigns

class CampaignsRepo:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._col = db[COL_CAMPAIGNS]

    async def add(
        self,
        content: dict[str, Any],
        repeat_count: int = 1,
        interval_between_messages_sec: int = 0,
        interval_between_targets_sec: int = 0,
    ) -> str:
        if repeat_count < 1:
            raise ValueError("repeat_count يجب أن يكون 1 أو أكثر.")
        if interval_between_messages_sec < 0 or interval_between_targets_sec < 0:
            raise ValueError("الفواصل الزمنية لا يمكن أن تكون سالبة.")

        campaign_id = new_campaign_id()
        await self._col.insert_one(
            {
                "campaign_id": campaign_id,
                "content": content,
                "repeat_count": repeat_count,
                "interval_between_messages_sec": interval_between_messages_sec,
                "interval_between_targets_sec": interval_between_targets_sec,
                "status": CampaignStatus.PENDING.value,
                "created_at": utcnow(),
                "started_at": None,
                "finished_at": None,
                "targets_total": 0,
                "expected_messages": 0,
            }
        )
        return campaign_id

    async def get(self, campaign_id: str) -> dict[str, Any] | None:
        return await self._col.find_one({"campaign_id": campaign_id})

    async def update_status(
        self,
        campaign_id: str,
        status: CampaignStatus,
        expected_status: CampaignStatus | None = None,
    ) -> bool:
        """يغيّر حالة الحملة.

        expected_status يوفّر تحديثاً ذرياً (compare-and-set) لمنع تسابق
        أمري pause و stop على نفس الحملة.
        """
        status = CampaignStatus(status)
        query: dict[str, Any] = {"campaign_id": campaign_id}
        if expected_status is not None:
            query["status"] = CampaignStatus(expected_status).value
        else:
            # لا يُسمح بإخراج حملة من حالة نهائية
            query["status"] = {"$nin": [s.value for s in TERMINAL_STATUSES]}

        update: dict[str, Any] = {"$set": {"status": status.value}}
        if status is CampaignStatus.RUNNING:
            update["$set"]["started_at"] = utcnow()
        elif status in TERMINAL_STATUSES:
            update["$set"]["finished_at"] = utcnow()

        res = await self._col.update_one(query, update)
        return res.modified_count > 0

    async def set_totals(
        self, campaign_id: str, targets_total: int, expected_messages: int
    ) -> bool:
        """لقطة لحجم الحملة عند بدء التنفيذ، تُستخدم لحساب نسبة التقدّم."""
        res = await self._col.update_one(
            {"campaign_id": campaign_id},
            {
                "$set": {
                    "targets_total": targets_total,
                    "expected_messages": expected_messages,
                }
            },
        )
        return res.matched_count > 0

    async def list_active(self, limit: int = 0) -> list[dict[str, Any]]:
        """الحملات غير المنتهية (pending / running / paused)."""
        return await self._col.find(
            {"status": {"$in": [s.value for s in ACTIVE_STATUSES]}}
        ).sort("created_at", ASCENDING).to_list(length=limit or None)

    async def list_running_or_paused(self, limit: int = 20) -> list[dict[str, Any]]:
        """الحملات القابلة للتحكم فقط (running / paused)."""
        return await self._col.find(
            {
                "status": {
                    "$in": [CampaignStatus.RUNNING.value, CampaignStatus.PAUSED.value]
                }
            }
        ).sort("created_at", DESCENDING).to_list(length=limit)

    async def list_by_status(
        self, status: CampaignStatus, limit: int = 50
    ) -> list[dict[str, Any]]:
        return await self._col.find(
            {"status": CampaignStatus(status).value}
        ).sort("created_at", DESCENDING).to_list(length=limit)

    async def count_all(self) -> int:
        return await self._col.count_documents({})

    async def list_recent(self, skip: int = 0, limit: int = 5) -> list[dict[str, Any]]:
        """أحدث الحملات أولاً، بترقيم صفحات."""
        return await self._col.find().sort("created_at", DESCENDING).skip(skip).to_list(
            length=limit
        )

    async def recover_stale_running(self) -> int:
        """يعيد الحملات المعلّقة على running إلى paused.

        أداة يدوية؛ الاستئناف التلقائي في recovery.py هو المسار الافتراضي.
        """
        res = await self._col.update_many(
            {"status": CampaignStatus.RUNNING.value},
            {"$set": {"status": CampaignStatus.PAUSED.value}},
        )
        return res.modified_count


# ---------------------------------------------------------------- logs

class LogsRepo:
    def __init__(self, db: AsyncIOMotorDatabase) -> None:
        self._col = db[COL_LOGS]

    async def add(
        self,
        campaign_id: str,
        target_chat_id: int,
        status: LogStatus,
        error_reason: str | None = None,
    ) -> Any:
        res = await self._col.insert_one(
            {
                "campaign_id": campaign_id,
                "target_chat_id": target_chat_id,
                "status": LogStatus(status).value,
                "error_reason": error_reason,
                "sent_at": utcnow(),
            }
        )
        return res.inserted_id

    async def get(
        self,
        campaign_id: str,
        status: LogStatus | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"campaign_id": campaign_id}
        if status is not None:
            query["status"] = LogStatus(status).value
        return await self._col.find(query).sort("sent_at", DESCENDING).to_list(length=limit)

    async def update_status(
        self,
        log_id: Any,
        status: LogStatus,
        error_reason: str | None = None,
    ) -> bool:
        """يُستخدم عند نجاح إعادة المحاولة لسجل فاشل."""
        res = await self._col.update_one(
            {"_id": log_id},
            {
                "$set": {
                    "status": LogStatus(status).value,
                    "error_reason": error_reason,
                    "updated_at": utcnow(),
                }
            },
        )
        return res.modified_count > 0

    async def list_active(self, campaign_id: str, limit: int = 0) -> list[dict[str, Any]]:
        """لا توجد حالة "active" للسجلات، فأعيد هنا السجلات الفاشلة
        القابلة لإعادة المحاولة — وهو المعنى العملي المكافئ."""
        return await self._col.find(
            {"campaign_id": campaign_id, "status": LogStatus.FAILED.value}
        ).sort("sent_at", ASCENDING).to_list(length=limit or None)

    async def already_sent(self, campaign_id: str, target_chat_id: int) -> bool:
        """يمنع التكرار عند استئناف حملة متوقفة."""
        return await self._col.count_documents(
            {
                "campaign_id": campaign_id,
                "target_chat_id": target_chat_id,
                "status": LogStatus.SUCCESS.value,
            },
            limit=1,
        ) > 0

    async def success_count(self, campaign_id: str, target_chat_id: int) -> int:
        """عدد الرسائل التي وصلت فعلاً لهذا الهدف في هذه الحملة."""
        return await self._col.count_documents(
            {
                "campaign_id": campaign_id,
                "target_chat_id": target_chat_id,
                "status": LogStatus.SUCCESS.value,
            }
        )

    async def distinct_targets(self, campaign_id: str) -> int:
        """عدد المحادثات التي جرت معالجتها (نجاحاً أو فشلاً) في هذه الحملة."""
        return len(
            await self._col.distinct("target_chat_id", {"campaign_id": campaign_id})
        )

    async def count_failed(self, campaign_id: str) -> int:
        return await self._col.count_documents(
            {"campaign_id": campaign_id, "status": LogStatus.FAILED.value}
        )

    async def list_failed(
        self, campaign_id: str, skip: int = 0, limit: int = 8
    ) -> list[dict[str, Any]]:
        return await self._col.find(
            {"campaign_id": campaign_id, "status": LogStatus.FAILED.value}
        ).sort("sent_at", DESCENDING).skip(skip).to_list(length=limit)

    async def stats(self, campaign_id: str) -> dict[str, int]:
        cursor = self._col.aggregate(
            [
                {"$match": {"campaign_id": campaign_id}},
                {"$group": {"_id": "$status", "count": {"$sum": 1}}},
            ]
        )
        counts = {LogStatus.SUCCESS.value: 0, LogStatus.FAILED.value: 0}
        async for row in cursor:
            counts[row["_id"]] = row["count"]
        counts["total"] = counts[LogStatus.SUCCESS.value] + counts[LogStatus.FAILED.value]
        return counts

    async def stats_many(self, campaign_ids: list[str]) -> dict[str, dict[str, int]]:
        """إحصاءات عدة حملات في استعلام واحد بدل استعلام لكل حملة."""
        if not campaign_ids:
            return {}

        out = {
            cid: {LogStatus.SUCCESS.value: 0, LogStatus.FAILED.value: 0, "total": 0}
            for cid in campaign_ids
        }
        cursor = self._col.aggregate(
            [
                {"$match": {"campaign_id": {"$in": campaign_ids}}},
                {
                    "$group": {
                        "_id": {"c": "$campaign_id", "s": "$status"},
                        "n": {"$sum": 1},
                    }
                },
            ]
        )
        async for row in cursor:
            cid, st = row["_id"]["c"], row["_id"]["s"]
            if cid in out and st in out[cid]:
                out[cid][st] = row["n"]

        for value in out.values():
            value["total"] = value[LogStatus.SUCCESS.value] + value[LogStatus.FAILED.value]
        return out

    async def failure_reasons(
        self, campaign_id: str, limit: int = 3
    ) -> list[tuple[str, int]]:
        """أكثر أسباب الفشل تكراراً، مجمَّعة بنوع الاستثناء (ما قبل ':')."""
        cursor = self._col.aggregate(
            [
                {"$match": {"campaign_id": campaign_id, "status": LogStatus.FAILED.value}},
                {
                    "$group": {
                        "_id": {
                            "$arrayElemAt": [
                                {"$split": [{"$ifNull": ["$error_reason", "unknown"]}, ":"]},
                                0,
                            ]
                        },
                        "n": {"$sum": 1},
                    }
                },
                {"$sort": {"n": -1}},
                {"$limit": limit},
            ]
        )
        return [(row["_id"] or "unknown", row["n"]) async for row in cursor]
