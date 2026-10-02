"""
alerts.py
بررسی دوره‌ای مصرف و تاریخ انقضای سرویس‌های VIP و اطلاع‌رسانی خودکار به کاربر
وقتی ۸۰٪/۹۰٪ حجم مصرف شده یا ۲ روز به پایان سرویس مانده است.
این هشدارها فقط مخصوص سرویس‌های VIP هستند (طبق درخواست کاربر).
"""
import logging
from datetime import datetime

import crypto
import database as db
import bot_info
import ui_editor
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, MessageEntity
from subscription import fetch_subscription_info, usage_bar, days_remaining
from keyboards import back_button
from utils import send_notification_sticker

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 1800  # هر ۳۰ دقیقه یک بار


async def check_usage_alerts(bot):
    """روی همه‌ی سرویس‌های VIP فعال حلقه می‌زند و در صورت لزوم هشدار می‌فرستد."""
    configs = db.get_active_vip_configs()
    for cfg in configs:
        try:
            await _check_single_config(bot, cfg)
        except Exception:
            logger.exception("خطا در بررسی هشدار مصرف برای سرویس %s", cfg.get("id"))


async def _check_single_config(bot, cfg):
    try:
        sub_link = crypto.decrypt_config(cfg["config"])
    except Exception:
        return
    if not sub_link.lower().startswith(("http://", "https://")):
        return

    usage = await fetch_subscription_info(sub_link)
    if not usage:
        return

    user = db.get_user_by_id(cfg["user_id"])
    if not user:
        return

    total = usage.get("total")
    used = (usage.get("upload") or 0) + (usage.get("download") or 0)

    if total:
        percent = min(100, round(used / total * 100))
        if percent >= 90 and not cfg.get("alert_90_sent"):
            await _send_usage_alert(bot, user, cfg, percent)
            db.set_config_alert_sent(cfg["id"], "alert_90_sent")
            db.set_config_alert_sent(cfg["id"], "alert_80_sent")
        elif percent >= 80 and not cfg.get("alert_80_sent"):
            await _send_usage_alert(bot, user, cfg, percent)
            db.set_config_alert_sent(cfg["id"], "alert_80_sent")

    expire_ts = usage.get("expire")
    remaining = days_remaining(expire_ts) if expire_ts else None
    if remaining is not None:
        if 0 <= remaining <= 2 and not cfg.get("alert_expiry_sent"):
            await _send_expiry_alert(bot, user, cfg, remaining)
            db.set_config_alert_sent(cfg["id"], "alert_expiry_sent")

    ended_by_time = remaining is not None and remaining < 0
    ended_by_volume = bool(total) and used >= total
    if (ended_by_time or ended_by_volume) and not cfg.get("alert_ended_sent"):
        await _send_ended_alert(bot, user, cfg, ended_by_volume)
        db.set_config_alert_sent(cfg["id"], "alert_ended_sent")


async def _send_usage_alert(bot, user, cfg, percent):
    bar = usage_bar(percent)
    text = (
        "🔔 هشدار حجم مصرفی سرویس\n\n"
        f"📦 {cfg['plan']}\n\n"
        f"{bar}\n"
        f"✅ شما تا الان {percent}٪ از حجم سرویستون رو مصرف کردید.\n\n"
        "برای جلوگیری از قطعی سرویس، پیشنهاد می‌کنیم همین الان تمدید کنید 🔁"
    )
    sticker_key = "notif_usage_90" if percent >= 90 else "notif_usage_80"
    await _safe_send(bot, user, cfg, text, sticker_key=sticker_key)


async def _send_expiry_alert(bot, user, cfg, remaining):
    days_text = "امروز به پایان می‌رسه" if remaining == 0 else f"فقط {remaining} روز دیگه مونده"
    text = (
        "⏰ هشدار پایان سرویس\n\n"
        f"📦 {cfg['plan']}\n\n"
        f"🟨 سرویس شما {days_text}!\n\n"
        "برای جلوگیری از قطعی، همین الان تمدید کنید 🔁"
    )
    await _safe_send(bot, user, cfg, text, sticker_key="notif_expiry")


async def _send_ended_alert(bot, user, cfg, ended_by_volume: bool):
    reason = "حجم سرویستون" if ended_by_volume else "زمان سرویستون"
    text = (
        "⛔️ سرویس شما به پایان رسید\n\n"
        f"📦 {cfg['plan']}\n\n"
        f"🔴 {reason} شما به پایان رسیده و سرویس منقضی شده است.\n\n"
        "برای ادامه‌ی استفاده، همین الان از بخش خرید اشتراک، سرویس خودتون رو تمدید کن 🔁"
    )
    try:
        await send_notification_sticker(bot, int(user["telegram_id"]), "notif_ended")
    except Exception:
        pass
    try:
        await bot.send_message(
            int(user["telegram_id"]), text,
            reply_markup=back_button("plans", "🛍 خرید اشتراک"),
        )
    except Exception:
        logger.exception("ارسال هشدار پایان سرویس ناموفق بود")


async def _safe_send(bot, user, cfg, text, sticker_key: str | None = None):
    try:
        if sticker_key:
            await send_notification_sticker(bot, int(user["telegram_id"]), sticker_key)
        await bot.send_message(
            int(user["telegram_id"]), text,
            reply_markup=back_button(f"viewconfig_{cfg['id']}", "📦 مشاهده سرویس"),
        )
    except Exception:
        logger.exception("ارسال هشدار مصرف به کاربر %s ناموفق بود", user.get("telegram_id"))


# ---------------------------------------------------------------------------
# 🛎 لاگ همه‌ی سفارش‌های نهایی‌شده (خرید/تمدید/تست رایگان/سرویس سفارشی) در
# کانال «اعتماد»، با قالب ثابت.
# ---------------------------------------------------------------------------
def _mask_telegram_id(telegram_id) -> str:
    """آیدی عددی را برای حفظ حریم خصوصی، در پیام کانال اعتماد به‌شکل ماسک‌شده
    نمایش می‌دهد؛ مثلاً 6512345515 → 65*****515 (۲ رقم اول + ۳ رقم آخر باقی می‌مانند)."""
    s = str(telegram_id or "-")
    if len(s) <= 5:
        return s
    return s[:2] + "*" * (len(s) - 5) + s[-3:]


async def log_order_to_channel(
    bot,
    *,
    order_label: str,
    user: dict,
    username: str | None,
    service_id: str | None,
    service_name: str | None,
    package_text: str,
    amount_text: str,
    expiry_text: str,
):
    from utils import now_tehran, to_jalali_str

    # 🗓 فیکس: تاریخ انقضا هم اگر به‌فرمت میلادی YYYY-MM-DD رسیده باشد
    # (خروجی معمول تولید config)، برای نمایش به شمسی تبدیل می‌شود؛ مقادیر
    # غیرتاریخی مثل «نامحدود» دست‌نخورده می‌مانند. تاریخ خام ذخیره‌شده در
    # دیتابیس (برای محاسبه‌ی انقضای واقعی سرویس) هیچ تغییری نمی‌کند — این فقط
    # متن نمایشی همین پیام لاگ است.
    display_expiry = expiry_text
    if expiry_text and expiry_text not in ("-", "نامحدود", "نامدود"):
        try:
            display_expiry = to_jalali_str(datetime.strptime(expiry_text[:10], "%Y-%m-%d"), with_time=False)
        except Exception:
            display_expiry = expiry_text

    # 🆕 بند ۱۲: «نام سرویس» در لاگ باید همان نامی باشد که واقعاً در ربات و
    # پنل ساخته شده (مثلاً businessvpnbot_39xpsj6h596)، نه اسم پلن فروشگاه
    # («۵۰ مگابایت ۱ ساعته»). service_id دقیقاً همان username ارسالی به پنل
    # است و از قبل هم به اینجا پاس داده می‌شد، فقط استفاده نمی‌شد. این برای
    # هر سه نوع لاگ (خرید، تمدید، تست رایگان) یکجا اعمال می‌شود، چون همه‌شان
    # از همین یک تابع رد می‌شوند.
    real_service_name = service_id or service_name or "-"

    # لاگ سفارش هم مثل پیام‌های کاربر از Editor و Custom Emoji پشتیبانی می‌کند.
    log_values = {
        "order_label": order_label,
        "customer": user.get("name", "-"),
        "telegram_id": _mask_telegram_id(user.get("telegram_id")),
        "service_name": real_service_name,
        "package": package_text,
        "amount": amount_text,
        "expiry": display_expiry,
        "logged_at": f"{to_jalali_str(now_tehran())} (به وقت تهران)",
    }
    log_fallback = (
        "{order_label}\n"
        "👤 مشتری: {customer}\n"
        "🆔 آیدی کاربر: {telegram_id}\n"
        "👤 نام سرویس: {service_name}\n"
        "📦 بسته: {package}\n"
        "💰 مبلغ: {amount}\n"
        "📅 انقضا: {expiry}\n"
        "⏰ زمان: {logged_at}"
    )
    text, raw_entities = ui_editor.render_template("order_log", log_values, fallback=log_fallback)
    entities = None
    if raw_entities:
        entities = [MessageEntity(
            type=e["type"], offset=e["offset"], length=e["length"],
            custom_emoji_id=e.get("custom_emoji_id")
        ) for e in raw_entities]

    reply_markup = None
    bot_username = (bot_info.get("bot_username") or "").strip().lstrip("@")
    if bot_username:
        from keyboards import InlineKeyboardButton as UIInlineKeyboardButton
        reply_markup = InlineKeyboardMarkup(inline_keyboard=[[
            UIInlineKeyboardButton(
                text=ui_editor.get_button("order_log", "order_log_test_buy", "🟢 تست و خرید"),
                url=f"https://t.me/{bot_username}",
                style="success",
                ui_screen="order_log",
                ui_button_key="order_log_test_buy",
            )
        ]])

    try:
        await bot.send_message(
            int(bot_info.get("order_log_channel_id")),
            text,
            reply_markup=reply_markup,
            entities=entities,
            _skip_auto_text=True,
        )
    except Exception:
        logger.exception("ارسال لاگ سفارش به کانال اعتماد ناموفق بود")


async def fetch_username(bot, telegram_id) -> str | None:
    try:
        chat = await bot.get_chat(int(telegram_id))
        return chat.username
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 💳 مانیتورینگ سلامت درگاه پرداخت آنلاین (یونیک‌پی)
# پولر هر ۲۰ ثانیه وضعیت اینوویس‌های در انتظار را چک می‌کند؛ اگر خودِ درگاه
# قطعی/کند باشد، این چک‌ها پشت‌سرهم fail می‌شوند ولی قبلاً هیچ‌جا به ادمین
# اطلاع داده نمی‌شد (فقط لاگ Render که با هر ری‌استارت پاک می‌شود). این بخش
# نرخ fail را در یک بازه‌ی زمانی می‌سنجد و در صورت عبور از آستانه، یک‌بار به
# ادمین پیام می‌دهد (با cooldown تا اسپم نشود).
# ---------------------------------------------------------------------------
import time as _time

UNIQUEPAY_ALERT_COOLDOWN_SECONDS = 30 * 60  # حداقل ۳۰ دقیقه بین دو هشدار مشابه
UNIQUEPAY_FAILURE_RATE_THRESHOLD = 0.5      # اگر بیش از ۵۰٪ چک‌های یک چرخه fail شوند
UNIQUEPAY_MIN_SAMPLE = 3                    # حداقل تعداد نمونه برای معنادار بودن نرخ

_uniquepay_state = {
    "last_check_alert_at": 0.0,
    "last_create_alert_at": 0.0,
    "create_fail_streak": 0,
}


async def report_uniquepay_check_cycle(bot, admin_id, checked: int, failed: int):
    """بعد از هر چرخه‌ی کامل پولر (بررسی همه‌ی اینوویس‌های در انتظار) صدا زده
    می‌شود. اگر نرخ خطا از آستانه بیشتر باشد، یک هشدار (با cooldown) می‌فرستد."""
    if checked < UNIQUEPAY_MIN_SAMPLE or failed == 0:
        return
    rate = failed / checked
    if rate < UNIQUEPAY_FAILURE_RATE_THRESHOLD:
        return

    now = _time.time()
    if now - _uniquepay_state["last_check_alert_at"] < UNIQUEPAY_ALERT_COOLDOWN_SECONDS:
        return
    _uniquepay_state["last_check_alert_at"] = now

    text = (
        "⚠️ هشدار درگاه پرداخت آنلاین (یونیک‌پی)\n\n"
        f"در آخرین چرخه‌ی بررسی، {failed} از {checked} چک وضعیت اینوویس ({round(rate * 100)}٪) "
        "با خطا مواجه شد.\n\n"
        "احتمالاً یونیک‌پی قطعی یا کند شده. تا رفع مشکل، بهتره کاربرها رو به پرداخت "
        "کارت‌به‌کارت یا کیف پول راهنمایی کنی.\n\n"
        "(این هشدار حداکثر هر ۳۰ دقیقه یک‌بار فرستاده می‌شود.)"
    )
    try:
        await bot.send_message(admin_id, text)
    except Exception:
        logger.exception("ارسال هشدار قطعی یونیک‌پی به ادمین ناموفق بود")


async def report_uniquepay_create_failure(bot, admin_id):
    """هر بار که ساخت اینوویس (create_invoice) برای یک کاربر شکست بخورد صدا
    زده می‌شود. بعد از ۳ شکست پشت‌سرهم (بدون هیچ موفقیت میانی)، یک هشدار
    می‌فرستد؛ با موفقیت بعدی، شمارنده صفر می‌شود."""
    _uniquepay_state["create_fail_streak"] += 1
    if _uniquepay_state["create_fail_streak"] < 3:
        return

    now = _time.time()
    if now - _uniquepay_state["last_create_alert_at"] < UNIQUEPAY_ALERT_COOLDOWN_SECONDS:
        return
    _uniquepay_state["last_create_alert_at"] = now

    text = (
        "⚠️ هشدار درگاه پرداخت آنلاین (یونیک‌پی)\n\n"
        f"{_uniquepay_state['create_fail_streak']} کاربر پشت‌سرهم موفق به ساخت لینک پرداخت آنلاین نشدند "
        "و به کارت‌به‌کارت هدایت شدند.\n\n"
        "احتمالاً یونیک‌پی قطعی یا کند شده. بد نیست پنل یونیک‌پی رو چک کنی.\n\n"
        "(این هشدار حداکثر هر ۳۰ دقیقه یک‌بار فرستاده می‌شود.)"
    )
    try:
        await bot.send_message(admin_id, text)
    except Exception:
        logger.exception("ارسال هشدار قطعی یونیک‌پی به ادمین ناموفق بود")


def report_uniquepay_create_success():
    _uniquepay_state["create_fail_streak"] = 0

