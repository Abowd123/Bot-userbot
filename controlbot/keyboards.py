"""لوحات الأزرار المشتركة بين bot.py والمعالجات."""

from __future__ import annotations

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# بادئات callback_data — مصدر الحقيقة الوحيد للتوجيه
CB_TARGETS = "targets"
CB_CAMPAIGN = "campaign"
CB_CONTROL = "control"
CB_LOGS = "logs"
CB_MENU = "menu"

# حد تيليجرام لـ callback_data هو 64 بايت، فالبادئات قصيرة عن قصد
SEPARATOR = ":"


def cb(prefix: str, action: str, *args: str | int) -> str:
    """يبني callback_data بصيغة prefix:action[:arg...]."""
    parts = [prefix, action, *(str(a) for a in args)]
    data = SEPARATOR.join(parts)
    if len(data.encode()) > 64:
        raise ValueError(f"callback_data أطول من 64 بايت: {data!r}")
    return data


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📋 إدارة المحادثات المستهدفة", callback_data=cb(CB_TARGETS, "menu")
                )
            ],
            [
                InlineKeyboardButton(
                    "📝 إنشاء حملة جديدة", callback_data=cb(CB_CAMPAIGN, "new")
                )
            ],
            [
                InlineKeyboardButton(
                    "▶️ الحملات النشطة", callback_data=cb(CB_CONTROL, "active")
                )
            ],
            [InlineKeyboardButton("📊 السجل", callback_data=cb(CB_LOGS, "menu"))],
        ]
    )


def back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ رجوع", callback_data=cb(CB_MENU, "home"))]]
    )
