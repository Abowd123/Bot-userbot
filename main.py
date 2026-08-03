"""نقطة التشغيل: userbot وبوت التحكم في نفس حلقة الأحداث."""

from __future__ import annotations

import asyncio
import logging
import sys

from pyrogram import Client, idle
from pyrogram.errors import (
    AccessTokenInvalid,
    ApiIdInvalid,
    AuthKeyDuplicated,
    AuthKeyUnregistered,
    RPCError,
    SessionExpired,
    SessionRevoked,
    UserDeactivated,
    UserDeactivatedBan,
)

from config import Config, ConfigError, load_config
from controlbot import create_controlbot
from controlbot.context import runtime
from controlbot.handlers.campaign import shutdown_campaign_tasks
from controlbot.handlers.controls import watchers
from db import DbError, init_db, mongo
from recovery import auto_resume
from userbot import create_userbot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
log = logging.getLogger("main")

INVALID_SESSION_ERRORS = (
    SessionRevoked,
    SessionExpired,
    AuthKeyUnregistered,
    AuthKeyDuplicated,
    UserDeactivated,
    UserDeactivatedBan,
)

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_INVALID_SESSION = 3
EXIT_NETWORK_ERROR = 4
EXIT_DB_ERROR = 5


def _print_account(me, role: str) -> None:
    full_name = " ".join(p for p in (me.first_name, me.last_name) if p) or "-"
    username = f"@{me.username}" if me.username else "لا يوجد"
    print(f"  {role:<12} | {full_name} | {username} | id={me.id}")


async def _start_clients(userbot: Client, controlbot: Client) -> None:
    """يشغّل العميلين بالتوازي على نفس حلقة الأحداث.

    gather يجعل مصافحة التسجيل تجري لكليهما في الوقت نفسه بدل التتابع.
    إن فشل أحدهما، نوقف الآخر حتى لا يبقى اتصال معلّق.
    """
    results = await asyncio.gather(
        userbot.start(), controlbot.start(), return_exceptions=True
    )

    failures = [r for r in results if isinstance(r, BaseException)]
    if failures:
        for client, result in zip((userbot, controlbot), results):
            if not isinstance(result, BaseException):
                try:
                    await client.stop()
                except Exception:  # noqa: BLE001
                    pass
        raise failures[0]


async def _stop_clients(userbot: Client, controlbot: Client) -> None:
    await asyncio.gather(userbot.stop(), controlbot.stop(), return_exceptions=True)


async def run() -> int:
    # 1) الإعدادات
    try:
        config: Config = load_config()
    except ConfigError as exc:
        log.error("خطأ في الإعدادات: %s", exc)
        return EXIT_CONFIG_ERROR

    # 2) قاعدة البيانات أولاً: أرخص وأسرع فشلاً من الاتصال بتيليجرام
    try:
        db = await init_db(config.mongo_uri, config.mongo_db_name)
    except DbError as exc:
        log.error("%s", exc)
        return EXIT_DB_ERROR

    userbot = create_userbot(config)
    controlbot = create_controlbot(config)
    started = False

    try:
        # 3) تشغيل العميلين معاً
        log.info("جارٍ تشغيل الـ userbot وبوت التحكم…")
        await _start_clients(userbot, controlbot)
        started = True

        me_user, me_bot = await asyncio.gather(userbot.get_me(), controlbot.get_me())
        print("\n" + "=" * 62)
        print("العميلان يعملان")
        print("=" * 62)
        _print_account(me_user, "userbot")
        _print_account(me_bot, "control bot")
        print("=" * 62 + "\n")

        # 4) ربط السياق: من هنا تصل معالجات بوت التحكم إلى الـ userbot
        runtime.bind(userbot=userbot, db=db)

        # 5) استئناف تلقائي للحملات المعلّقة
        try:
            await auto_resume(controlbot, config.owner_id, db)
        except Exception:  # noqa: BLE001 - فشل الاستئناف لا يمنع التشغيل
            log.exception("فشل الاستئناف التلقائي؛ متابعة التشغيل.")

        log.info("النظام جاهز. للإيقاف: Ctrl+C")
        await idle()
        return EXIT_OK

    except INVALID_SESSION_ERRORS as exc:
        log.error(
            "جلسة الـ userbot غير صالحة (%s). أنشئ SESSION_STRING جديداً.",
            type(exc).__name__,
        )
        return EXIT_INVALID_SESSION

    except AccessTokenInvalid:
        log.error("BOT_TOKEN غير صحيح. أعد توليده من @BotFather.")
        return EXIT_CONFIG_ERROR

    except ApiIdInvalid:
        log.error("API_ID و/أو API_HASH غير صحيحين. راجع my.telegram.org.")
        return EXIT_CONFIG_ERROR

    except (ValueError, TypeError) as exc:
        log.error("قيمة إعداد غير صالحة (تحقق من SESSION_STRING): %s", exc)
        return EXIT_INVALID_SESSION

    except (OSError, ConnectionError, asyncio.TimeoutError) as exc:
        log.error("فشل الاتصال بالشبكة: %s", exc)
        return EXIT_NETWORK_ERROR

    except RPCError as exc:
        log.error("خطأ من تيليجرام (%s): %s", type(exc).__name__, exc)
        return EXIT_NETWORK_ERROR

    finally:
        # 6) إيقاف نظيف بترتيب مقصود: المهام قبل الاتصالات قبل قاعدة البيانات
        log.info("جارٍ الإيقاف…")
        watchers.stop_all()
        try:
            await shutdown_campaign_tasks()
        except Exception:  # noqa: BLE001
            log.exception("خطأ أثناء إلغاء مهام الحملات.")
        if started:
            await _stop_clients(userbot, controlbot)
        await mongo.close()
        log.info("تم الإيقاف.")


def main() -> None:
    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:
        log.info("تم الإيقاف يدوياً.")
        sys.exit(EXIT_OK)


if __name__ == "__main__":
    main()
