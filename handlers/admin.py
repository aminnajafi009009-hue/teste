"""
handlers/admin.py
پنل کامل ادمین: آمار، لیست کاربران، جستجوی حرفه‌ای، شارژ کیف پول، ارسال
کانفیگ، تأیید/رد پرداخت کارت‌به‌کارت خرید سرویس، مدیریت تخفیف (ساخت گام‌به‌گام)،
مدیریت دعوت‌ها، پیام همگانی، بکاپ.

تمام handlerهای این فایل فقط برای ADMIN_ID فعال هستند.
"""

from datetime import datetime, timedelta
import asyncio
import html
import json
import logging
import os
import re

from aiogram import Router, F, types, BaseMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile

import database as db
import cache
import crypto
from payment_manager_v3 import PaymentManager as _PM
import alerts
from subscription import extract_meta, days_remaining, format_bytes, usage_bar, fetch_subscription_info, format_expire, extract_configs
from utils import parse_int_in_range, is_duplicate_action, now_tehran_naive, STICKER_SECTION_LABELS, STICKER_FILES, STICKERS_DIR, invalidate_section_sticker_cache, send_notification_sticker, clean_numeric_id, telegram_utf16_length, to_jalali_str, now_tehran, stat_period_bounds_utc, parse_jalali_date
from states import AdminStates, UserStates
from permissions import permission_for_callback as _registry_permission_for_callback, permission_for_message as _registry_permission_for_message
import bot_info
import panels as panel_api
_pm_instance: "_PM | None" = None
def _pm() -> "_PM":
    """Lazy singleton برای PaymentManager."""
    global _pm_instance
    if _pm_instance is None:
        _pm_instance = _PM(db.get_setting, db.set_setting)
        _pm_instance.load()
    return _pm_instance

from config import (
    ADMIN_ID,
    DATABASE_PATH,
    AGENCY_VIP_DISCOUNT_PERCENT,
    FREE_TEST_PLAN_KEY,
)
from keyboards import (
    InlineKeyboardButton,
    admin_panel_menu,
    admin_reply_keyboard,
    admin_back_button,
    admin_discount_menu,
    admin_user_actions_keyboard,
    admin_purchase_notify_keyboard,
    admin_custom_order_notify_keyboard,
    admin_broadcast_audience_keyboard,
    admin_gift_all_confirm_keyboard,
    admin_health_keyboard,
    admin_cache_keyboard,
    admin_custom_build_keyboard,
    admin_audit_logs_keyboard,
    admin_stats_keyboard,
    admin_userlist_menu,
    config_delivery_keyboard,
    main_reply_keyboard,
    admin_services_list_keyboard,
    admin_service_detail_keyboard,
    admin_order_queue_keyboard,
    admin_clear_orders_confirm_keyboard,
    admin_request_queue_menu,
    admin_pending_receipts_keyboard,
    admin_clear_receipts_confirm_keyboard,
    admin_purge_confirm_keyboard,
    admin_userlist_page_keyboard,
    admin_accounting_keyboard,
    discount_type_keyboard,
    discount_plans_select_keyboard,
    discount_plans_edit_keyboard,
    discount_detail_keyboard,
    discount_audience_keyboard,
    discount_delete_confirm_keyboard,
    admin_agency_menu,
    admin_agent_row_keyboard,
    admin_agent_actions_keyboard,
    admin_vip_categories_keyboard,
    vip_categories_keyboard,
    admin_vip_category_detail_keyboard,
    admin_vip_plan_detail_keyboard,
    admin_pm_cancel_keyboard,
    admin_referrers_page_keyboard,
    admin_referred_detail_keyboard,
    admin_guides_menu,
    admin_guide_detail_keyboard,
    admin_guide_delete_confirm_keyboard,
    admin_guide_cancel_keyboard,
    admin_error_logs_keyboard,
    admin_error_log_detail_keyboard,
    admin_error_logs_clear_confirm_keyboard,
    admin_stickers_menu,
    admin_sticker_detail_keyboard,
    admin_sticker_cancel_keyboard,
    admin_botinfo_menu,
    admin_botinfo_field_keyboard,
    admin_botinfo_channels_menu,
    admin_botinfo_wallet_menu,
    admin_botinfo_referral_menu,
    admin_campaigns_menu,
    campaign_detail_keyboard,
    campaign_delete_confirm_keyboard,
    admin_manage_admins_keyboard,
    admin_permissions_keyboard,
    admin_renewal_categories_menu, admin_renewal_category_menu, admin_renewal_plan_menu, admin_renewal_mode_menu, admin_renewal_scope_menu,
    admin_referral_settings_menu, admin_wallet_settings_menu, admin_payment_methods_menu, admin_log_settings_menu,
    admin_crypto_list_menu, admin_card_info_menu, admin_wallet_info_menu,
)

plan_type = db.plan_type  # نسخه‌ی DB-aware (دسته‌بندی‌های VIP را هم می‌شناسد)

import ui_editor as _ui_editor

TEXTS = {k: v.get("default", "") for k, v in _ui_editor.SCREENS.items()}
TEXT_CATEGORIES = {}
for _key, _screen in _ui_editor.SCREENS.items():
    TEXT_CATEGORIES.setdefault(_screen.get("category", "other"), []).append(_key)
CATEGORY_BY_KEY = {k: _ui_editor.SCREENS[k].get("category", "other") for k in _ui_editor.SCREENS}

def user_text(key: str, default: str = "", **values):
    value = _ui_editor.get_text(key, default)
    try:
        return value.format(**values)
    except (KeyError, ValueError, IndexError):
        return value

def refresh_user_text(key: str | None = None):
    return None

TELEGRAM_TEXT_LIMIT = 4096

def truncate_for_telegram(value: str, limit: int = TELEGRAM_TEXT_LIMIT) -> str:
    value = str(value or "")
    if telegram_utf16_length(value) <= limit:
        return value
    out = []
    total = 0
    for ch in value:
        n = len(ch.encode("utf-16-le")) // 2
        if total + n > limit:
            break
        out.append(ch); total += n
    return "".join(out)

def is_message_too_long_error(exc: Exception) -> bool:
    return "message is too long" in str(exc).lower() or "MESSAGE_TOO_LONG" in str(exc).upper()

def serialize_message_entities(entities):
    result = []
    for entity in entities or []:
        try:
            result.append(entity.model_dump(exclude_none=True))
        except Exception:
            try:
                result.append(dict(entity))
            except Exception:
                pass
    return result

def message_entities_from_dicts(items):
    result = []
    for item in items or []:
        try:
            result.append(types.MessageEntity(**item) if isinstance(item, dict) else item)
        except Exception:
            pass
    return result

async def enrich_configs_with_subscription_names(configs):
    for cfg in configs or []:
        cfg["_display_name"] = cfg.get("_display_name") or cfg.get("plan") or ""
        try:
            raw = crypto.decrypt_config(cfg.get("config", ""))
            if raw and raw.lower().startswith(("http://", "https://")):
                meta = await extract_meta(raw)
                name = (meta or {}).get("name")
                if name:
                    cfg["_display_name"] = name
        except Exception:
            pass
    return configs

def _admin_user_delete_keyboard(uid):
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، حذف کن", callback_data=f"deleteuserconfirm_{uid}", style="danger")],
        [InlineKeyboardButton(text="🔙 انصراف", callback_data=f"useropen_{uid}", style="primary")],
    ])

def _available_panels():
    rows = db.list_vpn_panels(enabled_only=True)
    return {str(p["id"]): panel_api.panel_label(p) for p in rows}

def _active_panel_id():
    value = db.get_setting("active_vpn_panel", "")
    return str(value) if value else None

def _set_active_panel(panel_id):
    try:
        pid = int(panel_id)
    except Exception:
        return False
    if db.get_vpn_panel(pid) is None:
        return False
    db.set_setting("active_vpn_panel", str(pid))
    return True

def _panel_choose_keyboard(available, active):
    buttons = []
    for pid, label in available.items():
        prefix = "✅ " if str(pid) == str(active) else ""
        buttons.append([InlineKeyboardButton(text=prefix + label, callback_data=f"panelchoose_{pid}", style="primary")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")])
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)

def _pasargad_menu(_text=""):
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📡 تست اتصال", callback_data="pasargadtest", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")],
    ])

router = Router(name="admin")
logger = logging.getLogger(__name__)


def _permission_for_message_text(text: str | None) -> str | None:
    registered = _registry_permission_for_message(text)
    if registered:
        return registered
    mapping = {
        "📊 آمار": "stats",
        "📥 صف درخواست‌ها": "requests",
        "👥 لیست کاربران": "users",
        "🔍 جستجوی کاربر": "users",
        "📢 پیام همگانی": "broadcast",
        "🎟 مدیریت تخفیف": "discounts",
        "🤝 نمایندگی (تخفیف VIP)": "agency",
        "🗂 دسته‌بندی‌های VIP": "plans",
        "🛒 خرید اشتراک برای خودم": "plans",
        "📦 نگاشت پلن‌ها به پنل فعال": "vpn_panel",
        "🛡️ اتصال پنل پاسارگارد": "vpn_panel",
        "🔀 انتخاب پنل VPN فعال": "vpn_panel",
        "🤝 مدیریت دعوت‌ها": "referrals",
        "📚 مدیریت راهنما": "guides",
        "🦖 لاگ خطاها": "logs",
        "ℹ️ اطلاعات ربات": "botinfo",
        "🎬 استیکرهای منو": "stickers",
        "💾 بکاپ": "backup",
        "🎁 تنظیم تست رایگان": "settings",
        "🔁 تنظیمات تمدید": "settings",
        "📝 مدیریت متن‌های کاربر": "texts",
        "✏️ ویرایش متن و دکمه‌ها": "texts",
        "🔴 خاموش کردن سفارشات": "orders_toggle",
        "🟢 روشن کردن سفارشات": "orders_toggle",
        "🎁 تنظیمات رفرال": "referral_settings",
        "💳 تنظیمات کیف‌پول": "wallet_settings",
        "💳 روش‌های پرداخت": "payment_methods",
        "📜 مدیریت لاگ‌ها": "log_settings",
    }
    return mapping.get(text or "")


def _permission_for_callback(data: str | None) -> str | None:
    d = data or ""
    registered = _registry_permission_for_callback(d)
    if registered:
        return registered
    if d in {"admin_back"}:
        return None
    if d.startswith(("approve_", "reject_", "approvepay|", "rejectpay|", "approvecustom_", "rejectcustom_", "clearreceipts")) or d == "admin_pending_receipts":
        return "receipts"
    groups = [
        (("admin_stats", "stat_"), "stats"),
        (("admin_request_queue", "admin_order_queue", "dismissorder_", "clearorders", "marzbansend|"), "requests"),
        (("admin_userlist", "userpage_", "useropen_", "accounting_", "admin_search", "useractions_", "pm_", "toggleblock_", "deleteuser_", "deleteuserconfirm_", "svcs_", "svcdetail_", "svcdelete_", "svcrestore_", "svcpurge", "svcedit_"), "users"),
        (("admin_broadcast",), "broadcast"),
        (("admin_discount", "discdetail_", "discdelete", "discedit_", "discaud_", "discplan", "new_discount", "disctype_"), "discounts"),
        (("admin_agency", "new_agent", "deleteagent_", "agentopen_", "editagentpercent_", "editagentcats_", "agentcat_", "agentcatall_", "editagentprefix_"), "agency"),
        (("admin_vip_categories", "admin_buy_subscription", "newvip", "vip"), "plans"),
        (("admin_marzban", "admin_pasargad", "admin_panel_choose", "panelchoose", "marz", "pasargad", "svcrevokesub_"), "vpn_panel"),
        (("admin_botinfo", "botinfo", "channel"), "botinfo"),
        (("admin_stickers", "sticker"), "stickers"),
        (("admin_referrals", "refpage_", "refdetail_"), "referrals"),
        (("admin_guides", "guide"), "guides"),
        (("admin_texts", "admin_text_editor", "textedit_", "textdelete_", "ui_"), "texts"),
        (("errlog",), "logs"),
        (("admin_backup",), "backup"),
        (("admin_orders_off", "admin_orders_on"), "orders_toggle"),
        (("admin_referral_settings", "refset_"), "referral_settings"),
        (("admin_wallet_settings", "wset_"), "wallet_settings"),
        (("admin_payment_methods", "paymt_card", "paymt_wallet", "paymt_uniquepay", "paymt_crypto", "paymt_add_coin", "paymt_del_coin", "paymt_toggle"), "payment_methods"),
        (("admin_log_settings", "admin_order_log_channel"), "log_settings"),
        (("free_test", "admin_free_test_settings", "admin_renewal_settings", "renewset", "renewsetscope", "renewsetcat"), "settings"),
    ]
    for prefixes, perm in groups:
        if any(d == x or d.startswith(x) for x in prefixes):
            return perm
    return None


class AdminPermissionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if not user or user.id == ADMIN_ID:
            return await handler(event, data)
        if not db.is_sub_admin(str(user.id)):
            return await handler(event, data)
        # مدیریت خود ادمین‌های فرعی فقط برای ادمین اصلی است.
        cb_data = getattr(event, "data", None)
        if cb_data and (cb_data == "admin_manage_admins" or cb_data.startswith("subadm")):
            await event.answer("⛔ فقط ادمین اصلی دسترسی دارد.", show_alert=True)
            return
        perm = _permission_for_callback(cb_data) if cb_data is not None else _permission_for_message_text(getattr(event, "text", None))
        # اگر دکمه‌ای داخل یک بخش مجاز ساخته شده باشد، همان مجوز بخش مادر کافی است.
        if user.id != ADMIN_ID and perm == "receipts" and db.sub_admin_has_permission(str(user.id), "requests"):
            return await handler(event, data)
        if perm and not db.sub_admin_has_permission(str(user.id), perm):
            if cb_data is not None:
                await event.answer("⛔ شما به این قابلیت دسترسی ندارید.", show_alert=True)
            else:
                await event.answer("⛔ شما به این قابلیت دسترسی ندارید.")
            return
        return await handler(event, data)


router.message.middleware(AdminPermissionMiddleware())
router.callback_query.middleware(AdminPermissionMiddleware())


def _is_main_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def _is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID or db.is_sub_admin(str(user_id))

def _admin_perm(user_id: int, permission: str) -> bool:
    return user_id == ADMIN_ID or db.sub_admin_has_permission(str(user_id), permission)

def _current_admin_permissions(user_id: int) -> set[str] | None:
    if user_id == ADMIN_ID:
        return None
    adm = db.get_sub_admin(str(user_id)) or {}
    return set(adm.get("permissions") or [])

def _admin_panel_kb_for(user_id: int):
    return admin_panel_menu(db.is_orders_enabled(), permissions=_current_admin_permissions(user_id), is_main_admin=(user_id == ADMIN_ID))

def _admin_reply_kb_for(user_id: int, orders_enabled: bool | None = None):
    return admin_reply_keyboard(orders_enabled, permissions=_current_admin_permissions(user_id), is_main_admin=(user_id == ADMIN_ID))


async def _notify_main_admin_action(bot, actor, action: str, target: str = "", details: str = ""):
    name = getattr(actor, "full_name", None) or getattr(actor, "first_name", None) or str(getattr(actor, "id", ""))
    actor_id = str(getattr(actor, "id", ""))
    try:
        db.log_admin_action(actor_id, name, action, target, details)
    except Exception:
        logger.exception("admin activity log failed")
    if actor_id != str(ADMIN_ID):
        try:
            await bot.send_message(ADMIN_ID, f"📝 گزارش عملکرد ادمین فرعی\n\n👤 {name}\n🆔 {actor_id}\n✅ عملیات: {action}\n🎯 مورد: {target or '-'}\nℹ️ جزئیات: {details or '-'}")
        except Exception:
            logger.exception("notify main admin failed")

async def _deny_no_perm(obj, text="⛔ شما به این بخش دسترسی ندارید."):
    if hasattr(obj, "answer"):
        try:
            await obj.answer(text, show_alert=True)
        except TypeError:
            await obj.answer(text)


async def _reply_with_user_actions(target, text: str, uid, is_blocked: bool, *, edit: bool = False):
    """پیام همراه با کیبورد اقدامات کاربر (admin_user_actions_keyboard) را می‌فرستد یا ویرایش می‌کند.
    اگر تلگرام به‌خاطر تنظیمات حریم‌خصوصی محدودتر همان کاربر خاص، دکمه‌ی «رفتن به پیوی کاربر»
    (لینک tg://user) را رد کند (خطای BUTTON_USER_PRIVACY_RESTRICTED)، به‌جای کرش کردن کل پیام،
    همان پیام را بدون این دکمه‌ی خاص دوباره می‌فرستد؛ برای بقیه‌ی کاربران دکمه همچنان نمایش داده می‌شود."""
    try:
        kb = admin_user_actions_keyboard(uid, is_blocked)
        if edit:
            await target.edit_text(text, reply_markup=kb)
        else:
            await target.answer(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "BUTTON_USER_PRIVACY_RESTRICTED" in str(e):
            kb = admin_user_actions_keyboard(uid, is_blocked, show_pm_link=False)
            if edit:
                await target.edit_text(text, reply_markup=kb)
            else:
                await target.answer(text, reply_markup=kb)
        else:
            raise


_RECEIPTS_QUEUE_MARKER = "🧾 رسیدهای در انتظار تایید"


async def _finish_receipt_message(message: types.Message, note: str, queue_refresh=None):
    """پیام رسید (چه پیام متنی معمولی از ربات کلاسیک، چه پیام عکس+کپشن از
    Mini App که دکمه‌ها مستقیم روی خودِ عکس هستند) را با یک خط نتیجه
    (تأیید/رد) نهایی می‌کند و دکمه‌ها را حذف می‌کند.
    توجه: روی پیام‌های عکس‌دار، edit_text خطا می‌دهد (تلگرام برای عکس‌ها
    caption دارد نه text)؛ برای همین باید edit_caption صدا زده شود.

    queue_refresh: یک تابع async بدون آرگومان. اگر همین دکمه‌ی تأیید/رد از
    داخل پیام «صف درخواست‌ها → رسیدهای در انتظار تایید» زده شده باشد (نه از
    پیام تک‌رسیدیِ اصلی)، به‌جای خالی‌کردن دکمه‌های کل لیست، همان لیست
    رفرش می‌شود تا آیتم‌های دیگرِ هنوز-در-انتظار از بین نروند."""
    text_or_caption = message.caption if message.photo else message.text
    if queue_refresh is not None and text_or_caption and text_or_caption.startswith(_RECEIPTS_QUEUE_MARKER):
        await queue_refresh()
        return
    empty_kb = types.InlineKeyboardMarkup(inline_keyboard=[])
    if message.photo:
        await message.edit_caption(caption=(message.caption or "") + note, reply_markup=empty_kb)
    else:
        await message.edit_text((message.text or "") + note, reply_markup=empty_kb)




def _format_volume_gb_label(volume_gb) -> str:
    if volume_gb is None:
        return "نامشخص"
    try:
        v = float(volume_gb)
    except (TypeError, ValueError):
        return str(volume_gb)
    if v <= 0:
        return "نامحدود"
    if v < 1:
        mb = round(v * 1024)
        return f"{mb} مگابایت"
    return f"{int(v) if v.is_integer() else v:g} گیگ"



def _english_digits(value) -> str:
    text = str(value if value is not None else "")
    return text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))


def _delivery_service_label(plan_name, volume_gb, days, user_limit, plan_key=None) -> str:
    """نام نمایشی تحویل سرویس بر اساس اطلاعات واقعی پلن خریداری‌شده."""
    if plan_key == FREE_TEST_PLAN_KEY:
        return "تست رایگان"

    volume_text = _format_volume_gb_label(volume_gb) if volume_gb is not None else "نامشخص"
    days_text = f"زمان {days} روزه" if days else "زمان نامحدود"
    user_limit_text = "نامحدود کاربر" if not user_limit else f"{user_limit} کاربر"

    return _english_digits(f"{volume_text} | {days_text} | {user_limit_text}")

def _gb_from_bytes(num_bytes) -> float | None:
    if not num_bytes:
        return None
    try:
        return round(int(num_bytes) / (1024 ** 3), 1)
    except (TypeError, ValueError):
        return None


@router.message(Command("admin"))
async def admin_entry(message: types.Message):
    if not _is_admin(message.from_user.id):
        return  # کاربر عادی هیچ پاسخی نمی‌گیرد (نه حتی پیام خطا) - امنیتی
    await message.answer("👨‍💻 پنل مدیریت:", reply_markup=_admin_panel_kb_for(message.from_user.id))


@router.callback_query(F.data == "admin_back")
async def admin_back(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text("👨‍💻 پنل مدیریت:", reply_markup=_admin_panel_kb_for(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data == "admin_manage_admins")
async def admin_manage_admins(callback: types.CallbackQuery):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("⛔ فقط ادمین اصلی دسترسی دارد.", show_alert=True)
        return
    # رفع باگ: اگر متن/دکمه دقیقاً همان چیزی باشد که الان روی پیام هست (مثلاً دوبار زدن روی دکمه)، تلگرام خطای "message is not modified"
    # می‌دهد که قبلاً گرفته نمی‌شد و به هندلر سراسری خطا می‌رفت و به کاربر "خطایی پیش آمد" نشان داده می‌شد.
    try:
        await callback.message.edit_text("👮 مدیریت ادمین‌های فرعی\n\nادمین را انتخاب کنید یا ادمین جدید اضافه کنید:", reply_markup=admin_manage_admins_keyboard(db.get_all_sub_admins()))
    except TelegramBadRequest:
        pass
    await callback.answer()

# 🐛 فیکس: دکمه‌ی «مدیریت ادمین‌ها» قبلاً فقط داخل پنل اینلاین بود، طبق درخواست کاربر الان به منوی پایین صفحه (reply keyboard) هم منتقل شد.
@router.message(F.text == "👮 مدیریت ادمین‌ها")
async def admin_manage_admins_from_menu(message: types.Message, state: FSMContext):
    if not _is_main_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer("👮 مدیریت ادمین‌های فرعی\n\nادمین را انتخاب کنید یا ادمین جدید اضافه کنید:", reply_markup=admin_manage_admins_keyboard(db.get_all_sub_admins()))


@router.callback_query(F.data == "subadm_add")
async def subadm_add(callback: types.CallbackQuery, state: FSMContext):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("⛔ فقط ادمین اصلی دسترسی دارد.", show_alert=True); return
    await state.set_state(AdminStates.waiting_sub_admin_id)
    try:
        await callback.message.edit_text(
            "آیدی عددی تلگرام ادمین فرعی را بفرستید. اگر خواستید نام هم ثبت شود این‌طور بفرستید:\n"
            "نام | آیدی\n"
            "مثال: علی پشتیبان | 123456789",
            reply_markup=admin_back_button(),
        )
    except TelegramBadRequest:
        pass
    await callback.answer()

@router.message(AdminStates.waiting_sub_admin_id)
async def subadm_add_id(message: types.Message, state: FSMContext):
    if not _is_main_admin(message.from_user.id): return
    raw = clean_numeric_id(message.text or "")
    if "|" in raw:
        name, tid = [x.strip() for x in raw.split("|", 1)]
        tid = clean_numeric_id(tid)
    else:
        tid, name = raw, raw
    if not tid.isdigit():
        await message.answer("❌ فرمت درست نیست. فقط آیدی عددی یا «نام | آیدی» بفرستید.", reply_markup=admin_back_button()); return
    try:
        db.add_sub_admin(tid, name=name or tid, permissions=[])
    except Exception:
        logger.exception("خطا در ثبت ادمین فرعی")
        await message.answer("⚠️ ثبت آیدی با خطا مواجه شد. لطفاً دوباره تلاش کنید.", reply_markup=admin_back_button())
        return
    await _notify_main_admin_action(message.bot, message.from_user, "افزودن ادمین فرعی", tid, name or tid)
    await state.clear()
    await message.answer("✅ ادمین فرعی اضافه شد. حالا قابلیت‌هایش را با تیک انتخاب کنید:", reply_markup=admin_permissions_keyboard(tid, []))

@router.callback_query(F.data.startswith("subadm_") & ~F.data.startswith("subadmperm_") & ~F.data.startswith("subadmdel_"))
async def subadm_open(callback: types.CallbackQuery):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("⛔ فقط ادمین اصلی دسترسی دارد.", show_alert=True); return
    tid = callback.data.split("_",1)[1]
    adm = db.get_sub_admin(tid)
    if not adm:
        await callback.answer("❌ ادمین یافت نشد.", show_alert=True); return
    try:
        await callback.message.edit_text(f"👤 ادمین فرعی: {tid}\n\nقابلیت‌ها را تیک بزنید/بردارید:", reply_markup=admin_permissions_keyboard(tid, adm.get("permissions") or []))
    except TelegramBadRequest:
        pass
    await callback.answer()

@router.callback_query(F.data.startswith("subadmperm_"))
async def subadm_toggle_perm(callback: types.CallbackQuery):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("⛔ فقط ادمین اصلی دسترسی دارد.", show_alert=True); return
    rest = callback.data.removeprefix("subadmperm_")
    # رفع باگ: برخی کلیدهای قابلیت خودشان زیرخط دارند (مثل "vpn_panel"، "orders_toggle")، با rsplit("_", 1) قبلی
    # آیدی و نام قابلیت اشتباه جدا می‌شدند (مثلاً آیدی "...vpn" و قابلیت "panel") و ادمین پیدا نمی‌شد. الان با ':' (که در هیچ‌کدام وجود ندارد) جدا می‌شوند.
    if ":" in rest:
        tid, perm = rest.split(":", 1)
    else:
        # سازگاری با دکمه‌های قدیمی (اگر callback زیرخطدار قبلی هنوز جایی فعال باشد)
        tid, perm = rest.rsplit("_", 1)
    adm = db.get_sub_admin(tid)
    if not adm: await callback.answer("❌ یافت نشد.", show_alert=True); return
    perms = set(adm.get("permissions") or [])
    if perm in perms: perms.remove(perm)
    else: perms.add(perm)
    db.update_sub_admin_permissions(tid, sorted(perms))
    await _notify_main_admin_action(callback.bot, callback.from_user, "تغییر دسترسی ادمین فرعی", tid, perm)
    try:
        await callback.message.edit_reply_markup(reply_markup=admin_permissions_keyboard(tid, sorted(perms)))
    except TelegramBadRequest:
        pass
    # فیکس: قبلاً فقط کیبورد تأیید خود ادمین اصلی به‌روز می‌شد، ولی منوی پایین صفحه‌ی خود ادمین فرعی
    # (که از آخرین /start او ساخته شده بود) همچنان قدیمی می‌ماند تا دوباره /start بزند. حالا
    # همینجا منوی پایین صفحه‌اش را با دسترسی‌های تازه دوباره برایش می‌فرستیم تا فوراً به‌روز شود.
    try:
        await callback.bot.send_message(
            int(tid),
            "🔄 دسترسی‌های شما توسط ادمین اصلی به‌روزرسانی شد. منوی پایین صفحه شما براساس دسترسی‌های جدید به‌روز شد.",
            reply_markup=admin_reply_keyboard(permissions=set(perms), is_main_admin=False),
        )
    except Exception:
        logger.exception("اطلاع ادمین فرعی از تغییر دسترسی ناموفق بود")
    await callback.answer("✅ بروزرسانی شد")

@router.callback_query(F.data.startswith("subadmdel_"))
async def subadm_delete(callback: types.CallbackQuery):
    if not _is_main_admin(callback.from_user.id):
        await callback.answer("⛔ فقط ادمین اصلی دسترسی دارد.", show_alert=True); return
    tid = callback.data.removeprefix("subadmdel_")
    db.delete_sub_admin(tid)
    await _notify_main_admin_action(callback.bot, callback.from_user, "حذف ادمین فرعی", tid, "")
    try:
        await callback.message.edit_text("✅ ادمین حذف شد.", reply_markup=admin_manage_admins_keyboard(db.get_all_sub_admins()))
    except TelegramBadRequest:
        pass
    await callback.answer()

@router.message(F.text == "📥 صف درخواست‌ها")
async def menu_admin_request_queue(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    order_count = len(db.get_pending_orders(limit=200))
    receipt_count = len(db.get_pending_receipts(limit=200))
    await message.answer(
        "📥 صف درخواست‌ها\n\nچه چیزی رو می‌خوای بررسی کنی؟ 👇",
        reply_markup=admin_request_queue_menu(order_count, receipt_count),
    )


@router.message(F.text == "🤝 نمایندگی (تخفیف VIP)")
async def menu_admin_agency(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    agents = db.get_all_agents()
    text = (
        "🤝 هنوز هیچ نماینده‌ای ثبت نشده.\n\nبرای افزودن، دکمه‌ی زیر را بزنید 👇"
        if not agents else
        "🤝 نمایندگان فعلی (تخفیف خودکار روی VIP)\n\nروی هرکدام بزنید تا مثل بخش «کاربران» مدیریتش کنید 👇"
    )
    await message.answer(text, reply_markup=admin_agency_menu(agents))


@router.message(F.text == "🗂 دسته‌بندی‌های VIP")
async def menu_admin_vip_categories(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    await message.answer(
        "🗂 دسته‌بندی‌های VIP\n\n"
        "این دسته‌ها همان چیزی هستند که کاربر موقع «خرید اشتراک → سرور VIP» می‌بیند.\n"
        "برای مدیریت پلن‌های داخل هر دسته، روی آن بزنید 👇",
        reply_markup=admin_vip_categories_keyboard(),
    )




@router.message(F.text == "🔴 خاموش کردن سفارشات")
async def menu_admin_orders_off(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    db.set_orders_enabled(False)
    users = db.get_all_users()
    sent, failed = 0, 0
    status_msg = await message.answer(f"⏳ در حال اطلاع‌رسانی به {len(users)} کاربر...")
    for u in users:
        try:
            await message.bot.send_message(
                int(u["telegram_id"]),
                db.get_text_override("orders_closed", "🔴 ربات به دلیل حجم سفارشات بالا موقتاً بسته می‌باشد.") + "\n\nروشن شدن دوباره‌ی آن اطلاع‌رسانی خواهد شد.",
            )
            sent += 1
        except Exception:
            failed += 1
    await status_msg.edit_text(f"🔴 بخش سفارشات خاموش شد. اطلاع‌رسانی به {sent} نفر موفق، {failed} نفر ناموفق.")
    await message.answer("👨‍💻 پنل مدیریت:", reply_markup=_admin_reply_kb_for(message.from_user.id, False))


@router.message(F.text == "🟢 روشن کردن سفارشات")
async def menu_admin_orders_on(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    db.set_orders_enabled(True)
    users = db.get_all_users()
    sent, failed = 0, 0
    status_msg = await message.answer(f"⏳ در حال اطلاع‌رسانی به {len(users)} کاربر...")
    for u in users:
        try:
            await message.bot.send_message(
                int(u["telegram_id"]),
                db.get_text_override("orders_opened", "🟢 ربات مجدداً فعال شد!") + "\n\nبا زدن /start می‌توانید دوباره سفارش ثبت کنید.",
            )
            sent += 1
        except Exception:
            failed += 1
    await status_msg.edit_text(f"🟢 بخش سفارشات روشن شد. اطلاع‌رسانی به {sent} نفر موفق، {failed} نفر ناموفق.")
    await message.answer("👨‍💻 پنل مدیریت:", reply_markup=_admin_reply_kb_for(message.from_user.id, True))


def _error_logs_text(logs: list, total: int) -> str:
    if not logs:
        return "🦖 لاگ خطاها\n\n✅ تا این لحظه هیچ خطای ثبت نشده."
    return f"🦖 لاگ خطاها — {total} خطای ثبت‌شده\n\nروی هرکدام بزنید تا جزئیاتش رو ببینید 👇"


async def _open_error_logs(target, edit: bool = False):
    logs = db.get_error_logs(limit=15)
    total = db.count_error_logs()
    text = _error_logs_text(logs, total)
    kb = admin_error_logs_keyboard(logs)
    if edit:
        await target.message.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


# 🐛 فیکس: برچسب دکمه در منو به «🦖 لاگ خطاها و Audit» تغییر کرده بود ولی
# این فیلتر همچنان فقط متن قدیمی را می‌گرفت، پس دکمه بی‌جواب می‌ماند.
@router.message(F.text.in_({"🦖 لاگ خطاها", "🦖 لاگ خطاها و Audit"}))
async def menu_admin_error_logs(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    await _open_error_logs(message, edit=False)


@router.callback_query(F.data == "errlogrefresh")
async def admin_error_logs_refresh(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await _open_error_logs(callback, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("errlogdetail_"))
async def admin_error_log_detail(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    log_id = int(callback.data.split("_", 1)[1])
    log = db.get_error_log(log_id)
    if log is None:
        await callback.answer("❌ یافت نشد.", show_alert=True)
        return
    # 🆕 فیکس: قبلاً پیام خطا جداگانه از یک طول ثابت (message تا ۲۰۰۰ کاراکتر + traceback تا ۳۵۰۰ کاراکتر) ساخته می‌شد؛ اگر خطایی با پیام (message) طولانی ثبت می‌شد (مثلاً همین خطای «MESSAGE_TOO_LONG»)، مجموع این دو می‌توانست از سقف تلگرام (۴۰۹۶) رد شود و خود صفحه‌ی «لاگ خطاها» هم با همان خطا مواجه می‌شد و ادمین اصلاً نمی‌توانست جزئیات را ببیند. حالا طول قابل‌نمایش traceback پویا بر اساس طول واقعی بقیه محاسبه می‌شود تا مجموع همیشه زیر سقف تلگرام بماند، و بازهم یک try/except محافظتی اضافه شده تا اگر بازهم محاسبه جایی کم بیافتاد، پیام با یک نسخه‌ی کاملاً مختصر‌شده بازهم فرستاده شود تا این بخش از پنل ادمین هرگز با ارور متوقف نشود.
    error_type_display = html.escape(str(log["error_type"]))
    occurred_at_display = html.escape(str(log.get("occurred_at") or ""))
    message_display = html.escape(str(log.get("message") or "")[:300])
    header = (
        f"⚠️ {error_type_display}\n"
        f"🕐 {occurred_at_display}\n\n"
        f"📝 {message_display}\n\n"
    )
    wrapper_len = len("<pre></pre>")
    max_tb_len = max(TELEGRAM_TEXT_LIMIT - len(header) - wrapper_len - 20, 200)
    tb = html.escape(str(log.get("traceback") or "")[:max_tb_len])
    text = f"{header}<pre>{tb}</pre>"
    try:
        await callback.message.edit_text(text, reply_markup=admin_error_log_detail_keyboard())
    except TelegramBadRequest as e:
        if is_message_too_long_error(e):
            fallback_text = truncate_for_telegram(
                f"⚠️ {error_type_display}\n🕐 {occurred_at_display}\n\n📝 {message_display}"
            )
            await callback.message.edit_text(fallback_text, reply_markup=admin_error_log_detail_keyboard())
        else:
            raise
    await callback.answer()


@router.callback_query(F.data == "errlogclear")
async def admin_error_logs_clear_ask(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "🗑 مطمئنید می‌خواهید همه‌ی لاگ ها پاک شوند؟", reply_markup=admin_error_logs_clear_confirm_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "errlogclearconfirm")
async def admin_error_logs_clear_do(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    db.clear_error_logs()
    await _open_error_logs(callback, edit=True)
    await callback.answer("✅ پاک شد.")


# ---------------------------------------------------------------------------
# 📊 آمار
# ---------------------------------------------------------------------------
_STAT_PERIOD_LABELS = {
    "all": "📊 آمار کل", "hour": "🕐 یک ساعت اخیر", "today": "☁️ امروز", "yesterday": "☀️ دیروز",
    "this_month": "☀️ ماه فعلی", "last_month": "☁️ ماه قبل",
}


def _render_stats_text(period: str, custom_dt=None) -> str:
    start, end = stat_period_bounds_utc(period, custom_dt)
    s = db.get_dashboard_stats(start, end)
    title = _STAT_PERIOD_LABELS.get(period)
    if not title:
        title = f"🗓 آمار روز {to_jalali_str(custom_dt, with_time=False)}"
    return (
        f"📊 آمار کلی ربات — {title}\n"
        "━━━━━━━━━━━━━━━\n\n"
        f"👤 تعداد کل کاربران: {s['total_users']:,} نفر\n"
        f"🛍 کاربران دارای خرید: {s['buyers_count']:,} نفر\n"
        f"🧪 اکانت‌های تست: {s['test_accounts']:,} نفر\n"
        f"💰 موجودی کل کاربران: {s['total_wallet']:,} تومان\n\n"
        f"🧾 تعداد کل فروش: {s['total_sales_count']:,} عدد\n"
        f"📇 تعداد فروش سرویس‌های فعال: {s['active_sales_count']:,} عدد\n"
        f"💵 جمع کل فروش: {s['total_sales_amount']:,} تومان\n"
        f"💴 جمع فروش سرویس‌های فعال: {s['active_sales_amount']:,} تومان\n"
        f"🔁 جمع کل تمدید: {s['renew_amount']:,} تومان\n\n"
        f"📈 نرخ تبدیل به مشتری: {s['conversion_rate']}٪\n"
        f"🧮 میانگین خرید هر مشتری: {s['avg_per_customer']:,} تومان\n"
        f"🌦 درآمد پیش‌بینی‌شده‌ی ماهانه: {s['predicted_monthly']:,} تومان\n"
        f"📊 درصد تمدید از فروش: {s['renew_pct']}٪\n\n"
        f"🧑‍💼 تعداد کل نمایندگان: {s['total_agents']:,} نفر\n"
        f"🔷 نمایندگان نوع N: {s['agents_type_n']:,} نفر\n"
        f"🔶 نمایندگان نوع N2: {s['agents_type_n2']:,} نفر\n"
        f"🧩 تعداد پنل‌ها: {s['total_panels']:,} عدد"
    )


@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery, state: FSMContext | None = None):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    if state is not None:
        await state.clear()
    await callback.message.edit_text(_render_stats_text("all"), reply_markup=admin_stats_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("stat_"))
async def admin_stats_period(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    period = callback.data.replace("stat_", "")
    if period == "custom":
        await state.set_state(AdminStates.waiting_stats_custom_date)
        await callback.message.answer(
            "🗓 تاریخ مورد نظر را به شمسی بفرست (مثلاً 1404/06/11):"
        )
        await callback.answer()
        return
    try:
        await callback.message.edit_text(_render_stats_text(period), reply_markup=admin_stats_keyboard())
    except TelegramBadRequest:
        pass
    await callback.answer("🔄 بروزرسانی شد.")


@router.message(AdminStates.waiting_stats_custom_date)
async def admin_stats_custom_date(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    dt = parse_jalali_date(message.text or "")
    if dt is None:
        await message.answer("❌ فرمت تاریخ نامعتبر است. مثال درست: 1404/06/11 — دوباره بفرست:")
        return
    await state.clear()
    await message.answer(_render_stats_text("custom", dt), reply_markup=admin_stats_keyboard())


# ---------------------------------------------------------------------------
# 👥 لیست کاربران
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_userlist")
async def admin_user_list(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    text = (
        f"👥 مدیریت کاربران\n\n"
        f"👥 کل کاربران: {db.count_users()}\n"
        f"🟢 کاربران فعال (خریدکرده): {db.count_customers()}\n"
        f"👤 کاربران بدون خرید: {db.count_non_customers()}\n\n"
        f"یکی از گزینه‌های زیر را انتخاب کنید 👇"
    )
    await callback.message.edit_text(text, reply_markup=admin_userlist_menu())
    await callback.answer()


async def _render_userlist_page(callback: types.CallbackQuery, list_kind: str, page: int):
    per_page = 10
    if list_kind == "active":
        users = db.get_customers_page(page, per_page)
        total = db.count_customers()
        title = "🟢 مشتریان فعال (خریدکرده)"
    else:
        # 🐛 فیکس بند ۱۹: قبلاً اینجا get_all_users_page بود (همه‌ی کاربران،
        # شامل مشتریان فعال) ولی عنوان و شمارشش «بدون خرید» بود — یعنی کاربر
        # فعال در هر دو لیست دیده می‌شد و شماره‌ی صفحات هم نمی‌خواند.
        users = db.get_non_customers_page(page, per_page)
        total = db.count_non_customers()
        title = "👤 کاربران بدون خرید"

    has_next = (page + 1) * per_page < total
    if not users and page == 0:
        text = f"{title}\n\nهنوز هیچ کاربری در این لیست نیست."
    else:
        start = page * per_page + 1
        text = f"{title} — {total} نفر (مرتب‌شده بر اساس بیشترین خرید)\nنمایش {start} تا {start + len(users) - 1}:\n\n"
        text += "برای مدیریت هرکدام روی نامش بزن 👇"

    await callback.message.edit_text(
        text, reply_markup=admin_userlist_page_keyboard(users, page, has_next, list_kind)
    )
    await callback.answer()


@router.callback_query(F.data == "admin_userlist_active")
async def admin_userlist_active(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await _render_userlist_page(callback, "active", 0)


@router.callback_query(F.data == "admin_userlist_all")
async def admin_userlist_all(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await _render_userlist_page(callback, "all", 0)


@router.callback_query(F.data.startswith("userpage_"))
async def admin_userlist_page_nav(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    _, list_kind, page_str = callback.data.split("_")
    await _render_userlist_page(callback, list_kind, int(page_str))


@router.callback_query(F.data.startswith("useropen_"))
async def admin_user_open(callback: types.CallbackQuery):
    """با زدن روی هرکدام از کاربران در لیست، مستقیم وارد صفحه‌ی مدیریت همان کاربر می‌شویم
    (همان صفحه‌ای که از طریق «🔍 جستجوی حرفه‌ای» هم بازش می‌شد)."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    uid = callback.data.replace("useropen_", "")
    user = db.get_user(uid)
    if user is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    stats = db.get_referral_stats(user["id"])
    text = (
        f"👤 {user['name']}\n"
        f"🆔 {user['telegram_id']}\n\n"
        f"👛 کیف پول آزاد: {user['wallet']:,} تومان\n"
        f"🔒 کیف پول مسدود: {user['locked_wallet']:,} تومان\n"
        f"🛒 کل خرید: {user['total_purchase']:,} تومان\n"
        f"📅 عضویت: {user['joined']}\n\n"
        f"🔗 کد دعوت: {user['invite_code']}\n"
        f"👥 دعوت: {stats['invited_count']} | موفق: {stats['successful_invites']}"
    )
    await _reply_with_user_actions(
        callback.message, text, user["telegram_id"], db.is_user_blocked(user["telegram_id"]), edit=True
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# 📒 حسابداری کاربر — تراکنش‌ها (کیف پول، خرید، شارژ، منشأ پول) با صفحه‌بندی
# ---------------------------------------------------------------------------
def _tx_type_label(tx_type: str) -> str:
    return {
        "charge": "💳 شارژ (تأیید کارت‌به‌کارت/دستی توسط ادمین)",
        "purchase": "🛒 خرید سرویس (کسر از کیف پول)",
        "referral_locked": "🔒 پاداش دعوت (در انتظار آزادسازی)",
        "referral_release": "🔓 آزادسازی پاداش دعوت",
        "referral_pending": "🔒 پاداش دعوت (در انتظار)",
    }.get(tx_type, f"📄 {tx_type}")


@router.callback_query(F.data.startswith("accounting_"))
async def admin_user_accounting(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    _, uid, page_str = callback.data.split("_")
    page = int(page_str)
    user = db.get_user(uid)
    if user is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    per_page = 10
    txs = db.get_transactions_page(user["id"], page, per_page)
    has_next = len(txs) == per_page

    text = (
        f"📒 حسابداری کاربر {user['name']} (🆔 {user['telegram_id']})\n\n"
        f"👛 کیف پول آزاد: {user['wallet']:,} تومان\n"
        f"🔒 کیف پول مسدود (پاداش دعوت در انتظار): {user['locked_wallet']:,} تومان\n"
        f"🛒 مجموع خرید: {user['total_purchase']:,} تومان\n\n"
        f"📋 تراکنش‌ها (صفحه {page + 1}):\n\n"
    )
    if not txs:
        text += "— تراکنشی در این صفحه نیست —"
    else:
        for tx in txs:
            sign = "+" if tx["amount"] >= 0 and tx["type"] in ("charge", "referral_release") else "-"
            text += (
                f"{_tx_type_label(tx['type'])}\n"
                f"{sign}{abs(tx['amount']):,} تومان | {tx['status']}\n"
                f"📝 {tx.get('description') or '-'}\n"
                f"🕐 {tx['created_at']}\n\n"
            )

    await callback.message.edit_text(text, reply_markup=admin_accounting_keyboard(uid, page, has_next))
    await callback.answer()


# ---------------------------------------------------------------------------
# 🔍 جستجوی حرفه‌ای
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_search")
async def admin_search_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "🔍 آیدی عددی یا کد دعوت کاربر را ارسال کنید:", reply_markup=admin_back_button()
    )
    await state.set_state(AdminStates.waiting_search_user)
    await callback.answer()


@router.message(AdminStates.waiting_search_user)
async def admin_search_result(message: types.Message, state: FSMContext):
    query = clean_numeric_id(message.text)

    user = db.get_user(query) if query.isdigit() else db.get_user_by_invite_code(query)
    if user is None:
        await message.answer("❌ کاربری با این مشخصات یافت نشد.", reply_markup=admin_back_button())
        return

    stats = db.get_referral_stats(user["id"])
    text = (
        f"👤 {user['name']}\n"
        f"🆔 {user['telegram_id']}\n\n"
        f"👛 کیف پول آزاد: {user['wallet']:,} تومان\n"
        f"🔒 کیف پول مسدود: {user['locked_wallet']:,} تومان\n"
        f"🛒 کل خرید: {user['total_purchase']:,} تومان\n"
        f"📅 عضویت: {user['joined']}\n\n"
        f"🔗 کد دعوت: {user['invite_code']}\n"
        f"👥 دعوت: {stats['invited_count']} | موفق: {stats['successful_invites']}"
    )
    await _reply_with_user_actions(
        message, text, user["telegram_id"], db.is_user_blocked(user["telegram_id"]), edit=False
    )
    await state.clear()


# ---------------------------------------------------------------------------
# 💳 شارژ کیف پول (تأیید/رد رسید + شارژ دستی)
# دسترسی از طریق «🔍 جستجوی حرفه‌ای» ← دکمه «💰 شارژ دستی» (برای بهینه شدن فضای منو)
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("approve_"))
async def approve_charge(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    if is_duplicate_action(f"approvecharge_{callback.data}") or not db.claim_admin_action(f"approvecharge_{callback.data}"):
        await callback.answer("⚠️ این رسید قبلاً پردازش شده.", show_alert=True)
        return

    _, uid, amount_str, receipt_id_str = callback.data.split("_")
    amount = int(amount_str)
    receipt_id = int(receipt_id_str)

    user = db.get_user(uid)
    if user is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    db.add_to_wallet(user["id"], amount, "شارژ کیف پول (تأیید رسید)")
    try:
        db.resolve_pending_receipt_by_id(receipt_id)
    except Exception:
        pass
    await _finish_receipt_message(
        callback.message, "\n\n✅ تأیید و شارژ شد.", queue_refresh=lambda: _render_pending_receipts(callback)
    )
    try:
        await send_notification_sticker(callback.bot, int(uid), "notif_wallet_charge")
        await callback.bot.send_message(int(uid), user_text("notif_wallet_charge_approved", amount=amount))
    except Exception:
        pass
    await _notify_main_admin_action(callback.bot, callback.from_user, "تأیید رسید شارژ کیف پول", uid, f"مبلغ {amount:,} تومان")
    await callback.answer("✅ شارژ شد.")


@router.callback_query(F.data.startswith("reject_"))
async def reject_charge(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    if is_duplicate_action(f"rejectcharge_{callback.data}") or not db.claim_admin_action(f"rejectcharge_{callback.data}"):
        await callback.answer("⚠️ این رسید قبلاً پردازش شده.", show_alert=True)
        return

    _, uid, receipt_id_str = callback.data.split("_")
    receipt_id = int(receipt_id_str)
    try:
        db.resolve_pending_receipt_by_id(receipt_id)
    except Exception:
        pass
    await _finish_receipt_message(
        callback.message, "\n\n❌ رد شد.", queue_refresh=lambda: _render_pending_receipts(callback)
    )
    try:
        await send_notification_sticker(callback.bot, int(uid), "notif_receipt_rejected")
        await callback.bot.send_message(int(uid), user_text("notif_receipt_rejected_short"))
    except Exception:
        pass
    await _notify_main_admin_action(callback.bot, callback.from_user, "رد رسید", uid if 'uid' in locals() else str(order_id) if 'order_id' in locals() else "", "")
    await callback.answer("❌ رد شد.")


@router.callback_query(F.data.startswith("custom_"))
async def custom_charge_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    uid = callback.data.replace("custom_", "")
    await state.update_data(charge_target=uid)
    await state.set_state(AdminStates.waiting_custom_amount)
    await callback.message.answer(f"💵 مبلغ شارژ برای کاربر {uid} را به تومان ارسال کنید:")
    await callback.answer()


@router.message(AdminStates.waiting_custom_amount)
async def custom_charge_apply(message: types.Message, state: FSMContext):
    if not message.text or not clean_numeric_id(message.text).isdigit():
        await message.answer("❌ فقط عدد ارسال کنید.")
        return

    data = await state.get_data()
    uid = data.get("charge_target")
    amount = int(clean_numeric_id(message.text))

    user = db.get_user(uid)
    if user is None:
        await message.answer("❌ کاربر یافت نشد.")
        await state.clear()
        return

    db.add_to_wallet(user["id"], amount, "شارژ دستی توسط ادمین")
    try:
        db.resolve_pending_receipt("charge", uid)
    except Exception:
        pass
    await message.answer(f"✅ {amount:,} تومان به کیف پول کاربر {uid} اضافه شد.")
    try:
        await send_notification_sticker(message.bot, int(uid), "notif_wallet_charge")
        await message.bot.send_message(int(uid), user_text("notif_wallet_charged", amount=amount))
    except Exception:
        pass
    await state.clear()


# ---------------------------------------------------------------------------
# 💳 تأیید/رد رسید خرید کارت‌به‌کارت (سرویس)
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("approvepay|"))
async def approve_purchase(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    _, uid, plan_key, price_str, receipt_id_str = callback.data.split("|")
    price = int(price_str)
    receipt_id = int(receipt_id_str)
    plan = db.get_effective_plan(plan_key)
    user = db.get_user(uid)
    if user is None or plan is None:
        await callback.answer("❌ کاربر یا پلن یافت نشد.", show_alert=True)
        return

    if plan_key == FREE_TEST_PLAN_KEY and db.has_used_free_test(user["id"]):
        await callback.answer(
            "⚠️ این کاربر قبلاً از «تست رایگان» استفاده کرده؛ هر کاربر فقط یک‌بار می‌تواند این پلن را بگیرد.",
            show_alert=True,
        )
        return

    # رسید را فقط بعد از اعتبارسنجی کامل claim می‌کنیم؛ در نسخه‌ی قبلی اگر
    # یکی از بررسی‌های بعدی خطا می‌داد، receipt برای همیشه «پردازش‌شده» علامت
    # می‌خورد و دفعه‌ی بعد دکمه‌ی تأیید بی‌دلیل می‌گفت قبلاً پردازش شده.
    if is_duplicate_action(f"approvepay_{callback.data}") or not db.claim_admin_action(f"approvepay_{callback.data}"):
        await callback.answer("⚠️ این رسید قبلاً پردازش شده.", show_alert=True)
        return

    # 🐛 فیکس: کد تخفیف کارت‌به‌کارت حالا فقط همین‌جا (تأیید ادمین) مصرف
    # می‌شود، نه هنگام ارسال رسید؛ اگر ادمین رد کند سهم کد تخفیف مصرف نمی‌شود.
    pending = None
    try:
        pending = db.get_pending_receipt_by_id(receipt_id)
    except Exception:
        pending = None

    db.record_purchase(user["id"], price, f"خرید {plan['name']} (کارت به کارت)")

    if pending and pending.get("discount_code"):
        try:
            db.use_discount(pending["discount_code"], user["id"])
        except Exception:
            logging.getLogger(__name__).exception("خطا در مصرف کد تخفیف کارت‌به‌کارت")

    order_id = db.create_order(user["id"], plan_key, plan["name"], plan_type(plan_key), price)

    # تأیید کارت‌به‌کارت = پرداخت واقعی و موفق؛ بنابراین فقط بعد از ساخت
    # سفارشِ همین خرید، پاداش دعوت را آزاد می‌کنیم.
    if price > 0 and plan_key != FREE_TEST_PLAN_KEY:
        try:
            db.complete_referral(user["id"], qualifying_order_id=order_id)
        except ValueError:
            pass
    try:
        db.resolve_pending_receipt_by_id(receipt_id)
    except Exception:
        pass

    await _finish_receipt_message(
        callback.message, "\n\n✅ تأیید شد و خرید ثبت شد.", queue_refresh=lambda: _render_pending_receipts(callback)
    )
    try:
        _discount_note = f"\n🎟 کد تخفیف {pending['discount_code']} برای این خرید مصرف شد." if pending and pending.get("discount_code") else ""
        _confirm_text = user_text("notif_purchase_approved", plan_name=plan["name"], discount_note=_discount_note)
        # fix: به کاربر بگوییم کد تخفیف همین تأیید مصرف شده تا گیج نشود چرا دیگر قابل‌استفاده نیست.
        if pending and pending.get("discount_code"):
            _confirm_text += f"\n🎟 کد تخفیف {pending['discount_code']} برای این خرید مصرف شد."
        await send_notification_sticker(callback.bot, int(uid), "notif_purchase_approved")
        await callback.bot.send_message(int(uid), _confirm_text)
    except Exception:
        pass
    await callback.message.answer(
        "📤 برای ارسال کانفیگ این خرید:", reply_markup=admin_purchase_notify_keyboard(uid, plan_key, order_id)
    )
    await _notify_main_admin_action(callback.bot, callback.from_user, "تأیید خرید/سفارش", uid if 'uid' in locals() else str(order_id) if 'order_id' in locals() else "", "ثبت شد")
    await callback.answer("✅ تأیید شد.")


@router.callback_query(F.data.startswith("rejectpay|"))
async def reject_purchase(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    _, uid, receipt_id_str = callback.data.split("|")
    receipt_id = int(receipt_id_str)
    if is_duplicate_action(f"rejectpay_{callback.data}") or not db.claim_admin_action(f"rejectpay_{callback.data}"):
        await callback.answer("⚠️ این رسید قبلاً پردازش شده.", show_alert=True)
        return
    try:
        db.resolve_pending_receipt_by_id(receipt_id)
    except Exception:
        pass
    await _finish_receipt_message(
        callback.message, "\n\n❌ رد شد.", queue_refresh=lambda: _render_pending_receipts(callback)
    )
    try:
        await send_notification_sticker(callback.bot, int(uid), "notif_receipt_rejected")
        await callback.bot.send_message(int(uid), user_text("notif_receipt_rejected"))
    except Exception:
        pass
    await _notify_main_admin_action(callback.bot, callback.from_user, "رد رسید", uid if 'uid' in locals() else str(order_id) if 'order_id' in locals() else "", "")
    await callback.answer("❌ رد شد.")


# ---------------------------------------------------------------------------
# 🛠 تأیید/رد رسید کارت‌به‌کارت برای «بساز سرویس خودت» / «تمدید سرویس»
#
# 🐛 فیکس بحرانی: این سه هندلر (approvecustom_ / rejectcustom_ /
# dismisscustomorder_) در نسخه‌ی V2.19 از فایل حذف شده بودند — فقط همین هدر
# کامنت با چند خط خالی زیرش باقی مانده بود. نتیجه این بود که دکمه‌های
# «✅ تأیید پرداخت» و «❌ رد رسید» روی رسیدهای «بساز سرویس خودت» و همچنین
# دکمه‌ی حذف از صف، کاملاً بی‌اثر بودند (aiogram هیچ هندلری پیدا نمی‌کرد و
# کلیک بی‌جواب می‌ماند) — دقیقاً همان «تایید و ارسال کانفیگ به مشکل خورده».
# اینجا دقیقاً با همان الگوی approvepay|/rejectpay| بالا بازسازی شده‌اند.
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("approvecustom_"))
async def approve_custom_order(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    order_id = int(callback.data.replace("approvecustom_", ""))
    order = db.get_custom_order(order_id)
    if order is None:
        await callback.answer("❌ این سفارش یافت نشد.", show_alert=True)
        return

    user = db.get_user_by_id(order["user_id"])
    if user is None:
        await callback.answer("❌ کاربر این سفارش یافت نشد.", show_alert=True)
        return

    if is_duplicate_action(f"approvecustom_{callback.data}") or not db.claim_admin_action(f"approvecustom_{callback.data}"):
        await callback.answer("⚠️ این رسید قبلاً پردازش شده.", show_alert=True)
        return

    db.set_custom_order_status(order_id, "paid")
    if order["price"] > 0:
        db.record_purchase(user["id"], order["price"], "خرید سرویس سفارشی (کارت به کارت)")
        try:
            db.complete_referral(user["id"])
        except ValueError:
            pass
    try:
        db.resolve_pending_receipt("custom", user["telegram_id"])
    except Exception:
        pass

    await _finish_receipt_message(
        callback.message, "\n\n✅ تأیید شد و سفارش ثبت شد.", queue_refresh=lambda: _render_pending_receipts(callback)
    )
    try:
        await send_notification_sticker(callback.bot, int(user["telegram_id"]), "notif_purchase_approved")
        await callback.bot.send_message(
            int(user["telegram_id"]),
            user_text("notif_purchase_approved", plan_name="سرویس سفارشی", discount_note=""),
        )
    except Exception:
        pass
    await callback.message.answer(
        "📤 برای ارسال کانفیگ این سفارش:", reply_markup=admin_custom_order_notify_keyboard(order_id)
    )
    await _notify_main_admin_action(callback.bot, callback.from_user, "تأیید سفارش سفارشی", str(order_id), "ثبت شد")
    await callback.answer("✅ تأیید شد.")


@router.callback_query(F.data.startswith("rejectcustom_"))
async def reject_custom_order(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    order_id = int(callback.data.replace("rejectcustom_", ""))
    order = db.get_custom_order(order_id)
    if order is None:
        await callback.answer("❌ این سفارش یافت نشد.", show_alert=True)
        return

    if is_duplicate_action(f"rejectcustom_{callback.data}") or not db.claim_admin_action(f"rejectcustom_{callback.data}"):
        await callback.answer("⚠️ این رسید قبلاً پردازش شده.", show_alert=True)
        return

    db.set_custom_order_status(order_id, "rejected")
    user = db.get_user_by_id(order["user_id"])
    if user:
        try:
            db.resolve_pending_receipt("custom", user["telegram_id"])
        except Exception:
            pass

    await _finish_receipt_message(
        callback.message, "\n\n❌ رد شد.", queue_refresh=lambda: _render_pending_receipts(callback)
    )
    if user:
        try:
            await send_notification_sticker(callback.bot, int(user["telegram_id"]), "notif_receipt_rejected")
            await callback.bot.send_message(int(user["telegram_id"]), user_text("notif_receipt_rejected"))
        except Exception:
            pass
    await _notify_main_admin_action(callback.bot, callback.from_user, "رد رسید سفارش سفارشی", str(order_id), "")
    await callback.answer("❌ رد شد.")


@router.callback_query(F.data.startswith("dismisscustomorder_"))
async def dismiss_custom_order(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    order_id = int(callback.data.replace("dismisscustomorder_", ""))
    db.set_custom_order_status(order_id, "dismissed")
    await _render_order_queue(callback)
    await callback.answer("🗑 از صف پاک شد.")


def _format_test_package_for_log(order: dict | None, package_text: str) -> str:
    """فرمت بسته‌ی تست رایگان برای لاگ کانال.

    تست‌های زیر ۱ گیگ با مگابایت و تست‌های زیر ۱ روز با ساعت نمایش داده
    می‌شوند تا مقادیری مثل 0.0488281 گیگ و 0.0416667 روز در لاگ دیده نشوند.
    """
    if not order or order.get("order_type") != "test":
        return package_text

    try:
        plan_key = order.get("plan_key")
        plan = db.get_effective_plan(plan_key) if plan_key else None
    except Exception:
        plan = None

    if not plan:
        return package_text

    # حجم: کمتر از ۱ گیگ → مگابایت؛ در غیر این صورت → گیگ
    try:
        volume_gb = float(plan.get("volume_gb", 0) or 0)
    except (TypeError, ValueError):
        volume_gb = 0

    if volume_gb <= 0:
        volume_label = "نامحدود"
    elif volume_gb < 1:
        volume_label = f"{round(volume_gb * 1024):g} مگابایت"
    else:
        volume_label = f"{volume_gb:g} گیگ"

    # مدت: کمتر از ۱ روز → ساعت؛ ۱ روز و بیشتر → روز
    try:
        days = float(plan.get("days", 0) or 0)
    except (TypeError, ValueError):
        days = 0

    if days <= 0:
        duration_label = "نامحدود"
    elif days < 1:
        hours = days * 24
        if abs(hours - round(hours)) < 1e-9:
            duration_label = f"{round(hours):g} ساعت"
        else:
            duration_label = f"{hours:g} ساعت"
    else:
        duration_label = f"{int(days) if days.is_integer() else days:g} روز"

    return f"{volume_label} | {duration_label}"


async def _log_fulfilled_order(
    bot, user: dict, *, plan_order_id=None, custom_order_id=None,
    target_config_id=None, service_id=None, service_name: str = "-",
    package_text: str = "-", expiry_text: str = "-",
):
    """پیام لاگ استاندارد سفارش را برای «کانال اعتماد» می‌سازد و ارسال می‌کند."""
    label = "🛒 خرید جدید"
    amount_text = "-"
    order = None

    if plan_order_id:
        order = db.get_order(plan_order_id)
        if order:
            amount_text = f"{order['price']:,} تومان" if order["price"] else "رایگان"
            # تشخیص نوع لاگ فقط از خودِ نوع سفارش انجام می‌شود. وجود
            # target_config_id به‌تنهایی به معنی تمدید نیست؛ هنگام تحویل یک
            # خرید عادی هم target_config_id داریم و قبلاً همین باعث می‌شد
            # «🛒 خرید جدید» اشتباهاً «🔁 تمدید سرویس» ثبت شود.
            order_type = str(order.get("order_type") or "").strip().lower()
            if order_type in ("test", "free_test"):
                label = "🎁 تست رایگان"
                package_text = _format_test_package_for_log(order, package_text)
            elif order_type == "renew":
                label = "🔁 تمدید سرویس"
            else:
                label = "🛒 خرید جدید"
    elif custom_order_id:
        order = db.get_custom_order(custom_order_id)
        if order:
            amount_text = f"{order['price']:,} تومان" if order["price"] else "رایگان"
            label = "🔁 تمدید سرویس" if order.get("order_type") == "renew" else "🛠 سرویس سفارشی جدید"
    elif target_config_id:
        label = "🔁 تمدید سرویس"

    username = await alerts.fetch_username(bot, user["telegram_id"])
    await alerts.log_order_to_channel(
        bot,
        order_label=label,
        user=user,
        username=username,
        service_id=service_id,
        service_name=service_name,
        package_text=package_text,
        amount_text=amount_text,
        expiry_text=expiry_text,
    )


# ---------------------------------------------------------------------------
# 🛒 خرید اشتراک برای خود ادمین
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_buy_subscription")
async def admin_buy_subscription(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    if not db.is_orders_enabled():
        await callback.answer("🔴 سفارشات در حال حاضر خاموش است.", show_alert=True)
        return

    await state.clear()
    if db.get_user(callback.from_user.id) is None:
        db.create_user(callback.from_user.id, callback.from_user.full_name or "Admin")

    await callback.message.answer(
        "🛒 خرید اشتراک برای خودم\n\n🚀 دسته‌بندی سرویس مورد نظر را انتخاب کنید 👇",
        reply_markup=vip_categories_keyboard(),
    )
    await callback.answer()


@router.message(F.text == "🛒 خرید اشتراک برای خودم")
async def admin_buy_subscription_reply(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    if not db.is_orders_enabled():
        await message.answer("🔴 سفارشات در حال حاضر خاموش است.")
        return
    await state.clear()
    if db.get_user(message.from_user.id) is None:
        db.create_user(message.from_user.id, message.from_user.full_name or "Admin")
    await message.answer(
        "🛒 خرید اشتراک برای خودم\n\n🚀 دسته‌بندی سرویس مورد نظر را انتخاب کنید 👇",
        reply_markup=vip_categories_keyboard(),
    )


# ---------------------------------------------------------------------------
# 📤 ارسال کانفیگ — عکس کیوآرکد + لینک ساب (نام/حجم/مدت به‌صورت خودکار
# از روی خود لینک تشخیص داده می‌شود؛ اگر تشخیص خودکار جواب نداد، به‌صورت
# دستی از ادمین پرسیده می‌شود)
# ---------------------------------------------------------------------------
async def _start_send_flow(callback: types.CallbackQuery, state: FSMContext, uid: str,
                            target_config_id: int | None, order_id: int | None,
                            plan_order_id: int | None = None, hint: str = ""):
    await state.update_data(
        send_target_uid=uid,
        send_target_config_id=target_config_id,
        send_order_id=order_id,
        send_plan_order_id=plan_order_id,
        qr_file_id=None,
    )
    await state.set_state(AdminStates.waiting_send_qr_photo)
    await callback.message.answer(
        f"📤 ارسال کانفیگ برای کاربر {uid}{hint}\n\n📸 اول عکس کیوآرکد سرویس رو ارسال کن:"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("sendvip_"))
async def send_config_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    raw = callback.data.replace("sendvip_", "")
    uid, _, plan_order_id = raw.partition("|")
    await _start_send_flow(
        callback, state, uid, target_config_id=None, order_id=None,
        plan_order_id=int(plan_order_id) if plan_order_id else None,
    )


# 🐛 فیکس بحرانی: هندلر «📤 شروع ارسال کانفیگ» برای سفارش‌های «بساز سرویس
# خودت» هم حذف شده بود (دقیقاً کنار همان sendvip_ بالا). یعنی بعد از تأیید
# رسید، دکمه‌ی ارسال کانفیگ هیچ کاری نمی‌کرد. اینجا با همان _start_send_flow
# بازسازی شده، فقط با order_id سفارش سفارشی تا حجم/مدت از خود سفارش خوانده شود.
@router.callback_query(F.data.startswith("sendcustomorder_"))
async def send_custom_order_config_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    order_id = int(callback.data.replace("sendcustomorder_", ""))
    order = db.get_custom_order(order_id)
    if order is None:
        await callback.answer("❌ این سفارش یافت نشد.", show_alert=True)
        return
    user = db.get_user_by_id(order["user_id"])
    if user is None:
        await callback.answer("❌ کاربر این سفارش یافت نشد.", show_alert=True)
        return
    await _start_send_flow(
        callback, state, str(user["telegram_id"]),
        target_config_id=order.get("target_config_id"), order_id=order_id, plan_order_id=None,
    )


@router.message(AdminStates.waiting_send_qr_photo, F.photo)
async def send_config_qr_received(message: types.Message, state: FSMContext):
    file_id = message.photo[-1].file_id
    await state.update_data(qr_file_id=file_id)
    await state.set_state(AdminStates.waiting_send_qr_link)
    await message.answer("🔗 حالا لینک ساب (Subscription) این سرویس رو ارسال کن:")


@router.message(AdminStates.waiting_send_qr_photo)
async def send_config_qr_wrong_format(message: types.Message):
    await message.answer("📸 لطفاً عکس کیوآرکد سرویس رو ارسال کن (نه متن).")


@router.message(AdminStates.waiting_send_qr_link)
async def send_config_link_received(message: types.Message, state: FSMContext):
    sub_link = (message.text or "").strip()
    if not sub_link.lower().startswith(("http://", "https://")):
        await message.answer("❌ این یک لینک معتبر نیست؛ لطفاً لینک ساب رو با http یا https ارسال کن:")
        return

    data = await state.get_data()
    order_id = data.get("send_order_id")
    order = db.get_custom_order(order_id) if order_id else None

    await message.answer("⏳ در حال تشخیص خودکار اطلاعات از روی لینک...")
    meta = await extract_meta(sub_link)
    userinfo = (meta or {}).get("userinfo") or {}
    fetched_name = (meta or {}).get("name")

    if order:
        # اطلاعات حجم/مدت از خود سفارش (که کاربر برایش پول پرداخت کرده) قابل‌اعتمادتر است
        volume_gb = order["volume_gb"]
        days = order["days"]
        name = fetched_name or order.get("custom_name") or "کاربر"
        await state.update_data(send_volume_gb=volume_gb, send_days=days, send_name=name, send_sub_link=sub_link)
        await _finalize_send(message, state)
        return

    volume_gb = _gb_from_bytes(userinfo.get("total"))
    days = days_remaining(userinfo.get("expire"))

    if volume_gb is not None and days is not None and fetched_name:
        await state.update_data(send_volume_gb=volume_gb, send_days=days, send_name=fetched_name, send_sub_link=sub_link)
        await _finalize_send(message, state)
        return

    # تشخیص خودکار کامل نبود؛ از ادمین می‌خواهیم دستی وارد کند
    await state.update_data(
        send_sub_link=sub_link,
        send_volume_gb=volume_gb,
        send_days=days,
        send_name=fetched_name,
    )
    await state.set_state(AdminStates.waiting_send_qr_manual)
    known = []
    if fetched_name:
        known.append(f"نام: {fetched_name}")
    if volume_gb is not None:
        known.append(f"حجم: {volume_gb} گیگ")
    if days is not None:
        known.append(f"مدت: {days} روز")
    known_text = ("\n✅ همین مقدار از روی لینک تشخیص داده شد: " + " | ".join(known)) if known else ""
    await message.answer(
        "⚠️ تشخیص خودکار کامل از روی این لینک ممکن نشد (احتمالاً این پنل هدر استاندارد ساب رو برنمی‌گردونه)."
        + known_text
        + "\n\nلطفاً این ۳ مورد رو هرکدام در یک خط، به همین ترتیب بفرست:\n"
        "نام کاربری سرویس\nحجم به گیگ (فقط عدد)\nمدت به روز (فقط عدد)\n\nمثال:\naminvpn1\n50\n30"
    )


@router.message(AdminStates.waiting_send_qr_manual)
async def send_config_manual_input(message: types.Message, state: FSMContext):
    lines = [l.strip() for l in (message.text or "").splitlines() if l.strip()]
    if len(lines) < 3:
        await message.answer("❌ باید دقیقاً ۳ خط بفرستی: نام / حجم (گیگ) / مدت (روز). دوباره امتحان کن:")
        return

    name = lines[0]
    volume_gb = parse_int_in_range(lines[1], 0, 100000)
    days = parse_int_in_range(lines[2], 0, 100000)
    if volume_gb is None or days is None:
        await message.answer("❌ خط دوم و سوم باید عدد ۰ یا بیشتر باشند (۰ = نامحدود). دوباره امتحان کن:")
        return

    await state.update_data(send_name=name, send_volume_gb=volume_gb, send_days=days)
    await _finalize_send(message, state)


async def _finalize_send(message: types.Message, state: FSMContext):
    data = await state.get_data()
    uid = data.get("send_target_uid")
    qr_file_id = data.get("qr_file_id")
    sub_link = data.get("send_sub_link")
    target_config_id = data.get("send_target_config_id")
    order_id = data.get("send_order_id")
    plan_order_id = data.get("send_plan_order_id")
    name = data.get("send_name") or "کاربر"

    # 🐛 فیکس: قبلاً متن تحویلی همیشه ثابت "نامحدود" نشان می‌داد و سقف کاربر (HWID Limit) واقعی پلن را نادیده می‌گرفت. حالا اگر این ارسال از روی یک سفارش پلن VIP باشد سقف کاربر همان پلن خوانده و نمایش داده می‌شود (0 = نامحدود).
    user_limit = None
    if plan_order_id:
        plan_order = db.get_order(plan_order_id)
        if plan_order and plan_order.get("plan_key"):
            order_plan = db.get_effective_plan(plan_order["plan_key"])
            if order_plan:
                user_limit = order_plan.get("user_limit")
    volume_gb = data.get("send_volume_gb")
    days = data.get("send_days")

    user = db.get_user(uid)
    if user is None or qr_file_id is None or sub_link is None:
        await message.answer("❌ مشکلی پیش آمد؛ لطفاً از ابتدا (📸 عکس کیوآرکد) دوباره امتحان کن.")
        await state.clear()
        return

    volume_text = _format_volume_gb_label(volume_gb)
    days_text = f"{days} روز" if days is not None else "نامحدود"
    plan_name_for_delivery = None
    if plan_order_id:
        plan_order = db.get_order(plan_order_id)
        if plan_order and plan_order.get("plan_key"):
            plan_obj = db.get_effective_plan(plan_order["plan_key"])
            if plan_obj:
                plan_name_for_delivery = plan_obj.get("name")
    delivery_label = (
        _delivery_service_label(
            plan_name_for_delivery,
            volume_gb,
            days,
            user_limit,
            plan_order.get("plan_key") if plan_order_id and plan_order else None,
        )
        if plan_name_for_delivery or plan_order_id
        else _delivery_service_label(name, volume_gb, days, None)
    )
    is_test_delivery = bool(plan_order_id and plan_order and plan_order.get("plan_key") == FREE_TEST_PLAN_KEY)
    delivery_text_key = "service_delivery_test_text" if is_test_delivery else "service_delivery_text"
    caption = user_text(delivery_text_key, service_label=_english_digits(delivery_label), link=sub_link)

    expiry_date = None
    if days is not None:
        expiry_date = (now_tehran_naive() + timedelta(days=days)).strftime("%Y-%m-%d")

    encrypted = crypto.encrypt_config(sub_link)
    plan_name = f"{name} | {volume_text} | {days_text}"

    if target_config_id:
        db.update_config(target_config_id, plan_name, encrypted, expiry=expiry_date, qr_file_id=qr_file_id)
    else:
        db.add_config(user["id"], plan_name, encrypted, expiry=expiry_date, config_type="vip", qr_file_id=qr_file_id)

    if order_id:
        db.set_custom_order_status(order_id, "fulfilled")
    if plan_order_id:
        db.set_order_status(plan_order_id, "fulfilled")

    try:
        await send_notification_sticker(message.bot, int(uid), "notif_service_delivery")
        await message.bot.send_photo(
            int(uid),
            qr_file_id,
            caption=caption,
            reply_markup=config_delivery_keyboard(is_test=is_test_delivery),
        )
        # منوی پایینی دائمی کاربر نباید بعد از تحویل سرویس مخفی شود.
        db.set_keyboard_hidden(int(uid), False)
        await message.bot.send_message(int(uid), "⬇️ منوی اصلی شما همچنان در دسترس است.", reply_markup=main_reply_keyboard(int(uid)))
        await message.answer("✅ کانفیگ برای کاربر ارسال شد.")
        await _notify_main_admin_action(message.bot, message.from_user, "ارسال کانفیگ", uid, plan_name)
    except Exception as e:
        await message.answer(f"⚠️ سرویس ذخیره شد ولی ارسال پیام به کاربر ناموفق بود: {e}")

    await _log_fulfilled_order(
        message.bot, user, plan_order_id=plan_order_id, custom_order_id=order_id,
        target_config_id=target_config_id, service_name=name,
        package_text=f"{volume_text} | {days_text}", expiry_text=expiry_date or "نامحدود",
    )

    await state.clear()


# ---------------------------------------------------------------------------
# 🎮 ارسال کانفیگ گیمینگ (WireGuard) — بدون کیوآرکد/mirroring/تمدید:
# ادمین ابتدا شناسه سرویس و لینک ساب را وارد می‌کند، سپس هر تعداد فایل
# .conf که بخواهد آپلود می‌کند (هرکدام می‌تواند کپشن/لوکیشن جدا داشته باشد)
# و در پایان با زدن دکمه «✅ پایان ارسال فایل‌ها» همه‌ی فایل‌ها یک‌جا برای
# کاربر ارسال می‌شوند - دقیقاً مطابق فرمت فایلی که در کانال نمونه دیده می‌شود.
# ---------------------------------------------------------------------------












# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 📝 مدیریت جامع متن‌های کاربر و اعلان‌ها
# همه‌ی متن‌های قابل ویرایش در text_catalog.py تعریف شده‌اند.
TEXT_KEYS = TEXTS


def _chunk2(items):
    rows = []
    for i in range(0, len(items), 2):
        rows.append(items[i:i + 2])
    return rows


def _text_manager_keyboard(category: str | None = None):
    """مدیریت متن‌ها؛ در هر ردیف تا ۲ دکمه (🐛 فیکس: قبلاً ۳ دکمه در هر ردیف بود که روی صفحه‌کلید شلوغ و فشرده بود)."""
    buttons = []
    if category is None:
        categories = list(TEXT_CATEGORIES.keys())
        for i in range(0, len(categories), 2):
            row = []
            for cat in categories[i:i + 2]:
                count = len(TEXT_CATEGORIES[cat])
                row.append(InlineKeyboardButton(
                    text=f"📝 {cat.split(' ', 1)[-1]} ({count})",
                    callback_data=f"textcat_{categories.index(cat)}",
                    style="primary",
                ))
            buttons.append(row)
        buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")])
        return types.InlineKeyboardMarkup(inline_keyboard=buttons)

    items = TEXT_CATEGORIES.get(category, [])
    delivery_labels = {
        "service_delivery_text": "✏️ تغییر متن تحویل سرویس (بسته‌ها)",
        "service_delivery_apps_button": "📱 دکمه لینک برنامه‌ها (بسته‌ها)",
        "service_delivery_connection_button": "🔧 دکمه اتصال کانفینگ (بسته‌ها)",
        "service_delivery_test_text": "✏️ تغییر متن تحویل سرویس (تست)",
        "service_delivery_test_apps_button": "📱 دکمه لینک برنامه‌ها (تست)",
        "service_delivery_test_connection_button": "🔧 دکمه اتصال کانفینگ (تست)",
    }
    for i in range(0, len(items), 2):
        row = []
        for key, default in items[i:i + 2]:
            value = user_text(key, default).replace("\n", " ")[:22]
            label = delivery_labels.get(key, f"✏️ {key[:14]} | {value}")
            if key in delivery_labels:
                label = f"✏️ {label}"
            row.append(InlineKeyboardButton(
                text=label,
                callback_data=f"textedit_{key}",
                style="primary",
            ))
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت به دسته‌ها", callback_data="admin_texts", style="primary")])
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_texts")
async def admin_texts(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "✏️ ویرایش متن و دکمه‌ها\n\nابتدا نوع موردی که می‌خواهید ویرایش کنید را انتخاب کنید:",
        reply_markup=_ui_editor.editor_mode_keyboard(),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# ✏️ ویرایشگر جامع متن و دکمه‌ها
# ui_editor.py از قبل منوها و ذخیره‌سازی را دارد؛ این handlerها اتصال آن‌ها
# به Router ادمین را کامل می‌کنند. این بخش مستقل است و به منطق خرید/تحویل دست
# نمی‌زند.
# ---------------------------------------------------------------------------

def _ui_editor_entities(message: types.Message) -> list[dict]:
    return serialize_message_entities(getattr(message, "entities", None))


@router.callback_query(F.data == "admin_text_editor")
async def ui_editor_open(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "✏️ ویرایش متن و دکمه‌ها\n\nنوع ویرایش را انتخاب کنید:",
        reply_markup=_ui_editor.editor_mode_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ui_mode:"))
async def ui_editor_mode(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    mode = callback.data.split(":", 1)[1]
    if mode == "text":
        markup = _ui_editor.categories_keyboard("text")
        title = "📝 انتخاب بخش متن"
    elif mode == "buttons":
        markup = _ui_editor.categories_keyboard("buttons")
        title = "🔘 انتخاب بخش دکمه‌ها"
    else:
        await callback.answer("❌ حالت ویرایش نامعتبر است.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(title, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("ui_cat:"))
async def ui_editor_category(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("❌ درخواست نامعتبر است.", show_alert=True)
        return
    _, mode, category = parts
    if category not in _ui_editor.CATEGORIES:
        await callback.answer("❌ بخش پیدا نشد.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        f"{_ui_editor.category_label(category)}\n\nمورد موردنظر را انتخاب کنید:",
        reply_markup=_ui_editor.screens_keyboard(category, mode),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ui_auto_groups:"))
async def ui_editor_auto_groups(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    try:
        page = max(0, int(callback.data.split(":", 1)[1]))
    except Exception:
        page = 0
    await callback.message.edit_text("🧩 پیام‌های واقعی ربات\n\nفایل/بخش موردنظر را انتخاب کنید:", reply_markup=_ui_editor.auto_source_groups_keyboard(page))
    await callback.answer()


@router.callback_query(F.data.startswith("ui_auto_group:"))
async def ui_editor_auto_group(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("❌ درخواست نامعتبر است.", show_alert=True)
        return
    source_file, page_raw = parts[1], parts[2]
    try:
        page = max(0, int(page_raw))
    except Exception:
        page = 0
    await callback.message.edit_text(
        f"🧩 پیام‌های واقعی — {source_file}\n\nمورد موردنظر را انتخاب کنید:",
        reply_markup=_ui_editor.auto_messages_keyboard(source_file, page),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ui_auto:"))
async def ui_editor_auto_preview(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    item = _ui_editor.get_auto_entry(key)
    if not item:
        await callback.answer("❌ متن پیدا نشد.", show_alert=True)
        return
    current = _ui_editor.get_text(key, item.get("template", ""))
    await callback.message.edit_text(
        f"🧩 پیش‌نمایش متن واقعی\n\n{current[:3900]}",
        reply_markup=_ui_editor.auto_preview_keyboard(key),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ui_screen:"))
async def ui_editor_screen(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("❌ درخواست نامعتبر است.", show_alert=True)
        return
    _, mode, key = parts
    if key not in _ui_editor.SCREENS:
        await callback.answer("❌ صفحه پیدا نشد.", show_alert=True)
        return
    await state.clear()
    sc = _ui_editor.SCREENS[key]
    if mode == "text":
        text = _ui_editor.preview_text(key)
        markup = _ui_editor.text_screen_keyboard(key)
    elif mode == "buttons":
        text = f"🔘 دکمه‌های «{sc.get('label', key)}»\n\nدکمه موردنظر را انتخاب کنید یا وضعیت/رنگ آن را تغییر دهید:"
        markup = _ui_editor.button_screen_keyboard(key)
    else:
        await callback.answer("❌ حالت نامعتبر است.", show_alert=True)
        return
    await callback.message.edit_text(text[:3900], reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("ui_screen_textpage:"))
async def ui_editor_text_page(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3 or parts[1] not in _ui_editor.SCREENS:
        await callback.answer("❌ صفحه پیدا نشد.", show_alert=True)
        return
    key, page = parts[1], max(0, int(parts[2]))
    await callback.message.edit_text(_ui_editor.preview_text(key, page), reply_markup=_ui_editor.text_screen_keyboard(key, page))
    await callback.answer()


@router.callback_query(F.data.startswith("ui_button_page:"))
async def ui_editor_button_page(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3 or parts[1] not in _ui_editor.SCREENS:
        await callback.answer("❌ صفحه پیدا نشد.", show_alert=True)
        return
    key, page = parts[1], max(0, int(parts[2]))
    sc = _ui_editor.SCREENS[key]
    await callback.message.edit_text(
        f"🔘 دکمه‌های «{sc.get('label', key)}»\n\nدکمه موردنظر را انتخاب کنید یا وضعیت/رنگ آن را تغییر دهید:",
        reply_markup=_ui_editor.button_screen_keyboard(key, page),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ui_edit_text:"))
async def ui_editor_edit_text(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    if key not in _ui_editor.SCREENS:
        await callback.answer("❌ متن پیدا نشد.", show_alert=True)
        return
    current = _ui_editor.get_text(key, _ui_editor.SCREENS[key].get("default", ""))
    variable_hint = ""
    if key == "purchase_history":
        variable_hint = "\n\n🔧 متغیرهای قابل استفاده:\n{purchase_reports} | {total_spent} | {purchase_count}"
    await state.update_data(ui_edit_kind="text", ui_text_key=key)
    await state.set_state(AdminStates.waiting_ui_editor_input)
    await callback.message.edit_text(
        f"✏️ ویرایش متن\n\n«{_ui_editor.SCREENS[key].get('label', key)}»\n\nمتن فعلی:\n{current}{variable_hint}\n\nمتن جدید را ارسال کنید.\n\n💎 اگر Premium/Custom Emoji بفرستید، entity آن هم ذخیره می‌شود.",
        reply_markup=admin_back_button(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ui_edit_auto:"))
async def ui_editor_edit_auto(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    key = callback.data.split(":", 1)[1]
    item = _ui_editor.get_auto_entry(key)
    if not item:
        await callback.answer("❌ متن پیدا نشد.", show_alert=True)
        return
    current = _ui_editor.get_text(key, item.get("template", ""))
    await state.update_data(ui_edit_kind="text", ui_text_key=key, ui_auto_back=item.get("source", ""))
    await state.set_state(AdminStates.waiting_ui_editor_input)
    await callback.message.edit_text(
        f"✏️ ویرایش متن واقعی\n\n{current}\n\nمتن جدید را ارسال کنید.",
        reply_markup=admin_back_button(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ui_button:"))
async def ui_editor_edit_button(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("❌ درخواست نامعتبر است.", show_alert=True)
        return
    _, screen_key, callback_key = parts
    sc = _ui_editor.SCREENS.get(screen_key)
    if not sc or callback_key not in {cb for cb, _ in sc.get("buttons", [])}:
        await callback.answer("❌ دکمه پیدا نشد.", show_alert=True)
        return
    default = _ui_editor._button_default(screen_key, callback_key)
    current = _ui_editor.get_button(screen_key, callback_key, default)
    meta = _ui_editor.get_button_meta(screen_key, callback_key)
    emoji_note = "\n💎 برای Premium Emoji، همان ایموجی را در ابتدای پیام بفرستید." if meta.get("custom_emoji_id") else ""
    await state.update_data(ui_edit_kind="button", ui_screen_key=screen_key, ui_callback_key=callback_key)
    await state.set_state(AdminStates.waiting_ui_editor_input)
    await callback.message.edit_text(
        f"🔘 ویرایش دکمه\n\nصفحه: {sc.get('label', screen_key)}\nمتن فعلی: {current}{emoji_note}\n\nمتن جدید دکمه را ارسال کنید:",
        reply_markup=admin_back_button(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ui_toggle:"))
async def ui_editor_toggle_button(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3 or parts[1] not in _ui_editor.SCREENS:
        await callback.answer("❌ دکمه پیدا نشد.", show_alert=True)
        return
    _ui_editor.toggle_button(parts[1], parts[2])
    await callback.message.edit_reply_markup(reply_markup=_ui_editor.button_screen_keyboard(parts[1]))
    await callback.answer("✅ وضعیت دکمه تغییر کرد.")


@router.callback_query(F.data.startswith("ui_style:"))
async def ui_editor_style_button(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3 or parts[1] not in _ui_editor.SCREENS:
        await callback.answer("❌ دکمه پیدا نشد.", show_alert=True)
        return
    screen_key, callback_key = parts[1], parts[2]
    meta = _ui_editor.get_button_meta(screen_key, callback_key)
    style = _ui_editor.next_button_style(meta.get("style"))
    _ui_editor.set_button_style(screen_key, callback_key, style)
    await callback.message.edit_reply_markup(reply_markup=_ui_editor.button_screen_keyboard(screen_key))
    await callback.answer("🎨 رنگ دکمه تغییر کرد.")


@router.callback_query(F.data.startswith("ui_move:"))
async def ui_editor_move_button(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":", 3)
    if len(parts) != 4 or parts[1] not in _ui_editor.SCREENS:
        await callback.answer("❌ دکمه پیدا نشد.", show_alert=True)
        return
    screen_key, callback_key, direction = parts[1], parts[2], parts[3]
    _ui_editor.move_button(screen_key, callback_key, direction)
    await callback.message.edit_reply_markup(reply_markup=_ui_editor.button_screen_keyboard(screen_key))
    await callback.answer("↕️ جای دکمه تغییر کرد.")


# ---------------------------------------------------------------------------
# ➕ دکمه‌های سفارشی ادمین (افزودن/حذف/جابه‌جایی دکمه‌ی دلخواه در هر صفحه)
#
# 🐛 فیکس بحرانی: کل این خانواده‌ی هندلرها (ui_cbtn_*) و همچنین
# ui_buttons_page: از فایل حذف شده بودند، در حالی که ui_editor.py همچنان این
# دکمه‌ها را می‌ساخت و permissions.py هم مجوزشان را تعریف کرده بود. نتیجه:
# «➕ دکمه‌های سفارشی این صفحه» و صفحه‌بندی فهرست دکمه‌ها کاملاً بی‌جواب
# بودند — بخش بزرگی از همان «ویرایش متن و دکمه‌ها کلاً باگه».
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("ui_buttons_page:"))
async def ui_editor_buttons_page(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    try:
        page = int(callback.data.split(":", 1)[1])
    except Exception:
        page = 0
    await callback.message.edit_reply_markup(reply_markup=_ui_editor.all_buttons_keyboard(page))
    await callback.answer()


@router.callback_query(F.data.startswith("ui_cbtn_list:"))
async def ui_cbtn_list(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    screen_key = callback.data.split(":", 1)[1]
    if screen_key not in _ui_editor.SCREENS:
        await callback.answer("❌ صفحه پیدا نشد.", show_alert=True)
        return
    await callback.message.edit_text(
        f"➕ دکمه‌های سفارشی «{_ui_editor.SCREENS[screen_key]['label']}»\n\n"
        "این دکمه‌ها علاوه بر دکمه‌های اصلی ربات، پایین همین صفحه نشان داده می‌شوند.",
        reply_markup=_ui_editor.custom_buttons_keyboard(screen_key),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ui_cbtn_move:"))
async def ui_cbtn_move(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("❌ درخواست نامعتبر.", show_alert=True)
        return
    button_id, direction = int(parts[1]), parts[2]
    btn = _ui_editor.get_custom_button(button_id)
    if not btn:
        await callback.answer("❌ این دکمه پیدا نشد.", show_alert=True)
        return
    _ui_editor.move_custom_button(button_id, direction)
    await callback.message.edit_reply_markup(reply_markup=_ui_editor.custom_buttons_keyboard(btn["screen_key"]))
    await callback.answer("↕️ جابه‌جا شد.")


@router.callback_query(F.data.startswith("ui_cbtn_del:"))
async def ui_cbtn_delete(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    button_id = int(callback.data.split(":", 1)[1])
    btn = _ui_editor.get_custom_button(button_id)
    if not btn:
        await callback.answer("❌ این دکمه پیدا نشد.", show_alert=True)
        return
    screen_key = btn["screen_key"]
    _ui_editor.delete_custom_button(button_id)
    await callback.message.edit_reply_markup(reply_markup=_ui_editor.custom_buttons_keyboard(screen_key))
    await callback.answer("🗑 دکمه حذف شد.")


@router.callback_query(F.data.startswith("ui_cbtn_open:"))
async def ui_cbtn_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    button_id = int(callback.data.split(":", 1)[1])
    btn = _ui_editor.get_custom_button(button_id)
    if not btn:
        await callback.answer("❌ این دکمه پیدا نشد.", show_alert=True)
        return
    kind = {"callback": "🔗 صفحه‌ی داخل ربات", "url": "🌐 لینک وب", "webapp": "📲 اپ‌لینک"}.get(btn["action_type"], btn["action_type"])
    await callback.answer(
        f"«{btn['text']}»\nنوع: {kind}\nمقصد: {btn['action_value']}\n\nبرای حذف از 🗑 کنارش استفاده کن.",
        show_alert=True,
    )


@router.callback_query(F.data.startswith("ui_cbtn_add:"))
async def ui_cbtn_add(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    screen_key = callback.data.split(":", 1)[1]
    if screen_key not in _ui_editor.SCREENS:
        await callback.answer("❌ صفحه پیدا نشد.", show_alert=True)
        return
    await state.update_data(cbtn_screen=screen_key)
    await callback.message.edit_text(
        "🎨 اول رنگ دکمه‌ی جدید را انتخاب کن:",
        reply_markup=_ui_editor.custom_button_style_keyboard(screen_key),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ui_cbtn_style:"))
async def ui_cbtn_style(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("❌ درخواست نامعتبر.", show_alert=True)
        return
    screen_key, style = parts[1], parts[2]
    await state.update_data(cbtn_screen=screen_key, cbtn_style=style)
    await callback.message.edit_text(
        "🔗 این دکمه کاربر را کجا ببرد؟",
        reply_markup=_ui_editor.custom_button_action_type_keyboard(screen_key),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ui_cbtn_type:"))
async def ui_cbtn_type(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("❌ درخواست نامعتبر.", show_alert=True)
        return
    screen_key, action_type = parts[1], parts[2]
    await state.update_data(cbtn_screen=screen_key, cbtn_action_type=action_type)

    if action_type == "callback_quick":
        await callback.message.edit_text(
            "🔗 دکمه به کدام صفحه‌ی ربات وصل شود؟",
            reply_markup=_ui_editor.quick_destination_keyboard(screen_key),
        )
        await callback.answer()
        return

    prompts = {
        "callback_manual": "✍️ مقدار callback دلخواه را بفرست (پیشرفته — باید دقیقاً با یکی از callbackهای ربات یکی باشد):",
        "url": "🌐 آدرس کامل لینک را بفرست (با https:// شروع شود):",
        "webapp": "📲 آدرس کامل Web App را بفرست (با https:// شروع شود):",
    }
    await state.set_state(AdminStates.waiting_ui_cbtn_value)
    await callback.message.answer(prompts.get(action_type, "مقدار مقصد را بفرست:"))
    await callback.answer()


@router.callback_query(F.data.startswith("ui_cbtn_dest:"))
async def ui_cbtn_dest(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("❌ درخواست نامعتبر.", show_alert=True)
        return
    screen_key, dest = parts[1], parts[2]
    await state.update_data(cbtn_screen=screen_key, cbtn_action_type="callback", cbtn_action_value=dest)
    await state.set_state(AdminStates.waiting_ui_cbtn_text)
    await callback.message.answer("✏️ حالا متن روی دکمه را بفرست (می‌تونی ایموجی پرمیوم هم بذاری):")
    await callback.answer()


@router.message(AdminStates.waiting_ui_cbtn_value)
async def ui_cbtn_value_input(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    action_type = data.get("cbtn_action_type")
    value = (message.text or "").strip()
    if action_type in ("url", "webapp") and not value.startswith("https://"):
        await message.answer("❌ آدرس باید با https:// شروع شود. دوباره بفرست:")
        return
    if not value:
        await message.answer("❌ مقدار خالی است. دوباره بفرست:")
        return
    stored_type = "callback" if action_type == "callback_manual" else action_type
    await state.update_data(cbtn_action_type=stored_type, cbtn_action_value=value)
    await state.set_state(AdminStates.waiting_ui_cbtn_text)
    await message.answer("✏️ حالا متن روی دکمه را بفرست (می‌تونی ایموجی پرمیوم هم بذاری):")


@router.message(AdminStates.waiting_ui_cbtn_text)
async def ui_cbtn_text_input(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    screen_key = data.get("cbtn_screen")
    text = (message.text or "").strip()
    if not screen_key or screen_key not in _ui_editor.SCREENS:
        await state.clear()
        await message.answer("❌ این مسیر منقضی شده؛ دوباره از ویرایشگر شروع کن.")
        return
    if not text:
        await message.answer("❌ متن دکمه خالی است. دوباره بفرست:")
        return
    _ui_editor.add_custom_button(
        screen_key, text,
        data.get("cbtn_style", "primary"),
        data.get("cbtn_action_type", "callback"),
        data.get("cbtn_action_value", "back"),
    )
    await state.clear()
    await message.answer(
        f"✅ دکمه‌ی «{text}» به صفحه‌ی «{_ui_editor.SCREENS[screen_key]['label']}» اضافه شد.",
        reply_markup=_ui_editor.custom_buttons_keyboard(screen_key),
    )


@router.callback_query(F.data.startswith("ui_rowlayout:"))
async def ui_editor_rowlayout_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    key=callback.data.split(":",1)[1]
    if key not in _ui_editor.SCREENS:
        await callback.answer("❌ صفحه پیدا نشد.", show_alert=True); return
    await state.update_data(ui_rowlayout_key=key)
    await state.set_state(AdminStates.waiting_ui_row_layout)
    await callback.message.answer("📐 الگوی تعداد دکمه‌ها در هر ردیف را با کاما بفرست. مثال: 1,3,2\nیعنی ردیف اول ۱ دکمه، ردیف دوم ۳ دکمه و ردیف سوم ۲ دکمه.")
    await callback.answer()

@router.message(AdminStates.waiting_ui_row_layout)
async def ui_editor_rowlayout_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    raw=(message.text or '').replace('،',',').strip()
    try:
        widths=[int(x.strip()) for x in raw.split(',') if x.strip()]
        if not widths or any(x<1 or x>8 for x in widths): raise ValueError
    except Exception:
        await message.answer("❌ فرمت نامعتبر است. مثال درست: 1,3,2"); return
    data=await state.get_data(); key=data.get('ui_rowlayout_key')
    if key not in _ui_editor.SCREENS:
        await state.clear(); await message.answer("❌ صفحه پیدا نشد."); return
    _ui_editor.set_row_layout(key,widths); await state.clear()
    await message.answer(f"✅ الگوی ردیف‌ها ذخیره شد: {', '.join(map(str,widths))}", reply_markup=_ui_editor.button_screen_keyboard(key))


@router.callback_query(F.data.startswith("ui_layout:"))
async def ui_editor_layout(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 4 or parts[1] not in _ui_editor.SCREENS:
        await callback.answer("❌ صفحه پیدا نشد.", show_alert=True)
        return
    screen_key, action, value = parts[1], parts[2], parts[3]
    if action == "set":
        cols = max(1, min(4, int(value)))
    elif action == "cycle":
        cols = max(1, min(4, int(value)))
    else:
        await callback.answer("❌ حالت چیدمان نامعتبر است.", show_alert=True)
        return
    _ui_editor.set_layout(screen_key, "vertical" if cols == 1 else "inline", cols)
    await callback.message.edit_reply_markup(reply_markup=_ui_editor.button_screen_keyboard(screen_key))
    await callback.answer("📐 چیدمان ذخیره شد.")


@router.message(AdminStates.waiting_ui_editor_input)
async def ui_editor_save_input(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    kind = data.get("ui_edit_kind")
    value = (message.text or "").strip()
    if not value:
        await message.answer("❌ مقدار نمی‌تواند خالی باشد. دوباره ارسال کنید:")
        return
    if len(value) > 3800:
        await message.answer("❌ متن خیلی طولانی است. لطفاً متن کوتاه‌تری ارسال کنید:")
        return
    try:
        if kind == "text":
            key = data.get("ui_text_key")
            if key not in _ui_editor.SCREENS and not str(key).startswith("auto_"):
                raise ValueError("invalid text key")
            _ui_editor.set_text(key, value, _ui_editor_entities(message))
            await state.clear()
            if str(key).startswith("auto_"):
                item = _ui_editor.get_auto_entry(key)
                source = (item or {}).get("source", "")
                markup = _ui_editor.auto_messages_keyboard(source, 0) if source else _ui_editor.editor_mode_keyboard()
                await message.answer("✅ متن ذخیره شد و Entityهای آن هم حفظ شدند.", reply_markup=markup)
            else:
                await message.answer("✅ متن ذخیره شد و Entityهای آن هم حفظ شدند.", reply_markup=_ui_editor.text_screen_keyboard(key))
            return
        if kind == "button":
            screen_key = data.get("ui_screen_key")
            callback_key = data.get("ui_callback_key")
            if screen_key not in _ui_editor.SCREENS:
                raise ValueError("invalid screen")
            defaults = dict(_ui_editor.SCREENS[screen_key].get("buttons") or [])
            if callback_key not in defaults:
                raise ValueError("invalid button")
            custom_emoji_id = None
            for entity in _ui_editor_entities(message):
                if entity.get("type") == "custom_emoji" and entity.get("custom_emoji_id"):
                    custom_emoji_id = str(entity["custom_emoji_id"])
                    break
            _ui_editor.set_button(screen_key, callback_key, value, custom_emoji_id=custom_emoji_id)
            await state.clear()
            await message.answer("✅ متن دکمه ذخیره شد." + (" 💎 Premium Emoji هم ذخیره شد." if custom_emoji_id else ""), reply_markup=_ui_editor.button_screen_keyboard(screen_key))
            return
        raise ValueError("unknown editor state")
    except Exception:
        logger.exception("UI editor save failed")
        await state.clear()
        await message.answer("❌ ذخیره انجام نشد. دوباره از منوی ویرایش متن و دکمه‌ها وارد شوید.")


@router.callback_query(F.data.startswith("textcat_"))
async def admin_text_category(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    try:
        index = int(callback.data.replace("textcat_", "", 1))
        category = list(TEXT_CATEGORIES.keys())[index]
    except Exception:
        await callback.answer("❌ بخش متن پیدا نشد.", show_alert=True); return
    await state.clear()
    note = "\n\n🔒 متن انقضای فاکتور کارت‌به‌کارت سیستمی است و از اینجا قابل تغییر نیست." if "فاکتور کارت‌به‌کارت" in category else ""
    await callback.message.edit_text(
        f"📝 {category}\n\nمتن موردنظر را برای ویرایش انتخاب کنید:{note}",
        reply_markup=_text_manager_keyboard(category),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("textedit_"))
async def admin_text_edit_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    key = callback.data.replace("textedit_", "", 1)
    if key not in TEXT_KEYS:
        await callback.answer("❌ متن پیدا نشد.", show_alert=True); return
    await state.update_data(text_override_key=key)
    await state.set_state(AdminStates.waiting_text_override_value)
    current = user_text(key, TEXT_KEYS[key])
    placeholder_note = "\n\nمتغیرهای قابل استفاده: " + ", ".join("{" + x + "}" for x in _template_vars(TEXT_KEYS[key])) if _template_vars(TEXT_KEYS[key]) else ""
    lock_note = "\n\n🔒 توجه: جمله‌ی زمان انقضای فاکتور در کد سیستمی تولید می‌شود و جزو این متن نیست." if key.startswith("invoice_") else ""
    await callback.message.edit_text(
        f"✏️ ویرایش متن: {key}\n\nمتن فعلی:\n{current}{placeholder_note}{lock_note}\n\nمتن جدید را ارسال کنید:",
        reply_markup=admin_back_button(),
    )
    await callback.answer()


def _template_vars(template: str) -> list[str]:
    import string
    vars_found = []
    for _, field, _, _ in string.Formatter().parse(template):
        if field and field not in vars_found:
            vars_found.append(field.split("!", 1)[0].split(":", 1)[0])
    return vars_found


@router.message(AdminStates.waiting_text_override_value)
async def admin_text_edit_save(message: types.Message, state: FSMContext):
    data = await state.get_data(); key = data.get("text_override_key")
    if key not in TEXT_KEYS:
        await state.clear(); await message.answer("❌ عملیات منقضی شد."); return
    value = (message.text or "").strip()
    if not value:
        await message.answer("❌ متن نمی‌تواند خالی باشد:"); return
    # 🆕 فیکس: اگر این متن از سقف مجاز تلگرام برای متن پیام (۴۰۹۶ کاراکتر) بلندتر ذخیره شود، بعداً هر‌بار که این متن (مثلاً برای فاکتور، پیام راهنما و غیره) برای کاربر فرستاده شود، تلگرام خطای «MESSAGE_TOO_LONG» برمی‌گرداند. اینجا قبل از ذخیره‌شدن گرفته می‌شود (علاوه بر محافظتی که در show_menu_with_sticker اضافه شد).
    if len(value) > 3800:
        await message.answer(
            f"❌ این متن خیلی طولانی است ({len(value)} کاراکتر) و ممکن است تلگرام آن را رد کند (سقف تلگرام: ۴۰۹۶ کاراکتر). لطفاً متن کوتاه‌تری بفرست:"
        )
        return
    # جلوگیری از خراب‌شدن فاکتور با حذف متغیرهای سیستمی
    required = set(_template_vars(TEXT_KEYS[key]))
    if required:
        supplied = set(_template_vars(value))
        missing = required - supplied
        if missing:
            await message.answer("❌ این متغیرهای ضروری حذف شده‌اند: " + ", ".join("{" + x + "}" for x in sorted(missing)))
            return
    entities = serialize_message_entities(getattr(message, "entities", None))
    custom_emoji_id = None
    for entity in entities:
        if entity.get("type") == "custom_emoji" and entity.get("custom_emoji_id"):
            custom_emoji_id = str(entity["custom_emoji_id"])
            break
    db.set_text_override(key, value, entities=entities)
    db.set_button_custom_emoji_id(key, custom_emoji_id)
    refresh_user_text(key)
    await state.clear()
    category = CATEGORY_BY_KEY.get(key)
    await message.answer("✅ متن ذخیره شد.", reply_markup=_text_manager_keyboard(category))


# 📥 صف سفارشات — لیست خریدهای تأییدشده‌ای که هنوز کانفیگ‌شان ارسال نشده،
# چه خرید پلن معمولی (VIP/گیمینگ) و چه سفارش سفارشی/تمدید.
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_orders_off")
async def admin_orders_off(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    db.set_orders_enabled(False)
    users = db.get_all_users()
    sent, failed = 0, 0
    status_msg = await callback.message.answer(f"⏳ در حال اطلاع‌رسانی به {len(users)} کاربر...")
    for u in users:
        try:
            await callback.bot.send_message(
                int(u["telegram_id"]),
                db.get_text_override("orders_closed", "🔴 ربات به دلیل حجم سفارشات بالا موقتاً بسته می‌باشد.") + "\n\nروشن شدن دوباره‌ی آن اطلاع‌رسانی خواهد شد.",
            )
            sent += 1
        except Exception:
            failed += 1
    await status_msg.edit_text(f"🔴 بخش سفارشات خاموش شد. اطلاع‌رسانی به {sent} نفر موفق، {failed} نفر ناموفق.")
    await callback.message.edit_text("👨‍💻 پنل مدیریت:", reply_markup=_admin_panel_kb_for(callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data == "admin_orders_on")
async def admin_orders_on(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    db.set_orders_enabled(True)
    users = db.get_all_users()
    sent, failed = 0, 0
    status_msg = await callback.message.answer(f"⏳ در حال اطلاع‌رسانی به {len(users)} کاربر...")
    for u in users:
        try:
            await callback.bot.send_message(
                int(u["telegram_id"]),
                db.get_text_override("orders_opened", "🟢 ربات مجدداً فعال شد!") + "\n\nبا زدن /start می‌توانید دوباره سفارش ثبت کنید.",
            )
            sent += 1
        except Exception:
            failed += 1
    await status_msg.edit_text(f"🟢 بخش سفارشات روشن شد. اطلاع‌رسانی به {sent} نفر موفق، {failed} نفر ناموفق.")
    await callback.message.edit_text("👨‍💻 پنل مدیریت:", reply_markup=_admin_panel_kb_for(callback.from_user.id))
    await callback.answer()


async def _render_order_queue(callback: types.CallbackQuery):
    pending = db.get_pending_orders(limit=25)
    for o in pending:
        u = db.get_user_by_id(o["user_id"])
        o["telegram_id"] = u["telegram_id"] if u else ""


    if not pending:
        text = "📦 سفارش‌های در انتظار\n\n✅ در حال حاضر هیچ سفارش در انتظار ارسالی وجود ندارد."
    else:
        text = f"📦 سفارش‌های در انتظار — {len(pending)} مورد در انتظار ارسال\n\nروی هرکدوم بزن تا مسیر ارسالش شروع بشه 👇"

    await callback.message.edit_text(text, reply_markup=admin_order_queue_keyboard(pending))


@router.callback_query(F.data == "admin_request_queue")
async def admin_request_queue(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    order_count = len(db.get_pending_orders(limit=200))
    receipt_count = len(db.get_pending_receipts(limit=200))
    await callback.message.edit_text(
        "📥 صف درخواست‌ها\n\nچه چیزی رو می‌خوای بررسی کنی؟ 👇",
        reply_markup=admin_request_queue_menu(order_count, receipt_count),
    )
    await callback.answer()


@router.callback_query(F.data == "admin_order_queue")
async def admin_order_queue(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await _render_order_queue(callback)
    await callback.answer()


# ---------------------------------------------------------------------------
# 🧾 رسیدهای در انتظار تایید — همه‌ی رسیدهای شارژ کیف پول و خرید کارت‌به‌کارت
# (پلن ثابت + بساز سرویس خودت) که هنوز ادمین تایید/رد نکرده، در یک لیست.
# تایید/رد از همینجا دقیقاً همان مسیر همیشگی (پیام فوروارد‌شده در چت ادمین)
# را صدا می‌زند، فقط یک راه میان‌بر برای دیدن همه‌چیز یک‌جاست.
# ---------------------------------------------------------------------------
async def _render_pending_receipts(callback: types.CallbackQuery):
    receipts = db.get_pending_receipts(limit=25)

    total = len(receipts)
    if total == 0:
        text = "🧾 رسیدهای در انتظار تایید\n\n✅ در حال حاضر هیچ رسید در انتظار تاییدی وجود ندارد."
    else:
        text = f"🧾 رسیدهای در انتظار تایید — {total} مورد\n\nروی ✅ برای تایید یا ❌ برای رد بزن 👇"

    await callback.message.edit_text(text, reply_markup=admin_pending_receipts_keyboard(receipts))


@router.callback_query(F.data == "admin_pending_receipts")
async def admin_pending_receipts(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await _render_pending_receipts(callback)
    await callback.answer()


@router.callback_query(F.data == "clearreceipts_confirm")
async def clear_receipts_confirm(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "⚠️ مطمئنی می‌خوای همه‌ی رسیدهای این لیست رو بررسی‌شده علامت بزنی؟\n"
        "(توجه: این کار فقط لیست رو خالی می‌کنه؛ اگر هنوز به کاربری تایید/رد اعلام نکردی، "
        "پیام اصلی رسیدش همچنان توی چتت هست و باید از همونجا اقدام کنی.)",
        reply_markup=admin_clear_receipts_confirm_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "clearreceipts_do")
async def clear_receipts_do(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    db.dismiss_all_pending_receipts()
    await _render_pending_receipts(callback)
    await callback.answer("🧹 لیست خالی شد.")


@router.callback_query(F.data.startswith("dismissorder_"))
async def dismiss_order(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    order_id = int(callback.data.replace("dismissorder_", ""))
    db.set_order_status(order_id, "dismissed")
    await _render_order_queue(callback)
    await callback.answer("🗑 از صف پاک شد.")




@router.callback_query(F.data == "clearorders_confirm")
async def clear_orders_confirm(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "⚠️ مطمئنی می‌خوای همه‌ی سفارش‌های این صف رو پاک کنی؟\n"
        "(این کار فقط سفارش‌ها رو از صف حذف می‌کنه؛ اگه کانفیگ کسی رو نفرستادی، دیگه اینجا یادآوریش نمی‌مونه.)",
        reply_markup=admin_clear_orders_confirm_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "clearorders_do")
async def clear_orders_do(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    pending = db.get_pending_orders(limit=1000)
    for o in pending:
        db.set_order_status(o["id"], "dismissed")
    await _render_order_queue(callback)
    await callback.answer(f"🧹 {len(pending)} سفارش پاک شد.")


# ---------------------------------------------------------------------------
# 📦 مشاهده و مدیریت سرویس‌های یک کاربر (لیست/حذف نرم/ادیت لینک ساب و کیوآرکد/
# مدیریت فایل‌های گیمینگ) — همه از طریق «🔍 جستجوی حرفه‌ای»
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("useractions_"))
async def admin_user_actions_back(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    uid = callback.data.replace("useractions_", "")
    await _reply_with_user_actions(
        callback.message, f"👤 مدیریت کاربر {uid}", uid, db.is_user_blocked(uid), edit=True
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# ✉️ پیام خصوصی ادمین به یک کاربر خاص (متن/عکس/فیلم/فوروارد)
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("pm_"))
async def admin_pm_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    uid = callback.data.replace("pm_", "")
    await state.set_state(AdminStates.waiting_pm_message)
    await state.update_data(pm_target_uid=uid)
    await callback.message.edit_text(
        f"✉️ پیامی که می‌خواهید به کاربر {uid} ارسال شود را بفرستید (متن، عکس، فیلم یا فوروارد هم پذیرفه است):",
        reply_markup=admin_pm_cancel_keyboard(uid),
    )
    await callback.answer()


@router.message(AdminStates.waiting_pm_message)
async def admin_pm_send(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    uid = data.get("pm_target_uid")
    if not uid:
        await state.clear()
        return
    try:
        await send_notification_sticker(message.bot, int(uid), "notif_personal_message")
        await message.bot.copy_message(int(uid), message.chat.id, message.message_id)
        await _reply_with_user_actions(message, "✅ پیام خصوصی برای کاربر ارسال شد.", uid, db.is_user_blocked(uid), edit=False)
    except Exception:
        await _reply_with_user_actions(message, "❌ ارسال پیام ناموفق بود (ممکن است کاربر ربات را مسدود کرده باشد).", uid, db.is_user_blocked(uid), edit=False)
    await state.clear()


# ---------------------------------------------------------------------------
# 🚫 مسدود/رفع مسدودیت کاربر — کاربر‌های مسدود نمی‌توانند از ربات استفاده کنند
# (بررسی در middleware/start.py)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ✏️ ویرایش بی‌صدای موجودی کیف‌پول کاربر (بند ۶)
# تفاوتش با «💰 شارژ دستی»: اینجا هیچ پیام/نوتیفیکیشنی برای کاربر ارسال
# نمی‌شود و ادمین می‌تواند موجودی را به هر مقداری (کمتر یا بیشتر) تنظیم کند،
# نه فقط اضافه کند. تراکنش داخلی برای ردگیری حسابداری همچنان ثبت می‌شود.
# ---------------------------------------------------------------------------
@router.callback_query(F.data.startswith("walletedit_"))
async def admin_wallet_edit_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    uid = callback.data.replace("walletedit_", "")
    user = db.get_user(uid)
    if user is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return
    await state.update_data(wallet_edit_uid=uid)
    await state.set_state(AdminStates.waiting_wallet_edit_amount)
    await callback.message.answer(
        f"✏️ ویرایش موجودی کیف‌پول «{user['name']}»\n\n"
        f"موجودی فعلی: {user['wallet']:,} تومان\n\n"
        "مقدار جدید را به تومان بفرست (موجودی دقیقاً روی همین عدد تنظیم می‌شود).\n"
        "برای کم/زیاد کردن نسبی می‌تونی از + یا - استفاده کنی، مثلاً: +50000 یا -20000\n\n"
        "⚠️ هیچ پیامی برای کاربر ارسال نخواهد شد."
    )
    await callback.answer()


@router.message(AdminStates.waiting_wallet_edit_amount)
async def admin_wallet_edit_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    uid = data.get("wallet_edit_uid")
    user = db.get_user(uid) if uid else None
    if user is None:
        await state.clear()
        await message.answer("❌ این مسیر منقضی شده؛ دوباره از لیست کاربران شروع کن.")
        return

    raw = (message.text or "").replace(",", "").replace("٬", "").strip()
    try:
        if raw.startswith(("+", "-")):
            delta = int(raw)
            new_balance = user["wallet"] + delta
        else:
            new_balance = int(raw)
        if new_balance < 0:
            await message.answer("❌ موجودی نمی‌تواند منفی شود. عدد دیگری بفرست:")
            return
    except Exception:
        await message.answer("❌ فقط عدد بفرست (یا +عدد / -عدد برای تغییر نسبی):")
        return

    old_balance = user["wallet"]
    diff = new_balance - old_balance
    db.set_user_wallet(user["id"], new_balance, f"ویرایش دستی توسط ادمین ({diff:+,} تومان)")
    await state.clear()
    await message.answer(
        f"✅ موجودی «{user['name']}» از {old_balance:,} به {new_balance:,} تومان تغییر کرد.\n"
        "🤫 هیچ پیامی برای کاربر ارسال نشد.",
        reply_markup=admin_user_actions_keyboard(uid, is_blocked=bool(user.get("is_blocked"))),
    )
    await _notify_main_admin_action(message.bot, message.from_user, "ویرایش بی‌صدای کیف‌پول", str(uid), f"{old_balance:,} → {new_balance:,}")


@router.callback_query(F.data.startswith("toggleblock_"))
async def admin_toggle_block(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    uid = callback.data.replace("toggleblock_", "")
    currently_blocked = db.is_user_blocked(uid)
    db.set_user_blocked(uid, not currently_blocked)

    user = db.get_user(uid)
    if user is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return
    stats = db.get_referral_stats(user["id"])
    text = (
        f"👤 {user['name']}\n"
        f"🆔 {user['telegram_id']}\n\n"
        f"💛 کیف پول آزاد: {user['wallet']:,} تومان\n"
        f"🔒 کیف پول مسدود: {user['locked_wallet']:,} تومان\n"
        f"🛍 کل خرید: {user['total_purchase']:,} تومان\n"
        f"📅 عضویت: {user['joined']}\n\n"
        f"🔗 کد دعوت: {user['invite_code']}\n"
        f"👥 دعوت: {stats['invited_count']} | موفق: {stats['successful_invites']}\n\n"
        + ("🚫 وضعیت: مسدود" if not currently_blocked else "✅ وضعیت: رفع مسدودیت شد")
    )
    await _reply_with_user_actions(callback.message, text, user["telegram_id"], not currently_blocked, edit=True)
    await callback.answer("🚫 کاربر مسدود شد." if not currently_blocked else "✅ مسدودیت برداشته شد.")


@router.callback_query(F.data.startswith("deleteuser_"))
async def admin_delete_user_ask(callback: types.CallbackQuery):
    if not _admin_perm(callback.from_user.id, "users"):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    uid = callback.data.replace("deleteuser_", "", 1)
    if uid == str(ADMIN_ID) or db.is_sub_admin(uid):
        await callback.answer("⛔ حساب ادمین قابل حذف نیست.", show_alert=True)
        return
    user = db.get_user(uid)
    if user is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    await callback.message.edit_text(
        f"⚠️ حذف کامل کاربر\n\n"
        f"👤 {user['name']}\n"
        f"🆔 {user['telegram_id']}\n\n"
        "این عملیات فقط اطلاعات کاربر را از دیتابیس ربات حذف می‌کند.\n"
        "هیچ درخواستی به پنل Marzban یا PasarGuard ارسال نمی‌شود.\n\n"
        "❗️این عملیات قابل بازگشت نیست.",
        reply_markup=_admin_user_delete_keyboard(uid),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("deleteuserconfirm_"))
async def admin_delete_user_confirm(callback: types.CallbackQuery):
    if not _admin_perm(callback.from_user.id, "users"):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    uid = callback.data.replace("deleteuserconfirm_", "", 1)
    if uid == str(ADMIN_ID) or db.is_sub_admin(uid):
        await callback.answer("⛔ حساب ادمین قابل حذف نیست.", show_alert=True)
        return
    user = db.get_user(uid)
    if user is None:
        await callback.answer("❌ کاربر قبلاً حذف شده یا یافت نشد.", show_alert=True)
        return

    try:
        deleted = db.delete_user_from_bot(uid)
    except Exception:
        logger.exception("خطا در حذف کامل کاربر از دیتابیس ربات: %s", uid)
        await callback.answer("❌ حذف کاربر انجام نشد؛ اطلاعات ربات تغییر نکرد.", show_alert=True)
        return

    if not deleted:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    await callback.message.edit_text(
        f"✅ کاربر «{user['name']}» با شناسه {uid} فقط از دیتابیس ربات حذف شد.\n\n"
        "🔒 هیچ تغییری در Marzban یا PasarGuard انجام نشد.",
        reply_markup=admin_back_button(),
    )
    await callback.answer("🗑 کاربر از ربات حذف شد.", show_alert=True)
    await _notify_main_admin_action(
        callback.bot,
        callback.from_user,
        "حذف کامل کاربر از ربات",
        uid,
        f"name={user['name']}",
    )


@router.callback_query(F.data.startswith("svcs_"))
async def admin_view_user_services(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    uid = callback.data.replace("svcs_", "")
    user = db.get_user(uid)
    if user is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    configs = db.get_configs(user["id"], include_deleted=True)
    await enrich_configs_with_subscription_names(configs)
    if not configs:
        await callback.message.edit_text(
            f"📦 کاربر {uid} هنوز هیچ سرویسی نداره.",
            reply_markup=admin_back_button(),
        )
    else:
        await callback.message.edit_text(
            f"📦 سرویس‌های کاربر {uid}\n\n❌ یعنی توسط خودِ کاربر حذف شده (ولی برای شما همچنان قابل‌مشاهده‌ست).\n\nروی هرکدوم بزن برای جزئیات و مدیریت 👇",
            reply_markup=admin_services_list_keyboard(configs, uid),
        )
    await callback.answer()


def _remaining_days_from_date_str(date_str) -> int | None:
    """تعداد روز باقی‌مانده تا انقضا را از یک رشته تاریخ به فرمت YYYY-MM-DD محاسبه می‌کند."""
    if not date_str:
        return None
    try:
        exp_date = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
        delta = exp_date - now_tehran_naive()
        return delta.days
    except Exception:
        return None


async def _service_detail_text(cfg: dict) -> str:
    """
    توجه: قبلاً این متن با parse_mode="Markdown" (نسخه‌ی قدیمی مارک‌داون
    تلگرام) فرستاده می‌شد و cfg['plan'] بدون هیچ escape‌ای مستقیم داخل متن
    قرار می‌گرفت. نسخه‌ی قدیمی Markdown تلگرام امکان escape کردن کاراکترهای
    خاص رو نداره؛ پس اگر نام پلن یک زیرخط (_) تک و جفت‌نشده داشت (مثل
    "Businesss_vpn - 1090174")، پارسر اون رو شروع ایتالیک در نظر می‌گرفت و
    چون بسته نمی‌شد، کل درخواست ویرایش پیام با خطای "can't parse entities"
    رد می‌شد و صفحه‌ی جزئیات سرویس اصلاً نمایش داده نمی‌شد.
    راه‌حل: استفاده از HTML به‌جای Markdown، چون HTML یک تابع escape رسمی و
    قابل‌اعتماد داره (html.escape) و این مشکل اصلاً پیش نمیاد.
    """
    icon = "🚀" if cfg.get("type", "vip") == "vip" else "🎮"
    status = "❌ حذف‌شده (توسط کاربر یا ادمین)" if cfg.get("deleted") else "✅ فعال"
    try:
        sub_preview = crypto.decrypt_config(cfg["config"])
    except Exception:
        sub_preview = "⚠️ خطا در رمزگشایی"

    plan_safe = html.escape(str(cfg.get("_display_name") or cfg.get("plan", "")))
    created_safe = html.escape(str(cfg.get("created_at", "")))
    sub_preview_safe = html.escape(sub_preview)

    text = (
        f"{icon} {plan_safe}\n\n"
        f"📌 وضعیت: {status}\n"
        f"📆 تاریخ ایجاد: {created_safe}\n"
    )
    if cfg.get("service_id"):
        text += f"🆔 شناسه سرویس: {html.escape(str(cfg['service_id']))}\n"

    usage = None
    sub_url = sub_preview if sub_preview.lower().startswith(("http://", "https://")) else None
    if cfg.get("type", "vip") == "vip" and sub_url:
        try:
            usage = await fetch_subscription_info(sub_url)
        except Exception:
            usage = None

    if usage:
        total = usage.get("total")
        used = (usage.get("upload") or 0) + (usage.get("download") or 0)
        remaining_bytes = (total - used) if total else None

        text += "\n📊 وضعیت مصرف (لحظه‌ای):\n"
        if total:
            text += f"   • حجم کل: {html.escape(format_bytes(total))}\n"
        text += f"   • مصرف‌شده: {html.escape(format_bytes(used))}\n"
        if remaining_bytes is not None:
            text += f"   • باقی‌مانده: {html.escape(format_bytes(remaining_bytes))}\n"
        if total:
            percent = min(100, round(used / total * 100))
            text += f"\n{usage_bar(percent)} {percent}٪ مصرف شده\n"
        text += f"\n⏰ تاریخ انقضا: {html.escape(format_expire(usage.get('expire')))}\n"
        remaining_days = days_remaining(usage.get("expire"))
        if remaining_days is not None:
            text += "⛔️ منقضی شده\n" if remaining_days <= 0 else f"⌛️ زمان باقی‌مانده: {remaining_days} روز\n"
    elif cfg.get("expiry"):
        text += f"⏰ انقضا: {html.escape(str(cfg['expiry']))}\n"
        remaining_days = _remaining_days_from_date_str(cfg.get("expiry"))
        if remaining_days is not None:
            if remaining_days <= 0:
                text += "⌛️ زمان باقی‌مانده: ⛔️ منقضی شده\n"
            else:
                text += f"⌛️ زمان باقی‌مانده: {remaining_days} روز\n"

    text += f"\n🔗 لینک ساب فعلی:\n<code>{sub_preview_safe}</code>\n"
    if cfg.get("type", "vip") == "vip":
        text += f"\n🎫 کیوآرکد: {'ثبت شده ✅' if cfg.get('qr_file_id') else 'ثبت نشده ❌'}"
    else:
        files = db.get_gaming_files(cfg["id"])
        text += f"\n📁 تعداد فایل کانفیگ: {len(files)}"
    return text


async def _render_service_detail(callback: types.CallbackQuery, cfg_id: int):
    cfg = db.get_config_by_id(cfg_id)
    if cfg is None:
        await callback.answer("❌ سرویس یافت نشد.", show_alert=True)
        return
    owner = db.get_user_by_id(cfg["user_id"])
    uid = owner["telegram_id"] if owner else ""

    await enrich_configs_with_subscription_names([cfg])
    text = await _service_detail_text(cfg)
    kb = admin_service_detail_keyboard(cfg, uid)
    # -----------------------------------------------------------------
    # علت اصلی خطای "خطایی پیش آمد..." روی این صفحه: نام پلن (cfg['plan'])
    # می‌تونه هرچیزی باشه (مثلاً چیزی شبیه "Businesss_vpn - 1090174") و اگر
    # داخلش یک زیرخط (_) تکی و جفت‌نشده باشه، پارسر Markdown قدیمی تلگرام
    # اون رو شروع ایتالیک در نظر می‌گیره و چون بسته نمی‌شه، کل پیام با خطای
    # "can't parse entities" رد می‌شه. قبلاً هیچ try/except اینجا نبود، پس
    # این خطا مستقیم می‌رفت به هندلر سراسری و کاربر فقط "خطایی پیش آمد" رو
    # می‌بیند بدون اینکه جزئیات سرویس اصلاً نمایش داده بشه. حالا اگر مارک‌داون
    # شکست بخوره، همون متن رو بدون فرمت (parse_mode=None) دوباره می‌فرستیم.
    # -----------------------------------------------------------------
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        if "message is not modified" in str(e).lower():
            pass
        else:
            logger.exception("خطا در نمایش جزئیات سرویس ادمین برای cfg_id=%s", cfg_id)
            try:
                await callback.message.edit_text(text, parse_mode=None, reply_markup=kb)
            except Exception:
                logger.exception("خطا در fallback بدون فرمت برای جزئیات سرویس cfg_id=%s", cfg_id)
                await callback.answer("❌ خطا در نمایش جزئیات سرویس. دوباره تلاش کنید.", show_alert=True)
                return
    await callback.answer()


@router.callback_query(F.data.startswith("svcdetail_"))
async def admin_service_detail(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    cfg_id = int(callback.data.replace("svcdetail_", ""))
    await _render_service_detail(callback, cfg_id)


@router.callback_query(F.data.startswith("svcdelete_"))
async def admin_service_delete(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    cfg_id = int(callback.data.replace("svcdelete_", ""))
    db.set_config_deleted(cfg_id, True)
    await _render_service_detail(callback, cfg_id)


@router.callback_query(F.data.startswith("svcrestore_"))
async def admin_service_restore(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    cfg_id = int(callback.data.replace("svcrestore_", ""))
    db.set_config_deleted(cfg_id, False)
    await _render_service_detail(callback, cfg_id)


@router.callback_query(F.data.startswith("svcpurge_"))
async def admin_service_purge_confirm(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    cfg_id = int(callback.data.replace("svcpurge_", ""))
    await callback.message.edit_text(
        "⚠️ این کار غیرقابل بازگشته و کل اطلاعات این سرویس (شامل فایل‌های گیمینگ) برای همیشه پاک می‌شه.\n\nمطمئنی؟",
        reply_markup=admin_purge_confirm_keyboard(cfg_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("svcpurgeconfirm_"))
async def admin_service_purge_apply(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    cfg_id = int(callback.data.replace("svcpurgeconfirm_", ""))
    cfg = db.get_config_by_id(cfg_id)
    owner = db.get_user_by_id(cfg["user_id"]) if cfg else None
    uid = owner["telegram_id"] if owner else ""
    db.delete_config_permanently(cfg_id)
    await callback.message.edit_text("✅ سرویس برای همیشه حذف شد.", reply_markup=admin_back_button())
    await callback.answer()


@router.callback_query(F.data.startswith("svcedit_link_"))
async def admin_service_edit_link_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    cfg_id = int(callback.data.replace("svcedit_link_", ""))
    await state.update_data(edit_config_id=cfg_id)
    await state.set_state(AdminStates.waiting_edit_sublink)
    await callback.message.answer("🔗 لینک ساب جدید این سرویس رو ارسال کن:")
    await callback.answer()


@router.message(AdminStates.waiting_edit_sublink)
async def admin_service_edit_link_apply(message: types.Message, state: FSMContext):
    new_link = (message.text or "").strip()
    if not new_link.lower().startswith(("http://", "https://")):
        await message.answer("❌ این یک لینک معتبر نیست؛ لطفاً لینک رو با http یا https ارسال کن:")
        return

    data = await state.get_data()
    cfg_id = data.get("edit_config_id")
    cfg = db.get_config_by_id(cfg_id) if cfg_id else None
    if cfg is None:
        await message.answer("❌ سرویس یافت نشد.")
        await state.clear()
        return

    db.update_config_link(cfg_id, crypto.encrypt_config(new_link))
    await message.answer("✅ لینک ساب سرویس بروزرسانی شد.", reply_markup=admin_back_button())
    await state.clear()


@router.callback_query(F.data.startswith("svcedit_qr_"))
async def admin_service_edit_qr_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    cfg_id = int(callback.data.replace("svcedit_qr_", ""))
    await state.update_data(edit_config_id=cfg_id)
    await state.set_state(AdminStates.waiting_edit_qr)
    await callback.message.answer("🖼 عکس کیوآرکد جدید این سرویس رو ارسال کن:")
    await callback.answer()


@router.message(AdminStates.waiting_edit_qr, F.photo)
async def admin_service_edit_qr_apply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    cfg_id = data.get("edit_config_id")
    cfg = db.get_config_by_id(cfg_id) if cfg_id else None
    if cfg is None:
        await message.answer("❌ سرویس یافت نشد.")
        await state.clear()
        return

    db.set_config_qr(cfg_id, message.photo[-1].file_id)
    await message.answer("✅ عکس کیوآرکد سرویس بروزرسانی شد.", reply_markup=admin_back_button())
    await state.clear()


@router.message(AdminStates.waiting_edit_qr)
async def admin_service_edit_qr_wrong_format(message: types.Message):
    await message.answer("📸 لطفاً عکس کیوآرکد رو ارسال کن (نه متن).")














# ---------------------------------------------------------------------------
# 🎟 مدیریت تخفیف (ساخت کد تخفیف به‌صورت گام‌به‌گام)
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_discount")
async def admin_discount_list(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    discounts = db.get_all_discounts()
    if not discounts:
        text = "🎟 هیچ کد تخفیفی هنوز ثبت نشده.\n\nبرای ساخت کد جدید، دکمه‌ی زیر را بزنید 👇"
    else:
        text = "🎟 کدهای تخفیف فعال:\n\nبرای مشاهده و ویرایش جزئیات هر کد، روی آن بزنید 👇"

    await callback.message.edit_text(text, reply_markup=admin_discount_menu(discounts))
    await callback.answer()


def _discount_detail_text(d: dict) -> str:
    if d.get("discount_type") == "amount":
        value_text = f"💵 {d['amount']:,} تومان"
    else:
        value_text = f"💯 {d['percent']}٪"

    plans_raw = d.get("applicable_plans")
    if not plans_raw:
        plans_text = "همه‌ی پلن‌ها"
    else:
        all_plans = db.get_all_plans()
        keys = json.loads(plans_raw)
        plans_text = ", ".join(all_plans.get(k, {}).get("name", k) for k in keys) or "همه‌ی پلن‌ها"

    users_raw = d.get("allowed_user_ids")
    if not users_raw:
        users_text = "—"
    else:
        ids = json.loads(users_raw)
        users_text = "، ".join(f"`{i}`" for i in ids)

    audience = d.get("audience") or "all"
    audience_text = {
        "all": "👥 همه‌ی کاربران", "buyers": "🛍 فقط خریداران قبلی",
        "non_buyers": "🆕 فقط بدون خرید قبلی", "specific": f"🎯 فقط افراد خاص: {users_text}",
    }.get(audience, audience)

    extra = ""
    if d.get("min_order_amount"):
        extra += f"\n💰 حداقل مبلغ سفارش: {d['min_order_amount']:,} تومان"
    if d.get("max_uses_per_user"):
        extra += f"\n🔂 سقف استفاده برای هر کاربر: {d['max_uses_per_user']}"
    if d.get("expires_at"):
        extra += f"\n⏰ انقضا: {d['expires_at']}"

    return (
        f"🎟 کد تخفیف: `{d['code']}`\n"
        f"{value_text}\n"
        f"🔁 تعداد استفاده‌ی باقی‌مانده: {d['uses']}\n"
        f"🎯 پلن‌های مجاز: {plans_text}\n"
        f"👥 مخاطب: {audience_text}"
        f"{extra}"
    )


@router.callback_query(F.data.startswith("discdetail_"))
async def admin_discount_detail(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    discount_id = int(callback.data.replace("discdetail_", ""))
    d = db.get_discount_by_id(discount_id)
    if d is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    await callback.message.edit_text(
        _discount_detail_text(d), parse_mode="Markdown", reply_markup=discount_detail_keyboard(discount_id, first_purchase_only=bool(d.get("first_purchase_only")), audience=d.get("audience") or "all")
    )
    await callback.answer()


# 🆕 «تخفیف اولین خرید» (بند ۱۴): درصد تخفیفی که فقط به کسی می‌رسد که هیچ
# خرید قبلی در فروشگاه ندارد — مشوقی برای مشتری تازه، نه برای کسی که همین
# الان هم دارد می‌خرد. زیرساخت دیتابیسی‌اش از قبل بود، فقط همین دکمه‌ی
# روشن/خاموش‌کردنش هندلر نداشت و بی‌جواب می‌ماند.
@router.callback_query(F.data.startswith("discedit_firstpurchase_"))
async def admin_discount_toggle_first_purchase(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discedit_firstpurchase_", ""))
    d = db.get_discount_by_id(discount_id)
    if d is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    new_value = 0 if bool(d.get("first_purchase_only")) else 1
    db.update_discount(discount_id, first_purchase_only=new_value)
    d = db.get_discount_by_id(discount_id)
    await callback.message.edit_text(
        _discount_detail_text(d), parse_mode="Markdown",
        reply_markup=discount_detail_keyboard(discount_id, first_purchase_only=bool(d.get("first_purchase_only")), audience=d.get("audience") or "all"),
    )
    await callback.answer("✅ فقط برای اولین خرید فعال شد." if new_value else "✅ برای همه‌ی خریدها فعال شد.")


@router.callback_query(F.data.startswith("discdelete_"))
async def admin_discount_delete_ask(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discdelete_", ""))
    d = db.get_discount_by_id(discount_id)
    if d is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    await callback.message.edit_text(
        f"❗️ آیا از حذف کد `{d['code']}` مطمئن هستید؟",
        parse_mode="Markdown",
        reply_markup=discount_delete_confirm_keyboard(discount_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("discdeleteconfirm_"))
async def admin_discount_delete_confirm(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discdeleteconfirm_", ""))
    db.delete_discount_by_id(discount_id)
    await callback.answer("✅ کد تخفیف حذف شد.", show_alert=True)
    discounts = db.get_all_discounts()
    text = "🎟 کدهای تخفیف فعال:\n\nبرای مشاهده و ویرایش جزئیات هر کد، روی آن بزنید 👇" if discounts else \
        "🎟 هیچ کد تخفیفی هنوز ثبت نشده.\n\nبرای ساخت کد جدید، دکمه‌ی زیر را بزنید 👇"
    await callback.message.edit_text(text, reply_markup=admin_discount_menu(discounts))


# --- ویرایش مقدار تخفیف (درصد/مبلغ) ---
@router.callback_query(F.data.startswith("discedit_value_"))
async def admin_discount_edit_value_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discedit_value_", ""))
    d = db.get_discount_by_id(discount_id)
    if d is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    await state.update_data(edit_discount_id=discount_id)
    label = "درصد جدید را وارد کنید (بین ۱ تا ۱۰۰)" if d.get("discount_type") != "amount" else \
        "مبلغ ثابت جدید را به تومان وارد کنید"
    await callback.message.edit_text(f"✏️ {label}:", reply_markup=admin_back_button())
    await state.set_state(AdminStates.waiting_discount_edit_value)
    await callback.answer()


@router.message(AdminStates.waiting_discount_edit_value)
async def admin_discount_edit_value_save(message: types.Message, state: FSMContext):
    data = await state.get_data()
    discount_id = data.get("edit_discount_id")
    d = db.get_discount_by_id(discount_id) if discount_id else None
    if d is None:
        await message.answer("❌ مشکلی پیش آمد.", reply_markup=admin_discount_menu(db.get_all_discounts()))
        await state.clear()
        return

    if not message.text or not clean_numeric_id(message.text).isdigit():
        await message.answer("❌ فقط عدد وارد کنید:")
        return
    value = int(clean_numeric_id(message.text))
    if d.get("discount_type") == "amount":
        if value <= 0:
            await message.answer("❌ مبلغ باید بزرگ‌تر از صفر باشد:")
            return
        db.update_discount(discount_id, amount=value)
    else:
        if not (1 <= value <= 100):
            await message.answer("❌ درصد باید بین ۱ تا ۱۰۰ باشد:")
            return
        db.update_discount(discount_id, percent=value)

    await state.clear()
    d = db.get_discount_by_id(discount_id)
    await message.answer("✅ مقدار تخفیف بروزرسانی شد.", reply_markup=admin_back_button())
    await message.answer(_discount_detail_text(d), parse_mode="Markdown", reply_markup=discount_detail_keyboard(discount_id, first_purchase_only=bool(d.get("first_purchase_only")), audience=d.get("audience") or "all"))


# --- ویرایش تعداد استفاده‌ی باقی‌مانده ---
@router.callback_query(F.data.startswith("discedit_uses_"))
async def admin_discount_edit_uses_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discedit_uses_", ""))
    if db.get_discount_by_id(discount_id) is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    await state.update_data(edit_discount_id=discount_id)
    await callback.message.edit_text(
        "✏️ تعداد دفعات مجاز باقی‌مانده‌ی این کد را وارد کنید (مثلاً 50):",
        reply_markup=admin_back_button(),
    )
    await state.set_state(AdminStates.waiting_discount_edit_uses)
    await callback.answer()


@router.message(AdminStates.waiting_discount_edit_uses)
async def admin_discount_edit_uses_save(message: types.Message, state: FSMContext):
    data = await state.get_data()
    discount_id = data.get("edit_discount_id")
    if not message.text or not clean_numeric_id(message.text).isdigit() or int(clean_numeric_id(message.text)) < 0:
        await message.answer("❌ لطفاً یک عدد صحیح غیرمنفی وارد کنید:")
        return
    if db.get_discount_by_id(discount_id) is None:
        await message.answer("❌ مشکلی پیش آمد.", reply_markup=admin_discount_menu(db.get_all_discounts()))
        await state.clear()
        return

    db.update_discount(discount_id, uses=int(clean_numeric_id(message.text)))
    await state.clear()
    d = db.get_discount_by_id(discount_id)
    await message.answer("✅ تعداد استفاده بروزرسانی شد.", reply_markup=admin_back_button())
    await message.answer(_discount_detail_text(d), parse_mode="Markdown", reply_markup=discount_detail_keyboard(discount_id, first_purchase_only=bool(d.get("first_purchase_only")), audience=d.get("audience") or "all"))


# --- ویرایش حداقل مبلغ سفارش ---
@router.callback_query(F.data.startswith("discedit_minorder_"))
async def admin_discount_edit_minorder_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discedit_minorder_", ""))
    if db.get_discount_by_id(discount_id) is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    await state.update_data(edit_discount_id=discount_id)
    await callback.message.edit_text(
        "✏️ حداقل مبلغ سفارش (به تومان) برای استفاده از این کد را وارد کنید.\n"
        "برای برداشتن محدودیت، عدد 0 را ارسال کنید.",
        reply_markup=admin_back_button(),
    )
    await state.set_state(AdminStates.waiting_discount_edit_min_order)
    await callback.answer()


@router.message(AdminStates.waiting_discount_edit_min_order)
async def admin_discount_edit_minorder_save(message: types.Message, state: FSMContext):
    data = await state.get_data()
    discount_id = data.get("edit_discount_id")
    if not message.text or not clean_numeric_id(message.text).isdigit():
        await message.answer("❌ فقط عدد وارد کنید:")
        return
    if db.get_discount_by_id(discount_id) is None:
        await message.answer("❌ مشکلی پیش آمد.", reply_markup=admin_discount_menu(db.get_all_discounts()))
        await state.clear()
        return

    db.update_discount(discount_id, min_order_amount=int(clean_numeric_id(message.text)))
    await state.clear()
    d = db.get_discount_by_id(discount_id)
    await message.answer("✅ حداقل مبلغ سفارش بروزرسانی شد.", reply_markup=admin_back_button())
    await message.answer(_discount_detail_text(d), parse_mode="Markdown", reply_markup=discount_detail_keyboard(discount_id, first_purchase_only=bool(d.get("first_purchase_only")), audience=d.get("audience") or "all"))


# --- ویرایش سقف استفاده‌ی هر کاربر ---
@router.callback_query(F.data.startswith("discedit_maxuser_"))
async def admin_discount_edit_maxuser_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discedit_maxuser_", ""))
    if db.get_discount_by_id(discount_id) is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    await state.update_data(edit_discount_id=discount_id)
    await callback.message.edit_text(
        "✏️ سقف تعداد دفعات استفاده‌ی هر کاربر از این کد را وارد کنید.\n"
        "برای بی‌محدودیت‌کردن، عدد 0 را ارسال کنید.",
        reply_markup=admin_back_button(),
    )
    await state.set_state(AdminStates.waiting_discount_edit_max_per_user)
    await callback.answer()


@router.message(AdminStates.waiting_discount_edit_max_per_user)
async def admin_discount_edit_maxuser_save(message: types.Message, state: FSMContext):
    data = await state.get_data()
    discount_id = data.get("edit_discount_id")
    if not message.text or not clean_numeric_id(message.text).isdigit():
        await message.answer("❌ فقط عدد وارد کنید:")
        return
    if db.get_discount_by_id(discount_id) is None:
        await message.answer("❌ مشکلی پیش آمد.", reply_markup=admin_discount_menu(db.get_all_discounts()))
        await state.clear()
        return

    db.update_discount(discount_id, max_uses_per_user=int(clean_numeric_id(message.text)))
    await state.clear()
    d = db.get_discount_by_id(discount_id)
    await message.answer("✅ سقف استفاده‌ی هر کاربر بروزرسانی شد.", reply_markup=admin_back_button())
    await message.answer(_discount_detail_text(d), parse_mode="Markdown", reply_markup=discount_detail_keyboard(discount_id, first_purchase_only=bool(d.get("first_purchase_only")), audience=d.get("audience") or "all"))


# --- ویرایش تاریخ انقضا ---
@router.callback_query(F.data.startswith("discedit_expiry_"))
async def admin_discount_edit_expiry_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discedit_expiry_", ""))
    if db.get_discount_by_id(discount_id) is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    await state.update_data(edit_discount_id=discount_id)
    await callback.message.edit_text(
        "✏️ تاریخ انقضا را به‌فرمت `YYYY-MM-DD` (مثلاً 2026-12-31) وارد کنید.\n"
        "برای برداشتن انقضا (کد همیشه معتبر باشد)، عدد 0 را ارسال کنید.",
        parse_mode="Markdown",
        reply_markup=admin_back_button(),
    )
    await state.set_state(AdminStates.waiting_discount_edit_expiry)
    await callback.answer()


@router.message(AdminStates.waiting_discount_edit_expiry)
async def admin_discount_edit_expiry_save(message: types.Message, state: FSMContext):
    data = await state.get_data()
    discount_id = data.get("edit_discount_id")
    if db.get_discount_by_id(discount_id) is None:
        await message.answer("❌ مشکلی پیش آمد.", reply_markup=admin_discount_menu(db.get_all_discounts()))
        await state.clear()
        return

    raw = (message.text or "").strip()
    if raw == "0":
        db.update_discount(discount_id, expires_at=None)
    else:
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            await message.answer("❌ فرمت نامعتبر است؛ به‌صورت YYYY-MM-DD وارد کنید (یا 0 برای حذف انقضا):")
            return
        db.update_discount(discount_id, expires_at=parsed.strftime("%Y-%m-%d 23:59:59"))

    await state.clear()
    d = db.get_discount_by_id(discount_id)
    await message.answer("✅ تاریخ انقضا بروزرسانی شد.", reply_markup=admin_back_button())
    await message.answer(_discount_detail_text(d), parse_mode="Markdown", reply_markup=discount_detail_keyboard(discount_id, first_purchase_only=bool(d.get("first_purchase_only")), audience=d.get("audience") or "all"))


# --- ویرایش کاربران مجاز ---
@router.callback_query(F.data.startswith("discedit_audience_"))
async def admin_discount_audience_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discedit_audience_", ""))
    d = db.get_discount_by_id(discount_id)
    if d is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    await callback.message.edit_text(
        "👥 این کد تخفیف برای چه کسانی قابل استفاده باشد؟",
        reply_markup=discount_audience_keyboard(discount_id, d.get("audience") or "all"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("discaud_"))
async def admin_discount_audience_set(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    # callback_data به‌شکل discaud_<value>_<discount_id> است؛ value خودش
    # ممکن است underscore داشته باشد (non_buyers)، پس از سمت راست جدا می‌شود.
    body = callback.data[len("discaud_"):]
    value, _, discount_id_str = body.rpartition("_")
    if not value or not discount_id_str.isdigit():
        await callback.answer("❌ درخواست نامعتبر.", show_alert=True)
        return
    discount_id = int(discount_id_str)
    d = db.get_discount_by_id(discount_id)
    if d is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    db.update_discount(discount_id, audience=value)
    d = db.get_discount_by_id(discount_id)
    await callback.message.edit_text(
        _discount_detail_text(d), parse_mode="Markdown",
        reply_markup=discount_detail_keyboard(discount_id, first_purchase_only=bool(d.get("first_purchase_only")), audience=d.get("audience") or "all"),
    )
    await callback.answer("✅ مخاطب تغییر کرد.")


@router.callback_query(F.data.startswith("discedit_users_"))
async def admin_discount_edit_users_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discedit_users_", ""))
    if db.get_discount_by_id(discount_id) is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    await state.update_data(edit_discount_id=discount_id)
    await callback.message.edit_text(
        "👤 آیدی‌های عددی تلگرام مجاز به استفاده از این کد را وارد کنید "
        "(هرکدام با کاما، فاصله یا خط جدید جدا شود).\n\n"
        "برای برداشتن محدودیت (باز کردن کد برای همه‌ی کاربران) عدد 0 را ارسال کنید.",
        reply_markup=admin_back_button(),
    )
    await state.set_state(AdminStates.waiting_discount_edit_users)
    await callback.answer()


@router.message(AdminStates.waiting_discount_edit_users)
async def admin_discount_edit_users_save(message: types.Message, state: FSMContext):
    data = await state.get_data()
    discount_id = data.get("edit_discount_id")
    if db.get_discount_by_id(discount_id) is None:
        await message.answer("❌ مشکلی پیش آمد.", reply_markup=admin_discount_menu(db.get_all_discounts()))
        await state.clear()
        return

    raw = (message.text or "").strip()
    ids = [p for p in re.split(r"[\s,،]+", raw) if p]

    if not ids or ids == ["0"]:
        db.update_discount(discount_id, allowed_user_ids=None, audience="all")
        summary = "بدون محدودیت (همه‌ی کاربران)"
    else:
        if not all(p.lstrip("-").isdigit() for p in ids):
            await message.answer("❌ فقط آیدی‌های عددی معتبر وارد کنید (یا 0 برای باز کردن برای همه):")
            return
        ids = sorted(set(ids))
        # 🆕 وارد کردن آیدی‌های خاص یعنی این کد صریحاً روی دسته‌ی «افراد
        # مشخص» تنظیم می‌شود (اگر قبلاً روی «خریداران»/«بدون‌خرید» بود، این
        # انتخاب صریح‌تر جایگزینش می‌شود).
        db.update_discount(discount_id, allowed_user_ids=ids, audience="specific")
        summary = "، ".join(f"`{i}`" for i in ids)

    await state.clear()
    d = db.get_discount_by_id(discount_id)
    await message.answer(f"✅ کاربران مجاز بروزرسانی شد: {summary}", parse_mode="Markdown", reply_markup=admin_back_button())
    await message.answer(_discount_detail_text(d), parse_mode="Markdown", reply_markup=discount_detail_keyboard(discount_id, first_purchase_only=bool(d.get("first_purchase_only")), audience=d.get("audience") or "all"))


# --- ویرایش پلن‌های مجاز ---
@router.callback_query(F.data.startswith("discedit_plans_"))
async def admin_discount_edit_plans_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    discount_id = int(callback.data.replace("discedit_plans_", ""))
    d = db.get_discount_by_id(discount_id)
    if d is None:
        await callback.answer("❌ این کد تخفیف یافت نشد.", show_alert=True)
        return
    selected = db._discount_plans(d) or []
    await state.update_data(edit_discount_id=discount_id, edit_discount_plans=selected)
    await callback.message.edit_text(
        "🎯 پلن‌های مجاز برای این کد را انتخاب کنید (هرکدام را بزنید تا انتخاب/لغو شود):",
        reply_markup=discount_plans_edit_keyboard(discount_id, selected),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("discplaned_"))
async def admin_discount_edit_plans_toggle(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    # discplaned_<id>_<key|all|done>
    rest = callback.data.replace("discplaned_", "")
    discount_id_str, _, key = rest.partition("_")
    discount_id = int(discount_id_str)
    data = await state.get_data()
    selected = data.get("edit_discount_plans", [])

    if key == "all":
        selected = []
    elif key == "done":
        db.update_discount(discount_id, applicable_plans=selected or None)
        await state.clear()
        d = db.get_discount_by_id(discount_id)
        await callback.message.edit_text(
            _discount_detail_text(d), parse_mode="Markdown", reply_markup=discount_detail_keyboard(discount_id, first_purchase_only=bool(d.get("first_purchase_only")), audience=d.get("audience") or "all")
        )
        await callback.answer("✅ پلن‌های مجاز ذخیره شد.")
        return
    else:
        selected = [p for p in selected if p != key] if key in selected else selected + [key]

    await state.update_data(edit_discount_plans=selected)
    await callback.message.edit_reply_markup(reply_markup=discount_plans_edit_keyboard(discount_id, selected))
    await callback.answer()


@router.callback_query(F.data == "new_discount")
async def new_discount_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text(
        "🎟 ساخت کد تخفیف جدید — مرحله ۱ از ۶\n\n"
        "✏️ کد تخفیف مورد نظر را بدون فاصله وارد کنید (مثلاً SUMMER20):",
        reply_markup=admin_back_button(),
    )
    await state.set_state(AdminStates.waiting_discount_code_step)
    await callback.answer()


@router.message(AdminStates.waiting_discount_code_step)
async def new_discount_code_input(message: types.Message, state: FSMContext):
    code = message.text.strip().upper() if message.text else ""
    if not code or " " in code:
        await message.answer("❌ کد نامعتبر است؛ بدون فاصله دوباره وارد کنید:")
        return
    await state.update_data(new_discount_code=code)
    await message.answer(
        "🎟 مرحله ۲ از ۶\n\nنوع تخفیف را انتخاب کنید:", reply_markup=discount_type_keyboard()
    )


@router.callback_query(F.data.startswith("disctype_"), AdminStates.waiting_discount_code_step)
async def new_discount_type_chosen(callback: types.CallbackQuery, state: FSMContext):
    disc_type = callback.data.replace("disctype_", "")
    await state.update_data(new_discount_type=disc_type, new_discount_plans=[])
    label = "درصد تخفیف را وارد کنید (عددی بین ۱ تا ۱۰۰، مثلاً 20)" if disc_type == "percent" else \
        "مبلغ ثابت تخفیف را به تومان وارد کنید (مثلاً 20000)"
    await callback.message.edit_text(f"🎟 مرحله ۳ از ۶\n\n💯 {label}:")
    await state.set_state(AdminStates.waiting_discount_value_step)
    await callback.answer()


@router.message(AdminStates.waiting_discount_value_step)
async def new_discount_value_input(message: types.Message, state: FSMContext):
    data = await state.get_data()
    disc_type = data.get("new_discount_type", "percent")
    if not message.text or not clean_numeric_id(message.text).isdigit():
        await message.answer("❌ فقط عدد وارد کنید:")
        return
    value = int(clean_numeric_id(message.text))
    if disc_type == "percent" and not (1 <= value <= 100):
        await message.answer("❌ درصد باید بین ۱ تا ۱۰۰ باشد:")
        return
    if disc_type == "amount" and value <= 0:
        await message.answer("❌ مبلغ باید بزرگ‌تر از صفر باشد:")
        return

    await state.update_data(new_discount_value=value)
    await message.answer(
        "🎟 مرحله ۴ از ۶\n\n"
        "🎯 این کد روی کدام پلن‌ها اعمال شود؟ (هرکدام را بزنید تا انتخاب/لغو شود؛ "
        "اگر «همه‌ی پلن‌ها» را بزنید، هیچ محدودیتی نخواهد داشت):",
        reply_markup=discount_plans_select_keyboard([]),
    )


@router.callback_query(F.data.startswith("discplan_"), AdminStates.waiting_discount_value_step)
async def new_discount_plan_toggle(callback: types.CallbackQuery, state: FSMContext):
    key = callback.data.replace("discplan_", "")
    data = await state.get_data()
    selected = data.get("new_discount_plans", [])

    if key == "all":
        selected = []
    elif key == "done":
        await state.update_data(new_discount_plans=selected)
        await callback.message.edit_text(
            "🎟 مرحله ۵ از ۶\n\n"
            "👤 این کد فقط برای چه کاربرانی مجاز باشد؟\n\n"
            "آیدی‌های عددی تلگرام را وارد کنید (هرکدام با کاما، فاصله یا خط جدید جدا شود).\n"
            "اگر می‌خواهید همه‌ی کاربران بتوانند از این کد استفاده کنند، عدد 0 را ارسال کنید."
        )
        await state.set_state(AdminStates.waiting_discount_users_step)
        await callback.answer()
        return
    else:
        selected = [p for p in selected if p != key] if key in selected else selected + [key]

    await state.update_data(new_discount_plans=selected)
    await callback.message.edit_reply_markup(reply_markup=discount_plans_select_keyboard(selected))
    await callback.answer()


@router.message(AdminStates.waiting_discount_users_step)
async def new_discount_users_input(message: types.Message, state: FSMContext):
    raw = (message.text or "").strip()
    ids = [p for p in re.split(r"[\s,،]+", raw) if p]

    if not ids or ids == ["0"]:
        await state.update_data(new_discount_users=None)
    elif all(p.lstrip("-").isdigit() for p in ids):
        await state.update_data(new_discount_users=sorted(set(ids)))
    else:
        await message.answer("❌ فقط آیدی‌های عددی معتبر وارد کنید (یا 0 برای باز کردن برای همه):")
        return

    await message.answer(
        "🎟 مرحله ۶ از ۶\n\n🔁 تعداد دفعات مجاز استفاده از این کد را وارد کنید (مثلاً 50):"
    )
    await state.set_state(AdminStates.waiting_discount_uses_step)


@router.message(AdminStates.waiting_discount_uses_step)
async def new_discount_uses_input(message: types.Message, state: FSMContext):
    if not message.text or not clean_numeric_id(message.text).isdigit() or int(clean_numeric_id(message.text)) <= 0:
        await message.answer("❌ لطفاً یک عدد صحیح مثبت وارد کنید:")
        return

    data = await state.get_data()
    code = data.get("new_discount_code")
    disc_type = data.get("new_discount_type", "percent")
    value = data.get("new_discount_value", 0)
    plans = data.get("new_discount_plans") or None
    allowed_users = data.get("new_discount_users") or None
    uses = int(clean_numeric_id(message.text))

    try:
        db.create_discount(
            code,
            percent=value if disc_type == "percent" else 0,
            uses=uses,
            discount_type=disc_type,
            amount=value if disc_type == "amount" else 0,
            applicable_plans=plans,
            allowed_user_ids=allowed_users,
        )
        value_text = f"{value}٪" if disc_type == "percent" else f"{value:,} تومان"
        all_plans = db.get_all_plans()
        plans_text = "همه‌ی پلن‌ها" if not plans else ", ".join(all_plans.get(p, {}).get("name", p) for p in plans)
        users_text = "همه‌ی کاربران" if not allowed_users else "، ".join(f"`{i}`" for i in allowed_users)
        await message.answer(
            f"✅ کد تخفیف جدید با موفقیت ساخته شد! 🎉\n\n"
            f"🎟 کد: `{code}`\n💯 مقدار: {value_text}\n🎯 پلن‌ها: {plans_text}\n"
            f"👤 کاربران مجاز: {users_text}\n🔁 تعداد استفاده: {uses}",
            parse_mode="Markdown",
            reply_markup=admin_discount_menu(db.get_all_discounts()),
        )
    except Exception:
        await message.answer("❌ این کد قبلاً ثبت شده.", reply_markup=admin_discount_menu(db.get_all_discounts()))
    await state.clear()


# ---------------------------------------------------------------------------
# 🤝 مدیریت دعوت‌ها
# ---------------------------------------------------------------------------
REFERRERS_PER_PAGE = 10


def _referrers_page_text(page: int, total: int) -> str:
    if total == 0:
        return "🤝 هنوز هیچ دعوتی ثبت نشده."
    return (
        f"🤝 مدیریت دعوت‌شده‌ها — مرتب‌شده بر اساس بیشترین دعوت\n\n"
        f"👥 تعداد کل دعوت‌کننده‌ها: {total}\n\n"
        f"روی هر کدام بزنید تا لیست دعوت‌شده‌هایش و وضعیت کیف پولش رو ببینید 👇"
    )


@router.callback_query(F.data == "admin_referrals")
async def admin_referrals(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await _render_referrers_page(callback, 0)
    await callback.answer()


async def _render_referrers_page(callback: types.CallbackQuery, page: int):
    total = db.count_referrers()
    users = db.get_referrers_page(page, REFERRERS_PER_PAGE)
    has_next = total > (page + 1) * REFERRERS_PER_PAGE
    await callback.message.edit_text(
        _referrers_page_text(page, total),
        reply_markup=admin_referrers_page_keyboard(users, page, has_next),
    )


@router.callback_query(F.data.startswith("refpage_"))
async def admin_referrers_page_nav(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    page = int(callback.data.replace("refpage_", ""))
    await _render_referrers_page(callback, page)
    await callback.answer()


@router.callback_query(F.data.startswith("refdetail_"))
async def admin_referrer_detail(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    _, uid, page_str = callback.data.split("_")
    page = int(page_str)

    referrer = db.get_user(uid)
    if referrer is None:
        await callback.answer("❌ کاربر یافت نشد.", show_alert=True)
        return

    invited = db.get_referred_users(referrer["id"])
    text = (
        f"🤝 دعوت‌شده‌های {referrer['name']} (🆔 {referrer['telegram_id']})\n\n"
        f"💛 کیف پول آزاد دعوت‌کننده: {referrer['wallet']:,} تومان\n"
        f"🔒 کیف پول مسدود دعوت‌کننده (در انتظار): {referrer['locked_wallet']:,} تومان\n"
        f"👥 تعداد دعوت: {referrer['invited_count']} | ✅ موفق: {referrer['successful_invites']}\n\n"
        f"📋 لیست افراد دعوت‌شده:\n"
    )
    if not invited:
        text += "— هنوز هیچ کاربری ثبت نشده."
    else:
        for i, u in enumerate(invited, 1):
            reward = u.get("referral_reward") or 0
            status = u.get("referral_status") or "-"
            text += (
                f"{i}. {u['name']} | 🆔 {u['telegram_id']} | "
                f"🎁 پاداش: {reward:,} تومان | وضعیت: {status}\n"
            )

    await callback.message.edit_text(text, reply_markup=admin_referred_detail_keyboard(uid, page))
    await callback.answer()


# ---------------------------------------------------------------------------
# 🤝 نمایندگی — با ثبت آیدی عددی یک فرد، همه‌ی خریدهای VIP بعدی او به‌صورت
# خودکار با درصد تعیین‌شده تخفیف می‌خورد (بدون نیاز به وارد کردن کد تخفیف).
# ---------------------------------------------------------------------------
def _user_detail_text(user: dict) -> str:
    """همان متن استاندارد صفحه‌ی «مدیریت کاربر» (بخش کاربران)؛ برای اینکه صفحه‌ی
    نماینده هم دقیقاً همین محیط را نشان دهد، این تابع مشترک استفاده می‌شود."""
    stats = db.get_referral_stats(user["id"])
    return (
        f"👤 {user['name']}\n"
        f"🆔 {user['telegram_id']}\n\n"
        f"👛 کیف پول آزاد: {user['wallet']:,} تومان\n"
        f"🔒 کیف پول مسدود: {user['locked_wallet']:,} تومان\n"
        f"🛒 کل خرید: {user['total_purchase']:,} تومان\n"
        f"📅 عضویت: {user['joined']}\n\n"
        f"🔗 کد دعوت: {user['invite_code']}\n"
        f"👥 دعوت: {stats['invited_count']} | موفق: {stats['successful_invites']}"
    )


@router.callback_query(F.data == "admin_agency")
async def admin_agency_list(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    agents = db.get_all_agents()
    text = (
        "🤝 هنوز هیچ نماینده‌ای ثبت نشده.\n\nبرای افزودن، دکمه‌ی زیر را بزنید 👇"
        if not agents else
        "🤝 نمایندگان فعلی (تخفیف خودکار روی VIP)\n\nروی هرکدام بزنید تا مثل بخش «کاربران» مدیریتش کنید 👇"
    )

    await callback.message.edit_text(text, reply_markup=admin_agency_menu(agents))
    await callback.answer()


@router.callback_query(F.data == "new_agent")
async def new_agent_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "🤝 افزودن نماینده — مرحله ۱ از ۲\n\n"
        "🆔 آیدی عددی (Telegram ID) فرد را ارسال کنید:",
        reply_markup=admin_back_button(),
    )
    await state.set_state(AdminStates.waiting_agent_id_step)
    await callback.answer()


@router.message(AdminStates.waiting_agent_id_step)
async def new_agent_id_input(message: types.Message, state: FSMContext):
    tid = (message.text or "").strip()
    if not tid.isdigit():
        await message.answer("❌ آیدی عددی نامعتبر است؛ فقط عدد ارسال کنید:")
        return
    await state.update_data(new_agent_id=tid)
    await message.answer(
        f"🧾 مرحله ۲ از ۲\n\n💯 درصد تخفیف VIP برای این نماینده را وارد کنید "
        f"(پیش‌فرض پیشنهادی: {AGENCY_VIP_DISCOUNT_PERCENT}):"
    )
    await state.set_state(AdminStates.waiting_agent_percent_step)


@router.message(AdminStates.waiting_agent_percent_step)
async def new_agent_percent_input(message: types.Message, state: FSMContext):
    if not message.text or not clean_numeric_id(message.text).isdigit() or not (1 <= int(clean_numeric_id(message.text)) <= 100):
        await message.answer("❌ لطفاً یک عدد بین ۱ تا ۱۰۰ وارد کنید:")
        return
    data = await state.get_data()
    tid = data.get("new_agent_id")
    percent = int(clean_numeric_id(message.text))
    db.add_agent(tid, percent)
    # اگر کاربر قبلاً /start زده باشد، همان لحظه پیام خوش‌آمد نمایندگی +
    # دکمه‌ی انتخاب نام دلخواه + منوی Reply مخصوص نماینده را دریافت می‌کند.
    try:
        from aiogram.types import InlineKeyboardMarkup
        welcome_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🏷 انتخاب نام دلخواه برای ساخت کانفیگ", callback_data="agentprefix", style="success")
        ]])
        await message.bot.send_message(
            int(tid),
            f"🎉 تبریک! شما به‌عنوان نماینده ربات ثبت شدید.\n\n💯 تخفیف نمایندگی شما: {percent}٪\n📦 این تخفیف در دسته‌های فعال نمایندگی برای شما اعمال می‌شود.\n\nمی‌توانید نام دلخواه خود را برای ساخت کانفیگ انتخاب کنید.",
            reply_markup=welcome_kb,
        )
        await message.bot.send_message(int(tid), "⬇️ مدیریت نمایندگی از منوی پایین در دسترس است.", reply_markup=main_reply_keyboard(int(tid)))
    except Exception:
        logger.exception("ارسال پیام خوش‌آمد نماینده %s ناموفق بود", tid)
    await message.answer(
        f"✅ نماینده ثبت شد!\n\n🆔 {tid}\n💯 تخفیف VIP: {percent}٪\n\n"
        f"از این به بعد، خریدهای VIP این آیدی به‌صورت خودکار {percent}٪ تخفیف می‌خورد.",
        reply_markup=admin_agency_menu(db.get_all_agents()),
    )
    await state.clear()


@router.callback_query(F.data.startswith("editagentcats_"))
async def admin_agent_categories(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    tid=callback.data.replace("editagentcats_", "")
    if not db.get_agent(tid):
        await callback.answer("❌ نماینده یافت نشد.", show_alert=True); return
    cats=db.get_vip_categories(); selected=set(db.get_agent_category_ids(tid))
    rows=[]
    for c in cats:
        cid=int(c["id"]); mark="✅" if (not selected or cid in selected) else "⬜️"
        rows.append([InlineKeyboardButton(text=f"{mark} {c['name']}", callback_data=f"agentcat_{tid}_{cid}", style="primary")])
    rows.append([InlineKeyboardButton(text="🌐 همه دسته‌ها", callback_data=f"agentcatall_{tid}", style="success")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"agentopen_{tid}", style="danger")])
    await callback.message.edit_text("🗂 دسته‌های مشمول تخفیف نمایندگی\n\nبا زدن روی هر دسته، تخفیف نماینده فقط روی همان دسته اعمال می‌شود. اگر همه را فعال کنی، تخفیف روی تمام دسته‌هاست.", reply_markup=__import__('aiogram').types.InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()

@router.callback_query(F.data.startswith("agentcatall_"))
async def admin_agent_categories_all(callback: types.CallbackQuery):
    tid=callback.data.replace("agentcatall_", "")
    if not _is_admin(callback.from_user.id) or not db.get_agent(tid):
        await callback.answer("❌ نماینده یافت نشد.", show_alert=True); return
    db.set_agent_categories(tid, [])
    await callback.answer("✅ تخفیف روی همه دسته‌ها فعال شد.")
    await admin_agent_categories(callback)

@router.callback_query(F.data.startswith("agentcat_"))
async def admin_agent_category_toggle(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    parts=callback.data.split("_")
    if len(parts)!=3 or not db.get_agent(parts[1]):
        await callback.answer("❌ نماینده یافت نشد.", show_alert=True); return
    tid,cid=parts[1],int(parts[2]); current=set(db.get_agent_category_ids(tid))
    # اگر حالت «همه» فعال بود، اولین انتخاب را به همه دسته‌ها تبدیل می‌کنیم و سپس
    # همان دسته را حذف می‌کنیم تا رفتار toggle قابل‌پیش‌بینی باشد.
    if not current:
        current={int(c["id"]) for c in db.get_vip_categories()}
    if cid in current: current.remove(cid)
    else: current.add(cid)
    db.set_agent_categories(tid, sorted(current))
    await admin_agent_categories(callback)
    await callback.answer("✅ ذخیره شد.")

@router.callback_query(F.data.startswith("editagentprefix_"))
async def admin_agent_prefix_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    tid=callback.data.replace("editagentprefix_", "")
    if not db.get_agent(tid):
        await callback.answer("❌ نماینده یافت نشد.", show_alert=True); return
    await state.update_data(agent_prefix_target=tid)
    await state.set_state(AdminStates.waiting_agent_prefix)
    await callback.message.answer("🏷 پیشوند دلخواه نماینده را بفرست (۱ تا 12 کاراکتر، فقط a-z، عدد و _):")
    await callback.answer()

@router.message(AdminStates.waiting_agent_prefix)
async def admin_agent_prefix_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    value=(message.text or '').strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{1,12}", value):
        await message.answer("❌ پیشوند نامعتبر است. فقط a-z، عدد و _ و حداکثر 12 کاراکتر."); return
    data=await state.get_data(); tid=data.get("agent_prefix_target")
    if not tid or not db.get_agent(tid):
        await state.clear(); await message.answer("❌ نماینده پیدا نشد."); return
    db.set_agent_prefix(tid,value); await state.clear()
    await message.answer(f"✅ پیشوند نماینده ذخیره شد: {value}", reply_markup=admin_agent_actions_keyboard(tid))


@router.callback_query(F.data.startswith("deleteagent_"))
async def delete_agent(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    tid = callback.data.replace("deleteagent_", "")
    db.remove_agent(tid)
    await callback.answer("✅ نماینده حذف شد.")
    await admin_agency_list(callback)


# --- 👤 باز کردن صفحه‌ی یک نماینده — دقیقاً همان صفحه‌ی «مدیریت کاربر»
# (بخش کاربران)، به‌علاوه‌ی دکمه‌ی «💯 تغییر درصد تخفیف نمایندگی» ---
@router.callback_query(F.data.startswith("agentopen_"))
async def admin_agent_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    tid = callback.data.replace("agentopen_", "")
    agent = db.get_agent(tid)
    if agent is None:
        await callback.answer("❌ این نماینده یافت نشد.", show_alert=True)
        return

    user = db.get_user(tid)
    if user is None:
        await callback.message.edit_text(
            f"🤝 نماینده 🆔 {tid} | 💯 {agent['vip_discount_percent']}٪\n\n"
            "⚠️ این آیدی هنوز ربات را /start نزده؛ اطلاعات کاربری‌ای برایش ثبت نشده.",
            reply_markup=admin_agent_actions_keyboard(tid),
        )
        await callback.answer()
        return

    text = _user_detail_text(user) + f"\n\n🤝 درصد تخفیف نمایندگی (VIP): {agent['vip_discount_percent']}٪"
    await callback.message.edit_text(text, reply_markup=admin_agent_actions_keyboard(tid))
    await callback.answer()


@router.callback_query(F.data.startswith("editagentpercent_"))
async def admin_agent_edit_percent_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    tid = callback.data.replace("editagentpercent_", "")
    if db.get_agent(tid) is None:
        await callback.answer("❌ این نماینده یافت نشد.", show_alert=True)
        return
    await state.update_data(edit_agent_id=tid)
    await state.set_state(AdminStates.waiting_agent_edit_percent)
    await callback.message.edit_text("💯 درصد تخفیف جدید (بین ۱ تا ۱۰۰) را ارسال کنید:")
    await callback.answer()


@router.message(AdminStates.waiting_agent_edit_percent)
async def admin_agent_edit_percent_apply(message: types.Message, state: FSMContext):
    if not message.text or not clean_numeric_id(message.text).isdigit() or not (1 <= int(clean_numeric_id(message.text)) <= 100):
        await message.answer("❌ لطفاً یک عدد بین ۱ تا ۱۰۰ وارد کنید:")
        return
    data = await state.get_data()
    tid = data.get("edit_agent_id")
    agent = db.get_agent(tid) if tid else None
    if agent is None:
        await message.answer("❌ مشکلی پیش آمد؛ از ابتدا امتحان کنید.", reply_markup=admin_agency_menu(db.get_all_agents()))
        await state.clear()
        return

    percent = int(clean_numeric_id(message.text))
    db.add_agent(tid, percent, agent.get("note"))
    await message.answer(
        f"✅ درصد تخفیف نماینده به‌روزرسانی شد:\n🆔 {tid}\n💯 {percent}٪",
        reply_markup=admin_agent_actions_keyboard(tid),
    )
    await state.clear()


# ---------------------------------------------------------------------------
# Premium/Custom Emoji for dynamic VIP category/plan names
# ---------------------------------------------------------------------------
def _save_dynamic_premium_emoji(text: str, message: types.Message):
    """Persist the custom emoji attached to a dynamic category/plan name."""
    try:
        for e in serialize_message_entities(getattr(message, "entities", None)):
            if e.get("type") == "custom_emoji" and e.get("custom_emoji_id"):
                db.set_button_custom_emoji_id("text:" + str(text), str(e["custom_emoji_id"]))
                return
        db.set_button_custom_emoji_id("text:" + str(text), None)
    except Exception:
        logger.exception("ذخیره Premium Emoji نام داینامیک ناموفق بود")

# ---------------------------------------------------------------------------
# 🗂 دسته‌بندی‌های VIP — می‌توان هر تعداد دسته و داخل هرکدام هر تعداد پلن اضافه
# کرد؛ همه‌شان خودکار در «🛒 خرید اشتراک → 🚀 سرور VIP» برای کاربر ظاهر می‌شوند.
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_vip_categories")
async def admin_vip_categories_list(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await callback.message.edit_text(
        "🗂 دسته‌بندی‌های VIP\n\n"
        "این دسته‌ها همان چیزی هستند که کاربر موقع «خرید اشتراک → سرور VIP» می‌بیند.\n"
        "برای مدیریت پلن‌های داخل هر دسته، روی آن بزنید 👇",
        reply_markup=admin_vip_categories_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "newvipcat")
async def admin_new_vip_category_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_vip_category_name)
    await callback.message.edit_text("🗂 نام دسته‌بندی جدید را ارسال کنید (مثلاً «💎 حجم بالای ویژه»):")
    await callback.answer()


@router.message(AdminStates.waiting_vip_category_name)
async def admin_new_vip_category_apply(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ نام نمی‌تواند خالی باشد؛ دوباره ارسال کنید:")
        return
    cat = db.create_vip_category(name)
    _save_dynamic_premium_emoji(name, message)
    await message.answer(
        f"✅ دسته‌بندی «{name}» ساخته شد!\n\nحالا می‌توانید از داخل همین دسته، پلن اضافه کنید 👇",
        reply_markup=admin_vip_category_detail_keyboard(cat["key"]),
    )
    await state.clear()


# 🐛 فیکس: این دو هندلر هم حذف شده بودند، در حالی که دکمه‌هایشان
# («✏️ ویرایش توضیح این دسته» و «👥 سقف دستگاه / HWID») همچنان در
# keyboards.py ساخته می‌شدند و کلیک روی‌شان بی‌جواب می‌ماند.
@router.callback_query(F.data.startswith("vipcatdesc_"))
async def admin_vip_category_edit_description(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    category_key = callback.data.replace("vipcatdesc_", "")
    cat = db.get_vip_category(category_key)
    if cat is None:
        await callback.answer("❌ این دسته یافت نشد.", show_alert=True)
        return
    await state.update_data(vip_category_key=category_key)
    await state.set_state(AdminStates.waiting_vip_category_description)
    current = cat.get("description") or "(خالی)"
    await callback.message.answer(
        f"📝 توضیح جدید این دسته را بفرست (این متن بالای دکمه‌های پلن‌های همین دسته نمایش داده می‌شود).\n\n"
        f"توضیح فعلی:\n{current}\n\nبرای حذف توضیح، فقط یک خط تیره «-» بفرست."
    )
    await callback.answer()


@router.message(AdminStates.waiting_vip_category_description)
async def admin_vip_category_save_description(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    category_key = data.get("vip_category_key")
    cat = db.get_vip_category(category_key) if category_key else None
    if cat is None:
        await state.clear()
        await message.answer("❌ این مسیر منقضی شده؛ دوباره از منوی دسته‌بندی‌ها شروع کن.")
        return
    raw = (message.text or "").strip()
    db.update_vip_category_description(cat["id"], None if raw == "-" else raw)
    await state.clear()
    await message.answer(
        "✅ توضیح دسته ذخیره شد." if raw != "-" else "✅ توضیح دسته حذف شد.",
        reply_markup=admin_vip_category_detail_keyboard(category_key),
    )


@router.callback_query(F.data.startswith("vipplanlimit_"))
async def admin_vip_plan_edit_user_limit(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    plan_key = callback.data.replace("vipplanlimit_", "")
    plan = db.get_vip_plan(plan_key)
    if plan is None:
        await callback.answer("❌ این پلن یافت نشد.", show_alert=True)
        return
    await state.update_data(vip_plan_key=plan_key)
    await state.set_state(AdminStates.waiting_vip_plan_edit_user_limit)
    await callback.message.answer(
        f"👥 سقف دستگاه همزمان (HWID) این پلن را به‌صورت عدد بفرست.\n"
        f"عدد ۰ یعنی نامحدود.\n\nمقدار فعلی: {plan.get('user_limit') or 0}"
    )
    await callback.answer()


@router.message(AdminStates.waiting_vip_plan_edit_user_limit)
async def admin_vip_plan_save_user_limit(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    plan_key = data.get("vip_plan_key")
    plan = db.get_vip_plan(plan_key) if plan_key else None
    if plan is None:
        await state.clear()
        await message.answer("❌ این مسیر منقضی شده؛ دوباره از منوی پلن‌ها شروع کن.")
        return
    try:
        limit = int((message.text or "").strip())
        if limit < 0:
            raise ValueError
    except Exception:
        await message.answer("❌ فقط یک عدد صحیح ۰ یا بیشتر بفرست (۰ = نامحدود):")
        return
    db.update_vip_plan(plan_key, user_limit=limit)
    await state.clear()
    await message.answer(
        f"✅ سقف دستگاه این پلن روی {'نامحدود' if limit == 0 else limit} تنظیم شد.",
        reply_markup=admin_vip_plan_detail_keyboard(plan_key),
    )


@router.callback_query(F.data.startswith("admincat_"))
async def admin_vip_category_detail(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    category_key = callback.data.replace("admincat_", "")
    cat = db.get_vip_category(category_key)
    if cat is None:
        await callback.answer("❌ این دسته یافت نشد.", show_alert=True)
        return
    plans = db.get_vip_plans(cat["id"])
    text = f"🗂 {cat['name']}\n\n📦 تعداد پلن: {len(plans)}\n\nبرای مدیریت هر پلن روی آن بزنید 👇"
    await callback.message.edit_text(text, reply_markup=admin_vip_category_detail_keyboard(category_key))
    await callback.answer()


@router.callback_query(F.data.startswith("delvipcat_"))
async def admin_delete_vip_category(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    category_key = callback.data.replace("delvipcat_", "")
    cat = db.get_vip_category(category_key)
    if cat is None:
        await callback.answer("❌ این دسته یافت نشد.", show_alert=True)
        return
    ok = db.delete_vip_category(cat["id"])
    if not ok:
        await callback.answer("❌ این دسته پلن دارد؛ اول همه‌ی پلن‌هایش را حذف کنید.", show_alert=True)
        return
    await callback.answer("✅ دسته حذف شد.")
    await admin_vip_categories_list(callback)


# --- افزودن پلن جدید به یک دسته (۴ مرحله: نام / قیمت / حجم گیگ / مدت روز) ---
@router.callback_query(F.data.startswith("newvipplan_"))
async def admin_new_vip_plan_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    category_key = callback.data.replace("newvipplan_", "")
    if db.get_vip_category(category_key) is None:
        await callback.answer("❌ این دسته یافت نشد.", show_alert=True)
        return
    await state.update_data(new_vip_plan_category=category_key)
    await state.set_state(AdminStates.waiting_vip_plan_name)
    await callback.message.edit_text(
        "📦 افزودن پلن جدید — مرحله ۱ از ۵\n\n✏️ نام پلن را ارسال کنید (مثلاً «۲۰۰ گیگ | کاربر و زمان ∞»):"
    )
    await callback.answer()


@router.message(AdminStates.waiting_vip_plan_name)
async def admin_new_vip_plan_name(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ نام نمی‌تواند خالی باشد؛ دوباره ارسال کنید:")
        return
    _save_dynamic_premium_emoji(name, message)
    await state.update_data(new_vip_plan_name=name)
    await state.set_state(AdminStates.waiting_vip_plan_price)
    await message.answer("📦 مرحله ۲ از ۵\n\n💰 قیمت را به تومان (فقط عدد) ارسال کنید:")


@router.message(AdminStates.waiting_vip_plan_price)
async def admin_new_vip_plan_price(message: types.Message, state: FSMContext):
    if not message.text or not clean_numeric_id(message.text).isdigit():
        await message.answer("❌ فقط عدد ارسال کنید:")
        return
    await state.update_data(new_vip_plan_price=int(clean_numeric_id(message.text)))
    await state.set_state(AdminStates.waiting_vip_plan_gb)
    await message.answer(
        "📦 مرحله ۳ از ۵\n\n🗜 حجم را به گیگابایت ارسال کنید (اگر نامحدود است، عدد 0 را بفرستید):"
    )


@router.message(AdminStates.waiting_vip_plan_gb)
async def admin_new_vip_plan_gb(message: types.Message, state: FSMContext):
    if not message.text or not clean_numeric_id(message.text).isdigit():
        await message.answer("❌ فقط عدد ارسال کنید (برای نامحدود، 0):")
        return
    await state.update_data(new_vip_plan_gb=int(clean_numeric_id(message.text)))
    await state.set_state(AdminStates.waiting_vip_plan_days)
    await message.answer("📦 مرحله ۴ از ۵\n\n⏳ مدت را به روز ارسال کنید (اگر نامحدود است، عدد 0 را بفرستید):")


@router.message(AdminStates.waiting_vip_plan_days)
async def admin_new_vip_plan_days(message: types.Message, state: FSMContext):
    if not message.text or not clean_numeric_id(message.text).isdigit():
        await message.answer("❌ فقط عدد ارسال کنید (برای نامحدود، 0):")
        return
    await state.update_data(new_vip_plan_days=int(clean_numeric_id(message.text)))
    await state.set_state(AdminStates.waiting_vip_plan_userlimit)
    await message.answer(
        "📦 مرحله ۵ از ۵\n\n"
        "👥 سقف کاربر همزمان (HWID Limit) را به عدد ارسال کنید (۰ تا ۱۰، ۰ = نامحدود):"
    )


@router.message(AdminStates.waiting_vip_plan_userlimit)
async def admin_new_vip_plan_userlimit(message: types.Message, state: FSMContext):
    user_limit = parse_int_in_range((message.text or "").strip(), 0, 10)
    if user_limit is None:
        await message.answer("❌ عددی بین ۰ تا ۱۰ ارسال کنید (۰ = نامحدود):")
        return
    data = await state.get_data()
    category_key = data.get("new_vip_plan_category")
    cat = db.get_vip_category(category_key)
    if cat is None:
        await message.answer("❌ مشکلی پیش آمد؛ از ابتدا امتحان کنید.")
        await state.clear()
        return

    days = data.get("new_vip_plan_days", 0)
    name = data.get("new_vip_plan_name")
    price = data.get("new_vip_plan_price")
    volume_gb = data.get("new_vip_plan_gb", 0)

    plan_key = db.add_vip_plan(cat["id"], name, price, days=days, volume_gb=volume_gb, user_limit=user_limit)
    # Emoji was captured from the name message; store it under the stable dynamic-name key.
    # (Calling again is harmless and keeps the flow resilient to FSM serialization.)
    await message.answer(
        f"✅ پلن جدید اضافه شد! 🎉\n\n📦 {name}\n💰 {price:,} تومان\n🗜 "
        f"{volume_gb if volume_gb else 'نامحدود'} گیگ\n⏳ {days if days else 'نامحدود'} روز\n👥 {'نامحدود' if user_limit == 0 else f'{user_limit} کاربر'} همزمان",
        reply_markup=admin_vip_category_detail_keyboard(category_key),
    )
    await state.clear()


# --- مشاهده/ویرایش/حذف یک پلن مشخص ---
@router.callback_query(F.data.startswith("vipplan_"))
async def admin_vip_plan_detail(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    plan_key = callback.data.replace("vipplan_", "")
    plan = db.get_vip_plan(plan_key)
    if plan is None:
        await callback.answer("❌ این پلن یافت نشد.", show_alert=True)
        return
    cat = db.get_vip_category(plan["category_id"])
    text = (
        f"📦 {plan['name']}\n\n"
        f"💰 قیمت: {plan['price']:,} تومان\n"
        f"🗜 حجم: {plan['volume_gb'] if plan['volume_gb'] else 'نامحدود'} گیگ\n"
        f"⏳ مدت: {plan['days'] if plan['days'] else 'نامحدود'} روز\n"
        f"👥 سقف کاربر: {'نامحدود' if plan.get('user_limit', 0) == 0 else str(plan['user_limit']) + ' کاربر'}\n"
        f"🗂 دسته: {cat['name'] if cat else '-'}"
    )
    await callback.message.edit_text(
        text, reply_markup=admin_vip_plan_detail_keyboard(plan_key, cat["key"] if cat else "")
    )
    await callback.answer()


def _vip_plan_edit_starter(field_state, prompt: str, prefix: str):
    async def handler(callback: types.CallbackQuery, state: FSMContext):
        if not _is_admin(callback.from_user.id):
            await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
            return
        plan_key = callback.data.replace(prefix, "")
        if db.get_vip_plan(plan_key) is None:
            await callback.answer("❌ این پلن یافت نشد.", show_alert=True)
            return
        await state.update_data(edit_vip_plan_key=plan_key)
        await state.set_state(field_state)
        await callback.message.edit_text(prompt)
        await callback.answer()
    return handler


router.callback_query(F.data.startswith("vipplanname_"))(
    _vip_plan_edit_starter(AdminStates.waiting_vip_plan_edit_name, "✏️ نام جدید پلن را ارسال کنید:", "vipplanname_")
)
router.callback_query(F.data.startswith("vipplanprice_"))(
    _vip_plan_edit_starter(AdminStates.waiting_vip_plan_edit_price, "💰 قیمت جدید را به تومان (فقط عدد) ارسال کنید:", "vipplanprice_")
)
router.callback_query(F.data.startswith("vipplangb_"))(
    _vip_plan_edit_starter(AdminStates.waiting_vip_plan_edit_gb, "🗜 حجم جدید را به گیگ (فقط عدد، 0 = نامحدود) ارسال کنید:", "vipplangb_")
)
router.callback_query(F.data.startswith("vipplandays_"))(
    _vip_plan_edit_starter(AdminStates.waiting_vip_plan_edit_days, "⏳ مدت جدید را به روز (فقط عدد، 0 = نامحدود) ارسال کنید:", "vipplandays_")
)
router.callback_query(F.data.startswith("vipplanuserlimit_"))(
    _vip_plan_edit_starter(
        AdminStates.waiting_vip_plan_edit_userlimit,
        "👥 سقف کاربر همزمان جدید را ارسال کنید (۰ تا ۱۰، ۰ = نامحدود):",
        "vipplanuserlimit_"
    )
)


async def _after_vip_plan_edit(message: types.Message, state: FSMContext, plan_key: str, success_text: str):
    plan = db.get_vip_plan(plan_key)
    cat = db.get_vip_category(plan["category_id"]) if plan else None
    await message.answer(
        success_text,
        reply_markup=admin_vip_plan_detail_keyboard(plan_key, cat["key"] if cat else ""),
    )
    await state.clear()


@router.message(AdminStates.waiting_vip_plan_edit_name)
async def admin_vip_plan_edit_name_apply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    plan_key = data.get("edit_vip_plan_key")
    new_name = (message.text or "").strip()
    if not plan_key or not new_name:
        await message.answer("❌ متن نامعتبر است؛ دوباره ارسال کنید:")
        return
    old_plan = db.get_vip_plan(plan_key)
    old_name = (old_plan or {}).get("name")
    db.update_vip_plan(plan_key, name=new_name)
    if old_name and old_name != new_name:
        db.set_button_custom_emoji_id("text:" + str(old_name), None)
    _save_dynamic_premium_emoji(new_name, message)
    await _after_vip_plan_edit(message, state, plan_key, f"✅ نام پلن به‌روزرسانی شد:\n📦 {new_name}")


@router.message(AdminStates.waiting_vip_plan_edit_price)
async def admin_vip_plan_edit_price_apply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    plan_key = data.get("edit_vip_plan_key")
    if not message.text or not clean_numeric_id(message.text).isdigit():
        await message.answer("❌ فقط عدد ارسال کنید:")
        return
    price = int(clean_numeric_id(message.text))
    db.update_vip_plan(plan_key, price=price)
    await _after_vip_plan_edit(message, state, plan_key, f"✅ قیمت پلن به‌روزرسانی شد:\n💰 {price:,} تومان")


@router.message(AdminStates.waiting_vip_plan_edit_gb)
async def admin_vip_plan_edit_gb_apply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    plan_key = data.get("edit_vip_plan_key")
    if not message.text or not clean_numeric_id(message.text).isdigit():
        await message.answer("❌ فقط عدد ارسال کنید (0 = نامحدود):")
        return
    volume_gb = int(clean_numeric_id(message.text))
    db.update_vip_plan(plan_key, volume_gb=volume_gb)
    await _after_vip_plan_edit(
        message, state, plan_key, f"✅ حجم پلن به‌روزرسانی شد:\n🗜 {volume_gb if volume_gb else 'نامحدود'} گیگ"
    )


@router.message(AdminStates.waiting_vip_plan_edit_days)
async def admin_vip_plan_edit_days_apply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    plan_key = data.get("edit_vip_plan_key")
    if not message.text or not clean_numeric_id(message.text).isdigit():
        await message.answer("❌ فقط عدد ارسال کنید (0 = نامحدود):")
        return
    days = int(clean_numeric_id(message.text))
    db.update_vip_plan(plan_key, days=days)
    await _after_vip_plan_edit(
        message, state, plan_key, f"✅ مدت پلن به‌روزرسانی شد:\n⏳ {days if days else 'نامحدود'} روز"
    )


@router.message(AdminStates.waiting_vip_plan_edit_userlimit)
async def admin_vip_plan_edit_userlimit_apply(message: types.Message, state: FSMContext):
    data = await state.get_data()
    plan_key = data.get("edit_vip_plan_key")
    user_limit = parse_int_in_range((message.text or "").strip(), 0, 10)
    if user_limit is None:
        await message.answer("❌ عددی بین ۰ تا ۱۰ ارسال کنید (۰ = نامحدود):")
        return
    db.update_vip_plan(plan_key, user_limit=user_limit)
    await _after_vip_plan_edit(
        message, state, plan_key,
        f"✅ سقف کاربر همزمان پلن به‌روزرسانی شد:\n👥 {'نامحدود' if user_limit == 0 else f'{user_limit} کاربر'}"
    )


@router.callback_query(F.data.startswith("delvipplan_"))
async def admin_delete_vip_plan(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    plan_key = callback.data.replace("delvipplan_", "")
    plan = db.get_vip_plan(plan_key)
    if plan is None:
        await callback.answer("❌ این پلن یافت نشد.", show_alert=True)
        return
    cat = db.get_vip_category(plan["category_id"])
    db.delete_vip_plan(plan_key)
    await callback.answer("✅ پلن حذف شد.")
    if cat:
        callback.data = f"admincat_{cat['key']}"
        await admin_vip_category_detail(callback)
    else:
        await admin_vip_categories_list(callback)


# --- ↕️ تغییر ترتیب دسته‌بندی‌ها/پلن‌های VIP ---
@router.callback_query(F.data.startswith("movevipcat_"))
async def admin_move_vip_category(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    raw = callback.data.replace("movevipcat_", "")
    category_key, _, direction = raw.rpartition("_")
    cat = db.get_vip_category(category_key)
    if cat is None:
        await callback.answer("❌ این دسته یافت نشد.", show_alert=True)
        return
    db.move_vip_category(cat["id"], direction)
    await callback.answer()
    await admin_vip_categories_list(callback)


@router.callback_query(F.data.startswith("movevipplan_"))
async def admin_move_vip_plan(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    raw = callback.data.replace("movevipplan_", "")
    plan_key, _, direction = raw.rpartition("_")
    plan = db.get_vip_plan(plan_key)
    if plan is None:
        await callback.answer("❌ این پلن یافت نشد.", show_alert=True)
        return
    db.move_vip_plan(plan["id"], direction)
    await callback.answer()
    cat = db.get_vip_category(plan["category_id"])
    if cat:
        callback.data = f"admincat_{cat['key']}"
        await admin_vip_category_detail(callback)


# 📢 پیام همگانی
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    # 🆕 بند ۱۸: اول مخاطب انتخاب می‌شود، بعد متن پیام گرفته می‌شود.
    await callback.message.edit_text(
        "📢 پیام همگانی\n\nاین پیام برای چه کسانی ارسال شود؟",
        reply_markup=admin_broadcast_audience_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("bcaud_"))
async def admin_broadcast_pick_audience(callback: types.CallbackQuery, state: FSMContext):
    if not _admin_perm(callback.from_user.id, "broadcast"):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    audience = callback.data.replace("bcaud_", "")
    if audience not in ("all", "buyers", "non_buyers"):
        await callback.answer("❌ گزینه نامعتبر.", show_alert=True)
        return
    label = {"all": "همه‌ی کاربران", "buyers": "فقط کسانی که خرید کرده‌اند", "non_buyers": "فقط کسانی که خرید نکرده‌اند"}[audience]
    count = len(db.get_broadcastable_users(audience))
    await state.update_data(broadcast_audience=audience)
    await state.set_state(UserStates.waiting_broadcast)
    await callback.message.edit_text(
        f"📢 مخاطب: {label} ({count:,} نفر)\n\n"
        "حالا پیامی که می‌خواهید ارسال شود را بفرستید.\n"
        "هر نوع پیامی پشتیبانی می‌شود (متن، عکس، فیلم، پیام فوروارد‌شده از کانال) و "
        "دقیقاً با همان قالب اصلی خودش — شامل ایموجی پرمیوم و دکمه‌های زیرش — ارسال می‌شود.",
        reply_markup=admin_back_button(),
    )
    await callback.answer()


async def _send_broadcast_rich(bot, target_chat_id: int, source: types.Message):
    """پیام همگانی را با مسیر nativeِ Telegram کپی می‌کند.

    مهم: برای حفظ ۱۰۰٪ فرمت پیام، مخصوصاً Custom/Premium Emoji و دکمه‌های
    Inline، اول از copyMessage خود Telegram استفاده می‌کنیم. بازسازی دستی
    متن + entities برای پیام‌های فورواردشده از کانال می‌تواند در بعضی موارد
    باعث از دست رفتن Custom Emoji شود. اگر copyMessage به هر دلیل برای یک
    نوع پیام قابل استفاده نباشد، مسیر مستقیم قبلی به‌عنوان fallback اجرا می‌شود.
    """
    try:
        # copyMessage خود Telegram تمام entityهای اصلی (از جمله custom_emoji)
        # و محتوای رسانه را حفظ می‌کند. reply_markup را هم صریحاً منتقل می‌کنیم
        # تا دکمه‌های Inline پیام هم باقی بمانند.
        return await bot.copy_message(
            chat_id=int(target_chat_id),
            from_chat_id=source.chat.id,
            message_id=source.message_id,
            reply_markup=source.reply_markup,
        )
    except Exception:
        # بعضی پیام‌های خاص/محافظت‌شده ممکن است قابل copy نباشند؛ در این حالت
        # همان مسیر entity-based قبلی را به‌عنوان fallback نگه می‌داریم.
        pass

    markup = source.reply_markup
    if source.text is not None:
        return await bot.send_message(
            int(target_chat_id), source.text, entities=source.entities or None,
            parse_mode=None, reply_markup=markup, _skip_auto_text=True,
        )
    if source.photo:
        return await bot.send_photo(
            int(target_chat_id), source.photo[-1].file_id, caption=source.caption,
            caption_entities=source.caption_entities or None, parse_mode=None,
            reply_markup=markup,
        )
    if source.video:
        return await bot.send_video(
            int(target_chat_id), source.video.file_id, caption=source.caption,
            caption_entities=source.caption_entities or None, parse_mode=None,
            reply_markup=markup,
        )
    if source.animation:
        return await bot.send_animation(
            int(target_chat_id), source.animation.file_id, caption=source.caption,
            caption_entities=source.caption_entities or None, parse_mode=None,
            reply_markup=markup,
        )
    if source.document:
        return await bot.send_document(
            int(target_chat_id), source.document.file_id, caption=source.caption,
            caption_entities=source.caption_entities or None, parse_mode=None,
            reply_markup=markup,
        )
    if source.audio:
        return await bot.send_audio(
            int(target_chat_id), source.audio.file_id, caption=source.caption,
            caption_entities=source.caption_entities or None, parse_mode=None,
            reply_markup=markup,
        )
    if source.voice:
        return await bot.send_voice(
            int(target_chat_id), source.voice.file_id, caption=source.caption,
            caption_entities=source.caption_entities or None, parse_mode=None,
            reply_markup=markup,
        )
    if source.sticker:
        return await bot.send_sticker(int(target_chat_id), source.sticker.file_id, reply_markup=markup)
    return await bot.copy_message(
        chat_id=int(target_chat_id),
        from_chat_id=source.chat.id,
        message_id=source.message_id,
        reply_markup=markup,
    )


@router.message(UserStates.waiting_broadcast)
async def admin_broadcast_send(message: types.Message, state: FSMContext):
    # 🐛 فیکس: قبلاً این هندلر فقط برای ADMIN_ID (ادمین اصلی) فیلتر شده بود، در حالی که
    # دکمه‌ی «📢 پیام همگانی» (چه از منوی پایین صفحه، چه از پنل ادمین) به ادمین‌های فرعی
    # دارای مجوز broadcast هم اجازه‌ی ورود به همین state را می‌داد. در نتیجه وقتی ادمین
    # فرعی متن پیام همگانی را ارسال می‌کرد، هیچ هندلری آن پیام را نمی‌گرفت (فیلتر رد می‌شد)
    # و کاربر بدون هیچ پاسخی می‌ماند. حالا مجوز را دوباره همینجا (به‌جای فیلتر ADMIN_ID) چک می‌کنیم.
    if not _admin_perm(message.from_user.id, "broadcast"):
        return

    data = await state.get_data()
    audience = data.get("broadcast_audience", "all")

    # 📨 ریچ‌مسیج (بند ۵): copy_message پیام را عیناً همان‌طور که هست کپی
    # می‌کند — متن، فرمت‌بندی، ایموجی پرمیوم و حتی دکمه‌های شیشه‌ای زیرش —
    # بدون هدر «فورواردشده از». این دقیقاً همان رفتاری است که قبلاً داشتید و
    # اینجا دست‌نخورده حفظ شده؛ فقط انتخاب مخاطب به آن اضافه شده است.
    # همچنین get_broadcastable_users کسانی که ربات را بلاک کرده‌اند از قبل حذف
    # می‌کند تا وقت ارسال روی مقصدهای قطعاً شکست‌خورده تلف نشود.
    users = db.get_broadcastable_users(audience)
    audience_label = {"all": "همه", "buyers": "خریداران", "non_buyers": "بدون خرید"}[audience]

    status_msg = await message.answer(f"📢 در حال ارسال به {len(users):,} کاربر ({audience_label})...")

    # 🚀 بهینه‌سازی سرعت: به‌جای ارسال یکی‌یکی و توقف ثابت بین هر پیام (که برای یک گروه بزرگ کاربر خیلی کند می‌شود)، حالا تا سقف مجاز تلگرام (حدوداً ۲۵-۳۰ پیام در ثانیه) به‌صورت هم‌زمان ارسال می‌شوند (دهها برابر سریع‌تر از حالت قبلی که یکی‌یکی با تاخیر ۵۰ میلی‌ثانیه ارسال می‌شد).
    semaphore = asyncio.Semaphore(25)
    lock = asyncio.Lock()
    counters = {"sent": 0, "failed": 0, "done": 0}

    async def _send_one(u):
        async with semaphore:
            try:
                await _send_broadcast_rich(message.bot, int(u["telegram_id"]), message)
                ok = True
            except TelegramForbiddenError:
                # کاربر ربات را بلاک کرده؛ علامت بزن تا دفعه‌ی بعد اصلاً امتحان نشود.
                try:
                    db.mark_bot_blocked(u["telegram_id"])
                except Exception:
                    pass
                ok = False
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 0.5)
                try:
                    await _send_broadcast_rich(message.bot, int(u["telegram_id"]), message)
                    ok = True
                except Exception:
                    ok = False
            except Exception:
                ok = False
        async with lock:
            counters["done"] += 1
            if ok:
                counters["sent"] += 1
            else:
                counters["failed"] += 1
            if counters["done"] % 200 == 0:
                try:
                    await status_msg.edit_text(
                        f"📢 در حال ارسال... ({counters['done']}/{len(users)}) ✅ {counters['sent']} | ❌ {counters['failed']}"
                    )
                except Exception:
                    pass

    await asyncio.gather(*(_send_one(u) for u in users))
    sent, failed = counters["sent"], counters["failed"]

    await status_msg.edit_text(f"✅ ارسال شد به {sent:,} نفر ({audience_label}). ناموفق: {failed:,} نفر.")
    await state.clear()


# ---------------------------------------------------------------------------
# 🎁 هدیه همگانی (بند ۴) — افزودن یک مبلغ به کیف‌پول همه‌ی کاربران با یک کلیک
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_gift_all")
async def admin_gift_all_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_gift_all_amount)
    await callback.message.answer(
        "🎁 هدیه همگانی\n\nمبلغی که می‌خواهی به کیف پول *همه‌ی* کاربران اضافه شود را به تومان بفرست:",
        parse_mode="Markdown",
    )
    await callback.answer()


@router.message(AdminStates.waiting_gift_all_amount)
async def admin_gift_all_amount(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    try:
        amount = int((message.text or "").replace(",", "").replace("٬", "").strip())
        if amount <= 0:
            raise ValueError
    except Exception:
        await message.answer("❌ فقط یک عدد مثبت بفرست:")
        return
    await state.update_data(gift_amount=amount)
    await state.clear()
    count = db.count_unblocked_users()
    await message.answer(
        f"⚠️ مطمئنی می‌خوای {amount:,} تومان به کیف پول {count:,} کاربر اضافه بشه؟\n\n"
        "این کار برگشت‌پذیر نیست.",
        reply_markup=admin_gift_all_confirm_keyboard(amount),
    )


@router.callback_query(F.data.startswith("giftall_do_"))
async def admin_gift_all_confirm(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    amount = int(callback.data.replace("giftall_do_", ""))
    await callback.message.edit_text("⏳ در حال واریز هدیه به کیف پول کاربران...")
    try:
        count = db.gift_all_users(amount)
    except Exception:
        logger.exception("هدیه همگانی ناموفق بود")
        await callback.message.edit_text("❌ واریز هدیه ناموفق بود؛ هیچ مبلغی واریز نشد.")
        await callback.answer()
        return
    await callback.message.edit_text(f"✅ {amount:,} تومان به کیف پول {count:,} کاربر اضافه شد.\n\n📨 در حال ارسال اطلاع‌رسانی به کاربران...")

    # 🔔 اطلاع‌رسانی شخصی به هر کاربری که هدیه دریافت کرده است.
    # هدیه در DB برای تمام کاربران غیرمسدود ثبت می‌شود؛ برای ارسال پیام فقط
    # کاربرانی را می‌گیریم که ربات را بلاک نکرده‌اند، چون برای بقیه ارسال ممکن نیست.
    gift_users = db.get_broadcastable_users("all")
    notify_semaphore = asyncio.Semaphore(25)
    notify_lock = asyncio.Lock()
    notify_counters = {"sent": 0, "failed": 0, "done": 0}

    async def _notify_gift_user(user):
        async with notify_semaphore:
            try:
                await callback.bot.send_message(
                    int(user["telegram_id"]),
                    f"💰 کیف پول شما به مبلغ {amount:,} تومان از سوی ادمین شارژ شد.",
                    parse_mode=None,
                )
                ok = True
            except TelegramForbiddenError:
                try:
                    db.mark_bot_blocked(user["telegram_id"])
                except Exception:
                    pass
                ok = False
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 0.5)
                try:
                    await callback.bot.send_message(
                        int(user["telegram_id"]),
                        f"💰 کیف پول شما به مبلغ {amount:,} تومان از سوی ادمین شارژ شد.",
                        parse_mode=None,
                    )
                    ok = True
                except Exception:
                    ok = False
            except Exception:
                ok = False

        async with notify_lock:
            notify_counters["done"] += 1
            if ok:
                notify_counters["sent"] += 1
            else:
                notify_counters["failed"] += 1

    await asyncio.gather(*(_notify_gift_user(u) for u in gift_users))
    await callback.message.edit_text(
        f"✅ {amount:,} تومان به کیف پول {count:,} کاربر اضافه شد.\n"
        f"📨 اطلاع‌رسانی ارسال شد: {notify_counters['sent']:,} نفر | "
        f"❌ ناموفق: {notify_counters['failed']:,} نفر"
    )
    await _notify_main_admin_action(callback.bot, callback.from_user, "هدیه همگانی", str(amount), f"{count} کاربر")
    await callback.answer("🎁 انجام شد.")


# ---------------------------------------------------------------------------
# 📚 مدیریت راهنما و اموزش‌ها — افزودن/ویرایش/حذف/تغییر ترتیب (متن/عکس/فیلم)
# ---------------------------------------------------------------------------
def _guide_detail_text_and_entities(guide: dict):
    title = str(guide.get("title") or "")
    body = str(guide.get("body_text") or "")
    prefix = f"📚 {title}"
    text = prefix + (f"\n\n{body}" if body else "")
    entities = []

    try:
        emoji_id = db.get_button_custom_emoji_id("text:" + title)
        if emoji_id:
            import unicodedata
            span = 0
            j = 0
            while j < len(title):
                ch = title[j]; cp = ord(ch)
                if j == 0 or cp in (0xFE0E, 0xFE0F, 0x200D, 0x20E3) or unicodedata.category(ch) in {"So", "Sk", "Sc"}:
                    span += len(ch.encode("utf-16-le")) // 2; j += 1; continue
                break
            if span:
                entities.append(types.MessageEntity(type="custom_emoji", offset=3, length=span, custom_emoji_id=str(emoji_id)))
    except Exception:
        logger.exception("بازسازی Premium Emoji عنوان راهنما ناموفق بود")

    if body:
        shift = telegram_utf16_length(prefix + "\n\n")
        for entity in message_entities_from_dicts(guide.get("body_entities") or []):
            try:
                entities.append(entity.model_copy(update={"offset": int(entity.offset) + shift}))
            except Exception:
                pass
    return text, entities


async def _send_guide_detail(target: types.Message, guide_id: int):
    guide = db.get_guide(guide_id)
    if guide is None:
        await target.answer("❌ این راهنما دیگر موجود نیست.")
        return
    guides = db.get_guides()
    idx = next((i for i, g in enumerate(guides) if g["id"] == guide_id), 0)
    caption, entities = _guide_detail_text_and_entities(guide)
    kb = admin_guide_detail_keyboard(guide_id, idx, len(guides))
    try:
        if guide["content_type"] == "photo" and guide.get("file_id"):
            await target.answer_photo(guide["file_id"], caption=caption, caption_entities=entities or None, reply_markup=kb)
        elif guide["content_type"] == "video" and guide.get("file_id"):
            await target.answer_video(guide["file_id"], caption=caption, caption_entities=entities or None, reply_markup=kb)
        else:
            await target.answer(caption, entities=entities or None, parse_mode=None, reply_markup=kb)
    except Exception:
        await target.answer(caption, entities=entities or None, parse_mode=None, reply_markup=kb)

@router.callback_query(F.data == "admin_guides")
async def admin_guides_list(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    guides = db.get_guides()
    text = (
        f"📚 مدیریت راهنما و اموزش‌ها\n\nتعداد: {len(guides)}\n\n"
        "از اینجا می‌تونید راهنما/آموزش جدید اضافه کنید یا موردهای موجود را ویرایش کنید:"
    )
    try:
        await callback.message.edit_text(text, reply_markup=admin_guides_menu(guides))
    except Exception:
        await callback.message.answer(text, reply_markup=admin_guides_menu(guides))
    await callback.answer()


@router.callback_query(F.data == "guidenew")
async def admin_guide_new_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_guide_title)
    await callback.message.answer(
        "📚 عنوان راهنما/آموزش جدید را بفرستید:",
        reply_markup=admin_guide_cancel_keyboard(),
    )
    await callback.answer()


@router.message(AdminStates.waiting_guide_title)
async def admin_guide_new_title(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    title = (message.text or "").strip()
    if not title:
        await message.answer("❌ عنوان نمی‌تواند خالی باشد. دوباره بفرستید:")
        return
    # Premium/Custom Emoji انتخاب‌شده برای عنوان راهنما را به نام داینامیک متصل می‌کنیم
    # تا همان Emoji در دکمه‌ی راهنما نمایش داده شود و Emoji پیش‌فرضی وجود نداشته باشد.
    _save_dynamic_premium_emoji(title, message)
    await state.update_data(guide_new_title=title)
    await state.set_state(AdminStates.waiting_guide_content)
    await message.answer(
        "📝 حالا محتوای این راهنما را بفرستید (متن، عکس با کپشن، یا فیلم با کپشن):",
        reply_markup=admin_guide_cancel_keyboard(),
    )


@router.message(AdminStates.waiting_guide_content)
async def admin_guide_new_content(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    title = data.get("guide_new_title")
    if not title:
        await state.clear()
        return

    if message.photo:
        content_type, file_id, body_text = "photo", message.photo[-1].file_id, (message.caption or "")
        body_entities = serialize_message_entities(getattr(message, "caption_entities", None))
    elif message.video:
        content_type, file_id, body_text = "video", message.video.file_id, (message.caption or "")
        body_entities = serialize_message_entities(getattr(message, "caption_entities", None))
    else:
        content_type, file_id, body_text = "text", None, (message.text or "")
        body_entities = serialize_message_entities(getattr(message, "entities", None))

    if not body_text and not file_id:
        await message.answer("❌ محتوا نمی‌تواند خالی باشد. دوباره بفرستید:")
        return

    guide = db.create_guide(
        title=title, content_type=content_type, body_text=body_text or None,
        file_id=file_id, body_entities=body_entities,
    )
    await state.clear()
    await message.answer(f"✅ راهنمای «{title}» اضافه شد.")
    await _send_guide_detail(message, guide["id"])


@router.callback_query(F.data.startswith("guideadminopen_"))
async def admin_guide_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    guide_id = int(callback.data.replace("guideadminopen_", ""))
    await _send_guide_detail(callback.message, guide_id)
    await callback.answer()


@router.callback_query(F.data.startswith("guidemove_"))
async def admin_guide_move(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    raw = callback.data.replace("guidemove_", "")
    guide_id_str, _, direction = raw.rpartition("_")
    guide_id = int(guide_id_str)
    db.move_guide(guide_id, direction)
    await _send_guide_detail(callback.message, guide_id)
    await callback.answer("↕️ ترتیب به‌روزرسانی شد.")


@router.callback_query(F.data.startswith("guideeditname_"))
async def admin_guide_edit_name_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    guide_id = int(callback.data.replace("guideeditname_", ""))
    guide = db.get_guide(guide_id)
    if guide is None:
        await callback.answer("❌ این راهنما دیگر موجود نیست.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_guide_edit_title)
    await state.update_data(guide_edit_id=guide_id)
    await callback.message.answer(
        f"✏️ عنوان جدید برای «{guide['title']}» را بفرستید:",
        reply_markup=admin_guide_cancel_keyboard(),
    )
    await callback.answer()


@router.message(AdminStates.waiting_guide_edit_title)
async def admin_guide_edit_name_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    guide_id = data.get("guide_edit_id")
    if not guide_id:
        await state.clear()
        return
    title = (message.text or "").strip()
    if not title:
        await message.answer("❌ عنوان نمی‌تواند خالی باشد. دوباره بفرستید:")
        return
    old_guide = db.get_guide(guide_id)
    old_title = (old_guide or {}).get("title")
    if old_title and old_title != title:
        db.set_button_custom_emoji_id("text:" + str(old_title), None)
    _save_dynamic_premium_emoji(title, message)
    db.update_guide(guide_id, title=title)
    await state.clear()
    await message.answer("✅ عنوان به‌روزرسانی شد.")
    await _send_guide_detail(message, guide_id)


@router.callback_query(F.data.startswith("guideeditcontent_"))
async def admin_guide_edit_content_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    guide_id = int(callback.data.replace("guideeditcontent_", ""))
    guide = db.get_guide(guide_id)
    if guide is None:
        await callback.answer("❌ این راهنما دیگر موجود نیست.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_guide_edit_content)
    await state.update_data(guide_edit_id=guide_id)
    await callback.message.answer(
        f"📝 محتوای جدید برای «{guide['title']}» را بفرستید (متن، عکس با کپشن، یا فیلم با کپشن):",
        reply_markup=admin_guide_cancel_keyboard(),
    )
    await callback.answer()


@router.message(AdminStates.waiting_guide_edit_content)
async def admin_guide_edit_content_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    guide_id = data.get("guide_edit_id")
    if not guide_id:
        await state.clear()
        return

    if message.photo:
        content_type, file_id, body_text = "photo", message.photo[-1].file_id, (message.caption or "")
        body_entities = serialize_message_entities(getattr(message, "caption_entities", None))
    elif message.video:
        content_type, file_id, body_text = "video", message.video.file_id, (message.caption or "")
        body_entities = serialize_message_entities(getattr(message, "caption_entities", None))
    else:
        content_type, file_id, body_text = "text", None, (message.text or "")
        body_entities = serialize_message_entities(getattr(message, "entities", None))

    if not body_text and not file_id:
        await message.answer("❌ محتوا نمی‌تواند خالی باشد. دوباره بفرستید:")
        return

    db.update_guide(
        guide_id, content_type=content_type, body_text=body_text, file_id=file_id,
        body_entities=body_entities,
    )
    await state.clear()
    await message.answer("✅ محتوا به‌روزرسانی شد.")
    await _send_guide_detail(message, guide_id)


@router.callback_query(F.data.startswith("guidedelete_"))
async def admin_guide_delete_confirm(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    guide_id = int(callback.data.replace("guidedelete_", ""))
    guide = db.get_guide(guide_id)
    if guide is None:
        await callback.answer("❌ این راهنما دیگر موجود نیست.", show_alert=True)
        return
    await callback.message.answer(
        f"❗️ آیا از حذف «{guide['title']}» مطمئن هستید؟",
        reply_markup=admin_guide_delete_confirm_keyboard(guide_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("guidedeleteconfirm_"))
async def admin_guide_delete_do(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    guide_id = int(callback.data.replace("guidedeleteconfirm_", ""))
    guide = db.get_guide(guide_id)
    title = guide["title"] if guide else ""
    db.delete_guide(guide_id)
    guides = db.get_guides()
    await callback.message.answer(
        f"✅ راهنمای «{title}» حذف شد.\n\n📚 مدیریت راهنما و اموزش‌ها\n\nتعداد: {len(guides)}",
        reply_markup=admin_guides_menu(guides),
    )
    await callback.answer("🗑 حذف شد.")


# ---------------------------------------------------------------------------
# 🎬 مدیریت استیکر/ویدیوی تستی هر بخش از منو
# ---------------------------------------------------------------------------
_STICKER_SECTION_ORDER = list(STICKER_SECTION_LABELS.keys())


def _sticker_status(section_key: str):
    """وضعیت فعلی یک بخش را برمی‌گرداند: (is_enabled, has_custom, file_id_or_None)."""
    row = db.get_section_sticker(section_key)
    if row is None:
        return True, False, None  # پیش‌فرض: فعال، بدون سفارشی‌سازی
    return bool(row["is_enabled"]), True, row.get("file_id")


async def _send_sticker_section_detail(target: types.Message, section_key: str):
    label = STICKER_SECTION_LABELS.get(section_key, section_key)
    is_enabled, has_custom, file_id = _sticker_status(section_key)

    if is_enabled:
        status_line = "✅ فعال — سفارشی (آپلود‌شده توسط ادمین)" if has_custom else "➖ فعال — استیکر پیش‌فرض پروژه"
    else:
        status_line = "🛑 غیرفعال — هیچ استیکری نشان داده نمی‌شود"

    text = f"🎬 {label}\n\nوضعیت: {status_line}"
    kb = admin_sticker_detail_keyboard(section_key, has_custom=has_custom, is_enabled=is_enabled)

    try:
        if is_enabled:
            if file_id:
                await target.answer_sticker(file_id)
            else:
                filename = STICKER_FILES.get(section_key)
                if filename:
                    await target.answer_sticker(FSInputFile(os.path.join(STICKERS_DIR, filename)))
    except Exception:
        logger.exception("خطا در پیش‌نمایش استیکر بخش '%s'", section_key)

    await target.answer(text, reply_markup=kb)


@router.callback_query(F.data == "admin_stickers")
async def admin_stickers_list(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    sections = []
    for key in _STICKER_SECTION_ORDER:
        is_enabled, has_custom, _fid = _sticker_status(key)
        if not is_enabled:
            emoji = "🛑"
        elif has_custom:
            emoji = "✅"
        else:
            emoji = "➖"
        sections.append({"key": key, "label": STICKER_SECTION_LABELS.get(key, key), "status_emoji": emoji})
    text = (
        "🎬 مدیریت استیکرهای منو\n\n"
        "✅ = سفارشی‌شده، ➖ = پیش‌فرض پروژه، 🛑 = غیرفعال\n\n"
        "یکی از بخش‌ها رو انتخاب کن:"
    )
    try:
        await callback.message.edit_text(text, reply_markup=admin_stickers_menu(sections))
    except Exception:
        await callback.message.answer(text, reply_markup=admin_stickers_menu(sections))
    await callback.answer()


@router.message(F.text == "🎬 استیکرهای منو")
async def admin_stickers_list_msg(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    sections = []
    for key in _STICKER_SECTION_ORDER:
        is_enabled, has_custom, _fid = _sticker_status(key)
        if not is_enabled:
            emoji = "🛑"
        elif has_custom:
            emoji = "✅"
        else:
            emoji = "➖"
        sections.append({"key": key, "label": STICKER_SECTION_LABELS.get(key, key), "status_emoji": emoji})
    text = (
        "🎬 مدیریت استیکرهای منو\n\n"
        "✅ = سفارشی‌شده، ➖ = پیش‌فرض پروژه، 🛑 = غیرفعال\n\n"
        "یکی از بخش‌ها رو انتخاب کن:"
    )
    await message.answer(text, reply_markup=admin_stickers_menu(sections))


@router.callback_query(F.data.startswith("stickeropen_"))
async def admin_sticker_open(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    section_key = callback.data.replace("stickeropen_", "")
    if section_key not in STICKER_SECTION_LABELS:
        await callback.answer("❌ بخش یافت نشد.", show_alert=True)
        return
    await _send_sticker_section_detail(callback.message, section_key)
    await callback.answer()


@router.callback_query(F.data.startswith("stickerset_"))
async def admin_sticker_set_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    section_key = callback.data.replace("stickerset_", "")
    if section_key not in STICKER_SECTION_LABELS:
        await callback.answer("❌ بخش یافت نشد.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_sticker_upload)
    await state.update_data(sticker_section_key=section_key)
    label = STICKER_SECTION_LABELS.get(section_key, section_key)
    await callback.message.answer(
        f"📤 استیکر/فایل موردنظرت رو برای «{label}» بفرست.\n\n"
        "فقط یک استیکر متحرک (ویدیویی) معتبر تلگرام قابل قبوله؛ هر ویدیوی معمولی رو تلگرام به‌عنوان استیکر قبول نمی‌کنه.",
        reply_markup=admin_sticker_cancel_keyboard(section_key),
    )
    await callback.answer()


@router.message(AdminStates.waiting_sticker_upload)
async def admin_sticker_upload_receive(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    section_key = data.get("sticker_section_key")
    if not section_key:
        await state.clear()
        return

    file_id = None
    if message.sticker:
        file_id = message.sticker.file_id
    elif message.document:
        file_id = message.document.file_id
    elif message.video:
        file_id = message.video.file_id
    elif message.animation:
        file_id = message.animation.file_id

    if not file_id:
        await message.answer("❌ فقط استیکر یا فایل/ویدیوی قابل قبوله. دوباره بفرست یا انصراف بده:")
        return

    db.set_section_sticker(section_key, file_id)
    invalidate_section_sticker_cache(section_key)
    await state.clear()
    label = STICKER_SECTION_LABELS.get(section_key, section_key)
    await message.answer(f"✅ استیکر «{label}» ذخیره شد.")
    await _send_sticker_section_detail(message, section_key)


@router.callback_query(F.data.startswith("stickeroff_"))
async def admin_sticker_off(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    section_key = callback.data.replace("stickeroff_", "")
    if section_key not in STICKER_SECTION_LABELS:
        await callback.answer("❌ بخش یافت نشد.", show_alert=True)
        return
    db.set_section_sticker_enabled(section_key, False)
    invalidate_section_sticker_cache(section_key)
    await _send_sticker_section_detail(callback.message, section_key)
    await callback.answer("🛑 غیرفعال شد.")


@router.callback_query(F.data.startswith("stickeron_"))
async def admin_sticker_on(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    section_key = callback.data.replace("stickeron_", "")
    if section_key not in STICKER_SECTION_LABELS:
        await callback.answer("❌ بخش یافت نشد.", show_alert=True)
        return
    db.set_section_sticker_enabled(section_key, True)
    invalidate_section_sticker_cache(section_key)
    await _send_sticker_section_detail(callback.message, section_key)
    await callback.answer("✅ فعال شد.")


@router.callback_query(F.data.startswith("stickerreset_"))
async def admin_sticker_reset(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    section_key = callback.data.replace("stickerreset_", "")
    if section_key not in STICKER_SECTION_LABELS:
        await callback.answer("❌ بخش یافت نشد.", show_alert=True)
        return
    db.reset_section_sticker(section_key)
    invalidate_section_sticker_cache(section_key)
    await _send_sticker_section_detail(callback.message, section_key)
    await callback.answer("♻️ به حالت پیش‌فرض برگشت.")


# ---------------------------------------------------------------------------
# 💾 بکاپ
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_backup")
async def admin_backup(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return

    try:
        if db.USE_TURSO:
            export_path = "/tmp/backup_export.json"
            db.export_backup_json(export_path)
            backup_file = FSInputFile(export_path, filename="backup.json")
            caption = "💾 بکاپ دیتابیس (Turso — JSON)"
        else:
            backup_file = FSInputFile(DATABASE_PATH)
            caption = "💾 بکاپ دیتابیس"
        await callback.message.answer_document(backup_file, caption=caption)
    except Exception:
        await callback.message.answer("❌ خطا در ساخت بکاپ.")
    await callback.answer()


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 🔁 تنظیمات تمدید سرویس — کپی ساختار ربات
# ---------------------------------------------------------------------------
def _admin_renewal_settings(category_id, plan_key=None):
    try: cid=int(category_id or 0)
    except Exception: cid=0
    if plan_key:
        return bot_info.get_renewal_plan_settings(plan_key,cid)
    return bot_info.get_renewal_settings(cid)

def _renewal_scope_parts(target):
    if str(target).startswith("cat_"):
        try: return "cat",int(str(target)[4:]),None
        except Exception: return None,None,None
    if str(target).startswith("plan_"):
        key=str(target)[5:]; plan=db.get_vip_plan(key)
        if plan and plan.get("category_id"): return "plan",int(plan["category_id"]),key
    return None,None,None

@router.callback_query(F.data == "admin_renewal_settings")
async def admin_renewal_settings_open(callback:types.CallbackQuery,state:FSMContext):
    if not _is_admin(callback.from_user.id): return
    await state.clear(); await callback.message.edit_text("🔁 تنظیمات تمدید سرویس\n\nبرای هر دسته‌بندی می‌توانی نوع تمدید، قیمت، حداقل/حداکثر و دکمه‌های انتخاب را جداگانه تنظیم کنی.",reply_markup=admin_renewal_categories_menu()); await callback.answer()

@router.message(F.text == "🔁 تنظیمات تمدید")
async def admin_renewal_settings_open_msg(message:types.Message,state:FSMContext):
    if not _is_admin(message.from_user.id): return
    await state.clear(); await message.answer("🔁 تنظیمات تمدید سرویس\n\nیک دسته‌بندی را انتخاب کن:",reply_markup=admin_renewal_categories_menu())

@router.callback_query(F.data.startswith("renewsetcat_"))
async def admin_renewal_category(callback:types.CallbackQuery,state:FSMContext):
    if not _is_admin(callback.from_user.id): return
    try: cid=int(callback.data.replace("renewsetcat_","",1))
    except Exception: await callback.answer("❌ دسته نامعتبر.",show_alert=True); return
    cat=db.get_vip_category(cid)
    if not cat: await callback.answer("❌ دسته پیدا نشد.",show_alert=True); return
    await state.clear(); await callback.message.edit_text(f"🔁 تنظیمات تمدید\n\nدسته: {cat['name']}\n\nمحدوده تنظیمات را انتخاب کن:",reply_markup=admin_renewal_scope_menu(cid)); await callback.answer()

@router.callback_query(F.data.startswith("renewsetscope_all_"))
async def admin_renewal_scope_all(callback:types.CallbackQuery,state:FSMContext):
    if not _is_admin(callback.from_user.id): return
    cid=int(callback.data.replace("renewsetscope_all_","",1)); await state.clear(); await callback.message.edit_text("🔁 تنظیمات تمدید\n\nمحدوده: همه پلن‌های این دسته",reply_markup=admin_renewal_category_menu(cid)); await callback.answer()

@router.callback_query(F.data.startswith("renewsetscope_plan_"))
async def admin_renewal_scope_plan(callback:types.CallbackQuery,state:FSMContext):
    if not _is_admin(callback.from_user.id): return
    raw=callback.data.replace("renewsetscope_plan_","",1)
    try: cid_s,key=raw.split("_",1); cid=int(cid_s)
    except Exception: await callback.answer("❌ پلن نامعتبر.",show_alert=True); return
    plan=db.get_vip_plan(key)
    if not plan: await callback.answer("❌ پلن پیدا نشد.",show_alert=True); return
    await state.clear(); await callback.message.edit_text(f"🔁 تنظیمات تمدید\n\nدسته: {(db.get_vip_category(cid) or {}).get('name',cid)}\nپلن: {plan.get('name',key)}",reply_markup=admin_renewal_plan_menu(cid,key)); await callback.answer()

@router.callback_query(F.data.regexp(r"^renewsetscope_\d+$"))
async def admin_renewal_scope_back(callback:types.CallbackQuery,state:FSMContext):
    if not _is_admin(callback.from_user.id): return
    cid=int(callback.data.replace("renewsetscope_","",1)); await state.clear(); await callback.message.edit_text("🔁 تنظیمات تمدید\n\nمحدوده تنظیمات را انتخاب کن:",reply_markup=admin_renewal_scope_menu(cid)); await callback.answer()

@router.callback_query(F.data.startswith("renewsetmode_"))
async def admin_renewal_mode(callback:types.CallbackQuery,state:FSMContext):
    if not _is_admin(callback.from_user.id): return
    target=callback.data.replace("renewsetmode_","",1); scope,cid,key=_renewal_scope_parts(target)
    if not scope: await callback.answer("❌ مقصد نامعتبر.",show_alert=True); return
    await callback.message.edit_text("نوع تمدید را انتخاب کن:",reply_markup=admin_renewal_mode_menu(cid,key)); await callback.answer()

@router.callback_query(F.data.startswith("renewsetmodeval_"))
async def admin_renewal_mode_save(callback:types.CallbackQuery,state:FSMContext):
    raw=callback.data.replace("renewsetmodeval_","",1)
    try: mode,target=raw.split("_",1)
    except Exception: await callback.answer("❌ مقدار نامعتبر.",show_alert=True); return
    scope,cid,key=_renewal_scope_parts(target)
    if mode not in ("day","gb","both") or not scope: await callback.answer("❌ مقدار نامعتبر.",show_alert=True); return
    if scope=="cat":
        bot_info.set_renewal_setting(cid,"mode",mode); bot_info.clear_renewal_plan_overrides_for_category(cid,"mode")
        kb=admin_renewal_category_menu(cid)
    else:
        bot_info.set_renewal_plan_setting(key,"mode",mode); kb=admin_renewal_plan_menu(cid,key)
    await callback.message.edit_text("✅ نوع تمدید ذخیره شد.",reply_markup=kb); await callback.answer()

@router.callback_query(F.data.startswith("renewset_"))
async def admin_renewal_numeric_start(callback:types.CallbackQuery,state:FSMContext):
    raw=callback.data.replace("renewset_","",1)
    if raw.startswith("options_"):
        parts=raw.split("_",2); unit=parts[1]; target=parts[2]
        scope,cid,key=_renewal_scope_parts(target)
        if unit not in ("day","gb") or not scope: await callback.answer("❌ گزینه نامعتبر.",show_alert=True); return
        await state.update_data(renewal_target=target,renewal_field=f"{unit}_options",renewal_unit=unit)
        await state.set_state(AdminStates.waiting_renewal_setting_value)
        st=_admin_renewal_settings(cid,key); await callback.message.answer(f"دکمه‌های {('روز' if unit=='day' else 'گیگ')} را با کاما جدا کن.\nمثال: {('30,60,90' if unit=='day' else '10,20,50')}\nمقدار فعلی: {st.get(f'{unit}_options')}"); await callback.answer(); return
    parts=raw.split("_",2)
    if len(parts)!=3: await callback.answer("❌ گزینه نامعتبر.",show_alert=True); return
    field,unit,target=parts; scope,cid,key=_renewal_scope_parts(target)
    if field not in ("price","min","max") or unit not in ("day","gb") or not scope: await callback.answer("❌ گزینه نامعتبر.",show_alert=True); return
    st=_admin_renewal_settings(cid,key); cur=st.get(f"{field}_{unit}")
    await state.update_data(renewal_target=target,renewal_field=f"{field}_{unit}",renewal_unit=unit)
    await state.set_state(AdminStates.waiting_renewal_setting_value)
    label={"price":"قیمت","min":"حداقل","max":"حداکثر"}[field]; suffix="تومان" if field=="price" else ("روز" if unit=="day" else "گیگ")
    shown="نامحدود" if field=="max" and not int(cur or 0) else cur
    await callback.message.answer(f"{label} {suffix} را وارد کن.\nمقدار فعلی: {shown}"); await callback.answer()

@router.message(AdminStates.waiting_renewal_setting_value)
async def admin_renewal_setting_save(message:types.Message,state:FSMContext):
    if not _is_admin(message.from_user.id): return
    data=await state.get_data(); target=data.get("renewal_target"); field=data.get("renewal_field")
    scope,cid,key=_renewal_scope_parts(target or "")
    if not scope or not field: await state.clear(); await message.answer("❌ خطای داخلی تنظیمات تمدید."); return
    value=(message.text or "").strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹","0123456789"))
    try:
        if field.endswith("_options"):
            nums=[]
            for x in value.replace("،",",").split(","):
                x=x.strip()
                if x: nums.append(int(x))
            nums=list(dict.fromkeys(nums))
            unit=field.split("_")[0]; st=_admin_renewal_settings(cid,key); mn=int(st.get(f"min_{unit}") or 1); mx=int(st.get(f"max_{unit}") or 0)
            if not nums or any(n<mn or (mx and n>mx) for n in nums): raise ValueError
            raw=",".join(map(str,nums))
        else:
            number=int(value.replace(",","").replace("٬","").strip())
            if number<0 or (field.startswith("min") and number<1): raise ValueError
            raw=number
        if scope=="cat":
            bot_info.set_renewal_setting(cid,field,raw); bot_info.clear_renewal_plan_overrides_for_category(cid,field); kb=admin_renewal_category_menu(cid)
        else:
            bot_info.set_renewal_plan_setting(key,field,raw); kb=admin_renewal_plan_menu(cid,key)
        await state.clear(); await message.answer("✅ تنظیم تمدید ذخیره شد.",reply_markup=kb)
    except Exception:
        await message.answer("❌ مقدار نامعتبر است. حداقل/حداکثر و گزینه‌ها باید با تنظیمات فعلی سازگار باشند.")

# ℹ️ اطلاعات ربات (قالب فروشی)
# ---------------------------------------------------------------------------
def _botinfo_status_text():
    values = bot_info.all_values()
    labels = bot_info.labels()
    lines = ["ℹ️ اطلاعات ربات\n", "برای ویرایش روی هر کدام بزنید:\n"]
    for key, label in labels.items():
        val = values.get(key) or "—"
        lines.append(f"• {label}: {val}")
    channels = bot_info.get_required_channels()
    lines.append(f"\n• کانال‌های اجباری: {len(channels)} عدد")
    return "\n".join(lines)


@router.callback_query(F.data == "admin_botinfo")
async def admin_botinfo_open(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    try:
        await callback.message.edit_text(_botinfo_status_text(), reply_markup=admin_botinfo_menu())
    except TelegramBadRequest:
        await callback.message.answer(_botinfo_status_text(), reply_markup=admin_botinfo_menu())
    await callback.answer()


@router.message(F.text == "ℹ️ اطلاعات ربات")
async def admin_botinfo_open_msg(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(_botinfo_status_text(), reply_markup=admin_botinfo_menu())



@router.callback_query(F.data == "botinfo_open")
async def admin_botinfo_open_alias(callback: types.CallbackQuery, state: FSMContext):
    """بازگشت استاندارد از زیرمنوهای اطلاعات ربات."""
    await admin_botinfo_open(callback, state)

@router.callback_query(F.data == "botinfo_referral")
async def admin_botinfo_referral_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    settings = db.get_referral_settings()
    text = (
        "🎁 مدیریت رفرال\n\n"
        f"💰 مبلغ پاداش: {settings['reward_amount']:,} تومان\n"
        f"📦 حداقل حجم: {settings['min_volume_gb']} گیگ\n"
        f"📏 شرط حداقل حجم: {'روشن' if settings['min_volume_enabled'] else 'خاموش'}\n"
        f"💳 شرط خرید پولی: {'روشن' if settings['paid_purchase_required'] else 'خاموش'}\n"
        f"🔒 قفل پاداش با خروج از کانال: {'روشن' if settings.get('channel_leave_lock_enabled', True) else 'خاموش'}\n"
        f"🚨 سیستم جریمه: {'روشن' if settings.get('channel_leave_penalty_enabled', False) else 'خاموش'}\n"
        f"💸 جریمه کیف‌پول: {settings.get('penalty_wallet_amount', 0):,} تومان\n"
        f"📉 جریمه سرویس: {settings.get('penalty_service_gb', 0)} گیگ"
    )
    await callback.message.edit_text(text, reply_markup=admin_botinfo_referral_menu(settings))
    await callback.answer()


@router.callback_query(F.data == "botinfo_wallet")
async def admin_botinfo_wallet_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    settings = db.get_wallet_settings()
    text = (
        "💳 مدیریت شارژ کیف‌پول\n\n"
        f"📉 حداقل شارژ: {settings['min_topup']:,} تومان\n"
        f"📈 حداکثر شارژ: {settings['max_topup']:,} تومان\n"
        f"🔘 مبالغ پیشنهادی: {' / '.join(f'{a:,}' for a in settings['quick_amounts'])}"
    )
    await callback.message.edit_text(text, reply_markup=admin_botinfo_wallet_menu(settings))
    await callback.answer()


@router.callback_query(F.data.startswith("refset_toggle_"))
async def admin_referral_toggle(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    key = callback.data.replace("refset_toggle_", "")
    settings = db.get_referral_settings()
    mapping = {
        "min_volume": "min_volume_enabled",
        "paid_purchase": "paid_purchase_required",
        "channel_leave_lock": "channel_leave_lock_enabled",
        "channel_leave_penalty": "channel_leave_penalty_enabled",
    }
    setting_key = mapping.get(key)
    if not setting_key:
        await callback.answer("❌ تنظیم نامعتبر است.", show_alert=True)
        return
    db.set_referral_setting(setting_key, not bool(settings[setting_key]))
    settings = db.get_referral_settings()
    await callback.message.edit_reply_markup(reply_markup=admin_botinfo_referral_menu(settings))
    await callback.answer("✅ تنظیم ذخیره شد.")


@router.callback_query(F.data.startswith("refset_edit_"))
async def admin_referral_edit_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    key = callback.data.replace("refset_edit_", "")
    prompts = {
        "min_volume": ("min_volume_gb", "📦 حداقل حجم لازم برای آزاد شدن پاداش را به گیگ وارد کنید (عدد مثبت):"),
        "reward": ("reward_amount", "💰 مبلغ پاداش هر رفرال را به تومان وارد کنید:"),
        "penalty_wallet": ("penalty_wallet_amount", "💸 مبلغ جریمه کیف‌پول را به تومان وارد کنید (۰ برای بدون جریمه):"),
        "penalty_gb": ("penalty_service_gb", "📉 مقدار جریمه از سرویس را به گیگ وارد کنید (۰ برای بدون جریمه):"),
    }
    item = prompts.get(key)
    if not item:
        await callback.answer("❌ تنظیم نامعتبر است.", show_alert=True)
        return
    setting_key, prompt = item
    settings = db.get_referral_settings()
    await state.update_data(referral_setting_key=setting_key)
    await state.set_state(AdminStates.waiting_referral_setting_value)
    await callback.message.edit_text(f"{prompt}\n\nمقدار فعلی: {settings.get(setting_key, 0)}", reply_markup=admin_back_button())
    await callback.answer()


@router.message(AdminStates.waiting_referral_setting_value)
async def admin_referral_edit_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    key = data.get("referral_setting_key")
    raw = clean_numeric_id(message.text or "").strip()
    if not raw.isdigit():
        await message.answer("❌ فقط عدد وارد کنید:")
        return
    value = int(raw)
    if key in {"min_volume_gb", "reward_amount"} and value <= 0:
        await message.answer("❌ مقدار باید بزرگ‌تر از صفر باشد:")
        return
    try:
        db.set_referral_setting(key, value)
    except Exception as e:
        await message.answer(f"❌ ذخیره تنظیمات ناموفق بود: {e}")
        return
    await state.clear()
    settings = db.get_referral_settings()
    await message.answer("✅ تنظیم رفرال ذخیره شد.", reply_markup=admin_botinfo_referral_menu(settings))


@router.callback_query(F.data.startswith("wset_edit_"))
async def admin_wallet_setting_edit_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    key = callback.data.replace("wset_edit_", "")
    prompts = {
        "min_topup": "📉 حداقل مبلغ شارژ را به تومان وارد کنید:",
        "max_topup": "📈 حداکثر مبلغ شارژ را به تومان وارد کنید:",
        "quick_amounts": "🔘 مبالغ پیشنهادی را با کاما/فاصله جدا کنید؛ مثال: 50000,100000,200000",
    }
    if key not in prompts:
        await callback.answer("❌ تنظیم نامعتبر است.", show_alert=True)
        return
    settings = db.get_wallet_settings()
    current = settings[key]
    if isinstance(current, list):
        current = ", ".join(str(x) for x in current)
    await state.update_data(wallet_setting_key=key)
    await state.set_state(AdminStates.waiting_wallet_setting_value)
    await callback.message.edit_text(f"{prompts[key]}\n\nمقدار فعلی: {current}", reply_markup=admin_back_button())
    await callback.answer()


@router.message(AdminStates.waiting_wallet_setting_value)
async def admin_wallet_setting_edit_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    key = data.get("wallet_setting_key")
    raw = (message.text or "").strip()
    try:
        if key == "quick_amounts":
            parts = [x for x in re.split(r"[\s,،]+", clean_numeric_id(raw)) if x]
            if not parts or not all(x.isdigit() for x in parts):
                raise ValueError("فقط اعداد معتبر وارد کنید")
            values = [int(x) for x in parts if int(x) > 0]
            db.set_wallet_setting("quick_amounts", values)
        elif key in {"min_topup", "max_topup"}:
            value = int(clean_numeric_id(raw))
            if value <= 0:
                raise ValueError("مبلغ باید بزرگ‌تر از صفر باشد")
            current = db.get_wallet_settings()
            if key == "min_topup" and value > int(current["max_topup"]):
                raise ValueError("حداقل مبلغ نمی‌تواند از حداکثر بیشتر باشد")
            if key == "max_topup" and value < int(current["min_topup"]):
                raise ValueError("حداکثر مبلغ نمی‌تواند از حداقل کمتر باشد")
            db.set_wallet_setting(key, value)
        else:
            raise ValueError("تنظیم نامعتبر است")
    except Exception as e:
        await message.answer(f"❌ {e}\n\nدوباره وارد کنید:")
        return
    await state.clear()
    settings = db.get_wallet_settings()
    await message.answer("✅ تنظیمات کیف‌پول ذخیره شد.", reply_markup=admin_botinfo_wallet_menu(settings))


# ---------------------------------------------------------------------------
# 📣 لینک‌های تبلیغاتی / کمپین‌ها
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_campaigns")
async def admin_campaigns_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    campaigns = db.list_campaigns()
    text = "📣 لینک‌های تبلیغاتی\n\n" + ("هنوز کمپینی ساخته نشده.\n\nیک کمپین جدید بسازید:" if not campaigns else "کمپین‌ها را برای مشاهده آمار و مدیریت انتخاب کنید:")
    await callback.message.edit_text(text, reply_markup=admin_campaigns_menu(campaigns))
    await callback.answer()


@router.callback_query(F.data == "new_campaign")
async def admin_campaign_new_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminStates.waiting_campaign_code)
    await callback.message.edit_text("📣 ساخت کمپین جدید\n\nمرحله ۱ از ۲\n\nکد کوتاه کمپین را وارد کنید (فقط حروف انگلیسی، عدد و _):", reply_markup=admin_back_button())
    await callback.answer()


@router.message(AdminStates.waiting_campaign_code)
async def admin_campaign_code_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    code = db.normalize_campaign_code(message.text or "")
    if not code:
        await message.answer("❌ کد کمپین نامعتبر است. فقط حروف انگلیسی، عدد و _ وارد کنید:")
        return
    if db.get_campaign_by_code(code):
        await message.answer("❌ این کد قبلاً استفاده شده است. یک کد دیگر وارد کنید:")
        return
    await state.update_data(campaign_code=code)
    await state.set_state(AdminStates.waiting_campaign_name)
    await message.answer(f"📣 مرحله ۲ از ۲\n\nنام نمایشی کمپین «{code}» را وارد کنید:", reply_markup=admin_back_button())


@router.message(AdminStates.waiting_campaign_name)
async def admin_campaign_name_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("❌ نام کمپین نمی‌تواند خالی باشد:")
        return
    data = await state.get_data()
    code = data.get("campaign_code")
    try:
        campaign_id = db.create_campaign(code, name)
    except Exception:
        await message.answer("❌ ساخت کمپین ناموفق بود؛ احتمالاً کد تکراری است.")
        return
    await state.clear()
    campaign = db.get_campaign_by_id(campaign_id)
    await _send_campaign_detail(message, campaign)


async def _send_campaign_detail(target, campaign: dict | None):
    if not campaign:
        if isinstance(target, types.CallbackQuery):
            await target.answer("❌ کمپین یافت نشد.", show_alert=True)
        else:
            await target.answer("❌ کمپین یافت نشد.")
        return
    stats = db.get_campaign_stats(campaign["id"])
    username = bot_info.get("bot_username") or "your_bot"
    link = f"https://t.me/{username}?start=c_{campaign['code']}"
    text = (
        f"📣 کمپین: {campaign['name']}\n"
        f"🔑 کد: {campaign['code']}\n"
        f"🔗 لینک: {link}\n\n"
        f"🟢 وضعیت: {'فعال' if campaign.get('is_active', 1) else 'غیرفعال'}\n"
        f"👥 ورودی: {stats['members']}\n"
        f"🛒 تبدیل به خرید: {stats['converted']}\n"
        f"💰 مجموع خرید: {stats['total_spent']:,} تومان"
    )
    markup = campaign_detail_keyboard(campaign)
    if isinstance(target, types.CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("campdetail_"))
async def admin_campaign_detail(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    try:
        cid = int(callback.data.replace("campdetail_", ""))
    except ValueError:
        await callback.answer("❌ کمپین نامعتبر است.", show_alert=True)
        return
    await _send_campaign_detail(callback, db.get_campaign_by_id(cid))


@router.callback_query(F.data.startswith("camptoggle_"))
async def admin_campaign_toggle(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    try:
        cid = int(callback.data.replace("camptoggle_", ""))
    except ValueError:
        await callback.answer("❌ کمپین نامعتبر است.", show_alert=True)
        return
    campaign = db.get_campaign_by_id(cid)
    if not campaign:
        await callback.answer("❌ کمپین یافت نشد.", show_alert=True)
        return
    db.set_campaign_active(cid, not bool(campaign.get("is_active", 1)))
    await _send_campaign_detail(callback, db.get_campaign_by_id(cid))


@router.callback_query(F.data.startswith("campedit_"))
async def admin_campaign_rename_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    try:
        cid = int(callback.data.replace("campedit_", ""))
    except ValueError:
        await callback.answer("❌ کمپین نامعتبر است.", show_alert=True)
        return
    campaign = db.get_campaign_by_id(cid)
    if not campaign:
        await callback.answer("❌ کمپین یافت نشد.", show_alert=True)
        return
    await state.update_data(campaign_rename_id=cid)
    await state.set_state(AdminStates.waiting_campaign_rename)
    await callback.message.edit_text(f"✏️ نام جدید برای کمپین «{campaign['name']}» را ارسال کنید:", reply_markup=admin_back_button())
    await callback.answer()


@router.message(AdminStates.waiting_campaign_rename)
async def admin_campaign_rename_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    cid = data.get("campaign_rename_id")
    name = (message.text or "").strip()
    if not cid or not name:
        await message.answer("❌ نام نامعتبر است. دوباره ارسال کنید:")
        return
    db.rename_campaign(int(cid), name)
    await state.clear()
    await _send_campaign_detail(message, db.get_campaign_by_id(int(cid)))


@router.callback_query(F.data.startswith("campdelete_"))
async def admin_campaign_delete_start(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    try:
        cid = int(callback.data.replace("campdelete_", ""))
    except ValueError:
        await callback.answer("❌ کمپین نامعتبر است.", show_alert=True)
        return
    campaign = db.get_campaign_by_id(cid)
    if not campaign:
        await callback.answer("❌ کمپین یافت نشد.", show_alert=True)
        return
    await callback.message.edit_text(f"⚠️ کمپین «{campaign['name']}» حذف شود؟\n\nکاربرانی که با این کمپین وارد شده‌اند حذف نمی‌شوند.", reply_markup=campaign_delete_confirm_keyboard(cid))
    await callback.answer()


@router.callback_query(F.data.startswith("campdeleteconfirm_"))
async def admin_campaign_delete_do(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    try:
        cid = int(callback.data.replace("campdeleteconfirm_", ""))
    except ValueError:
        await callback.answer("❌ کمپین نامعتبر است.", show_alert=True)
        return
    campaign = db.get_campaign_by_id(cid)
    if not campaign:
        await callback.answer("❌ کمپین یافت نشد.", show_alert=True)
        return
    name = campaign["name"]
    db.delete_campaign(cid)
    await callback.message.edit_text(f"✅ کمپین «{name}» حذف شد.", reply_markup=admin_campaigns_menu(db.list_campaigns()))
    await callback.answer("🗑 حذف شد.")

@router.callback_query(F.data.startswith("botinfo_edit_"))
async def admin_botinfo_edit_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    key = callback.data.replace("botinfo_edit_", "")
    labels = bot_info.labels()
    if key not in labels:
        await callback.answer("❌ یافت نشد.", show_alert=True)
        return
    await state.update_data(botinfo_key=key)
    await state.set_state(AdminStates.waiting_botinfo_value)
    current = bot_info.get(key) or "—"
    await callback.message.answer(
        f"✏️ مقدار جدید برای «{labels[key]}» را بفرستید.\nمقدار فعلی: {current}",
        reply_markup=admin_botinfo_field_keyboard(key),
    )
    await callback.answer()


@router.message(AdminStates.waiting_botinfo_value)
async def admin_botinfo_edit_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    key = data.get("botinfo_key")
    labels = bot_info.labels()
    if not key or key not in labels:
        await state.clear()
        await message.answer("❌ خطای داخلی. دوباره تلاش کنید.")
        return
    value = (message.text or "").strip()
    if key == "config_name_prefix":
        cleaned = re.sub(r"[^A-Za-z0-9_]+", "", value)
        if not cleaned:
            await message.answer(
                "❌ پیشوند باید فقط از حروف/عدد انگلیسی و زیرخط (_) تشکیل شده باشد؛ دوباره وارد کن:"
            )
            return
        value = cleaned
    # متن را تا سقف واقعی Telegram Bot API می‌پذیریم. مسیر /start هنگام ارسال
    # متن ثابت پایین را هم حساب می‌کند و اگر مجموع از سقف عبور کند، امن کوتاه می‌شود.
    from utils import telegram_utf16_length
    if telegram_utf16_length(value) > TELEGRAM_TEXT_LIMIT:
        await message.answer(
            f"❌ متن از سقف تلگرام بیشتر است. سقف واقعی: {TELEGRAM_TEXT_LIMIT} واحد UTF-16. لطفاً متن کوتاه‌تری بفرست:"
        )
        return
    if key == "welcome_text":
        # Custom/Premium Emoji به‌صورت MessageEntity می‌آید؛ ذخیره‌ی صرفِ message.text
        # شناسه‌ی emoji را از بین می‌برد.
        bot_info.set_welcome_text_with_entities(value, serialize_message_entities(message.entities))
    else:
        bot_info.set(key, value)
    await state.clear()
    await message.answer(f"✅ «{labels[key]}» به‌روز شد.", reply_markup=admin_botinfo_menu())


@router.callback_query(F.data == "botinfo_channels")
async def admin_botinfo_channels_open(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    channels = bot_info.get_required_channels()
    text = "📢 کانال‌های عضویت اجباری\n\nروی هرکدام بزنید تا حذف شود." if channels else "📢 هیچ کانال اجباریثبت نشده."
    try:
        await callback.message.edit_text(text, reply_markup=admin_botinfo_channels_menu(channels))
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=admin_botinfo_channels_menu(channels))
    await callback.answer()


@router.callback_query(F.data == "botinfo_channel_add")
async def admin_botinfo_channel_add_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_botinfo_channel_add)
    await callback.message.answer(
        "➕ افزودن کانال اجباری\n\nفرمت زیر را ارسال کنید (با | جدا شده):\nآیدی/یوزرنیم کانال | نام نمایشی | لینک دعوت\nمثال: -1001234567890 | کانال ما | https://t.me/mychannel",
        reply_markup=admin_botinfo_field_keyboard(""),
    )
    await callback.answer()


@router.message(AdminStates.waiting_botinfo_channel_add)
async def admin_botinfo_channel_add_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    parts = [p.strip() for p in (message.text or "").split("|")]
    # 🐛 فیکس: قبلاً فقط خالی‌نبودن آیدی کانال (parts[0]) چک می‌شد؛ اگر ادمین لینک دعوت
    # (parts[2]) را خالی می‌فرستاد یا فراموش می‌کرد، همان مقدار خالی مستقیم به‌عنوان url
    # دکمه‌ی «عضویت» ذخیره می‌شد و بعداً برای هر کاربری که هنوز عضو نشده بود، تلگرام موقع
    # نمایش منوی عضویت اجباری خطای BUTTON_URL_INVALID می‌داد و /start با ارور مواجه می‌شد.
    if len(parts) != 3 or not parts[0] or not parts[2]:
        await message.answer("❌ فرمت نادرست یا لینک دعوت خالی است. هر سه بخش (آیدی | نام | لینک دعوت) باید پر باشند. دوباره تلاش کنید یا /cancel بزنید.")
        return
    raw_chat_id, name, url = parts
    raw_chat_id = clean_numeric_id(raw_chat_id)

    # 🐛 فیکس: قبلاً هرچی ادمین می‌فرستاد بدون هیچ بررسی ذخیره می‌شد؛ اگر فرمت آیدی/یوزرنیم اشتباه بود (مثلاً بدون پیشوند "-100" برای کانال/سوپرگروه، یا بودن خود لینک دعوت به‌جای آیدی/یوزرنیم) یا ربات هنوز ادمین کانال نشده بود، ربات بی‌صدا ذخیره می‌شد و بعداً تأیید عضویت هیچ‌وقت برای هیچ کاربری درست قبول نمی‌شد. اینجا همان لحظه خود ربات را تست می‌کنیم.
    chat_id_input = raw_chat_id if raw_chat_id.startswith("@") or raw_chat_id.startswith("-") or raw_chat_id.lstrip("-").isdigit() else f"@{raw_chat_id.lstrip('@')}"
    try:
        chat = await message.bot.get_chat(chat_id_input)
    except Exception as e:
        await message.answer(
            "❌ ربات نتوانست این کانال را پیدا کند. ممکن است:\n"
            "• فرمت آیدی اشتباه باشد (برای کانال باید با «-100» شروع شود، متل -1001234567890)\n"
            "• ربات هنوز به این کانال اضافه/عضو نشده باشد\n\n"
            f"خطای دقیق: {e}\n\nدوباره تلاش کنید یا /cancel بزنید."
        )
        return

    try:
        bot_member = await message.bot.get_chat_member(chat.id, message.bot.id)
        if bot_member.status not in ("administrator", "creator"):
            await message.answer(
                "⚠️ ربات عضو این کانال هست ولی «ادمین» نیست. برای اینکه ربات بتواند عضویت کاربرها را در این کانال ببیند، باید ربات را در آن کانال «ادمین» کنی ‌(نه فقط عضو)، بعد دوباره همین پیام را بفرست."
            )
            return
    except Exception as e:
        await message.answer(f"❌ بررسی وضعیت عضویت ربات در این کانال با خطا مواجه شد: {e}\n\nدوباره تلاش کنید یا /cancel بزنید.")
        return

    # 🐛 فیکس: به‌جای متنی که ادمین خودش تایپ کرده، همیشه از chat.id عددی واقعی که تلگرام برمی‌گرداند استفاده می‌کنیم تا همیشه با همان فرمتی که check_membership انتظار دارد ذخیره شود (نه متنی که ادمین تایکرده و ممکن است فرمتش اشتباه باشد).
    bot_info.add_required_channel(chat.id, name, url)
    await state.clear()
    channels = bot_info.get_required_channels()
    await message.answer(
        f"✅ کانال اضافه شد و تایید شد که ربات به درستی در آن ادمین است (آیدی واقعی: {chat.id}).",
        reply_markup=admin_botinfo_channels_menu(channels),
    )


@router.callback_query(F.data.startswith("botinfo_channel_del_"))
async def admin_botinfo_channel_del(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    chat_id = callback.data.replace("botinfochdel_", "")
    bot_info.remove_required_channel(chat_id)
    channels = bot_info.get_required_channels()
    text = "📢 کانال‌های عضویت اجباری" if channels else "📢 هیچ کانال اجباریثبت نشده."
    try:
        await callback.message.edit_text(text, reply_markup=admin_botinfo_channels_menu(channels))
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=admin_botinfo_channels_menu(channels))
    await callback.answer("✅ حذف شد.")


@router.message(F.text.in_({"📝 مدیریت متن‌های کاربر", "✏️ ویرایش متن و دکمه‌ها"}))
async def admin_texts_from_menu(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(
        "✏️ ویرایش متن و دکمه‌ها\n\nابتدا نوع موردی که می‌خواهید ویرایش کنید را انتخاب کنید:",
        reply_markup=_ui_editor.editor_mode_keyboard(),
    )


@router.callback_query(F.data == "errlogsentryguide")
async def admin_error_logs_sentry_guide(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    text = (
        "🔗 راهنمای فعال‌سازی Sentry\n\n"
        "برای فعال‌سازی، مقدار DSN سرویس Sentry را در متغیر محیطی SENTRY_DSN قرار دهید و ربات را ری‌استارت کنید.\n\n"
        "پس از بالا آمدن ربات، خطاهای جدید در بخش «🦖 لاگ خطاها» ثبت و در صورت فعال بودن Sentry ارسال می‌شوند."
    )
    await callback.message.edit_text(text, reply_markup=admin_back_button())
    await callback.answer()


@router.callback_query(F.data == "errlog")
async def admin_error_logs_open_callback(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await _open_error_logs(callback, edit=True)
    await callback.answer()


@router.callback_query(F.data == "admin_free_test_settings")
async def admin_free_test_settings_open_callback(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_free_test_settings)
    await callback.message.edit_text(_free_test_settings_text())
    await callback.answer()


# ---------------------------------------------------------------------------
# 🎁 تنظیم حجم/روز پلن «تست رایگان» از پنل ادمین
# ---------------------------------------------------------------------------
FREE_TEST_MIN_VOLUME_MB = 50
FREE_TEST_MAX_VOLUME_MB = 1024
FREE_TEST_MIN_DAYS = 1
FREE_TEST_MAX_DAYS = 7
FREE_TEST_MIN_PRICE = 0
FREE_TEST_MAX_PRICE = 2000


def _free_test_settings_text() -> str:
    plan = db.get_effective_free_test_plan()
    price = plan.get("price", 0)
    price_label = "رایگان" if price == 0 else f"{price:,} تومان"
    return (
        "🎁 تنظیم پلن «تست رایگان»\n\n"
        f"مقدار فعلی: {plan['name']} — قیمت: {price_label}\n\n"
        "برای تغییر، حجم (مگابایت)، تعداد روز و قیمت (تومان) را با | جدا و ارسال کنید.\n"
        f"محدوده‌ی مجاز: حجم بین {FREE_TEST_MIN_VOLUME_MB} تا {FREE_TEST_MAX_VOLUME_MB} مگابایت، "
        f"روز بین {FREE_TEST_MIN_DAYS} تا {FREE_TEST_MAX_DAYS} روز، "
        f"قیمت بین {FREE_TEST_MIN_PRICE} (رایگان) تا {FREE_TEST_MAX_PRICE} تومان.\n"
        "مثال: 500 | 3 | 1500\n\n"
        "برای انصراف /cancel بزنید."
    )


@router.message(F.text == "🎁 تنظیم تست رایگان")
async def admin_free_test_settings_open(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.set_state(AdminStates.waiting_free_test_settings)
    await message.answer(_free_test_settings_text())


@router.message(AdminStates.waiting_free_test_settings)
async def admin_free_test_settings_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if text == "/cancel":
        await state.clear()
        await message.answer("❌ لغو شد.", reply_markup=_admin_reply_kb_for(message.from_user.id))
        return
    parts = [p.strip() for p in text.split("|")]
    if len(parts) != 3:
        await message.answer("❌ فرمت نادرست. مثال درست: 500 | 3 | 1500\n\nدوباره تلاش کنید یا /cancel بزنید.")
        return
    volume_str, days_str, price_str = parts
    volume_mb = parse_int_in_range(volume_str, FREE_TEST_MIN_VOLUME_MB, FREE_TEST_MAX_VOLUME_MB)
    days = parse_int_in_range(days_str, FREE_TEST_MIN_DAYS, FREE_TEST_MAX_DAYS)
    price = parse_int_in_range(price_str, FREE_TEST_MIN_PRICE, FREE_TEST_MAX_PRICE)
    if volume_mb is None or days is None or price is None:
        await message.answer(
            f"❌ مقدار نامعتبر. حجم باید بین {FREE_TEST_MIN_VOLUME_MB} تا {FREE_TEST_MAX_VOLUME_MB} مگابایت، "
            f"روز باید بین {FREE_TEST_MIN_DAYS} تا {FREE_TEST_MAX_DAYS} و "
            f"قیمت باید بین {FREE_TEST_MIN_PRICE} تا {FREE_TEST_MAX_PRICE} تومان باشد.\n\nدوباره تلاش کنید یا /cancel بزنید."
        )
        return
    try:
        db.set_free_test_override(volume_mb, days, price)
    except Exception as e:
        logger.exception("خطا در ذخیره تنظیمات تست رایگان")
        await message.answer(f"❌ ذخیره تنظیمات ناموفق بود: {e}")
        return
    await state.clear()
    plan = db.get_effective_free_test_plan()
    price_label = "رایگان" if price == 0 else f"{price:,} تومان"
    await message.answer(
        f"✅ پلن «تست رایگان» به‌روزرسانی شد: {plan['name']} — قیمت: {price_label}",
        reply_markup=_admin_reply_kb_for(message.from_user.id),
    )


# ---------------------------------------------------------------------------
# 🧩 تنظیم قیمت/محدوده‌ی «بساز سرویس خودت» از پنل ادمین
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# 🛡️ اتصال پنل پاسارگارد (پنل VPN دوم، در کنار مرزبان)
# ---------------------------------------------------------------------------
def _pasargad_status_text():
    lines = ["🛡️ پنل پاسارگارد (PasarGuard)\n"]
    lines.append(f"وضعیت: {'✅ فعال' if bool(db.list_vpn_panels('pasargad', enabled_only=True)) else '⚪ غیرفعال (PASARGAD_BASE_URL/PASARGAD_USERNAME/PASARGAD_PASSWORD در env تنظیم نشده)'}")
    active = _active_panel_id()
    active_panel = db.get_vpn_panel(int(active)) if active and str(active).isdigit() else None
    lines.append(f"پنل VPN فعال فعلی: {panel_api.panel_label(active_panel) if active_panel else 'هیچکدام'}")
    return "\n".join(lines)


@router.callback_query(F.data == "admin_pasargad")
async def admin_pasargad_open(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.clear()
    text = _pasargad_status_text()
    try:
        await callback.message.edit_text(text, reply_markup=_pasargad_menu(text))
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=_pasargad_menu(text))
    await callback.answer()


@router.message(F.text == "🛡️ اتصال پنل پاسارگارد")
async def admin_pasargad_open_msg(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    text = _pasargad_status_text()
    await message.answer(text, reply_markup=_pasargad_menu(text))


@router.callback_query(F.data == "pasargadtest")
async def admin_pasargad_test(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await callback.answer("⏳ در حال بررسی...")
    panel = next(iter(db.list_vpn_panels("pasargad", enabled_only=True)), None)
    if panel:
        ok, data, msg = await panel_api.test_connection(panel)
    else:
        ok, data, msg = False, None, "هیچ پنل پاسارگارد فعالی ثبت نشده است."
    icon = "✅" if ok else "❌"
    await callback.message.answer(f"{icon} {msg}")


# ---------------------------------------------------------------------------
# 🔀 انتخاب پنل VPN فعال (اگر هر دو پنل متصل باشند)
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "admin_panel_choose")
async def admin_panel_choose_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    available = _available_panels()
    if not available:
        await callback.answer(
            "هیچ پنل VPNی متصل نیست. ابتدا مرزبان یا پاسارگارد را وصل کنید.",
            show_alert=True,
        )
        return
    active = _active_panel_id()
    text = (
        "🔀 انتخاب پنل VPN فعال\n\n"
        "ساخت/تمدید/فعال‌سازی خودکار سرویس‌ها از طریق پنلی انجام می‌شود که اینجا به‌عنوان پنل فعال انتخاب شود."
    )
    try:
        await callback.message.edit_text(text, reply_markup=_panel_choose_keyboard(available, active))
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=_panel_choose_keyboard(available, active))
    await callback.answer()


@router.callback_query(F.data.startswith("panelchoose_"))
async def admin_panel_choose_set(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    name = callback.data.replace("panelchoose_", "", 1)
    available = _available_panels()
    if name not in available:
        await callback.answer("این پنل وصل نیست.", show_alert=True)
        return
    _set_active_panel(name)
    active = _active_panel_id()
    text = (
        f"✅ پنل فعال تغییر کرد: {available.get(str(active), 'هیچکدام')}\n\n"
        "ساخت/تمدید/فعال‌سازی خودکار سرویس‌ها از این پس از طریق همین پنل انجام می‌شود."
    )
    try:
        await callback.message.edit_text(text, reply_markup=_panel_choose_keyboard(available, active))
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=_panel_choose_keyboard(available, active))
    await callback.answer("✅ ذخیره شد.")


@router.message(F.text == "🔀 انتخاب پنل VPN فعال")
async def admin_panel_choose_open_msg(message: types.Message):
    if not _is_admin(message.from_user.id):
        return
    available = _available_panels()
    if not available:
        await message.answer("هیچ پنل VPNی متصل نیست. ابتدا مرزبان یا پاسارگارد را وصل کنید.")
        return
    active = _active_panel_id()
    text = (
        "🔀 انتخاب پنل VPN فعال\n\n"
        "ساخت/تمدید/فعال‌سازی خودکار سرویس‌ها از طریق پنلی انجام می‌شود که اینجا به‌عنوان پنل فعال انتخاب شود."
    )
    await message.answer(text, reply_markup=_panel_choose_keyboard(available, active))


# ---------------------------------------------------------------------------
# 🩺 سلامت ربات / 🚀 وضعیت کش / 🧩 تنظیم بساز سرویس خودت / 📋 گزارش فعالیت ادمین
#
# 🐛 فیکس: برچسب این چهار دکمه در منوی ادمین وجود داشت، ولی هیچ هندلری
# (نه message و نه callback) برایشان باقی نمانده بود — یعنی زدن‌شان هیچ
# اتفاقی نمی‌انداخت. (ممیزی callback این‌ها را نمی‌گرفت چون دکمه‌های
# ریپلای‌کیبورد با «متن» کار می‌کنند نه callback_data.)
# ---------------------------------------------------------------------------
def _health_report_text() -> str:
    import platform
    lines = ["🩺 سلامت ربات", ""]
    try:
        cur = db.get_connection().cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        lines.append("🗄 دیتابیس: ✅ در دسترس" + (" (Turso/libsql)" if db.USE_TURSO else " (SQLite محلی)"))
    except Exception as exc:
        lines.append(f"🗄 دیتابیس: ❌ خطا — {str(exc)[:120]}")

    try:
        lines.append(f"👥 کل کاربران: {len(db.get_all_users()):,}")
        lines.append(f"🛒 مشتریان: {db.count_customers():,}")
        lines.append(f"🧾 رسیدهای در انتظار: {len(db.get_pending_receipts(limit=200)):,}")
    except Exception:
        lines.append("📊 آمار: ❌ قابل خواندن نبود")

    try:
        panels_list = db.list_vpn_panels()
        enabled = [p for p in panels_list if p.get("enabled")]
        lines.append(f"🖥 پنل‌ها: {len(enabled)} فعال از {len(panels_list)}")
    except Exception:
        lines.append("🖥 پنل‌ها: ❌ قابل خواندن نبود")

    try:
        c = cache.stats()
        lines.append(f"🚀 کش: {c['entries']:,} کلید | نرخ اصابت {c['hit_rate']}٪")
    except Exception:
        pass

    lines.append("")
    lines.append(f"🐍 پایتون: {platform.python_version()}")
    lines.append(f"⏰ زمان سرور: {to_jalali_str(now_tehran())} (تهران)")
    return "\n".join(lines)


@router.message(F.text == "🩺 سلامت ربات")
async def admin_health_message(message: types.Message):
    if not _admin_perm(message.from_user.id, "health"):
        return
    await message.answer(_health_report_text(), reply_markup=admin_health_keyboard())


@router.callback_query(F.data == "admin_health")
async def admin_health_cb(callback: types.CallbackQuery):
    if not _admin_perm(callback.from_user.id, "health"):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    try:
        await callback.message.edit_text(_health_report_text(), reply_markup=admin_health_keyboard())
    except TelegramBadRequest:
        pass
    await callback.answer("🔄 بروزرسانی شد.")


def _cache_status_text() -> str:
    c = cache.stats()
    return (
        "🚀 وضعیت کش\n\n"
        f"🔑 کلیدهای ذخیره‌شده: {c['entries']:,}\n"
        f"✅ اصابت (hit): {c['hits']:,}\n"
        f"❌ عدم اصابت (miss): {c['misses']:,}\n"
        f"📈 نرخ اصابت: {c['hit_rate']}٪\n\n"
        "هرچه نرخ اصابت بالاتر باشد، ربات کوئری کمتری به دیتابیس می‌زند و سریع‌تر است."
    )


@router.message(F.text == "🚀 وضعیت کش")
async def admin_cache_message(message: types.Message):
    if not _admin_perm(message.from_user.id, "health"):
        return
    await message.answer(_cache_status_text(), reply_markup=admin_cache_keyboard())


@router.callback_query(F.data == "admin_cache_status")
async def admin_cache_cb(callback: types.CallbackQuery):
    if not _admin_perm(callback.from_user.id, "health"):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    try:
        await callback.message.edit_text(_cache_status_text(), reply_markup=admin_cache_keyboard())
    except TelegramBadRequest:
        pass
    await callback.answer("🔄 بروزرسانی شد.")


@router.callback_query(F.data == "cache_clear_do")
async def admin_cache_clear(callback: types.CallbackQuery):
    if not _admin_perm(callback.from_user.id, "health"):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    cache.clear_all()
    await callback.message.edit_text(_cache_status_text(), reply_markup=admin_cache_keyboard())
    await callback.answer("🧹 کش پاک شد.")


def _custom_build_settings_text() -> str:
    s = db.get_effective_custom_build_settings()
    return (
        "🧩 تنظیم «بساز سرویس خودت»\n\n"
        f"💰 قیمت هر گیگ: {s['price_per_gb']:,} تومان\n"
        f"📅 قیمت هر ۳۰ روز: {s['price_per_30_days']:,} تومان\n\n"
        f"📦 حجم مجاز: از {s['min_gb']} تا {s['max_gb']} گیگ\n"
        f"⏳ مدت مجاز: از {s['min_days']} تا {s['max_days']} روز\n\n"
        "برای تغییر، روی دکمه‌ی زیر بزن."
    )


@router.message(F.text == "🧩 تنظیم بساز سرویس خودت")
async def admin_custom_build_message(message: types.Message):
    if not _admin_perm(message.from_user.id, "settings"):
        return
    await message.answer(_custom_build_settings_text(), reply_markup=admin_custom_build_keyboard())


@router.callback_query(F.data == "admin_custom_build_settings")
async def admin_custom_build_cb(callback: types.CallbackQuery):
    if not _admin_perm(callback.from_user.id, "settings"):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    try:
        await callback.message.edit_text(_custom_build_settings_text(), reply_markup=admin_custom_build_keyboard())
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data == "cbset_edit")
async def admin_custom_build_edit_start(callback: types.CallbackQuery, state: FSMContext):
    if not _admin_perm(callback.from_user.id, "settings"):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    s = db.get_effective_custom_build_settings()
    await state.set_state(AdminStates.waiting_custom_build_settings)
    await callback.message.answer(
        "🧩 شش مقدار را در یک خط و با فاصله (یا کاما) بفرست، به همین ترتیب:\n\n"
        "`قیمت‌هر‌گیگ قیمت‌۳۰روز حداقل‌گیگ حداکثر‌گیگ حداقل‌روز حداکثر‌روز`\n\n"
        f"مقادیر فعلی:\n`{s['price_per_gb']} {s['price_per_30_days']} {s['min_gb']} {s['max_gb']} {s['min_days']} {s['max_days']}`",
        parse_mode="Markdown",
    )
    await callback.answer()


@router.message(AdminStates.waiting_custom_build_settings)
async def admin_custom_build_edit_save(message: types.Message, state: FSMContext):
    if not _admin_perm(message.from_user.id, "settings"):
        return
    raw = (message.text or "").replace(",", " ").replace("٬", " ").split()
    try:
        if len(raw) != 6:
            raise ValueError
        vals = [int(x) for x in raw]
        if any(v < 0 for v in vals):
            raise ValueError
        if vals[2] > vals[3] or vals[4] > vals[5]:
            await message.answer("❌ حداقل نمی‌تواند از حداکثر بیشتر باشد. دوباره بفرست:")
            return
        db.set_custom_build_override(*vals)
    except Exception:
        await message.answer("❌ باید دقیقاً ۶ عدد مثبت با فاصله بفرستی. دوباره تلاش کن:")
        return
    await state.clear()
    await message.answer("✅ تنظیمات ذخیره شد.\n\n" + _custom_build_settings_text(), reply_markup=admin_custom_build_keyboard())


def _audit_logs_text(page: int = 0, per_page: int = 10) -> tuple[str, bool]:
    """🆕 بند ۱۶ — گزارش فعالیت ادمین با صفحه‌بندی، تا لیست طولانی از سقف
    ۴۰۹۶ کاراکتری تلگرام رد نشود و پیام اصلاً ارسال نشود.

    ⚠️ نکته‌ی مهم: این پروژه دو جدول لاگ جدا دارد که هر دو واقعاً پر می‌شوند:
      • admin_activity_logs → از طریق db.log_admin_action (اقدامات پنل ادمین)
      • admin_audit_log     → از طریق db.audit_event (رویدادهای سیستمی/سفارش)
    اگر فقط یکی خوانده شود، نیمی از تاریخچه نامرئی می‌ماند. اینجا هر دو
    خوانده و بر اساس زمان با هم ادغام می‌شوند.
    """
    rows = []
    try:
        for lg in db.get_recent_admin_logs(limit=300):
            rows.append({
                "when": lg.get("created_at") or "",
                "actor": lg.get("admin_name") or lg.get("admin_id") or "-",
                "action": lg.get("action") or "-",
                "target": lg.get("target") or "",
                "detail": lg.get("details") or "",
            })
    except Exception:
        logger.exception("خواندن admin_activity_logs ناموفق بود")
    try:
        for lg in db.get_recent_audit_logs(limit=300):
            rows.append({
                "when": lg.get("created_at") or "",
                "actor": lg.get("actor_name") or lg.get("actor_id") or lg.get("actor_type") or "سیستم",
                "action": lg.get("action") or "-",
                "target": f"{lg.get('target_type') or ''} {lg.get('target_id') or ''}".strip(),
                "detail": lg.get("details") or "",
            })
    except Exception:
        logger.exception("خواندن admin_audit_log ناموفق بود")

    rows.sort(key=lambda r: str(r["when"]), reverse=True)
    total = len(rows)
    start = page * per_page
    chunk = rows[start:start + per_page]
    if not chunk:
        return ("📋 گزارش فعالیت ادمین\n\nهنوز هیچ فعالیتی ثبت نشده.", False)

    lines = [f"📋 گزارش فعالیت ادمین — {total} مورد", f"نمایش {start + 1} تا {start + len(chunk)}", ""]
    for r in chunk:
        when = r["when"] or "-"
        try:
            when = to_jalali_str(datetime.strptime(str(when)[:19], "%Y-%m-%d %H:%M:%S"))
        except Exception:
            pass
        line = f"👮 {r['actor']} — {r['action']}"
        if r["target"]:
            line += f" ({r['target']})"
        if r["detail"]:
            line += f"\n   ↳ {str(r['detail'])[:120]}"
        lines.append(f"{line}\n   🕐 {when}")
    return ("\n".join(lines), start + per_page < total)


@router.message(F.text == "📋 گزارش فعالیت ادمین")
async def admin_audit_logs_message(message: types.Message):
    if not _admin_perm(message.from_user.id, "logs"):
        return
    text, has_next = _audit_logs_text(0)
    await message.answer(text, reply_markup=admin_audit_logs_keyboard(0, has_next))


@router.callback_query(F.data.startswith("auditpage_"))
async def admin_audit_logs_page(callback: types.CallbackQuery):
    if not _admin_perm(callback.from_user.id, "logs"):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    page = int(callback.data.replace("auditpage_", ""))
    text, has_next = _audit_logs_text(page)
    try:
        await callback.message.edit_text(text, reply_markup=admin_audit_logs_keyboard(page, has_next))
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data == "admin_audit_logs")
async def admin_audit_logs_cb(callback: types.CallbackQuery):
    if not _admin_perm(callback.from_user.id, "logs"):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    text, has_next = _audit_logs_text(0)
    try:
        await callback.message.edit_text(text, reply_markup=admin_audit_logs_keyboard(0, has_next))
    except TelegramBadRequest:
        pass
    await callback.answer()

# ─────────────────────────────────────────────────────────────────────────────
# تنظیمات رفرال (مستقل از بخش اطلاعات ربات)
@router.callback_query(F.data == "admin_referral_settings")
async def admin_referral_settings_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    settings = db.get_referral_settings()
    text = (
        "🎁 تنظیمات سیستم رفرال\n\n"
        f"💰 پاداش معرفی: {settings['reward_amount']:,} تومان\n"
        f"📦 حداقل حجم: {settings['min_volume_gb']} گیگ\n"
        f"📏 شرط حجم: {'روشن' if settings['min_volume_enabled'] else 'خاموش'}\n"
        f"💳 شرط خرید پولی: {'روشن' if settings['paid_purchase_required'] else 'خاموش'}\n"
        f"🔒 قفل پاداش با خروج از کانال: {'روشن' if settings.get('channel_leave_lock_enabled', True) else 'خاموش'}\n"
        f"🚨 سیستم جریمه: {'روشن' if settings.get('channel_leave_penalty_enabled', False) else 'خاموش'}\n"
        f"💸 جریمه کیف‌پول: {settings.get('penalty_wallet_amount', 0):,} تومان\n"
        f"📉 جریمه سرویس: {settings.get('penalty_service_gb', 0)} گیگ"
    )
    await callback.message.edit_text(text, reply_markup=admin_referral_settings_menu(settings))
    await callback.answer()


# تنظیمات کیف‌پول (مستقل)
@router.callback_query(F.data == "admin_wallet_settings")
async def admin_wallet_settings_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    settings = db.get_wallet_settings()
    text = (
        "💳 تنظیمات کیف‌پول\n\n"
        f"📉 حداقل شارژ: {settings['min_topup']:,} تومان\n"
        f"📈 حداکثر شارژ: {settings['max_topup']:,} تومان\n"
        f"🔘 مبالغ پیشنهادی: {'  /  '.join(f'{a:,}' for a in settings['quick_amounts'])}"
    )
    await callback.message.edit_text(text, reply_markup=admin_wallet_settings_menu(settings))
    await callback.answer()


# روش‌های پرداخت
@router.callback_query(F.data == "admin_payment_methods")
async def admin_payment_methods_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    text = (
        "💳 مدیریت روش‌های پرداخت\n\n"
        "در این بخش می‌توانید درگاه‌های پرداخت فعال را مشاهده و مدیریت کنید."
    )
    await callback.message.edit_text(text, reply_markup=admin_payment_methods_menu())
    await callback.answer()


# تنظیمات لاگ
@router.callback_query(F.data == "admin_log_settings")
async def admin_log_settings_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    from database import Database as _DB
    log_channel = db.get_setting("log_order_channel_id", "")
    text = (
        "📋 مدیریت لاگ‌ها\n\n"
        f"📡 کانال لاگ سفارشات: {log_channel or 'تنظیم نشده'}\n\n"
        "برای دریافت لاگ خودکار سفارشات، ربات را به کانال اضافه کنید و آی‌دی کانال را وارد نمایید."
    )
    await callback.message.edit_text(text, reply_markup=admin_log_settings_menu())
    await callback.answer()


@router.callback_query(F.data == "admin_order_log_channel")
async def admin_order_log_channel_set(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_for_log_channel)
    await callback.message.edit_text(
        "📡 لطفاً آی‌دی کانال لاگ سفارشات را وارد کنید:\n"
        "(مثال: @mychannel یا -1001234567890)"
    )
    await callback.answer()


@router.message(AdminStates.waiting_for_log_channel)
async def admin_order_log_channel_receive(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    channel_id = message.text.strip()
    db.set_setting("log_order_channel_id", channel_id)
    await state.clear()
    await message.answer(
        f"✅ کانال لاگ سفارشات به {channel_id} تنظیم شد.",
        reply_markup=admin_log_settings_menu()
    )


# مینی‌اپ ادمین
@router.callback_query(F.data == "admin_miniapp_open")
async def admin_miniapp_open_handler(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
    from bot_info import BOT_WEBAPP_BASE_URL
    webapp_url = f"{BOT_WEBAPP_BASE_URL}/admin-panel"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖥 باز کردن پنل ادمین", web_app=WebAppInfo(url=webapp_url))],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back")]
    ])
    await callback.message.edit_text(
        "🖥 پنل مدیریت (Mini App)\n\nبرای باز کردن پنل مدیریت گرافیکی روی دکمه زیر کلیک کنید:",
        reply_markup=kb
    )
    await callback.answer()


# ═══════════════════════════════════════════════════════════════════════════════
# 💳 مدیریت روش‌های پرداخت (با قابلیت افزودن ارز دیجیتال دلخواه)
# ═══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "paymt_card")
async def paymt_card_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ دسترسی ندارید.", show_alert=True); return
    pm = _pm(); card = pm.card
    st = "✅ فعال" if card.enabled else "❌ غیرفعال"
    text = (
        "💳 کارت‌به‌کارت دستی\n\n"
        f"وضعیت: {st}\n"
        f"شماره کارت: {card.number or 'تنظیم نشده'}\n"
        f"نام صاحب: {card.owner or 'تنظیم نشده'}"
    )
    await callback.message.edit_text(text, reply_markup=admin_card_info_menu(card))
    await callback.answer()


@router.callback_query(F.data == "paymt_card_toggle")
async def paymt_card_toggle(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True); return
    pm = _pm(); pm.update_card(enabled=not pm.card.enabled)
    await paymt_card_open(callback)


@router.callback_query(F.data == "paymt_card_number")
async def paymt_card_number_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True); return
    await state.set_state(AdminStates.waiting_card_number)
    await callback.message.edit_text("💳 شماره کارت جدید را وارد کنید (16 رقم):")
    await callback.answer()


@router.message(AdminStates.waiting_card_number)
async def paymt_card_number_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    num = (message.text or "").replace("-", "").replace(" ", "").strip()
    _pm().update_card(number=num); await state.clear()
    await message.answer(f"✅ شماره کارت ذخیره شد: {num}", reply_markup=admin_card_info_menu(_pm().card))


@router.callback_query(F.data == "paymt_card_owner")
async def paymt_card_owner_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True); return
    await state.set_state(AdminStates.waiting_card_owner)
    await callback.message.edit_text("👤 نام صاحب کارت را وارد کنید:")
    await callback.answer()


@router.message(AdminStates.waiting_card_owner)
async def paymt_card_owner_save(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    _pm().update_card(owner=(message.text or "").strip()); await state.clear()
    await message.answer("✅ نام صاحب کارت ذخیره شد.", reply_markup=admin_card_info_menu(_pm().card))


@router.callback_query(F.data == "paymt_wallet")
async def paymt_wallet_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True); return
    pm = _pm(); w = pm.wallet
    st = "✅ فعال" if w.enabled else "❌ غیرفعال"
    text = (
        "💰 پرداخت از کیف‌پول\n\n"
        f"وضعیت: {st}\n"
        f"حداقل موجودی لازم: {w.min_balance:,} تومان"
    )
    await callback.message.edit_text(text, reply_markup=admin_wallet_info_menu(w))
    await callback.answer()


@router.callback_query(F.data == "paymt_wallet_toggle")
async def paymt_wallet_toggle(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True); return
    pm = _pm(); pm.update_wallet(enabled=not pm.wallet.enabled)
    await paymt_wallet_open(callback)


@router.callback_query(F.data == "paymt_uniquepay")
async def paymt_uniquepay_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True); return
    pm = _pm(); u = pm.uniquepay
    st = "✅ فعال" if u.enabled else "❌ غیرفعال"
    text = (
        "🌐 پرداخت آنلاین (UniquePay)\n\n"
        f"وضعیت: {st}\n"
        f"API Key: {(u.api_key or 'تنظیم نشده')[:20]}..."
    )
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("❌ غیرفعال کردن" if u.enabled else "✅ فعال کردن"), callback_data="paymt_uniquepay_toggle", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_payment_methods", style="danger")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "paymt_uniquepay_toggle")
async def paymt_uniquepay_toggle(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True); return
    pm = _pm(); pm.update_uniquepay(enabled=not pm.uniquepay.enabled)
    await paymt_uniquepay_open(callback)


@router.callback_query(F.data == "paymt_crypto")
async def paymt_crypto_open(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True); return
    pm = _pm(); cryptos = pm.cryptos
    active_cnt = sum(1 for c in cryptos if c.enabled)
    text = (
        f"💎 مدیریت ارزهای دیجیتال\n\n"
        f"تعداد کل: {len(cryptos)} ارز | فعال: {active_cnt}\n\n"
        "✅ = فعال | ❌ = غیرفعال\n"
        "🗑 = حذف ارز"
    )
    await callback.message.edit_text(text, reply_markup=admin_crypto_list_menu(cryptos))
    await callback.answer()


@router.callback_query(F.data.startswith("paymt_toggle:"))
async def paymt_crypto_toggle(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True); return
    symbol = callback.data.split(":", 1)[1]
    pm = _pm(); c = pm.get_crypto(symbol)
    if c:
        c.enabled = not c.enabled
        pm._save_cryptos()
        await callback.answer(f"{'✅ فعال' if c.enabled else '❌ غیرفعال'} شد.")
    await paymt_crypto_open(callback)


@router.callback_query(F.data.startswith("paymt_del_coin:"))
async def paymt_crypto_delete(callback: types.CallbackQuery):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True); return
    symbol = callback.data.split(":", 1)[1]
    _pm().remove_crypto(symbol)
    await callback.answer(f"🗑 {symbol} حذف شد.")
    await paymt_crypto_open(callback)


@router.callback_query(F.data == "paymt_add_coin")
async def paymt_add_coin_start(callback: types.CallbackQuery, state: FSMContext):
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True); return
    await state.set_state(AdminStates.waiting_crypto_symbol)
    await callback.message.edit_text(
        "💎 افزودن ارز دیجیتال جدید\n\n"
        "مرحله ۱/۴: نماد ارز را وارد کنید\n"
        "(مثال: SOL, MATIC, BNB, ETH)"
    )
    await callback.answer()


@router.message(AdminStates.waiting_crypto_symbol)
async def paymt_add_coin_symbol(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    sym = (message.text or "").strip().upper()
    await state.update_data(new_coin_symbol=sym)
    await state.set_state(AdminStates.waiting_crypto_name)
    await message.answer(f"✅ نماد: {sym}\n\nمرحله ۲/۴: نام کامل ارز را وارد کنید\n(مثال: Solana, Polygon, BNB)")


@router.message(AdminStates.waiting_crypto_name)
async def paymt_add_coin_name(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    await state.update_data(new_coin_name=(message.text or "").strip())
    await state.set_state(AdminStates.waiting_crypto_network)
    await message.answer("مرحله ۳/۴: نام شبکه را وارد کنید\n(مثال: Solana, ERC20, BEP20, TRC20)")


@router.message(AdminStates.waiting_crypto_network)
async def paymt_add_coin_network(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    await state.update_data(new_coin_network=(message.text or "").strip())
    await state.set_state(AdminStates.waiting_crypto_wallet)
    await message.answer("مرحله ۴/۴: آدرس کیف‌پول خود را وارد کنید:")


@router.message(AdminStates.waiting_crypto_wallet)
async def paymt_add_coin_wallet(message: types.Message, state: FSMContext):
    if not _is_admin(message.from_user.id): return
    data = await state.get_data()
    sym = data.get("new_coin_symbol", "")
    name = data.get("new_coin_name", sym)
    network = data.get("new_coin_network", sym)
    wallet = (message.text or "").strip()
    _pm().add_crypto(symbol=sym, name=name, network=network, emoji="💎", wallet=wallet)
    await state.clear()
    await message.answer(
        f"✅ ارز {sym} ({name}) روی شبکه {network} اضافه شد!\n"
        f"آدرس کیف‌پول: {wallet[:20]}...",
        reply_markup=admin_crypto_list_menu(_pm().cryptos)
    )
