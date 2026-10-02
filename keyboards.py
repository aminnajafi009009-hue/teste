"""
keyboards.py
تمام کیبوردهای Inline و Reply ربات. هیچ handlerای نباید خودش InlineKeyboardMarkup
بسازد؛ همه از این فایل صدا زده می‌شوند تا تغییر ظاهر منو در یک‌جا متمرکز باشد.
"""

from aiogram.types import (
    InlineKeyboardMarkup as _RealInlineKeyboardMarkup,
    InlineKeyboardButton as _RealInlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    CopyTextButton,
    WebAppInfo,
)

import database as db
import bot_info
from permissions import PERMISSION_LABELS
from config import UNIQUEPAY_ENABLED, SHAHRAH_ENABLED, ONLINE_PAYMENT_MIN_AMOUNT
from panels import PANEL_TYPE_LABELS, PANEL_TYPES


# fix: callback_data محدودیت 64 بایت دارد (محدودیت Telegram Bot API).
# نام دسته/پلن توسط ادمین قابل‌ساخت است و ممکن است طولانی باشد،
# به همین دلیل هر callback_data قبل استفاده از این تابع رد می‌شود.
def _safe_callback_data(data: str) -> str:
    encoded = data.encode("utf-8")
    if len(encoded) <= 64:
        return data
    return encoded[:64].decode("utf-8", errors="ignore")


# fix: به‌جای ویرایش تک‌تک ۱۵۰+ محلی که InlineKeyboardButton ساخته می‌شود،
# یک Wrapper مرکزی می‌سازیم تا callback_data همه‌ی دکمه‌ها همیشه از این تابع
# رد شود و هیچ دکمه‌ای هرگز به‌خاطر طول callback_data توسط تلگرام رد نشود.
def InlineKeyboardButton(*args, **kwargs):
    """Wrapper مرکزی دکمه‌های اینلاین.

    علاوه بر محدودیت callback_data، نام دکمه، Custom Emoji، و رنگ (style)
    ذخیره‌شده در ویرایشگر UI را اعمال می‌کند. برای دکمه‌های Premium Emoji از
    icon_custom_emoji_id استفاده می‌شود؛ متن خود دکمه همچنان متن معمولی است.

    🎨 رنگ دکمه واقعاً روی تلگرام اثر دارد: طبق Bot API 9.4 (۹ فوریه ۲۰۲۶)،
    فیلد style روی InlineKeyboardButton رسماً پشتیبانی می‌شود. اما طبق
    مستندات رسمی، فقط سه مقدار «primary» (آبی)، «success» (سبز) و «danger»
    (قرمز) معتبرند؛ هر مقدار دیگری (مثلاً «secondary» که در قسمت‌های زیادی
    از این پروژه به‌عنوان «حالت خنثی/پیش‌فرض» استفاده شده) اینجا فیلتر و به
    None تبدیل می‌شود تا هرگز یک درخواست را با یک enum نامعتبر به تلگرام
    خراب نکند؛ None یعنی «رنگ پیش‌فرض اپ» که دقیقاً همان ظاهر قبلی است.
    ادمین می‌تواند از ویرایشگر متن/دکمه رنگ هر دکمه‌ی ثابت را عوض کند؛ آن
    انتخاب همیشه اولویت دارد و رنگ هاردکد در پایتون را override می‌کند.
    """
    _VALID_STYLES = {"primary", "success", "danger"}
    try:
        import ui_editor
        current = kwargs.get("text")
        cb = kwargs.get("callback_data")
        screen = kwargs.pop("ui_screen", None)
        button_key = kwargs.pop("ui_button_key", None)
        style_override_applied = False
        if screen and button_key and current:
            current = ui_editor.get_button(screen, button_key, current)
            meta = ui_editor.get_button_meta(screen, button_key)
            if meta.get("custom_emoji_id"):
                kwargs.setdefault("icon_custom_emoji_id", meta["custom_emoji_id"])
            if meta.get("style"):
                kwargs["style"] = meta["style"]
                style_override_applied = True
        if cb is not None:
            cb = str(cb)
            kwargs["callback_data"] = _safe_callback_data(cb)
            if current:
                if screen and screen in ui_editor.SCREENS:
                    for pattern, default in ui_editor.SCREENS[screen].get("buttons", []):
                        if ui_editor._pattern_matches(pattern, cb):
                            current = ui_editor.get_button(screen, pattern, current)
                            meta = ui_editor.get_button_meta(screen, pattern)
                            if meta.get("custom_emoji_id"):
                                kwargs.setdefault("icon_custom_emoji_id", meta["custom_emoji_id"])
                            if meta.get("style"):
                                kwargs["style"] = meta["style"]
                                style_override_applied = True
                            break
                else:
                    current = ui_editor.override_button_text(cb, current)
                    meta = ui_editor.button_meta_for_callback(cb)
                    if meta and meta.get("custom_emoji_id"):
                        kwargs.setdefault("icon_custom_emoji_id", meta["custom_emoji_id"])
                    if meta and meta.get("style"):
                        kwargs["style"] = meta["style"]
                        style_override_applied = True
                kwargs["text"] = current
        elif screen:
            kwargs.pop("ui_screen", None)
        if not style_override_applied and kwargs.get("style") not in _VALID_STYLES:
            kwargs.pop("style", None)
    except Exception:
        kwargs.pop("ui_screen", None)
        if kwargs.get("style") not in _VALID_STYLES:
            kwargs.pop("style", None)
    return _RealInlineKeyboardButton(*args, **kwargs)


def _pack_buttons(buttons, columns):
    """چیدمان دکمه‌ها بدون نیاز به تقسیم‌پذیری کامل.

    اگر تعداد دکمه‌ها فرد باشد و columns=2 باشد، مثلاً 7 دکمه
    به شکل 2+2+2+1 چیده می‌شوند. آخرین ردیف می‌تواند کمتر از columns
    دکمه داشته باشد و هرگز به خاطر باقی‌مانده، کل چیدمان رد نمی‌شود.
    """
    try:
        columns = max(1, int(columns or 1))
    except (TypeError, ValueError):
        columns = 1
    return [buttons[i:i + columns] for i in range(0, len(buttons), columns)]


def InlineKeyboardMarkup(*args, **kwargs):
    """سازنده‌ی مرکزی کیبورد.

    ui_screen یک پارامتر داخلی پروژه است و به Telegram ارسال نمی‌شود. وقتی
    صفحه مشخص باشد، تنظیمات ویرایشگر دقیقاً روی همان صفحه اعمال می‌شود؛ دیگر
    از حدس‌زدن صفحه با callbackهای مشترکی مثل «back» استفاده نمی‌کنیم. همین
    تغییر جلوی باگ جابه‌جایی «بازگشت» به ابتدای منوها را می‌گیرد.
    """
    import ui_editor

    screen = kwargs.pop("ui_screen", None)
    markup = _RealInlineKeyboardMarkup(*args, **kwargs)
    # حتی کیبوردهایی که هنوز ui_screen ندارند هم باید قانون عمومی «بازگشت پایین»
    # را رعایت کنند. در صورت نبود Screen فقط همین قانون عمومی اعمال می‌شود.
    try:
        sc = ui_editor.SCREENS.get(screen, {"buttons": []}) if screen else {"buttons": []}
        lay = ui_editor.get_layout(screen)
        order = ui_editor.get_order(screen)
        pos = {cb: i for i, cb in enumerate(order)}

        # دکمه‌های مخفی را حذف می‌کنیم.
        filtered = []
        for row in markup.inline_keyboard:
            new_row = []
            for btn in row:
                cb = getattr(btn, "callback_data", None)
                matched = None
                if cb is not None:
                    for pattern, _label in sc.get("buttons", []):
                        if ui_editor._pattern_matches(pattern, str(cb)):
                            matched = pattern
                            break
                if matched and ui_editor.get_button_meta(screen, matched).get("hidden"):
                    continue
                new_row.append(btn)
            if new_row:
                filtered.append(new_row)

        # فقط دکمه‌هایی که در رجیستری این صفحه هستند در چیدمان ویرایشگر شرکت می‌کنند.
        managed = []
        other = []
        for row in filtered:
            for btn in row:
                cb = getattr(btn, "callback_data", None)
                # fix: اینجا قبلاً به‌جای فقط رشته‌ی callback pattern، کل تاپل
                # (pattern, label) به _pattern_matches پاس داده می‌شد، که
                # همیشه AttributeError می‌داد («'tuple' object has no
                # attribute 'endswith'»). چون این خطا داخل try/except بیرونی
                # بی‌صدا قورت داده می‌شد، عملاً هیچ‌وقت هیچ‌کدوم از قابلیت‌های
                # ویرایشگر روی کیبورد واقعی کاربر اعمال نمی‌شد: نه چیدمان
                # ۲/۳/۴ دکمه در ردیف، نه مخفی‌کردن دکمه، نه قانون «بازگشت
                # همیشه پایین‌ترین ردیف». همین یک خط باگ اصلی شلختگی و
                # کارنکردن ویرایشگر متن/دکمه روی صفحات واقعی بوده.
                if cb is not None and any(ui_editor._pattern_matches(pattern, str(cb)) for pattern, _label in sc.get("buttons", [])):
                    managed.append(btn)
                else:
                    other.append([btn])

        managed.sort(key=lambda b: next(
            (pos.get(pattern, 999) for pattern, _ in sc.get("buttons", []) if ui_editor._pattern_matches(pattern, str(getattr(b, "callback_data", "")))),
            999,
        ))
        cols = lay["columns"] if lay["mode"] == "inline" else 1
        widths = lay.get("row_widths") or []
        if widths:
            new_rows=[]; _pos=0
            for _width in widths:
                if _pos >= len(managed): break
                new_rows.append(managed[_pos:_pos+_width]); _pos += _width
            if _pos < len(managed): new_rows.extend(_pack_buttons(managed[_pos:], cols))
        else:
            new_rows = _pack_buttons(managed, cols)
        if other:
            new_rows.extend(other)

        # دکمه‌های سفارشی‌ای که ادمین از «ویرایشگر متن و دکمه‌ها» به این صفحه
        # اضافه کرده: هر کدام یک ردیف مستقل، درست بالای دکمه‌ی بازگشت.
        if screen:
            for cbtn in ui_editor.get_custom_buttons(screen):
                action_type = cbtn.get("action_type")
                action_value = cbtn.get("action_value") or ""
                style = cbtn.get("style") or "primary"
                try:
                    if action_type == "url":
                        btn = InlineKeyboardButton(text=cbtn["text"], url=action_value, style=style)
                    elif action_type == "webapp":
                        btn = _RealInlineKeyboardButton(text=cbtn["text"], web_app=WebAppInfo(url=action_value))
                    else:
                        btn = InlineKeyboardButton(text=cbtn["text"], callback_data=action_value, style=style)
                    new_rows.append([btn])
                except Exception:
                    continue

        # «بازگشت/انصراف» همیشه پایین‌ترین ردیف باشد؛ حتی اگر در دیتابیس قبلاً
        # ترتیب اشتباه ذخیره شده باشد. این قانون عمداً عمومی است تا هیچ صفحه‌ای
        # نتواند دکمه برگشت را به ابتدای منو ببرد.
        back_rows = []
        normal_rows = []
        for row in new_rows:
            is_back = False
            for btn in row:
                cb = str(getattr(btn, "callback_data", "") or "")
                txt = str(getattr(btn, "text", "") or "")
                if cb == "back" or cb == "admin_back" or "بازگشت" in txt or "انصراف" in txt:
                    is_back = True
                    break
            (back_rows if is_back else normal_rows).append(row)
        markup.inline_keyboard = normal_rows + back_rows
    except Exception:
        import traceback; traceback.print_exc()
        # ویرایشگر UI نباید هیچ‌وقت باعث خراب‌شدن کیبورد اصلی ربات شود.
        return markup
    return markup


# ---------------------------------------------------------------------------
# عضویت اجباری
# ---------------------------------------------------------------------------
def join_channels_keyboard(channels):
    buttons = [[InlineKeyboardButton(text=f"📢 {ch['name']}", url=ch["url"], style="primary")] for ch in channels]
    buttons.append([InlineKeyboardButton(text="✅ عضو شدم", callback_data="check_join", style="success")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------------------------------------------------------------------
# منوی پایین صفحه (Reply Keyboard) — همیشه در دسترس کاربر
# ---------------------------------------------------------------------------
def main_reply_keyboard(user_id=None):
    # منوی پایین مشتری هم از ویرایشگر UI قابل تغییر است.
    # پیش‌فرض همیشه یک دکمه در هر ردیف است؛ ادمین بعداً می‌تواند چیدمان را تغییر دهد.
    import ui_editor
    items = [
        ("plans", "🛒 خرید اشتراک", "success"),
        ("free_test", "🎁 تست رایگان", "success"),
        ("services", "📱 سرویس‌های من", "primary"),
        ("wallet", "💰 کیف پول", "primary"),
        ("referral", "👥 دعوت دوستان", "primary"),
        ("profile", "👤 پروفایل من", "primary"),
        ("support", "👨‍💻 پشتیبانی", "primary"),
        ("guides", "📚 راهنما", "primary"),
        ("agency", "🤝 درخواست نمایندگی", "danger"),
    ]
    if user_id is not None:
        try:
            if db.get_agent(user_id):
                items.append(("agent_manage", "🤝 مدیریت نمایندگی", "success"))
        except Exception:
            pass
    visible = [(key, ui_editor.get_button("main_reply", key, label), style)
               for key, label, style in items
               if not ui_editor.get_button_meta("main_reply", key).get("hidden")]
    lay = ui_editor.get_layout("main_reply")
    cols = lay["columns"] if lay["mode"] == "inline" else 1
    # منوی پایین ذاتاً reply است؛ حالت «inline» در ادیتور یعنی چندتایی در هر ردیف.
    if lay["order"]:
        order = ui_editor.get_order("main_reply")
        pos = {k: i for i, k in enumerate(order)}
        visible.sort(key=lambda x: pos.get(x[0], 999))
    rows = []
    widths = lay.get("row_widths") or []
    if widths:
        _pos=0
        for _width in widths:
            if _pos >= len(visible): break
            chunk=visible[_pos:_pos+_width]; _pos += _width
            rows.append([KeyboardButton(text=text, style=style) for _key,text,style in chunk])
        if _pos < len(visible):
            rows.extend([[KeyboardButton(text=text, style=style) for _key,text,style in chunk] for chunk in _pack_buttons(visible[_pos:], cols)])
    else:
        for chunk in _pack_buttons(visible, cols):
            rows.append([KeyboardButton(text=text, style=style) for _key, text, style in chunk])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=False, one_time_keyboard=False)


def _admin_menu_items(orders_enabled: bool | None = None):
    """Single source of truth for the two admin menus.

    Exactly 22 capabilities are exposed so both the top InlineKeyboard and the
    bottom ReplyKeyboard always render 2 buttons per row.
    """
    if orders_enabled is None:
        try:
            orders_enabled = db.is_orders_enabled()
        except Exception:
            orders_enabled = True
    return [
        ("stats", "📊 آمار", "admin_stats", "primary"),
        ("requests", "📥 صف درخواست‌ها", "admin_request_queue", "success"),
        ("users", "👥 لیست کاربران", "admin_userlist", "primary"),
        ("users", "🔍 جستجوی حرفه‌ای", "admin_search", "primary"),
        ("broadcast", "📢 پیام همگانی", "admin_broadcast", "primary"),
        ("discounts", "🎟 مدیریت تخفیف", "admin_discount", "primary"),
        ("agency", "🤝 نمایندگی (تخفیف VIP)", "admin_agency", "primary"),
        ("plans", "🗂 دسته‌بندی‌های VIP", "admin_vip_categories", "primary"),
        ("vpn_panel", "🖥 مدیریت پنل‌های VPN", "admin_vpn_panels", "primary"),
        ("referrals", "🤝 مدیریت دعوت‌ها", "admin_referrals", "primary"),
        ("tickets", "🎫 مدیریت تیکت‌ها", "admin_tickets", "success"),
        # 🆕 بند ۳: «📣 لینک‌های تبلیغاتی» از زیرمنوی کد تخفیف به منوی اصلی
        # ادمین (همین ریپلای‌کیبورد) منتقل شد تا یک کلیک دورتر نباشد.
        ("discounts", "📣 لینک‌های تبلیغاتی", "admin_campaigns", "success"),
        ("vpn_panel", "🛠 ایجاد سرویس (ادمین)", "admin_create_service", "success"),
        ("guides", "📚 مدیریت راهنما", "admin_guides", "primary"),
        ("texts", "✏️ ویرایش متن و دکمه‌ها", "admin_text_editor", "primary"),
        ("logs", "🦖 لاگ خطاها و Audit", "errlog", "primary"),
        ("logs", "📋 گزارش فعالیت ادمین", "admin_audit_logs", "primary"),
        ("botinfo", "ℹ️ اطلاعات ربات", "admin_botinfo", "primary"),
        ("stickers", "🎬 استیکرهای منو", "admin_stickers", "primary"),
        ("backup", "💾 بکاپ", "admin_backup", "primary"),
        ("settings", "🎁 تنظیم تست رایگان", "admin_free_test_settings", "primary"),
        ("settings", "🔁 تنظیمات تمدید", "admin_renewal_settings", "success"),
        ("settings", "🧩 تنظیم بساز سرویس خودت", "admin_custom_build_settings", "primary"),
        ("health", "🩺 سلامت ربات", "admin_health", "primary"),
        ("health", "🚀 وضعیت کش", "admin_cache_status", "primary"),
        ("manage_admins", "👮 مدیریت ادمین‌ها", "admin_manage_admins", "danger"),
        ("referral_settings", "🎁 تنظیمات رفرال", "admin_referral_settings", "success"),
        ("wallet_settings", "💳 تنظیمات کیف‌پول", "admin_wallet_settings", "primary"),
        ("payment_methods", "💳 روش‌های پرداخت", "admin_payment_methods", "primary"),
        ("log_settings", "📜 مدیریت لاگ‌ها", "admin_log_settings", "primary"),
        ("miniapp", "🖥 پنل مدیریت (Mini App)", "admin_miniapp_open", "success"),
        ("orders_toggle", ("🔴 خاموش کردن سفارشات" if orders_enabled else "🟢 روشن کردن سفارشات"), ("admin_orders_off" if orders_enabled else "admin_orders_on"), ("danger" if orders_enabled else "success")),
    ]

def _admin_menu_allowed(perm: str, permissions: set[str] | None, is_main_admin: bool) -> bool:
    return is_main_admin or permissions is None or perm in permissions


def admin_reply_keyboard(orders_enabled: bool | None = None, permissions: set[str] | None = None, is_main_admin: bool = True):
    if orders_enabled is None:
        try:
            orders_enabled = db.is_orders_enabled()
        except Exception:
            orders_enabled = True

    import ui_editor
    entries = []
    for perm, label, callback, _style in _admin_menu_items(orders_enabled):
        if not _admin_menu_allowed(perm, permissions, is_main_admin):
            continue
        key = callback
        entries.append((key, ui_editor.get_button("admin_panel", key, label)))
    meta_order = ui_editor.get_order("admin_panel")
    pos = {k:i for i,k in enumerate(meta_order)}
    entries.sort(key=lambda x: pos.get(x[0], 999))
    lay = ui_editor.get_layout("admin_panel")
    cols = max(1, int(lay.get("columns") or 2))
    widths=lay.get("row_widths") or []
    rows=[]; _pos=0
    if widths:
        for _width in widths:
            if _pos >= len(entries): break
            chunk=entries[_pos:_pos+_width]; _pos += _width
            rows.append([KeyboardButton(text=t, style="primary") for _k,t in chunk])
        if _pos < len(entries):
            rows.extend([[KeyboardButton(text=t, style="primary") for _k,t in chunk] for chunk in _pack_buttons(entries[_pos:], cols)])
    else:
        rows=[[KeyboardButton(text=t, style="primary") for _k,t in chunk] for chunk in _pack_buttons(entries, cols)]

    if not rows:
        rows = [[KeyboardButton(text="⛔ بدون دسترسی", style="danger")]]

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=False,
        one_time_keyboard=False,
    )


# ---------------------------------------------------------------------------
# ℹ️ اطلاعات ربات — منوی ادمین برای ویرایش هر یک از فیلدهای bot_info + مدیریت کانال‌ها
# ---------------------------------------------------------------------------
def admin_botinfo_menu():
    labels = bot_info.labels()
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"botinfo_edit_{key}", style="primary")]
        for key, label in labels.items()
    ]
    rows.append([InlineKeyboardButton(text="📢 مدیریت کانال‌های اجباری", callback_data="botinfo_channels", style="primary")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_botinfo_wallet_menu(settings: dict):
    amounts_text = " / ".join(f"{a:,}" for a in settings["quick_amounts"])
    rows = [
        [InlineKeyboardButton(text=f"📉 تغییر حداقل شارژ: {settings['min_topup']:,} تومان", callback_data="wset_edit_min_topup", style="primary")],
        [InlineKeyboardButton(text=f"📈 تغییر حداکثر شارژ: {settings['max_topup']:,} تومان", callback_data="wset_edit_max_topup", style="primary")],
        [InlineKeyboardButton(text=f"🔘 دکمه‌های مبلغ پیشنهادی: {amounts_text}", callback_data="wset_edit_quick_amounts", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="botinfo_open", style="danger")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_botinfo_referral_menu(settings: dict):
    min_enabled = settings["min_volume_enabled"]
    paid_required = settings["paid_purchase_required"]
    leave_lock = settings.get("channel_leave_lock_enabled", True)
    penalty_on = settings.get("channel_leave_penalty_enabled", False)
    rows = [
        [InlineKeyboardButton(
            text=("🟢 شرط حداقل حجم روشن" if min_enabled else "🔴 شرط حداقل حجم خاموش"),
            callback_data="refset_toggle_min_volume", style="primary"
        )],
        [InlineKeyboardButton(
            text=("🟢 شرط خرید پولی روشن" if paid_required else "🔴 شرط خرید پولی خاموش"),
            callback_data="refset_toggle_paid_purchase", style="primary"
        )],
        [InlineKeyboardButton(text=f"📦 تغییر حداقل حجم: {settings['min_volume_gb']} گیگ", callback_data="refset_edit_min_volume", style="primary")],
        [InlineKeyboardButton(text=f"💰 تغییر مبلغ رفرال: {settings['reward_amount']:,} تومان", callback_data="refset_edit_reward", style="primary")],
        [InlineKeyboardButton(
            text=("🟢 قفل‌شدن پاداش با لفت‌دادن از کانال: روشن" if leave_lock else "🔴 قفل‌شدن پاداش با لفت‌دادن از کانال: خاموش"),
            callback_data="refset_toggle_channel_leave_lock", style="primary"
        )],
        [InlineKeyboardButton(
            text=("🚨 سیستم جریمه: روشن" if penalty_on else "⚪️ سیستم جریمه: خاموش"),
            callback_data="refset_toggle_channel_leave_penalty", style="danger" if penalty_on else "secondary",
        )],
        [InlineKeyboardButton(text=f"💸 مبلغ جریمه‌ی کیف‌پول: {settings.get('penalty_wallet_amount', 0):,} تومان", callback_data="refset_edit_penalty_wallet", style="primary")],
        [InlineKeyboardButton(text=f"📉 گیگ جریمه از سرویس: {settings.get('penalty_service_gb', 0)} گیگ", callback_data="refset_edit_penalty_gb", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="botinfo_open", style="danger")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_botinfo_field_keyboard(key: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="botinfo_open", style="danger")],
    ])


def admin_botinfo_channels_menu(channels: list):
    rows = []
    for ch in channels:
        name = ch.get("name") or str(ch.get("id"))
        rows.append([
            InlineKeyboardButton(text=f"❌ حذف «{name}»", callback_data=f"botinfo_channel_del_{ch.get('id')}", style="danger"),
        ])
    rows.append([InlineKeyboardButton(text="➕ افزودن کانال جدید", callback_data="botinfo_channel_add", style="success")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="botinfo_open", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)



def admin_referral_settings_menu(settings: dict):
    """کیبورد مستقل تنظیمات رفرال — جدا از اطلاعات ربات"""
    min_enabled = settings["min_volume_enabled"]
    paid_required = settings["paid_purchase_required"]
    leave_lock = settings.get("channel_leave_lock_enabled", True)
    penalty_on = settings.get("channel_leave_penalty_enabled", False)
    rows = [
        [InlineKeyboardButton(
            text=("🟢 شرط حداقل حجم: روشن" if min_enabled else "🔴 شرط حداقل حجم: خاموش"),
            callback_data="refset_toggle_min_volume", style="success" if min_enabled else "danger"
        )],
        [InlineKeyboardButton(
            text=("🟢 شرط خرید پولی: روشن" if paid_required else "🔴 شرط خرید پولی: خاموش"),
            callback_data="refset_toggle_paid_purchase", style="success" if paid_required else "danger"
        )],
        [InlineKeyboardButton(text=f"📦 حداقل حجم: {settings['min_volume_gb']} گیگ", callback_data="refset_edit_min_volume", style="primary")],
        [InlineKeyboardButton(text=f"💰 مبلغ رفرال: {settings['reward_amount']:,} تومان", callback_data="refset_edit_reward", style="primary")],
        [InlineKeyboardButton(
            text=("🟢 قفل پاداش با لفت: روشن" if leave_lock else "🔴 قفل پاداش با لفت: خاموش"),
            callback_data="refset_toggle_channel_leave_lock", style="success" if leave_lock else "danger"
        )],
        [InlineKeyboardButton(
            text=("🚨 سیستم جریمه: روشن" if penalty_on else "⚪️ سیستم جریمه: خاموش"),
            callback_data="refset_toggle_channel_leave_penalty", style="danger" if penalty_on else "primary",
        )],
        [InlineKeyboardButton(text=f"💸 جریمه کیف‌پول: {settings.get('penalty_wallet_amount', 0):,} تومان", callback_data="refset_edit_penalty_wallet", style="primary")],
        [InlineKeyboardButton(text=f"📉 جریمه سرویس: {settings.get('penalty_service_gb', 0)} گیگ", callback_data="refset_edit_penalty_gb", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_wallet_settings_menu(settings: dict):
    """کیبورد مستقل تنظیمات کیف‌پول — جدا از اطلاعات ربات"""
    amounts_text = " / ".join(f"{a:,}" for a in settings["quick_amounts"])
    rows = [
        [InlineKeyboardButton(text=f"📉 حداقل شارژ: {settings['min_topup']:,} تومان", callback_data="wset_edit_min_topup", style="primary")],
        [InlineKeyboardButton(text=f"📈 حداکثر شارژ: {settings['max_topup']:,} تومان", callback_data="wset_edit_max_topup", style="primary")],
        [InlineKeyboardButton(text=f"🔘 مبالغ پیشنهادی: {amounts_text}", callback_data="wset_edit_quick_amounts", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_payment_methods_menu():
    """کیبورد روش‌های پرداخت"""
    rows = [
        [InlineKeyboardButton(text="💳 کارت‌به‌کارت", callback_data="paymt_card", style="primary"),
         InlineKeyboardButton(text="💰 کیف پول", callback_data="paymt_wallet", style="primary")],
        [InlineKeyboardButton(text="🌐 UniqueP ay (آنلاین)", callback_data="paymt_uniquepay", style="primary")],
        [InlineKeyboardButton(text="💎 ارز دیجیتال", callback_data="paymt_crypto", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_log_settings_menu():
    """کیبورد مدیریت لاگ‌ها"""
    rows = [
        [InlineKeyboardButton(text="🦖 لاگ خطاها", callback_data="errlog", style="danger")],
        [InlineKeyboardButton(text="📋 گزارش فعالیت ادمین", callback_data="admin_audit_logs", style="primary")],
        [InlineKeyboardButton(text="📊 لاگ سفارش‌ها", callback_data="admin_order_log_channel", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# منوی اصلی (Inline) — کاربر عادی
# ---------------------------------------------------------------------------
def main_menu():
    return InlineKeyboardMarkup(ui_screen="start", inline_keyboard=[
        [InlineKeyboardButton(text="🛒 خرید اشتراک", callback_data="plans", style="success")],
        [InlineKeyboardButton(text="🎁 تست رایگان", callback_data="buy_plan_test", style="success")],
        [InlineKeyboardButton(text="📱 سرویس‌های من", callback_data="my_configs", style="primary")],
        [InlineKeyboardButton(text="💰 کیف پول", callback_data="wallet", style="primary")],
        [InlineKeyboardButton(text="👥 دعوت دوستان و کسب درآمد", callback_data="referral", style="primary")],
        [InlineKeyboardButton(text="👤 پروفایل من", callback_data="profile", style="primary")],
        [InlineKeyboardButton(text="👨‍💻 پشتیبانی", callback_data="support", style="primary")],
        [InlineKeyboardButton(text="📚 راهنما", callback_data="user_guides", style="primary")],
       
    ])


def back_button(callback_data: str = "back", text: str = "🏠 بازگشت به منوی اصلی", screen: str | None = None):
    """دکمه‌ی بازگشت/انصراف تکی.

    fix: پارامتر screen قبلاً هیچ‌جا در امضای این تابع تعریف نشده بود ولی
    در ده‌ها جای کد (menu.py/plans.py/ticket.py/wallet.py/profile.py) با
    back_button(..., screen="...") صدا زده می‌شد؛ در نتیجه همه‌ی این صفحات
    (راهنما، لغو تیکت، سبد خالی سرویس‌ها، کد تخفیف، کیف پول قفل/آزاد و...)
    با TypeError کرش می‌کردن. الان این پارامتر پذیرفته می‌شه و اگر داده بشه،
    دکمه به ویرایشگر متن/دکمه هم وصل می‌شه تا متن و رنگش هم از پنل ادمین قابل
    تغییر باشه (درست مثل بقیه‌ی دکمه‌های ثابت ربات).
    """
    if screen:
        btn = InlineKeyboardButton(
            text=text, callback_data=callback_data, style="danger",
            ui_screen=screen, ui_button_key="back",
        )
    else:
        btn = InlineKeyboardButton(text=text, callback_data=callback_data, style="danger")
    return InlineKeyboardMarkup(ui_screen=screen, inline_keyboard=[[btn]])


# ✅ کیبورد نمایش‌داده‌شده به کاربر بعد از ارسال رسید کارت‌به‌کارت (خرید سرویس/شارژ کیف پول/سرویس سفارشی)
def receipt_submitted_keyboard():
    return InlineKeyboardMarkup(ui_screen="receipt_submitted", inline_keyboard=[
        [
            InlineKeyboardButton(text="👨‍💻 ارتباط با پشتیبانی", url=bot_info.get_support_url(), style="primary"),
            InlineKeyboardButton(text="🏠 بازگشت به صفحه اصلی", callback_data="back", style="danger"),
        ],
    ])


# ✅ سه دکمه‌ی کمکی زیر پیام «پرداخت کارت به کارت» (خرید سرویس/شارژ کیف پول/سرویس سفارشی):
# دو دکمه‌ی کپی (شماره کارت و مبلغ به ریال) در ردیف بالا، و دکمه‌ی تغییر روش پرداخت
# در ردیف پایین که فاکتور فعلی را منقضی کرده و به مرحله‌ی انتخاب روش پرداخت برمی‌گردد.
def card_payment_actions_keyboard(card_number: str, amount_toman: int, change_method_callback: str):
    amount_rial = amount_toman * 10
    return InlineKeyboardMarkup(ui_screen="card_payment_actions", inline_keyboard=[
        [
            InlineKeyboardButton(text="📋 کپی شماره کارت", copy_text=CopyTextButton(text=str(card_number)), style="primary"),
            InlineKeyboardButton(text="📋 کپی مبلغ به ریال", copy_text=CopyTextButton(text=str(amount_rial)), style="primary"),
        ],
        [
            InlineKeyboardButton(text="🔄 انتخاب روش پرداخت دیگر", callback_data=change_method_callback, style="danger"),
        ],
    ])


def profile_menu():
    return InlineKeyboardMarkup(ui_screen="profile", inline_keyboard=[
        [InlineKeyboardButton(text="💰 کیف پول آزاد", callback_data="wallet_free", style="primary")],
        [InlineKeyboardButton(text="🔒 کیف پول مسدود", callback_data="wallet_locked", style="danger")],
        [InlineKeyboardButton(text="🛒 تاریخچه خرید", callback_data="purchase_history", style="primary")],
        [InlineKeyboardButton(text="📋 تاریخچه تراکنش", callback_data="transactions", style="primary")],
        [InlineKeyboardButton(text="🔗 لینک دعوت اختصاصی", callback_data="referral", style="success")],
        [InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data="back", style="danger")],
    ])


def wallet_menu():
    return InlineKeyboardMarkup(ui_screen="wallet", inline_keyboard=[
        [InlineKeyboardButton(text="💳 شارژ کیف پول", callback_data="charge", style="success")],
        [InlineKeyboardButton(text="🎟 ثبت کد تخفیف", callback_data="use_discount", style="success")],
        [InlineKeyboardButton(text="📋 تراکنش‌های من", callback_data="transactions", style="primary")],
        [InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data="back", style="danger")],
    ])


def charge_amount_keyboard(amounts: list[int] | None = None):
    # 💡 دکمه‌های مبلغ پیشنهادی دیگر ثابت نیستند: از تنظیمات «مدیریت شارژ
    # کیف‌پول» در دیتابیس خوانده می‌شوند (پیش‌فرض: ۵۰,۱۰۰,۲۰۰ هزار تومان).
    if amounts is None:
        import database as db
        amounts = db.get_wallet_settings()["quick_amounts"]
    rows = [
        [InlineKeyboardButton(text=f"💰 {amount:,} تومان", callback_data=f"charge_{amount}", style="primary")]
        for amount in amounts
    ]
    rows.append([InlineKeyboardButton(text="💵 مبلغ دلخواه", callback_data="charge_custom", style="primary")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="wallet", style="danger")])
    return InlineKeyboardMarkup(ui_screen="wallet_charge", inline_keyboard=rows)


def charge_payment_method_keyboard(amount: int):
    """انتخاب روش پرداخت برای شارژ کیف پول. دکمه‌ی «پرداخت آنلاین» فقط وقتی
    نمایش داده می‌شود که درگاه فعال باشد و مبلغ بیشتر از
    ONLINE_PAYMENT_MIN_AMOUNT باشد (برای مبالغ مساوی یا کمتر، درگاه آنلاین
    اصلاً پیشنهاد نمی‌شود و فقط کارت‌به‌کارت در دسترس است)."""
    buttons = []
    if UNIQUEPAY_ENABLED and amount > ONLINE_PAYMENT_MIN_AMOUNT:
        buttons.append(
            [InlineKeyboardButton(text="🌐 پرداخت آنلاین (تایید خودکار)", callback_data=f"chargepay_online_{amount}", style="success")]
        )
    buttons.append([InlineKeyboardButton(text="💳 پرداخت کارت به کارت", callback_data=f"chargepay_card_{amount}", style="success")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="charge", style="danger")])
    return InlineKeyboardMarkup(ui_screen="walletcharge_method", inline_keyboard=buttons)


def online_payment_wallet_keyboard(payment_link: str, online_payment_id: int, white_label: dict | None = None):
    rows = []
    if white_label:
        rows.append([
            InlineKeyboardButton(text="📋 کپی شماره کارت", copy_text=CopyTextButton(text=str(white_label["cardNumber"])), style="primary"),
            InlineKeyboardButton(text="📋 کپی مبلغ", copy_text=CopyTextButton(text=str(white_label["payableAmount"])), style="primary"),
        ])
    rows.append([InlineKeyboardButton(text="💳 پرداخت در صفحه UniquePay", url=payment_link, style="success")])
    rows.append([InlineKeyboardButton(text="✅ پرداخت را انجام دادم / بررسی کن", callback_data=f"checkpay_{online_payment_id}", style="success")])
    rows.append([InlineKeyboardButton(text="🔙 انصراف", callback_data="wallet", style="danger")])
    return InlineKeyboardMarkup(ui_screen="walletcharge_pay_online", inline_keyboard=rows)


def referral_menu():
    return InlineKeyboardMarkup(ui_screen="referral", inline_keyboard=[
        [InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data="back", style="danger")],
    ])


def support_menu():
    return InlineKeyboardMarkup(ui_screen="support", inline_keyboard=[
        [InlineKeyboardButton(text="🎫 ارسال تیکت", callback_data="ticket", style="primary")],
        [InlineKeyboardButton(text="📋 تیکت‌های من", callback_data="my_tickets", style="primary")],
        [InlineKeyboardButton(text="📢 کانال اصلی و پشتیبان", url=bot_info.get_support_url(), style="primary")],
        [InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data="back", style="danger")],
    ])


_TICKET_STATUS_BADGE = {"open": "🟢 باز", "answered": "🟡 پاسخ داده شد", "closed": "🔴 بسته شده"}


def my_tickets_list_keyboard(tickets: list[dict]):
    buttons = [
        [InlineKeyboardButton(
            text=f"{_TICKET_STATUS_BADGE.get(t['status'], t['status'])} — تیکت #{t['id']}",
            callback_data=f"myticketview_{t['id']}", style="primary",
        )]
        for t in tickets
    ]
    buttons.append([InlineKeyboardButton(text="🎫 تیکت جدید", callback_data="ticket", style="success")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="support", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def ticket_thread_user_keyboard(ticket_id: int, status: str):
    buttons = []
    if status != "closed":
        buttons.append([InlineKeyboardButton(text="✍️ پاسخ", callback_data=f"ticketreply_{ticket_id}", style="success")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت به لیست تیکت‌ها", callback_data="my_tickets", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------------------------------------------------------------------
# سرویس‌ها / خرید اشتراک
# ---------------------------------------------------------------------------
def plans_menu():
    return InlineKeyboardMarkup(ui_screen="plans", inline_keyboard=[
        [InlineKeyboardButton(text="🚀 سرور VIP (V2Ray)", callback_data="plans_vip", style="success")],
        [InlineKeyboardButton(text="✨〰️〰️〰️〰️〰️✨", callback_data="noop")],
        [InlineKeyboardButton(text="🚀 کانفیگ خودتو بساز (ویژه VIP) 🛠", callback_data="cbuild_start", style="primary")],
        [InlineKeyboardButton(text="✨〰️〰️〰️〰️〰️✨", callback_data="noop")],
        [InlineKeyboardButton(text="🔙 بازگشت به منوی اصلی", callback_data="back", style="danger")],
    ])


def custom_build_payment_keyboard(show_discount: bool = True):
    buttons = [
        [InlineKeyboardButton(text="👛 پرداخت از کیف پول", callback_data="cbuild_pay_wallet", style="success")],
    ]
    if UNIQUEPAY_ENABLED:
        buttons.append(
            [InlineKeyboardButton(text="🌐 پرداخت آنلاین (تایید خودکار)", callback_data="cbuild_pay_online", style="success")]
        )
    buttons.append([InlineKeyboardButton(text="💳 پرداخت کارت به کارت", callback_data="cbuild_pay_card", style="success")])
    if show_discount:
        buttons.append([InlineKeyboardButton(text="🎟 ثبت کد تخفیف", callback_data="discount_cbuild", style="primary")])
    buttons.append([InlineKeyboardButton(text="🔙 انصراف", callback_data="plans", style="danger")])
    return InlineKeyboardMarkup(ui_screen="cbuild_payment_method", inline_keyboard=buttons)


def custom_build_cancel_keyboard():
    return InlineKeyboardMarkup(ui_screen="custom_build", inline_keyboard=[
        [InlineKeyboardButton(text="🔙 انصراف", callback_data="plans", style="danger")],
    ])

def renew_payment_keyboard():
    buttons=[[InlineKeyboardButton(text="💰 پرداخت از کیف پول", callback_data="cbuild_pay_wallet", style="success")]]
    if UNIQUEPAY_ENABLED:
        buttons.append([InlineKeyboardButton(text="🌐 پرداخت آنلاین", callback_data="cbuild_pay_online", style="success")])
    buttons.append([InlineKeyboardButton(text="💳 کارت به کارت", callback_data="cbuild_pay_card", style="success")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="renew_cancel", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def _renewal_edit_menu(category_id:int, plan_key:str|None=None):
    target=f"plan_{plan_key}" if plan_key else f"cat_{int(category_id)}"
    st=bot_info.get_renewal_plan_settings(plan_key,category_id) if plan_key else bot_info.get_renewal_settings(category_id)
    mode_labels={"day":"⏳ فقط زمان","gb":"📦 فقط حجم","both":"📦⏳ حجم و زمان"}
    day_opts=str(st.get("day_options") or "30,60,90")
    gb_opts=str(st.get("gb_options") or "10,20,50")
    rows=[
        [InlineKeyboardButton(text=f"🔁 نوع تمدید: {mode_labels.get(st.get('mode'),'⏳ فقط زمان')}",callback_data=f"renewsetmode_{target}",style="primary")],
        [InlineKeyboardButton(text=f"💰 قیمت هر روز: {int(st.get('price_day') or 0):,}",callback_data=f"renewset_price_day_{target}",style="primary"), InlineKeyboardButton(text=f"💰 قیمت هر گیگ: {int(st.get('price_gb') or 0):,}",callback_data=f"renewset_price_gb_{target}",style="primary")],
        [InlineKeyboardButton(text=f"⬇️ حداقل روز: {int(st.get('min_day') or 1)}",callback_data=f"renewset_min_day_{target}",style="primary"), InlineKeyboardButton(text=f"⬆️ حداکثر روز: {'نامحدود' if not int(st.get('max_day') or 0) else int(st.get('max_day'))}",callback_data=f"renewset_max_day_{target}",style="primary")],
        [InlineKeyboardButton(text=f"⬇️ حداقل گیگ: {int(st.get('min_gb') or 1)}",callback_data=f"renewset_min_gb_{target}",style="primary"), InlineKeyboardButton(text=f"⬆️ حداکثر گیگ: {'نامحدود' if not int(st.get('max_gb') or 0) else int(st.get('max_gb'))}",callback_data=f"renewset_max_gb_{target}",style="primary")],
        [InlineKeyboardButton(text=f"🔘 دکمه‌های روز: {day_opts}",callback_data=f"renewset_options_day_{target}",style="primary")],
        [InlineKeyboardButton(text=f"🔘 دکمه‌های گیگ: {gb_opts}",callback_data=f"renewset_options_gb_{target}",style="primary")],
    ]
    if plan_key:
        rows.append([InlineKeyboardButton(text="🔙 بازگشت به تنظیمات دسته",callback_data=f"renewsetscope_{int(category_id)}",style="danger")])
    else:
        rows.append([InlineKeyboardButton(text="🔙 بازگشت",callback_data="admin_renewal_settings",style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_renewal_categories_menu():
    cats=db.get_vip_categories() or []
    rows=[[InlineKeyboardButton(text=str(c.get('name') or c.get('id')),callback_data=f"renewsetcat_{int(c['id'])}",style="primary")] for c in cats]
    if not rows: rows=[[InlineKeyboardButton(text="⚠️ دسته‌ای ثبت نشده",callback_data="noop",style="danger")]]
    rows.append([InlineKeyboardButton(text="🔙 بازگشت",callback_data="admin_back",style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_renewal_scope_menu(category_id:int):
    plans=db.get_vip_plans(int(category_id)) or []
    rows=[[InlineKeyboardButton(text="🌐 همه پلن‌های این دسته",callback_data=f"renewsetscope_all_{int(category_id)}",style="success")]]
    rows += [[InlineKeyboardButton(text=f"📦 فقط {p.get('name') or p.get('plan_key')}",callback_data=f"renewsetscope_plan_{int(category_id)}_{p['plan_key']}",style="primary")] for p in plans]
    rows.append([InlineKeyboardButton(text="🔙 بازگشت",callback_data="admin_renewal_settings",style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_renewal_category_menu(category_id:int): return _renewal_edit_menu(category_id)
def admin_renewal_plan_menu(category_id:int,plan_key:str): return _renewal_edit_menu(category_id,plan_key)
def admin_renewal_mode_menu(category_id:int,plan_key:str|None=None):
    target=f"plan_{plan_key}" if plan_key else f"cat_{int(category_id)}"
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏳ فقط زمان",callback_data=f"renewsetmodeval_day_{target}",style="primary")],[InlineKeyboardButton(text="📦 فقط حجم",callback_data=f"renewsetmodeval_gb_{target}",style="primary")],[InlineKeyboardButton(text="📦⏳ حجم و زمان",callback_data=f"renewsetmodeval_both_{target}",style="primary")],[InlineKeyboardButton(text="🔙 بازگشت",callback_data=(f"renewsetscope_{category_id}" if plan_key else f"renewsetscope_all_{category_id}"),style="danger")]])

def renew_mode_keyboard():
    """منوی انتخاب حالت تمدید برای مسیر قدیمی custom-build.

    این callbackها با renew_mode_start در handlers/plans.py هماهنگ هستند.
    مسیر جدید renewcfg_* نوع تمدید را مستقیماً از تنظیمات دسته/پلن می‌گیرد
    و این کیبورد فقط برای سازگاری با callbackهای renew_* قدیمی نگه داشته شده است.
    """
    return InlineKeyboardMarkup(ui_screen="renew_menu", inline_keyboard=[
        [InlineKeyboardButton(text="⏳ تمدید زمان", callback_data="renewmode_time", style="success")],
        [InlineKeyboardButton(text="🗜 تمدید حجم", callback_data="renewmode_volume", style="success")],
        [InlineKeyboardButton(text="🔁 تمدید حجم و زمان", callback_data="renewmode_both", style="success")],
        [InlineKeyboardButton(text="🔙 انصراف", callback_data="plans", style="danger")],
    ])


def _plans_keyboard(plans_dict: dict, icon: str, discount_percent: int = 0):
    buttons = []
    for key, plan in plans_dict.items():
        price = plan["price"]
        if discount_percent:
            price = int(price * (1 - discount_percent / 100))
        buttons.append([InlineKeyboardButton(
            text=f"{icon} {plan['name']} — {price:,} تومان",
            callback_data=f"buy_{key}"
        , style="success")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="plans", style="danger")])
    return InlineKeyboardMarkup(ui_screen="plan_select", inline_keyboard=buttons)


def vip_categories_keyboard():
    """مرحله‌ی اول خرید VIP: لیست دسته‌بندی‌ها (بعداً از پنل ادمین می‌توان دسته‌ی
    جدید اضافه کرد؛ همه‌شان اینجا خودکار ظاهر می‌شوند)."""
    buttons = []
    for cat in db.get_vip_categories():
        buttons.append([InlineKeyboardButton(text=f"🚀 {cat['name']}", callback_data=f"vipcat_{cat['key']}", style="primary")])
    if not buttons:
        buttons.append([InlineKeyboardButton(text="😔 فعلاً هیچ دسته‌ای موجود نیست", callback_data="noop", style="primary")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="plans", style="danger")])
    return InlineKeyboardMarkup(ui_screen="vip_category_list", inline_keyboard=buttons)


def vip_category_plans_keyboard(category_key: str, discount_percent: int = 0):
    """مرحله‌ی دوم: پلن‌های داخل یک دسته‌ی VIP خاص."""
    cat = db.get_vip_category(category_key)
    plans = db.get_vip_plans(cat["id"]) if cat else []
    buttons = []
    for plan in plans:
        price = plan["price"]
        if discount_percent:
            price = int(price * (1 - discount_percent / 100))
        buttons.append([InlineKeyboardButton(
            text=f"🚀 {plan['name']} — {price:,} تومان", callback_data=f"buy_{plan['plan_key']}"
        , style="primary")])
    if not buttons:
        buttons.append([InlineKeyboardButton(text="😔 فعلاً هیچ پلنی در این دسته نیست", callback_data="noop", style="primary")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت به دسته‌بندی‌ها", callback_data="plans_vip", style="danger")])
    return InlineKeyboardMarkup(ui_screen="vip_plans", inline_keyboard=buttons)






def all_plans_discount_keyboard(discount_percent: int):
    return _plans_keyboard(db.get_all_plans(), "📅", discount_percent)


def free_test_confirm_keyboard(plan_key: str):
    """🆕 صفحه‌ی تایید تست رایگان: چون قیمت تست رایگان صفر است، هیچ روش پرداختی
    (کیف پول/کارت/آنلاین) نشان داده نمی‌شود و فقط یک دکمه‌ی سبز تایید وجود دارد که
    همان هندلر موجود پرداخت از کیف پول (pay_wallet_) را صدا می‌زند و چون قیمت صفر است، کسر
    شدن از کیف پول بدون هیچ مشکلی انجام می‌شود و سرویس بلافاصله از پنل فعال نگاشته‌شده ساخته
    و ارسال می‌شود (auto_fulfill_vip_via_panel).
    """
    return InlineKeyboardMarkup(ui_screen="free_test", inline_keyboard=[
        [InlineKeyboardButton(text="⚡️ همین الان تست رایگان بگیر", callback_data=f"pay_wallet_{plan_key}", style="success")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="plans", style="danger")],
    ])


def purchase_payment_keyboard(plan_key: str, show_discount: bool = True):
    buttons = [
        [InlineKeyboardButton(text="👛 پرداخت از کیف پول", callback_data=f"pay_wallet_{plan_key}", style="success")],
    ]
    if UNIQUEPAY_ENABLED:
        buttons.append(
            [InlineKeyboardButton(text="🌐 پرداخت آنلاین (تایید خودکار)", callback_data=f"pay_online_{plan_key}", style="success")]
        )
    buttons.append([InlineKeyboardButton(text="💳 پرداخت کارت به کارت", callback_data=f"pay_card_{plan_key}", style="success")])
    if show_discount:
        buttons.append([InlineKeyboardButton(text="🎟 ثبت کد تخفیف", callback_data=f"discount_plan_{plan_key}", style="primary")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="plans", style="danger")])
    return InlineKeyboardMarkup(ui_screen="plan_payment_method", inline_keyboard=buttons)


def online_payment_keyboard(payment_link: str, online_payment_id: int, white_label: dict | None = None, back_callback: str = "plans"):
    rows = []
    if white_label:
        rows.append([
            InlineKeyboardButton(text="📋 کپی شماره کارت", copy_text=CopyTextButton(text=str(white_label["cardNumber"])), style="primary"),
            InlineKeyboardButton(text="📋 کپی مبلغ", copy_text=CopyTextButton(text=str(white_label["payableAmount"])), style="primary"),
        ])
    rows.append([InlineKeyboardButton(text="💳 پرداخت در صفحه UniquePay", url=payment_link, style="primary")])
    rows.append([InlineKeyboardButton(text="✅ پرداخت را انجام دادم / بررسی کن", callback_data=f"checkpay_{online_payment_id}", style="success")])
    rows.append([InlineKeyboardButton(text="🔙 انصراف", callback_data=back_callback, style="danger")])
    return InlineKeyboardMarkup(ui_screen="plan_pay_online", inline_keyboard=rows)


def insufficient_balance_keyboard():
    return InlineKeyboardMarkup(ui_screen="insufficient_balance", inline_keyboard=[
        [InlineKeyboardButton(text="💵 شارژ کیف پول", callback_data="wallet", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="plans", style="danger")],
    ])


# ---------------------------------------------------------------------------
# سرویس‌های من
# ---------------------------------------------------------------------------
def my_configs_menu():
    return InlineKeyboardMarkup(ui_screen="services", inline_keyboard=[
        [InlineKeyboardButton(text="🚀 سرویس‌های VIP من", callback_data="my_configs_vip", style="primary")],
        [InlineKeyboardButton(text="🔎 جستجوی سرویس", callback_data="service_search", style="secondary")],
        [InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data="back", style="danger")],
    ])


def my_configs_list_keyboard(configs, icon: str, back_callback: str, show_search: bool = True):
    # 🆕 اسم روی دکمه‌ی هر سرویس دیگر نام نمایشی پلن فروشگاه نیست؛ همان نامی
    # است که واقعاً در پنل ساخته شده (و داخل خود لینک ساب هم همین است) —
    # چون service_id همیشه دقیقاً همان username ارسالی به پنل است، نیازی به
    # فراخوانی زنده‌ی پنل برای نمایش لیست نیست.
    buttons = [
        [InlineKeyboardButton(text=f"{icon} {cfg.get('service_id') or cfg['plan']}", callback_data=f"viewconfig_{cfg['id']}", style="primary")]
        for cfg in configs
    ]
    if show_search:
        buttons.append([InlineKeyboardButton(text="🔎 جستجوی سرویس", callback_data="service_search", style="secondary")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=back_callback, style="danger")])
    return InlineKeyboardMarkup(ui_screen="my_configs_list_has", inline_keyboard=buttons)


def service_search_results_keyboard(configs, query: str):
    buttons = [
        [InlineKeyboardButton(text=f"🔎 {cfg.get('service_id') or cfg['plan']}", callback_data=f"viewconfig_{cfg['id']}", style="primary")]
        for cfg in configs
    ]
    buttons.append([InlineKeyboardButton(text="🔁 جستجوی دوباره", callback_data="service_search", style="secondary")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت به سرویس‌های من", callback_data="my_configs", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def config_detail_keyboard(cfg_id, sub_link_url: str | None = None, has_qr: bool = False, back_callback: str = "my_configs_vip", sub_link_disabled: bool = False, can_manage_link: bool = False, panel_direct_configs: bool = False):
    """کیبورد جزئیات سرویس VIP: تمدید + کیوآرکد + لینک ساب + حذف سرویس."""
    buttons = [
        [InlineKeyboardButton(text="🔁 تمدید سرویس", callback_data=f"renewcfg_{cfg_id}", style="success")],
    ]
    # وقتی لینک ساب خود کاربر غیرفعال کرده، دکمههای بازکردن/دریافت کانفیگ از روی آن معنا ندارند
    # (چون خود لینک فعلاً در پنل غیرفعال است و هیچ کانفیگی برنمی‌گرداند)
    # تا کاربر به جای یک خطای گنگ متوجه شود وضعیت فعلی را ببیند و اول لینک را فعال کند.
    row = []
    if has_qr:
        row.append(InlineKeyboardButton(text="🖼 مشاهده کیوآرکد", callback_data=f"viewqr_{cfg_id}", style="primary"))
    if sub_link_url and not sub_link_disabled:
        row.append(InlineKeyboardButton(text="🔗 باز کردن لینک ساب", url=sub_link_url, style="primary"))
    if row:
        buttons.append(row)
    if (sub_link_url and not sub_link_disabled) or panel_direct_configs:
        buttons.append([InlineKeyboardButton(text="🔗 دریافت کانفیگ‌های تکی", callback_data=f"mirrorconfigs_{cfg_id}", style="success")])
    if can_manage_link:
        buttons.append([InlineKeyboardButton(text="🔄 تغییر لینک ساب", callback_data=f"usersvclink_{cfg_id}", style="primary")])
        if sub_link_disabled:
            buttons.append([InlineKeyboardButton(text="▶️ فعال‌کردن لینک ساب", callback_data=f"usersvcenable_{cfg_id}", style="success")])
        else:
            buttons.append([InlineKeyboardButton(text="⏸ غیرفعال‌کردن لینک ساب", callback_data=f"usersvcdisable_{cfg_id}", style="danger")])
    buttons.append([InlineKeyboardButton(text="🗑 حذف سرویس", callback_data=f"delconfig_{cfg_id}", style="danger")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت به سرویس‌های VIP من", callback_data=back_callback, style="danger")])
    return InlineKeyboardMarkup(ui_screen="config_detail", inline_keyboard=buttons)


def confirm_change_sublink_keyboard(cfg_id, back_callback: str = "my_configs_vip"):
    """پیام هشدار قبل از تغییر واقعی لینک ساب (قطع دسترسی سایرین)."""
    return InlineKeyboardMarkup(ui_screen="config_detail", inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"viewconfig_{cfg_id}", style="danger")],
        [InlineKeyboardButton(text="✅ تغییر لینک ساب", callback_data=f"usersvclinkconfirm_{cfg_id}", style="success")],
    ])


def confirm_delete_config_keyboard(cfg_id):
    return InlineKeyboardMarkup(ui_screen="config_delete_confirm", inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، حذف کن", callback_data=f"delconfirm_{cfg_id}", style="danger")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data=f"viewconfig_{cfg_id}", style="danger")],
    ])


# ---------------------------------------------------------------------------
# پنل ادمین
# ---------------------------------------------------------------------------
def admin_panel_menu(orders_enabled: bool = True, permissions: set[str] | None = None, is_main_admin: bool = True):
    """Top admin inline menu.

    Uses the exact same capability registry as the bottom admin keyboard.
    Two buttons are intentionally placed on every row.
    """
    buttons = []
    row = []
    for perm, text, cb, style in _admin_menu_items(orders_enabled):
        if not _admin_menu_allowed(perm, permissions, is_main_admin):
            continue
        row.append(InlineKeyboardButton(text=text, callback_data=cb, style=style))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    if not buttons:
        buttons.append([InlineKeyboardButton(text="⛔ بدون دسترسی", callback_data="noop", style="danger")])
    return InlineKeyboardMarkup(ui_screen="admin_panel", inline_keyboard=buttons)

def admin_manage_admins_keyboard(admins=None):
    rows=[[InlineKeyboardButton(text=f"👤 {a.get('name') or a['telegram_id']} — {a['telegram_id']}", callback_data=f"subadm_{a['telegram_id']}", style="primary")] for a in (admins or [])]
    rows.append([InlineKeyboardButton(text="➕ افزودن ادمین فرعی", callback_data="subadm_add", style="success")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_permissions_keyboard(admin_id: str, selected=None):
    selected=set(selected or [])
    rows=[]
    for key,label in PERMISSION_LABELS.items():
        mark="✅" if key in selected else "☑️"
        rows.append([InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"subadmperm_{admin_id}:{key}", style="success" if key in selected else "primary")])
    rows.append([InlineKeyboardButton(text="🗑 حذف این ادمین", callback_data=f"subadmdel_{admin_id}", style="danger")])
    rows.append([InlineKeyboardButton(text="🔙 لیست ادمین‌ها", callback_data="admin_manage_admins", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_back_button():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")]])


def admin_userlist_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 مشتریان فعال (خریدکرده)", callback_data="admin_userlist_active", style="success")],
        [InlineKeyboardButton(text="👥 کل کاربران", callback_data="admin_userlist_all", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")],
    ])


def admin_discount_menu(discounts: list | None = None):
    buttons = []
    for d in (discounts or []):
        value_text = f"{d['amount']:,}ت" if d.get("discount_type") == "amount" else f"{d['percent']}٪"
        buttons.append([InlineKeyboardButton(
            text=f"🎟 {d['code']} | {value_text} | 🔁 {d['uses']}",
            callback_data=f"discdetail_{d['id']}", style="primary",
        )])
    buttons.append([InlineKeyboardButton(text="➕ ساخت کد تخفیف جدید", callback_data="new_discount", style="primary")])
    buttons.append([InlineKeyboardButton(text="🎁 هدیه همگانی", callback_data="admin_gift_all", style="success")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


_DISCOUNT_AUDIENCE_LABELS = {
    "all": "👥 همه‌ی کاربران", "buyers": "🛍 فقط خریداران قبلی",
    "non_buyers": "🆕 فقط بدون خرید قبلی", "specific": "🎯 فقط افراد خاص (با آیدی)",
}


def discount_detail_keyboard(discount_id: int, first_purchase_only: bool = False, audience: str = "all"):
    fp_text = "🆕 تخفیف اولین خرید: ✅ فعال (بزن تا خاموش شود)" if first_purchase_only \
        else "🆕 تخفیف اولین خرید: ❌ غیرفعال (بزن تا فعال شود)"
    aud_label = _DISCOUNT_AUDIENCE_LABELS.get(audience, audience)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💯 ویرایش مقدار تخفیف", callback_data=f"discedit_value_{discount_id}", style="primary")],
        [InlineKeyboardButton(text=f"👥 مخاطب: {aud_label}", callback_data=f"discedit_audience_{discount_id}", style="success")],
        [InlineKeyboardButton(text="🎯 ویرایش پلن‌های مجاز", callback_data=f"discedit_plans_{discount_id}", style="primary")],
        [InlineKeyboardButton(text="🔁 ویرایش تعداد استفاده", callback_data=f"discedit_uses_{discount_id}", style="success")],
        [InlineKeyboardButton(text="💰 ویرایش حداقل مبلغ سفارش", callback_data=f"discedit_minorder_{discount_id}", style="primary")],
        [InlineKeyboardButton(text="🔂 ویرایش سقف استفاده هر کاربر", callback_data=f"discedit_maxuser_{discount_id}", style="primary")],
        [InlineKeyboardButton(text="⏰ ویرایش تاریخ انقضا", callback_data=f"discedit_expiry_{discount_id}", style="primary")],
        [InlineKeyboardButton(text=fp_text, callback_data=f"discedit_firstpurchase_{discount_id}", style="success" if first_purchase_only else "secondary")],
        [InlineKeyboardButton(text="🗑 حذف کد تخفیف", callback_data=f"discdelete_{discount_id}", style="danger")],
        [InlineKeyboardButton(text="🔙 بازگشت به لیست", callback_data="admin_discount", style="primary")],
    ])


# 🆕 دسته‌بندی مخاطب کد تخفیف — دقیقاً هم‌خانواده با admin_broadcast_audience_keyboard
# پایین‌تر؛ «افراد خاص» به فلوی موجود discedit_users_ (وارد کردن آیدی عددی) می‌رود.
def discount_audience_keyboard(discount_id: int, current: str = "all"):
    def mark(v):
        return "✅ " if current == v else ""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{mark('all')}👥 همه‌ی کاربران", callback_data=f"discaud_all_{discount_id}", style="primary")],
        [InlineKeyboardButton(text=f"{mark('buyers')}🛍 فقط خریداران قبلی (تست حساب نمی‌شود)", callback_data=f"discaud_buyers_{discount_id}", style="success")],
        [InlineKeyboardButton(text=f"{mark('non_buyers')}🆕 فقط بدون خرید قبلی", callback_data=f"discaud_non_buyers_{discount_id}", style="secondary")],
        [InlineKeyboardButton(text=f"{mark('specific')}🎯 فقط افراد خاص (با آیدی عددی)", callback_data=f"discedit_users_{discount_id}", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"discdetail_{discount_id}", style="danger")],
    ])


# ---------------------------------------------------------------------------
# 📣 لینک‌های تبلیغاتی (کمپین) — برای هر تبلیغ یک کد اختصاصی ساخته می‌شود و
# لینکش (t.me/<bot>?start=c_<code>) منتشر می‌شود؛ آمار عضو/تبدیل/درآمد هر
# کمپین همین‌جا قابل مشاهده است.
# ---------------------------------------------------------------------------
# 🆕 بند ۱۸ — انتخاب مخاطب پیام همگانی
def admin_broadcast_audience_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 همه‌ی کاربران", callback_data="bcaud_all", style="primary")],
        [InlineKeyboardButton(text="🛒 فقط کسانی که خرید کرده‌اند", callback_data="bcaud_buyers", style="success")],
        [InlineKeyboardButton(text="🕸 فقط کسانی که خرید نکرده‌اند", callback_data="bcaud_non_buyers", style="secondary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")],
    ])


# 🆕 بند ۴ — تأیید نهایی هدیه همگانی
def admin_gift_all_confirm_keyboard(amount: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ بله، {amount:,} تومان به همه بده", callback_data=f"giftall_do_{amount}", style="success")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_discount", style="danger")],
    ])


def admin_campaigns_menu(campaigns: list | None = None):
    buttons = []
    for c in (campaigns or []):
        status = "🟢" if c.get("is_active", 1) else "🔴"
        buttons.append([InlineKeyboardButton(
            text=f"{status} {c['name']} ({c['code']})",
            callback_data=f"campdetail_{c['id']}", style="primary",
        )])
    buttons.append([InlineKeyboardButton(text="➕ ساخت کمپین جدید", callback_data="new_campaign", style="primary")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_discount", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def campaign_detail_keyboard(campaign: dict):
    toggle_text = "⏸ غیرفعال کردن لینک" if campaign.get("is_active", 1) else "▶️ فعال کردن لینک"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data=f"camptoggle_{campaign['id']}", style="secondary")],
        [InlineKeyboardButton(text="✏️ تغییر نام کمپین", callback_data=f"campedit_{campaign['id']}", style="primary")],
        [InlineKeyboardButton(text="🗑 حذف کمپین", callback_data=f"campdelete_{campaign['id']}", style="danger")],
        [InlineKeyboardButton(text="🔙 بازگشت به لیست", callback_data="admin_campaigns", style="primary")],
    ])


def campaign_delete_confirm_keyboard(campaign_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، حذف کن", callback_data=f"campdeleteconfirm_{campaign_id}", style="danger")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data=f"campdetail_{campaign_id}", style="danger")],
    ])


def discount_delete_confirm_keyboard(discount_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، حذف کن", callback_data=f"discdeleteconfirm_{discount_id}", style="danger")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data=f"discdetail_{discount_id}", style="danger")],
    ])


def admin_user_actions_keyboard(uid: str, is_blocked: bool = False, show_pm_link: bool = True):
    block_btn = (
        InlineKeyboardButton(text="✅ رفع مسدودیت کاربر", callback_data=f"toggleblock_{uid}", style="success")
        if is_blocked else
        InlineKeyboardButton(text="🚫 مسدود کردن کاربر", callback_data=f"toggleblock_{uid}", style="danger")
    )
    pm_row = [InlineKeyboardButton(text="✉️ پیام خصوصی به کاربر", callback_data=f"pm_{uid}", style="primary")]
    # دکمه‌ی "رفتن به پیوی کاربر" (لینک tg://user) برای برخی کاربران با تنظیمات حریم‌خصوصی محدودتر
    # توسط تلگرام رد می‌شود، پس handlers/admin.py در صورت خطای BUTTON_USER_PRIVACY_RESTRICTED همین کیبورد را با
    # show_pm_link=False دوباره می‌سازد تا فقط همین دکمه حذف شود.
    if show_pm_link:
        pm_row.append(InlineKeyboardButton(text="💬 رفتن به پیوی کاربر", url=f"tg://user?id={uid}", style="primary"))
    return InlineKeyboardMarkup(inline_keyboard=[
        pm_row,
        [InlineKeyboardButton(text="💰 شارژ دستی", callback_data=f"custom_{uid}", style="primary")],
        # 🆕 بند ۶: ویرایش مستقیم موجودی کیف‌پول، *بدون* هیچ پیام اطلاع‌رسانی
        # به کاربر (برخلاف «شارژ دستی» بالا که به کاربر خبر می‌دهد).
        [InlineKeyboardButton(text="✏️ ویرایش موجودی کیف‌پول (بی‌صدا)", callback_data=f"walletedit_{uid}", style="secondary")],
        [InlineKeyboardButton(text="📒 حسابداری کاربر (تراکنش‌ها/منشأ پول)", callback_data=f"accounting_{uid}_0", style="primary")],
        [InlineKeyboardButton(text="🚀 ارسال کانفیگ VIP (QR)", callback_data=f"sendvip_{uid}", style="primary")],
        [InlineKeyboardButton(text="📦 مشاهده و مدیریت سرویس‌های کاربر", callback_data=f"svcs_{uid}", style="primary")],
        [block_btn],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")],
    ])


def admin_pm_cancel_keyboard(uid: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف از پیام خصوصی", callback_data=f"useropen_{uid}", style="danger")],
    ])


def admin_charge_approval_keyboard(uid: str, amount: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ تأیید {amount:,}", callback_data=f"approve_{uid}_{amount}", style="success")],
        [InlineKeyboardButton(text="💵 مبلغ دلخواه", callback_data=f"custom_{uid}", style="primary")],
        [InlineKeyboardButton(text="❌ رد", callback_data=f"reject_{uid}", style="danger")],
    ])


def admin_purchase_card_approval_keyboard(uid: str, plan_key: str, price: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ تأیید پرداخت ({price:,} ت)", callback_data=f"approvepay|{uid}|{plan_key}|{price}", style="success")],
        [InlineKeyboardButton(text="❌ رد رسید", callback_data=f"rejectpay|{uid}", style="danger")],
    ])


def admin_purchase_notify_keyboard(uid: str, plan_key: str | None = None, order_id: int | None = None):
    suffix = f"|{order_id}" if order_id else ""
    oid = order_id or 0

    auto_row = []
    if plan_key:
        mapping = db.get_panel_map_for_plan_key(plan_key)
        if mapping and mapping.get("enabled"):
            type_label = PANEL_TYPE_LABELS.get(mapping.get("panel_type"), mapping.get("panel_type"))
            auto_row = [[InlineKeyboardButton(
                text=f"📤 ارسال خودکار از پنل {type_label}", callback_data=f"panelsend|{uid}|{plan_key}|{oid}", style="primary"
            )]]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 ارسال کانفیگ VIP (QR) — دستی", callback_data=f"sendvip_{uid}{suffix}", style="primary")],
        *auto_row,
    ])


def admin_custom_order_card_approval_keyboard(order_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تأیید پرداخت", callback_data=f"approvecustom_{order_id}", style="success")],
        [InlineKeyboardButton(text="❌ رد رسید", callback_data=f"rejectcustom_{order_id}", style="danger")],
    ])


def admin_custom_order_notify_keyboard(order_id: int):
    buttons = [
        [InlineKeyboardButton(text="📤 شروع ارسال کانفیگ — دستی", callback_data=f"sendcustomorder_{order_id}", style="primary")],
    ]
    if db.list_vpn_panels(enabled_only=True):
        buttons.append([InlineKeyboardButton(
            text="🧩 ساخت خودکار از یک پنل (کانفیگ خودتو بساز)",
            callback_data=f"panelcustom_{order_id}", style="primary",
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def config_delivery_keyboard(guide_url: str):
    buttons = []
    if guide_url and guide_url.strip().lower().startswith(("http://", "https://")):
        buttons.append([InlineKeyboardButton(
            text="🧑‍🦯 دریافت روش اتصال", url=guide_url, style="primary",
            ui_screen="config_delivery", ui_button_key="guide",
        )])
    return InlineKeyboardMarkup(ui_screen="config_delivery", inline_keyboard=buttons) if buttons else None


def ticket_reply_keyboard(uid: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="↩️ پاسخ", callback_data=f"replyticket_{uid}", style="primary")],
    ])


# ---------------------------------------------------------------------------
# 🎫 مدیریت تیکت‌ها (سمت ادمین) — قبلاً تیکت اصلاً حافظه‌ی پایدار نداشت (فقط
# یک پیام فوروارد‌شده با یک دکمه‌ی پاسخ)، پس هیچ لیست/وضعیت/تاریخچه‌ای هم
# ممکن نبود. الان با جدول tickets/ticket_messages، همه‌ی این‌ها اضافه شده.
# ---------------------------------------------------------------------------
def admin_tickets_menu(counts: dict):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🟢 باز ({counts.get('open', 0)})", callback_data="admtickets_open_0", style="success")],
        [InlineKeyboardButton(text=f"🟡 پاسخ‌داده‌شده ({counts.get('answered', 0)})", callback_data="admtickets_answered_0", style="primary")],
        [InlineKeyboardButton(text=f"🔴 بسته‌شده ({counts.get('closed', 0)})", callback_data="admtickets_closed_0", style="secondary")],
        [InlineKeyboardButton(text="📋 همه‌ی تیکت‌ها", callback_data="admtickets_all_0", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")],
    ])


def admin_tickets_list_keyboard(tickets: list[dict], status: str, offset: int, has_more: bool):
    buttons = [
        [InlineKeyboardButton(
            text=f"{_TICKET_STATUS_BADGE.get(t['status'], t['status'])} — #{t['id']} — {t.get('user_name') or 'کاربر'}",
            callback_data=f"admticketview_{t['id']}", style="primary",
        )]
        for t in tickets
    ]
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"admtickets_{status}_{max(0, offset - 10)}", style="secondary"))
    if has_more:
        nav.append(InlineKeyboardButton(text="➡️ بعدی", callback_data=f"admtickets_{status}_{offset + 10}", style="secondary"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_tickets", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_ticket_detail_keyboard(ticket: dict):
    buttons = []
    if ticket["status"] != "closed":
        buttons.append([InlineKeyboardButton(text="↩️ پاسخ", callback_data=f"admticketreply_{ticket['id']}", style="primary")])
        buttons.append([InlineKeyboardButton(text="✅ بستن تیکت", callback_data=f"admticketclose_{ticket['id']}", style="danger")])
    else:
        buttons.append([InlineKeyboardButton(text="🔓 بازگشایی تیکت", callback_data=f"admticketreopen_{ticket['id']}", style="success")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت به لیست", callback_data=f"admtickets_{ticket['status']}_0", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------------------------------------------------------------------
# 📦 مدیریت سرویس‌های کاربران توسط ادمین
# ---------------------------------------------------------------------------
def admin_services_list_keyboard(configs, uid: str):
    buttons = []
    for cfg in configs:
        icon = "🚀"
        mark = "❌ " if cfg.get("deleted") else ""
        buttons.append([InlineKeyboardButton(
            text=f"{mark}{icon} {cfg['plan']}", callback_data=f"svcdetail_{cfg['id']}"
        , style="primary")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"useractions_{uid}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_service_detail_keyboard(cfg: dict, uid: str):
    cfg_id = cfg["id"]
    is_deleted = bool(cfg.get("deleted"))
    is_vip = True
    buttons = []

    if is_deleted:
        buttons.append([InlineKeyboardButton(text="♻️ بازگردانی سرویس", callback_data=f"svcrestore_{cfg_id}", style="primary")])
        buttons.append([InlineKeyboardButton(text="🗑 حذف همیشگی (غیرقابل بازگشت)", callback_data=f"svcpurge_{cfg_id}", style="danger")])
    else:
        panel_managed = cfg.get("source") in ("shahrah", "marzban", "pasargad") and cfg.get("service_id") and cfg.get("panel_id")
        if panel_managed:
            buttons.append([InlineKeyboardButton(text="🔄 تغییر لینک از پنل", callback_data=f"panelrevoke_{cfg_id}", style="primary")])
        else:
            buttons.append([InlineKeyboardButton(text="✏️ تغییر لینک ساب", callback_data=f"svcedit_link_{cfg_id}", style="primary")])
        if cfg.get("qr_file_id"):
            buttons.append([InlineKeyboardButton(text="🖼 تغییر عکس کیوآرکد", callback_data=f"svcedit_qr_{cfg_id}", style="primary")])

        if panel_managed:
            buttons.append([InlineKeyboardButton(text="🔁 تمدید از پنل", callback_data=f"panelrenew_{cfg_id}", style="success")])
            if cfg.get("sub_link_disabled"):
                buttons.append([InlineKeyboardButton(text="▶️ فعال‌کردن لینک ساب", callback_data=f"panelenable_{cfg_id}", style="success")])
            else:
                buttons.append([InlineKeyboardButton(text="⏸ غیرفعال‌کردن لینک ساب", callback_data=f"paneldisable_{cfg_id}", style="danger")])

        buttons.append([InlineKeyboardButton(text="🗑 حذف سرویس (مخفی از کاربر)", callback_data=f"svcdelete_{cfg_id}", style="danger")])

    buttons.append([InlineKeyboardButton(text="🔙 بازگشت به لیست سرویس‌ها", callback_data=f"svcs_{uid}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_purge_confirm_keyboard(cfg_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، برای همیشه حذف کن", callback_data=f"svcpurgeconfirm_{cfg_id}", style="danger")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data=f"svcdetail_{cfg_id}", style="danger")],
    ])


def admin_request_queue_menu(order_count: int = 0, receipt_count: int = 0):
    order_label = f"📦 سفارش‌های در انتظار ({order_count})" if order_count else "📦 سفارش‌های در انتظار"
    receipt_label = f"🧾 رسیدهای در انتظار تایید ({receipt_count})" if receipt_count else "🧾 رسیدهای در انتظار تایید"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=order_label, callback_data="admin_order_queue", style="primary")],
        [InlineKeyboardButton(text=receipt_label, callback_data="admin_pending_receipts", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")],
    ])


def admin_pending_receipts_keyboard(receipts, custom_receipts):
    """receipts: ردیف‌های جدول pending_receipts (kind='charge' یا 'plan_card').
    custom_receipts: ردیف‌های custom_orders با status='pending' (بساز سرویس خودت)."""
    buttons = []
    for r in receipts:
        if r["kind"] == "charge":
            label = f"💰 شارژ {r['amount']:,} ت — {r['telegram_id']}"
            buttons.append([
                InlineKeyboardButton(text=f"✅ {label}", callback_data=f"approve_{r['telegram_id']}_{r['amount']}", style="success"),
                InlineKeyboardButton(text="❌", callback_data=f"reject_{r['telegram_id']}", style="danger"),
            ])
        else:  # plan_card
            label = f"💳 {r['label']} — {r['amount']:,} ت — {r['telegram_id']}"
            buttons.append([
                InlineKeyboardButton(text=f"✅ {label}", callback_data=f"approvepay|{r['telegram_id']}|{r['extra']}|{r['amount']}", style="success"),
                InlineKeyboardButton(text="❌", callback_data=f"rejectpay|{r['telegram_id']}", style="danger"),
            ])
    for co in custom_receipts:
        buttons.append([
            InlineKeyboardButton(
                text=f"🛠 سفارشی {co['volume_gb']}GB/{co['days']}روز — {co['price']:,} ت",
                callback_data=f"approvecustom_{co['id']}",
                style="success",
            ),
            InlineKeyboardButton(text="❌", callback_data=f"rejectcustom_{co['id']}", style="danger"),
        ])
    if receipts or custom_receipts:
        buttons.append([InlineKeyboardButton(text="🧹 علامت‌گذاری همه به‌عنوان بررسی‌شده", callback_data="clearreceipts_confirm", style="primary")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_request_queue", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_clear_receipts_confirm_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، همه رو علامت بزن", callback_data="clearreceipts_do", style="danger")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_pending_receipts", style="danger")],
    ])


def admin_order_queue_keyboard(orders, custom_orders):
    """orders: هر آیتم باید کلید 'telegram_id' هم داشته باشد (توسط admin.py قبل از صدا زدن اضافه می‌شود)."""
    buttons = []
    for o in orders:
        icon = "🚀"
        prefix = "sendvip"
        buttons.append([
            InlineKeyboardButton(
                text=f"{icon} {o['plan_name']} — {o['price']:,} ت",
                callback_data=f"{prefix}_{o['telegram_id']}|{o['id']}", style="primary",
            ),
            InlineKeyboardButton(text="🗑", callback_data=f"dismissorder_{o['id']}", style="danger"),
        ])
    for co in custom_orders:
        buttons.append([
            InlineKeyboardButton(
                text=f"🛠 سفارش سفارشی #{co['id']} — {co['volume_gb']}GB/{co['days']}روز",
                callback_data=f"sendcustomorder_{co['id']}", style="primary",
            ),
            InlineKeyboardButton(text="🗑", callback_data=f"dismisscustomorder_{co['id']}", style="danger"),
        ])
    if orders or custom_orders:
        buttons.append([InlineKeyboardButton(text="🧹 پاک کردن همه‌ی سفارش‌های این صف", callback_data="clearorders_confirm", style="primary")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_request_queue", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_clear_orders_confirm_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، همه رو پاک کن", callback_data="clearorders_do", style="danger")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_order_queue", style="danger")],
    ])


# ---------------------------------------------------------------------------
# 👥 لیست کاربران با صفحه‌بندی ۱۰تا۱۰تا (مرتب‌شده بر اساس بیشترین خرید)
# ---------------------------------------------------------------------------
def admin_userlist_page_keyboard(users: list, page: int, has_next: bool, list_kind: str = "active"):
    buttons = []
    for u in users:
        buttons.append([InlineKeyboardButton(
            text=f"👤 {u['name']} | 🆔 {u['telegram_id']} | 🛒 {u['total_purchase']:,} ت",
            callback_data=f"useropen_{u['telegram_id']}", style="primary",
        )])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ صفحه قبل", callback_data=f"userpage_{list_kind}_{page - 1}", style="primary"))
    if has_next:
        nav_row.append(InlineKeyboardButton(text="➡️ صفحه بعد", callback_data=f"userpage_{list_kind}_{page + 1}", style="primary"))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_userlist", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------------------------------------------------------------------
# 📚 راهنما و اموزش — فهرست قابل‌رشد از پنل ادمین (متن/عکس/فیلم)
# ---------------------------------------------------------------------------
def user_guides_menu(guides: list):
    if not guides:
        buttons = []
    else:
        buttons = [
            [InlineKeyboardButton(text=f"📖 {g['title']}", callback_data=f"guideopen_{g['id']}", style="primary")]
            for g in guides
        ]
    buttons.append([InlineKeyboardButton(text="🏠 بازگشت به منوی اصلی", callback_data="back", style="primary")])
    return InlineKeyboardMarkup(ui_screen="guides_has", inline_keyboard=buttons)


def user_guide_detail_keyboard():
    return InlineKeyboardMarkup(ui_screen="guides_has", inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت به لیست راهنما", callback_data="user_guides", style="primary")],
    ])


def admin_guides_menu(guides: list):
    buttons = []
    for i, g in enumerate(guides):
        buttons.append([InlineKeyboardButton(text=f"📖 {g['title']}", callback_data=f"guideadminopen_{g['id']}", style="primary")])
    buttons.append([InlineKeyboardButton(text="➕ افزودن راهنما/اموزش جدید", callback_data="guidenew", style="success")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_guide_detail_keyboard(guide_id: int, index: int, total: int):
    move_row = []
    if index > 0:
        move_row.append(InlineKeyboardButton(text="⬆️ بالاتر", callback_data=f"guidemove_{guide_id}_up", style="primary"))
    if index < total - 1:
        move_row.append(InlineKeyboardButton(text="⬇️ پایین‌تر", callback_data=f"guidemove_{guide_id}_down", style="primary"))
    buttons = [move_row] if move_row else []
    buttons += [
        [InlineKeyboardButton(text="✏️ ویرایش عنوان", callback_data=f"guideeditname_{guide_id}", style="primary")],
        [InlineKeyboardButton(text="📝 ویرایش محتوا (متن/عکس/فیلم)", callback_data=f"guideeditcontent_{guide_id}", style="primary")],
        [InlineKeyboardButton(text="🗑 حذف این راهنما", callback_data=f"guidedelete_{guide_id}", style="danger")],
        [InlineKeyboardButton(text="🔙 بازگشت به لیست راهنما", callback_data="admin_guides", style="primary")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_guide_delete_confirm_keyboard(guide_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، حذف کن", callback_data=f"guidedeleteconfirm_{guide_id}", style="danger")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data=f"guideadminopen_{guide_id}", style="danger")],
    ])


def admin_guide_cancel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data="admin_guides", style="danger")],
    ])


def admin_stickers_menu(sections: list[dict]):
    """sections: [{"key": ..., "label": ..., "status_emoji": ...}, ...]"""
    buttons = [
        [InlineKeyboardButton(
            text=f"{s['status_emoji']} {s['label']}",
            callback_data=f"stickeropen_{s['key']}",
            style="primary",
        )]
        for s in sections
    ]
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_sticker_detail_keyboard(section_key: str, has_custom: bool, is_enabled: bool):
    buttons = [
        [InlineKeyboardButton(text="📤 آپلود/تغییر استیکر", callback_data=f"stickerset_{section_key}", style="success")],
    ]
    if is_enabled:
        buttons.append([InlineKeyboardButton(text="🛑 غیرفعال کردن (بدون استیکر)", callback_data=f"stickeroff_{section_key}", style="danger")])
    else:
        buttons.append([InlineKeyboardButton(text="✅ فعال‌سازی دوباره", callback_data=f"stickeron_{section_key}", style="success")])
    if has_custom:
        buttons.append([InlineKeyboardButton(text="♻️ بازگرداندن به پیش‌فرض", callback_data=f"stickerreset_{section_key}", style="primary")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت به لیست بخش‌ها", callback_data="admin_stickers", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_sticker_cancel_keyboard(section_key: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ انصراف", callback_data=f"stickeropen_{section_key}", style="danger")],
    ])


def admin_error_logs_keyboard(logs: list):
    buttons = []
    for log in logs:
        ts = str(log.get("occurred_at") or "")[:16]
        buttons.append([InlineKeyboardButton(
            text=f"⚠️ {ts} | {log['error_type']}",
            callback_data=f"errlogdetail_{log['id']}", style="danger",
        )])
    if logs:
        buttons.append([InlineKeyboardButton(text="🗑 این لاگ پاک‌سازیشون", callback_data="errlogclear", style="danger")])
    buttons.append([InlineKeyboardButton(text="🔄 به‌روزرسانی", callback_data="errlogrefresh", style="primary")])
    buttons.append([InlineKeyboardButton(text="🔗 راهنمای فعال‌سازی Sentry", callback_data="errlogsentryguide", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_error_log_detail_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت به لیست لاگ‌ها", callback_data="errlogrefresh", style="primary")],
    ])


def admin_error_logs_clear_confirm_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، پاکشون", callback_data="errlogclearconfirm", style="danger")],
        [InlineKeyboardButton(text="❌ انصراف", callback_data="errlogrefresh", style="danger")],
    ])


def admin_referrers_page_keyboard(users: list, page: int, has_next: bool):
    buttons = []
    for u in users:
        buttons.append([InlineKeyboardButton(
            text=f"🤝 {u['name']} | 👥 دعوت: {u['invited_count']} | ✅ موفق: {u['successful_invites']}",
            callback_data=f"refdetail_{u['telegram_id']}_{page}", style="primary",
        )])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ صفحه قبل", callback_data=f"refpage_{page - 1}", style="primary"))
    if has_next:
        nav_row.append(InlineKeyboardButton(text="➡️ صفحه بعد", callback_data=f"refpage_{page + 1}", style="primary"))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_referred_detail_keyboard(referrer_uid: str, back_page: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 مشاهدهی کامل کاربر دعوت‌کننده", callback_data=f"useropen_{referrer_uid}", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت به لیست دعوت‌کنندگان", callback_data=f"refpage_{back_page}", style="primary")],
    ])


def admin_accounting_keyboard(uid: str, page: int, has_next: bool):
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ قبل", callback_data=f"accounting_{uid}_{page - 1}", style="primary"))
    if has_next:
        nav_row.append(InlineKeyboardButton(text="➡️ بعد", callback_data=f"accounting_{uid}_{page + 1}", style="primary"))
    buttons = [nav_row] if nav_row else []
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت به کاربر", callback_data=f"useropen_{uid}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------------------------------------------------------------------
# 🎟 ساخت کد تخفیف — نوع تخفیف و پلن‌های قابل‌اعمال
# ---------------------------------------------------------------------------
def discount_type_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💯 درصدی", callback_data="disctype_percent", style="primary")],
        [InlineKeyboardButton(text="💵 مبلغ ثابت (تومان)", callback_data="disctype_amount", style="primary")],
    ])


def discount_plans_select_keyboard(selected: list):
    """با هر بار زدن روی یک پلن، انتخاب/عدم‌انتخابش toggle می‌شود؛ ✅ همه یعنی روی همه‌ی پلن‌ها اعمال شود."""
    buttons = [[InlineKeyboardButton(
        text="✅ همه‌ی پلن‌ها (بدون محدودیت)" if not selected else "☑️ همه‌ی پلن‌ها (بدون محدودیت)",
        callback_data="discplan_all", style="success",
    )]]
    for key, plan in db.get_all_plans().items():
        mark = "☑️" if key in selected else "⬜️"
        buttons.append([InlineKeyboardButton(text=f"{mark} {plan['name']}", callback_data=f"discplan_{key}", style="primary")])
    buttons.append([InlineKeyboardButton(text="✅ تأیید و ادامه", callback_data="discplan_done", style="success")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def discount_plans_edit_keyboard(discount_id: int, selected: list):
    """نسخه‌ی ویرایشِ کد تخفیف موجود؛ همان discount_plans_select_keyboard است اما با
    callback_data متفاوت (discplaned_) تا با مسیر ساخت کد جدید تداخل نکند."""
    buttons = [[InlineKeyboardButton(
        text="✅ همه‌ی پلن‌ها (بدون محدودیت)" if not selected else "☑️ همه‌ی پلن‌ها (بدون محدودیت)",
        callback_data=f"discplaned_{discount_id}_all", style="success",
    )]]
    for key, plan in db.get_all_plans().items():
        mark = "☑️" if key in selected else "⬜️"
        buttons.append([InlineKeyboardButton(text=f"{mark} {plan['name']}", callback_data=f"discplaned_{discount_id}_{key}", style="primary")])
    buttons.append([InlineKeyboardButton(text="✅ ذخیره", callback_data=f"discplaned_{discount_id}_done", style="success")])
    buttons.append([InlineKeyboardButton(text="🔙 انصراف", callback_data=f"discdetail_{discount_id}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------------------------------------------------------------------
# 🤝 نمایندگی — تخفیف خودکار روی VIP برای آیدی عددی‌های خاص
# ---------------------------------------------------------------------------
def admin_agency_menu(agents: list | None = None):
    """لیست نمایندگان به‌صورت دکمه؛ با زدن روی هرکدام دقیقاً همان صفحه‌ی
    مدیریت کاربر (مثل بخش «کاربران») باز می‌شود، به‌علاوه‌ی گزینه‌ی تغییر درصد تخفیف."""
    buttons = []
    for a in (agents or []):
        buttons.append([InlineKeyboardButton(
            text=f"🆔 {a['telegram_id']} | 💯 {a['vip_discount_percent']}٪",
            callback_data=f"agentopen_{a['telegram_id']}", style="primary",
        )])
    buttons.append([InlineKeyboardButton(text="➕ افزودن نماینده", callback_data="new_agent", style="success")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_agent_row_keyboard(telegram_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 حذف این نماینده", callback_data=f"deleteagent_{telegram_id}", style="danger")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_agency", style="primary")],
    ])


def admin_agent_actions_keyboard(uid: str):
    """دقیقاً همان کیبورد مدیریت کاربر (admin_user_actions_keyboard)، به‌علاوه‌ی
    یک دکمه‌ی اضافه برای تغییر درصد تخفیف نمایندگی؛ دکمه‌ی بازگشت هم به لیست
    نمایندگان برمی‌گردد (نه لیست کلی کاربران)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💯 تغییر درصد تخفیف نمایندگی", callback_data=f"editagentpercent_{uid}", style="primary")],
        [InlineKeyboardButton(text="🗂 دسته‌های مشمول تخفیف", callback_data=f"editagentcats_{uid}", style="primary")],
        [InlineKeyboardButton(text="🏷 انتخاب پیشوند ساخت کانفیگ", callback_data=f"editagentprefix_{uid}", style="primary")],
        [InlineKeyboardButton(text="💰 شارژ دستی", callback_data=f"custom_{uid}", style="primary")],
        [InlineKeyboardButton(text="📒 حسابداری کاربر (تراکنش‌ها/منشأ پول)", callback_data=f"accounting_{uid}_0", style="primary")],
        [InlineKeyboardButton(text="🚀 ارسال کانفیگ VIP (QR)", callback_data=f"sendvip_{uid}", style="primary")],
        [InlineKeyboardButton(text="📦 مشاهده و مدیریت سرویس‌های کاربر", callback_data=f"svcs_{uid}", style="primary")],
        [InlineKeyboardButton(text="🗑 حذف این نماینده", callback_data=f"deleteagent_{uid}", style="danger")],
        [InlineKeyboardButton(text="🔙 بازگشت به لیست نمایندگان", callback_data="admin_agency", style="primary")],
    ])


# ---------------------------------------------------------------------------
# 🗂 دسته‌بندی‌های VIP (پنل ادمین) — افزودن دسته‌ی جدید، ورود به هر دسته برای
# افزودن/ویرایش/حذف پلن‌های داخلش + تغییر ترتیب نمایش (⬆️/⬇️) دسته‌ها و پلن‌ها.
# ---------------------------------------------------------------------------
def admin_vip_categories_keyboard():
    buttons = []
    cats = db.get_vip_categories()
    for i, cat in enumerate(cats):
        n = len(db.get_vip_plans(cat["id"]))
        buttons.append([InlineKeyboardButton(
            text=f"🚀 {cat['name']} ({n} پلن)", callback_data=f"admincat_{cat['key']}"
        , style="primary")])
        move_row = []
        if i > 0:
            move_row.append(InlineKeyboardButton(text="⬆️", callback_data=f"movevipcat_{cat['key']}_up", style="primary"))
        if i < len(cats) - 1:
            move_row.append(InlineKeyboardButton(text="⬇️", callback_data=f"movevipcat_{cat['key']}_down", style="primary"))
        if move_row:
            buttons.append(move_row)
    buttons.append([InlineKeyboardButton(text="➕ دسته‌بندی جدید", callback_data="newvipcat", style="primary")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_vip_category_detail_keyboard(category_key: str):
    cat = db.get_vip_category(category_key)
    buttons = []
    if cat:
        plans = db.get_vip_plans(cat["id"])
        for i, plan in enumerate(plans):
            buttons.append([InlineKeyboardButton(
                text=f"📦 {plan['name']} — {plan['price']:,} ت", callback_data=f"vipplan_{plan['plan_key']}"
            , style="primary")])
            move_row = []
            if i > 0:
                move_row.append(InlineKeyboardButton(text="⬆️", callback_data=f"movevipplan_{plan['plan_key']}_up", style="primary"))
            if i < len(plans) - 1:
                move_row.append(InlineKeyboardButton(text="⬇️", callback_data=f"movevipplan_{plan['plan_key']}_down", style="primary"))
            if move_row:
                buttons.append(move_row)
    buttons.append([InlineKeyboardButton(text="➕ افزودن پلن به این دسته", callback_data=f"newvipplan_{category_key}", style="success")])
    buttons.append([InlineKeyboardButton(text="✏️ ویرایش توضیح این دسته", callback_data=f"vipcatdesc_{category_key}", style="primary")])
    buttons.append([InlineKeyboardButton(text="🗑 حذف این دسته (فقط اگر خالی باشد)", callback_data=f"delvipcat_{category_key}", style="danger")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_vip_categories", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_vip_plan_detail_keyboard(plan_key: str, category_key: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ ویرایش نام", callback_data=f"vipplanname_{plan_key}", style="primary")],
        [InlineKeyboardButton(text="💰 ویرایش قیمت", callback_data=f"vipplanprice_{plan_key}", style="primary")],
        [InlineKeyboardButton(text="📦 ویرایش حجم (گیگ)", callback_data=f"vipplangb_{plan_key}", style="primary")],
        [InlineKeyboardButton(text="⏳ ویرایش مدت (روز، ۰=نامحدود)", callback_data=f"vipplandays_{plan_key}", style="primary")],
        [InlineKeyboardButton(text="👥 سقف دستگاه / HWID (۰=نامحدود)", callback_data=f"vipplanlimit_{plan_key}", style="primary")],
        [InlineKeyboardButton(text="🗑 حذف این پلن", callback_data=f"delvipplan_{plan_key}", style="danger")],
        [InlineKeyboardButton(text="🔙 بازگشت به دسته", callback_data=f"admincat_{category_key}", style="primary")],
    ])


# ---------------------------------------------------------------------------

# 🖥 مدیریت پنل‌های VPN — هر سه نوع (شاهراه/مرزبان/پاسارگارد) هم‌زمان
# فعال هستند و هر کدام می‌تواند چند نمونه (Instance) هم‌زمان داشته باشد.
# ---------------------------------------------------------------------------
def admin_vpn_panel_types_keyboard():
    """قدم اول: انتخاب نوع پنل برای مدیریت. هر سه نوع مستقل هم‌زمان قابل فعال‌شدن هستند."""
    buttons = [
        [InlineKeyboardButton(text=PANEL_TYPE_LABELS[t], callback_data=f"vpntype|{t}", style="primary")]
        for t in PANEL_TYPES
    ]
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_vpn_panel_list_keyboard(panel_type: str, panels: list[dict]):
    """لیست نمونه‌های ساخته‌شده از یک نوع پنل (می‌توانند چندتایی باشند
    و همه هم‌زمان فعال بمانند) + دکمه‌ی افزودن نمونه‌ی جدید."""
    buttons = []
    for p in panels:
        mark = "🟢" if p.get("enabled") else "🔴"
        buttons.append([InlineKeyboardButton(
            text=f"{mark} {p['name']}", callback_data=f"vpndetail|{p['id']}", style="primary"
        )])
    buttons.append([InlineKeyboardButton(
        text=f"➕ افزودن پنل {PANEL_TYPE_LABELS.get(panel_type, panel_type)} جدید",
        callback_data=f"vpnadd|{panel_type}", style="success",
    )])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت به انتخاب نوع پنل", callback_data="admin_vpn_panels", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_vpn_panel_detail_keyboard(panel: dict):
    """منوی مدیریت یک نمونه‌ی مشخص از پنل."""
    pid = panel["id"]
    if panel.get("enabled"):
        toggle_text = "🔴 غیرفعال کردن این پنل"
    else:
        toggle_text = "🟢 فعال‌کردن این پنل"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📡 تست اتصال", callback_data=f"vpntest|{pid}", style="primary")],
        [InlineKeyboardButton(text="✏️ ویرایش اطلاعات پنل", callback_data=f"vpnedit|{pid}", style="primary")],
        [InlineKeyboardButton(text="🗂 نگاشت پلن‌ها/بسته‌ها به این پنل", callback_data=f"vpnmap|{pid}", style="primary")],
        [InlineKeyboardButton(text=toggle_text, callback_data=f"vpntoggle|{pid}", style="danger" if panel.get("enabled") else "success")],
        [InlineKeyboardButton(text="🗑 حذف این پنل", callback_data=f"vpndelete|{pid}", style="danger")],
        [InlineKeyboardButton(text="🔙 بازگشت به لیست", callback_data=f"vpntype|{panel['panel_type']}", style="primary")],
    ])


def admin_vpn_panel_delete_confirm_keyboard(panel_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ بله، حذف کن", callback_data=f"vpndeleteconfirm|{panel_id}", style="danger")],
        [InlineKeyboardButton(text="🔙 انصراف", callback_data=f"vpndetail|{panel_id}", style="primary")],
    ])


def admin_vpn_panel_edit_menu_keyboard(panel: dict):
    """فیلدهای قابل‌ویرایش به نوع پنل و روش اتصال انتخاب‌شده بستگی دارد:
    شاهراه همیشه با API Key کار می‌کند. مرزبان/پاسارگارد بسته به auth_method
    یا با یوزرنیم+پسورد یا با یک API Key ثابت کار می‌کنند و ادمین می‌تواند
    با دکمه‌ی «تغییر روش اتصال» بین این دو جابه‌جا شود."""
    pid = panel["id"]
    buttons = [
        [InlineKeyboardButton(text="✏️ نام", callback_data=f"vpneditfield|{pid}|name", style="primary")],
        [InlineKeyboardButton(text="✏️ آدرس پایه (base URL)", callback_data=f"vpneditfield|{pid}|base_url", style="primary")],
    ]
    if panel["panel_type"] == "shahrah":
        buttons.append([InlineKeyboardButton(text="✏️ API Key", callback_data=f"vpneditfield|{pid}|api_key", style="primary")])
    else:
        auth_method = panel.get("auth_method") or "userpass"
        if auth_method == "api_key":
            buttons.append([InlineKeyboardButton(text="✏️ API Key", callback_data=f"vpneditfield|{pid}|api_key", style="primary")])
        else:
            buttons.append([InlineKeyboardButton(text="✏️ نام کاربری", callback_data=f"vpneditfield|{pid}|username", style="primary")])
            buttons.append([InlineKeyboardButton(text="✏️ رمز عبور", callback_data=f"vpneditfield|{pid}|password", style="primary")])
        other = "👤 یوزرنیم/پسورد" if auth_method == "api_key" else "🔑 API Key"
        buttons.append([InlineKeyboardButton(text=f"🔀 تغییر روش اتصال به {other}", callback_data=f"vpnauthswitch|{pid}", style="secondary")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"vpndetail|{pid}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------------------------------------------------------------------
# 🛠 ایجاد سرویس دستی توسط ادمین (برای خودش) — مشابه «بساز سرویس خودت» ولی
# بدون قیمت/محدودیت حجم-روز و بدون نیاز به پرداخت؛ ادمین آزاد است از هر
# پنل فعال و هر تمپلیت/اینباند آن، هر حجم/روزی که بخواهد بسازد.
# ---------------------------------------------------------------------------
def admin_create_service_panel_keyboard(panels_list: list[dict]):
    import panels as _panels
    buttons = [
        [InlineKeyboardButton(text=_panels.panel_label(p), callback_data=f"adminsvcpanel_{p['id']}", style="primary")]
        for p in panels_list
    ]
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_create_service_catalog_keyboard(items: list[dict], panel_id: int):
    buttons = [
        [InlineKeyboardButton(text=p["label"], callback_data=f"adminsvccatalog_{panel_id}_{p['idx']}", style="primary")]
        for p in items
    ]
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_create_service", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_vpn_panel_auth_choice_keyboard(panel_type: str):
    """قدم اضافه‌ی «افزودن پنل جدید» برای انواعی که هم یوزر/پسورد و هم API Key
    را پشتیبانی می‌کنند: ادمین انتخاب می‌کند کدام روش برای این نمونه استفاده شود."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 یوزرنیم و پسورد", callback_data=f"vpnauthadd|{panel_type}|userpass", style="primary")],
        [InlineKeyboardButton(text="🔑 API Key", callback_data=f"vpnauthadd|{panel_type}|api_key", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_vpn_panels", style="danger")],
    ])


def vpn_panel_back_keyboard(panel_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"vpndetail|{panel_id}", style="primary")],
    ])


def admin_vpn_panel_types_cancel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_vpn_panels", style="primary")],
    ])


def admin_vpn_panel_map_menu_keyboard(panel_id: int):
    """قدم اول نگاشت: برای این نمونه‌ی پنل، کدام بخش نگاشت شود؟"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗂 دسته‌بندی‌های VIP", callback_data=f"vpnmapvip|{panel_id}", style="primary")],
        [InlineKeyboardButton(text="🧩 «بساز سرویس خودت»", callback_data=f"vpnmapcustom|{panel_id}", style="primary")],
        [InlineKeyboardButton(text="🧪 «تست رایگان»", callback_data=f"vpnmapfreetest|{panel_id}", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"vpndetail|{panel_id}", style="primary")],
    ])


def vpn_map_category_pick_keyboard(categories: list[dict], scope: str, panel_id: int):
    """لیست دسته‌بندی‌های VIP برای نگاشت پیش‌فرض کل دسته به این نمونه‌ی پنل."""
    buttons = []
    for cat in categories:
        mapping = db.get_panel_plan_map(scope, cat["id"])
        mark = f" ✅ ({mapping['remote_name'] or mapping['remote_ref']})" if mapping else ""
        buttons.append([InlineKeyboardButton(
            text=f"{cat['name']}{mark}", callback_data=f"vpnmapcat|{panel_id}|{scope}|{cat['id']}"
        , style="primary")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"vpnmap|{panel_id}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def vpn_map_vip_category_pick_keyboard(categories: list[dict], panel_id: int):
    """قدم اول نگاشت اختصاصی VIP برای این نمونه‌ی پنل: انتخاب دسته‌بندی."""
    buttons = [
        [InlineKeyboardButton(text=cat["name"], callback_data=f"vpnmapvipcat|{panel_id}|{cat['id']}", style="primary")]
        for cat in categories
    ]
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"vpnmap|{panel_id}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def vpn_map_vip_plans_keyboard(category_id: int, plans: list[dict], panel_id: int):
    """قدم دوم نگاشت اختصاصی VIP: هر پلن با نگاشت اختصاصی خودش به این نمونه‌ی پنل،
    یا نگاشت پیش‌فرض کل دسته."""
    buttons = []
    for p in plans:
        mapping = db.get_panel_plan_map("vip_plan", p["id"])
        mark = f" ✅ ({mapping['remote_name'] or mapping['remote_ref']})" if mapping else " ⚪️ نگاشت‌نشده"
        label = f"{p['name']} — {p['volume_gb']}GB/{p['days']}روز{mark}"
        if len(label) > 64:
            label = label[:61] + "..."
        buttons.append([InlineKeyboardButton(
            text=label, callback_data=f"vpnmapvipplan|{panel_id}|{category_id}|{p['id']}"
        , style="primary")])
        if mapping:
            buttons.append([InlineKeyboardButton(text="🗑 حذف نگاشت این پلن", callback_data=f"vpnmapdelplan|{panel_id}|{p['id']}", style="danger")])
    buttons.append([InlineKeyboardButton(
        text="🗂 نگاشت پیش‌فرض کل این دسته (اختیاری)",
        callback_data=f"vpnmapcat|{panel_id}|vip_category|{category_id}", style="primary",
    )])
    if db.get_panel_plan_map("vip_category", category_id):
        buttons.append([InlineKeyboardButton(text="🗑 حذف نگاشت پیش‌فرض این دسته", callback_data=f"vpnmapdelcat|{panel_id}|vip_category|{category_id}", style="danger")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"vpnmapvip|{panel_id}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def vpn_catalog_pick_keyboard(items: list[dict], panel_id: int):
    """لیست بسته‌ها/تمپلیت‌های واقعی گرفته‌شده از خودِ پنل برای انتخاب نهایی — items هرکدام
    حداقل 'idx' (اندیس محلی در state) و متن نمایشی 'label' داشته باشند."""
    buttons = [
        [InlineKeyboardButton(text=p["label"], callback_data=f"vpncatalogpick|{panel_id}|{p['idx']}", style="primary")]
        for p in items
    ]
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"vpnmap|{panel_id}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------------------------------------------------------------------
# 🧩 نگاشت «بدون تمپلیت» — ادمین بین ساختن سرویس از روی یک تمپلیت آماده‌ی پنل
# یا مستقیم از روی اینباند/گروه‌های زنده‌ی پنل (بدون نیاز به هیچ تمپلیتی)
# انتخاب می‌کند.
# ---------------------------------------------------------------------------
def vpn_map_mode_keyboard(panel_id: int, supports_direct: bool):
    buttons = [[InlineKeyboardButton(text="📦 از تمپلیت پنل استفاده کن", callback_data=f"vpnmapmode|{panel_id}|template", style="primary")]]
    if supports_direct:
        buttons.append([InlineKeyboardButton(text="🧩 بدون تمپلیت (مستقیم از اینباند/گروه)", callback_data=f"vpnmapmode|{panel_id}|direct", style="success")])
    buttons.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data=f"vpnmap|{panel_id}", style="primary")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def vpn_direct_protocol_keyboard(protocols: list[str], panel_id: int):
    rows=[[InlineKeyboardButton(text=f"🔌 پروتکل {proto}", callback_data=f"vpndirectprotocol|{panel_id}|{proto}", style="primary")] for proto in protocols]
    rows.append([InlineKeyboardButton(text="🌐 همه پروتکل‌ها", callback_data=f"vpndirectprotocol|{panel_id}|all", style="success")])
    rows.append([InlineKeyboardButton(text="🔙 انصراف", callback_data=f"vpnmap|{panel_id}", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def vpn_direct_multiselect_keyboard(items: list[dict], panel_id: int, selected: set):
    buttons = []
    for it in items:
        mark = "✅" if it["idx"] in selected else "⬜️"
        buttons.append([InlineKeyboardButton(
            text=f"{mark} {it['label']}", callback_data=f"vpndirecttoggle|{panel_id}|{it['idx']}", style="secondary",
        )])
    buttons.append([InlineKeyboardButton(
        text=f"✅ ذخیره ({len(selected)} انتخاب‌شده)", callback_data=f"vpndirectconfirm|{panel_id}", style="success",
    )])
    buttons.append([InlineKeyboardButton(text="🔙 انصراف", callback_data=f"vpnmap|{panel_id}", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ---------------------------------------------------------------------------
# 🩺 سلامت / 🚀 کش / 🧩 بساز سرویس خودت / 📋 گزارش فعالیت ادمین
# (کیبوردهای این چهار صفحه هم همراه خود صفحات حذف شده بودند)
# ---------------------------------------------------------------------------
def admin_health_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="admin_health", style="primary")],
        [InlineKeyboardButton(text="🚀 وضعیت کش", callback_data="admin_cache_status", style="primary")],
    ])


def admin_cache_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 بروزرسانی آمار", callback_data="admin_cache_status", style="primary")],
        [InlineKeyboardButton(text="🧹 پاک‌کردن کامل کش", callback_data="cache_clear_do", style="danger")],
        [InlineKeyboardButton(text="🩺 سلامت ربات", callback_data="admin_health", style="primary")],
    ])


def admin_custom_build_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ تغییر قیمت‌ها و محدوده‌ها", callback_data="cbset_edit", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")],
    ])


def admin_audit_logs_keyboard(page: int, has_next: bool):
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ قبلی", callback_data=f"auditpage_{page - 1}", style="primary"))
    if has_next:
        nav.append(InlineKeyboardButton(text="➡️ بعدی", callback_data=f"auditpage_{page + 1}", style="primary"))
    rows = [nav] if nav else []
    rows.append([InlineKeyboardButton(text="🔄 بروزرسانی", callback_data="admin_audit_logs", style="primary")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# 📊 بازطراحی کامل صفحه‌ی آمار (بند ۲۳) — مطابق اسکرین‌شاتی که ارسال شد
# ---------------------------------------------------------------------------
def admin_stats_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 آمار کل", callback_data="stat_all", style="success")],
        [InlineKeyboardButton(text="🕐 یک ساعت اخیر", callback_data="stat_hour", style="primary")],
        [
            InlineKeyboardButton(text="☁️ امروز", callback_data="stat_today", style="primary"),
            InlineKeyboardButton(text="☀️ دیروز", callback_data="stat_yesterday", style="primary"),
        ],
        [
            InlineKeyboardButton(text="☀️ ماه فعلی", callback_data="stat_this_month", style="primary"),
            InlineKeyboardButton(text="☁️ ماه قبل", callback_data="stat_last_month", style="primary"),
        ],
        [InlineKeyboardButton(text="🗓 مشاهده آمار در تاریخ مشخص", callback_data="stat_custom", style="secondary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_back", style="danger")],
    ])

def admin_crypto_list_menu(cryptos: list) -> InlineKeyboardMarkup:
    """لیست ارزهای دیجیتال با دکمه افزودن سفارشی"""
    rows = []
    for c in cryptos:
        status = "✅" if c.enabled else "❌"
        rows.append([
            InlineKeyboardButton(text=f"{c.emoji} {c.symbol} ({c.network}) {status}", callback_data=f"paymt_toggle:{c.symbol}", style="primary"),
            InlineKeyboardButton(text="🗑", callback_data=f"paymt_del_coin:{c.symbol}", style="danger"),
        ])
    rows.append([InlineKeyboardButton(text="➕ افزودن ارز دلخواه", callback_data="paymt_add_coin", style="success")])
    rows.append([InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_payment_methods", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_card_info_menu(card) -> InlineKeyboardMarkup:
    """کیبورد مدیریت کارت‌به‌کارت"""
    status = "❌ غیرفعال" if not card.enabled else "✅ فعال"
    rows = [
        [InlineKeyboardButton(text=f"{status}", callback_data="paymt_card_toggle", style="primary")],
        [InlineKeyboardButton(text="💳 شماره کارت", callback_data="paymt_card_number", style="primary"),
         InlineKeyboardButton(text="👤 نام دارنده", callback_data="paymt_card_owner", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_payment_methods", style="danger")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_wallet_info_menu(wallet) -> InlineKeyboardMarkup:
    """کیبورد مدیریت کیف‌پول"""
    rows = [
        [InlineKeyboardButton(text=f"کیف‌پول: ✅ فعال" if wallet.enabled else "کیف‌پول: ❌ غیرفعال", callback_data="paymt_wallet_toggle", style="primary")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="admin_payment_methods", style="danger")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)
