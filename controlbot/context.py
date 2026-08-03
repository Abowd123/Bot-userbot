"""سياق التشغيل المشترك: يعطي معالجات بوت التحكم وصولاً إلى الـ userbot وقاعدة البيانات.

العميلان مستقلان تماماً؛ هذا هو الجسر الوحيد بينهما.
"""

from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorDatabase
from pyrogram import Client


class RuntimeNotReady(Exception):
    """طُلب مورد قبل ربطه في main.py."""


class Runtime:
    def __init__(self) -> None:
        self._userbot: Client | None = None
        self._db: AsyncIOMotorDatabase | None = None

    def bind(self, *, userbot: Client, db: AsyncIOMotorDatabase) -> None:
        self._userbot = userbot
        self._db = db

    @property
    def userbot(self) -> Client:
        if self._userbot is None:
            raise RuntimeNotReady("لم يُربط الـ userbot بسياق التشغيل.")
        return self._userbot

    @property
    def db(self) -> AsyncIOMotorDatabase:
        if self._db is None:
            raise RuntimeNotReady("لم تُربط قاعدة البيانات بسياق التشغيل.")
        return self._db


runtime = Runtime()
