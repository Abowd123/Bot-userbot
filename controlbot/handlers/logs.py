"""عرض سجل الحملات المنتهية والجارية، وتفاصيل أسباب الفشل لكل محادثة."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from pyrogram import Client
from pyrogram.errors import RPCError
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from controlbot.context import RuntimeNotReady, runtime
from controlbot.handlers.base import Route, edit, route
from controlbot.handlers.controls import STATUS_EMOJI, STATUS_LABEL
from controlbot.keyboards import CB_LOGS, CB_MENU, cb
from db import CampaignsRepo, CampaignStatus, LogsRepo, TargetsRepo

log = logging.getLogger(__name__)

PAGE_SIZE = 5           # حملات لكل صفحة
FAIL_PAGE_SIZE = 8      # سجلات فشل لكل صفحة
TOP_REASONS = 3

MAX_REASON_LEN = 90
MAX_TITLE_LEN = 24

# المنطقة الزمنية للعرض فقط؛ التخزين يبقى UTC دائماً
_TZ_NAME = os.getenv("DISPLAY_TZ", "UTC")
try:
    DISPLAY_TZ = ZoneInfo(_TZ_NAME)
except Exception:  # noqa: BLE001 - قاعدة بيانات tzdata غائبة أو اسم خاطئ
    log.warning("DISPLAY_TZ=%r غير صالح؛ سيُستخدم UTC.", _TZ_NAME)
    DISPLAY_TZ = timezone.utc


# ------------------------------------------------------------------ أدوات

def _code(text: str) -> str:
    """يجعل النص آمناً داخل `inline code` بمنع علامات backtick."""
    return str(text).replace("`", "'")


def _shorten(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _to_display(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(DISPLAY_TZ)


def _fmt_dt(dt: datetime | None) -> str:
    local = _to_display(dt)
    return local.strftime("%Y-%m-%d %H:%M") if local else "-"


def _fmt_dt_short(dt: datetime | None) -> str:
    local = _to_display(dt)
    return local.strftime("%m-%d %H:%M") if local else "-"


def _page_count(total: int, size: int) -> int:
    return max(1, -(-total // size))


def _clamp(page: int, total: int, size: int) -> int:
    return max(0, min(page, _page_count(total, size) - 1))


# ------------------------------------------------------------------ اللوحات

def _list_kb(
    campaigns: list[dict], stats: dict, page: int, pages: int
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    for doc in campaigns:
        cid = doc["campaign_id"]
        st = stats.get(cid, {})
        emoji = STATUS_EMOJI.get(CampaignStatus(doc["status"]), "•")
        rows.append(
            [
                InlineKeyboardButton(
                    f"{emoji} {_fmt_dt_short(doc.get('created_at'))} "
                    f"• ✅{st.get('success', 0)} ❌{st.get('failed', 0)}",
                    callback_data=cb(CB_LOGS, "fail", cid, 0),
                )
            ]
        )

    if pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    "◀️ السابق", callback_data=cb(CB_LOGS, "page", page - 1)
                )
            )
        nav.append(
            InlineKeyboardButton(f"{page + 1}/{pages}", callback_data=cb(CB_LOGS, "noop"))
        )
        if page < pages - 1:
            nav.append(
                InlineKeyboardButton(
                    "التالي ▶️", callback_data=cb(CB_LOGS, "page", page + 1)
                )
            )
        rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton("🔄 تحديث", callback_data=cb(CB_LOGS, "page", page)),
            InlineKeyboardButton("◀️ رجوع", callback_data=cb(CB_MENU, "home")),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _fail_kb(campaign_id: str, page: int, pages: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    "◀️ السابق", callback_data=cb(CB_LOGS, "fail", campaign_id, page - 1)
                )
            )
        nav.append(
            InlineKeyboardButton(f"{page + 1}/{pages}", callback_data=cb(CB_LOGS, "noop"))
        )
        if page < pages - 1:
            nav.append(
                InlineKeyboardButton(
                    "التالي ▶️", callback_data=cb(CB_LOGS, "fail", campaign_id, page + 1)
                )
            )
        rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton(
                "🔄 تحديث", callback_data=cb(CB_LOGS, "fail", campaign_id, page)
            ),
            InlineKeyboardButton("◀️ السجل", callback_data=cb(CB_LOGS, "menu")),
        ]
    )
    rows.append([InlineKeyboardButton("🏠 الرئيسية", callback_data=cb(CB_MENU, "home"))])
    return InlineKeyboardMarkup(rows)


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("◀️ السجل", callback_data=cb(CB_LOGS, "menu")),
                InlineKeyboardButton("🏠 الرئيسية", callback_data=cb(CB_MENU, "home")),
            ]
        ]
    )


# ------------------------------------------------------------------ العرض

async def _render_list(query: CallbackQuery, page: int, notice: str | None = None) -> None:
    campaigns_repo = CampaignsRepo(runtime.db)
    logs_repo = LogsRepo(runtime.db)

    total = await campaigns_repo.count_all()
    if total == 0:
        text = "**📊 السجل**\n\nلا توجد حملات مسجَّلة بعد."
        if notice:
            text += f"\n\n{notice}"
        await edit(query, text, _back_kb())
        return

    page = _clamp(page, total, PAGE_SIZE)
    campaigns = await campaigns_repo.list_recent(skip=page * PAGE_SIZE, limit=PAGE_SIZE)
    stats = await logs_repo.stats_many([c["campaign_id"] for c in campaigns])

    lines = [
        "**📊 السجل**",
        "",
        f"إجمالي الحملات: **{total}**"
        f"  •  الصفحة **{page + 1}** من **{_page_count(total, PAGE_SIZE)}**",
        "",
    ]

    for offset, doc in enumerate(campaigns, start=page * PAGE_SIZE + 1):
        cid = doc["campaign_id"]
        st = stats.get(cid, {})
        status = CampaignStatus(doc["status"])
        lines += [
            f"**{offset}.** `{cid}`  {STATUS_EMOJI.get(status, '•')} "
            f"{STATUS_LABEL.get(status, status.value)}",
            f"     🗓 {_fmt_dt(doc.get('created_at'))}"
            f"  •  نوع: {doc.get('content', {}).get('kind', '-')}",
            f"     ✅ نجاح **{st.get('success', 0)}**"
            f"  •  ❌ فشل **{st.get('failed', 0)}**",
            "",
        ]

    lines.append(f"التوقيت المعروض: {_TZ_NAME}")
    lines.append("اضغط على أي حملة لعرض تفاصيل الفشل.")
    if notice:
        lines += ["", notice]

    await edit(
        query,
        "\n".join(lines),
        _list_kb(campaigns, stats, page, _page_count(total, PAGE_SIZE)),
    )


async def _render_failures(query: CallbackQuery, campaign_id: str, page: int) -> None:
    if not campaign_id:
        await _render_list(query, 0, notice="⚠️ معرّف حملة غير صالح.")
        return

    campaigns_repo = CampaignsRepo(runtime.db)
    logs_repo = LogsRepo(runtime.db)

    campaign = await campaigns_repo.get(campaign_id)
    if campaign is None:
        await _render_list(query, 0, notice=f"⚠️ لا توجد حملة بالمعرّف `{campaign_id}`.")
        return

    status = CampaignStatus(campaign["status"])
    stats = await logs_repo.stats(campaign_id)
    total_failed = await logs_repo.count_failed(campaign_id)

    header = [
        f"**📊 حملة** `{campaign_id}`",
        "",
        f"الحالة: {STATUS_EMOJI.get(status, '•')} "
        f"**{STATUS_LABEL.get(status, status.value)}**",
        f"أُنشئت: **{_fmt_dt(campaign.get('created_at'))}**",
        f"✅ نجاح: **{stats.get('success', 0)}**  •  ❌ فشل: **{stats.get('failed', 0)}**",
    ]

    if total_failed == 0:
        header += ["", "لا توجد حالات فشل في هذه الحملة."]
        await edit(query, "\n".join(header), _back_kb())
        return

    page = _clamp(page, total_failed, FAIL_PAGE_SIZE)
    rows = await logs_repo.list_failed(
        campaign_id, skip=page * FAIL_PAGE_SIZE, limit=FAIL_PAGE_SIZE
    )
    titles = await TargetsRepo(runtime.db).get_titles(
        [int(r["target_chat_id"]) for r in rows]
    )

    reasons = await logs_repo.failure_reasons(campaign_id, limit=TOP_REASONS)
    if reasons:
        header += ["", "**أكثر أسباب الفشل:**"]
        header += [f"• `{_code(name)}` — **{count}**" for name, count in reasons]

    header += [
        "",
        f"**تفاصيل الفشل** ({page + 1}/{_page_count(total_failed, FAIL_PAGE_SIZE)})",
        "",
    ]

    for entry in rows:
        chat_id = int(entry["target_chat_id"])
        title = _shorten(titles.get(chat_id, str(chat_id)), MAX_TITLE_LEN)
        reason = _shorten(entry.get("error_reason") or "سبب غير مسجَّل", MAX_REASON_LEN)
        header += [
            f"❌ `{_code(title)}` · `{chat_id}`",
            f"     🕐 {_fmt_dt(entry.get('sent_at'))}",
            f"     `{_code(reason)}`",
            "",
        ]

    await edit(
        query,
        "\n".join(header),
        _fail_kb(campaign_id, page, _page_count(total_failed, FAIL_PAGE_SIZE)),
    )


# ------------------------------------------------------------------ المعالج

@route(CB_LOGS)
async def handle_logs(client: Client, query: CallbackQuery, route_obj: Route) -> None:
    action = route_obj.action

    if action == "noop":
        return

    try:
        if action == "menu":
            await _render_list(query, 0)

        elif action == "page":
            await _render_list(query, int(route_obj.arg(0, "0")))

        elif action == "fail":
            await _render_failures(query, route_obj.arg(0, ""), int(route_obj.arg(1, "0")))

        else:
            await _render_list(query, 0, notice="⚠️ إجراء غير معروف.")

    except RuntimeNotReady as exc:
        log.error("سياق التشغيل غير مكتمل: %s", exc)
        await edit(query, f"⚠️ {exc}", _back_kb())

    except ValueError as exc:
        log.warning("callback_data معطوب %r: %s", route_obj.raw, exc)
        await _render_list(query, 0, notice="⚠️ زر غير صالح.")

    except RPCError as exc:
        log.exception("خطأ تيليجرام في قسم السجل.")
        await edit(query, f"⚠️ خطأ من تيليجرام: `{type(exc).__name__}`", _back_kb())
