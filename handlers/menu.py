"""
handlers/menu.py
هندلرهای منوی پایین صفحه (Reply Keyboard) که همیشه در دسترس کاربر/ادمین است.

این روتر باید قبل از همه‌ی روترهای دیگر در bot.py ثبت شود تا دکمه‌های این
منو در هر حالتی (حتی وسط یک مکالمه‌ی FSM مثل ارسال رسید یا تیکت) همیشه کار
کنند و کاربر هیچ‌وقت در یک مرحله گیر نکند.
"""

import ui_editor


from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile

import database as db
import crypto
import bot_info
from subscription import is_config_expired
from utils import show_menu_with_sticker
from states import UserStates, AdminStates
from config import ADMIN_ID, PLANS_INTRO_TEXT, DATABASE_PATH
from keyboards import (
    plans_menu,
    my_configs_menu,
    my_configs_list_keyboard,
    wallet_menu,
    profile_menu,
    referral_menu,
    support_menu,
    back_button,
    admin_panel_menu,
    admin_back_button,
    admin_discount_menu,
    admin_userlist_menu,
    purchase_payment_keyboard,
    free_test_confirm_keyboard,
    main_reply_keyboard,
    admin_reply_keyboard,
    admin_referrers_page_keyboard,
    user_guides_menu,
    user_guide_detail_keyboard,
    admin_guides_menu,
)

router = Router(name="menu")


def _is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID or db.is_sub_admin(str(user_id))


def _has_admin_permission(user_id: int, permission: str) -> bool:
    if user_id == ADMIN_ID:
        return True
    if not db.is_sub_admin(str(user_id)):
        return False
    return db.sub_admin_has_permission(str(user_id), permission)


# ---------------------------------------------------------------------------
# منوی کاربر عادی
# ---------------------------------------------------------------------------
@router.message(lambda message: message.text == ui_editor.get_button("main_reply", "plans", "🛒 خرید اشتراک"))
async def menu_plans(message: types.Message, state: FSMContext):
    await state.clear()
    if not db.is_orders_enabled():
        await message.answer(
            ui_editor.get_text("msg_a351fcf2fa", "🔴 ربات به دلیل حجم سفارشات بالا موقتاً بسته می‌باشد.\n\nروشن شدن دوباره‌ی آن اطلاع‌رسانی خواهد شد.")
        )
        return
    # 🧪 تست: استیکر service.webm درست بالای منوی خرید اشتراک
    await show_menu_with_sticker(
        message.bot, message.chat.id, "buy_plans",
        PLANS_INTRO_TEXT, reply_markup=plans_menu(), parse_mode="Markdown",
    )
    
    
@router.message(lambda message: message.text == ui_editor.get_button("main_reply", "free_test", "🎁 تست رایگان"))
async def menu_free_test(message: types.Message, state: FSMContext):
    from config import FREE_TEST_PLAN_KEY

    if not db.is_orders_enabled():
        await message.answer(ui_editor.get_text("msg_91ed1eceef", "🔴 ربات به دلیل حجم سفارشات بالا موقتاً بسته می‌باشد."))
        return

    plan = db.get_effective_plan(FREE_TEST_PLAN_KEY)
    user = db.get_user(message.from_user.id)
    if user is None:
        await message.answer(ui_editor.get_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."))
        return

    await state.clear()
    # fix: تست رایگان قیمتش صفر است، پس هیچ روش پرداختی نشان داده نمی‌شود؛
    # فقط یک متن تشویقی + یک دکمه‌ی سبز ارسال خودکار از پنل فعال (مطابق handlers/plans.py).
    text = (
        f"🎁 {plan['name']}\n\n"
        "🛡 پیش از پرداخت، فقط حرف ما رو باور نکن — خودت امتحانش کن!\n\n"
        "۱۰۰٪ رایگان و بدون نیاز به هیچ پرداختی — همین الان سرعت و پایداری واقعی سرویس رو با چشم خودت ببین.\n\n"
        "⚡️ روی دکمه‌ی سبز زیر بزن؛ کانفیگ تستت همین الان و کاملاً خودکار از پنل فعال ساخته و برات ارسال می‌شود. ✅"
    )
    # 🧪 تست: استیکر test.webm درست بالای منوی تست رایگان
    await show_menu_with_sticker(
        message.bot, message.chat.id, "free_test",
        text, reply_markup=free_test_confirm_keyboard(FREE_TEST_PLAN_KEY),
    )


@router.callback_query(F.data == "buy_plan_test")
async def menu_free_test_callback(callback: types.CallbackQuery, state: FSMContext):
    from config import FREE_TEST_PLAN_KEY

    if not db.is_orders_enabled():
        await callback.answer(ui_editor.get_alert_text("msg_91ed1eceef", "🔴 ربات به دلیل حجم سفارشات بالا موقتاً بسته می‌باشد."), show_alert=True)
        return

    plan = db.get_effective_plan(FREE_TEST_PLAN_KEY)
    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return

    await state.clear()
    text = (
        f"🎁 {plan['name']}\n\n"
        "🛡 پیش از پرداخت، فقط حرف ما رو باور نکن — خودت امتحانش کن!\n\n"
        "۱۰۰٪ رایگان و بدون نیاز به هیچ پرداختی — همین الان سرعت و پایداری واقعی سرویس رو با چشم خودت ببین.\n\n"
        "⚡️ روی دکمه‌ی سبز زیر بزن؛ کانفیگ تستت همین الان و کاملاً خودکار از پنل فعال ساخته و برات ارسال می‌شود. ✅"
    )
    await show_menu_with_sticker(
        callback.bot, callback.message.chat.id, "free_test",
        text, reply_markup=free_test_confirm_keyboard(FREE_TEST_PLAN_KEY),
    )
    await callback.answer()


@router.message(lambda message: message.text == ui_editor.get_button("main_reply", "services", "📱 سرویس‌های من"))
async def menu_my_configs(message: types.Message, state: FSMContext):
    # 🛠 همان فیکس UX سرویس‌های من: دیگر منوی میانیِ تک‌گزینه‌ای نشون نمی‌دیم،
    # مستقیم لیست سرویس‌های VIP کاربر رو نشون می‌دیم.
    await state.clear()
    user = db.get_user(message.from_user.id)
    if user is None:
        await message.answer(ui_editor.get_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."))
        return

    configs = [c for c in db.get_configs_by_type(user["id"], "vip") if not is_config_expired(c)]
    if not configs:
        await show_menu_with_sticker(
            message.bot, message.chat.id, "my_configs_list_empty",
            "🚀 شما هنوز هیچ سرویس VIPی خریداری نکرده‌اید.\n\nبرای خرید، از «🛒 خرید اشتراک» اقدام کنید.",
            reply_markup=back_button("back", "🏠 بازگشت به منوی اصلی", screen="my_configs_list_empty"),
        )
    else:
        await show_menu_with_sticker(
            message.bot, message.chat.id, "my_configs_list_has",
            "🚀 سرویس‌های VIP شما\n\nبرای مشاهده‌ی لینک سابسکریپشن و مدیریت هرکدام، روی نام آن بزنید 👇",
            reply_markup=my_configs_list_keyboard(configs, "🚀", "back"),
        )


@router.message(lambda message: message.text == ui_editor.get_button("main_reply", "wallet", "💰 کیف پول"))
async def menu_wallet(message: types.Message, state: FSMContext):
    await state.clear()
    user = db.get_user(message.from_user.id)
    if user is None:
        await message.answer(ui_editor.get_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."))
        return

    rs = db.get_referral_settings()
    if rs["paid_purchase_required"] and rs["min_volume_enabled"]:
        wallet_condition = f"ℹ️ موجودی در انتظار، پس از خرید حجم {rs['min_volume_gb']} گیگ یا بیشتر توسط فردی که با لینک شما عضو شده، به‌صورت خودکار آزاد می‌شود."
    elif rs["paid_purchase_required"]:
        wallet_condition = "ℹ️ موجودی در انتظار، پس از هر خرید پولی توسط فردی که با لینک شما عضو شده، به‌صورت خودکار آزاد می‌شود."
    else:
        wallet_condition = "ℹ️ پاداش دعوت بدون نیاز به خرید فرد دعوت‌شده، بلافاصله پس از عضویت او آزاد می‌شود."

    text = (
        f"💰 کیف پول شما\n\n"
        f"👛 موجودی قابل استفاده: {user['wallet']:,} تومان\n"
        f"🔒 موجودی در انتظار: {user['locked_wallet']:,} تومان\n\n"
        f"{wallet_condition}"
    )
    await show_menu_with_sticker(message.bot, message.chat.id, "wallet", text, reply_markup=wallet_menu())


@router.message(lambda message: message.text == ui_editor.get_button("main_reply", "referral", "👥 دعوت دوستان"))
async def menu_referral(message: types.Message, state: FSMContext):
    await state.clear()
    user = db.get_user(message.from_user.id)
    if user is None:
        await message.answer(ui_editor.get_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."))
        return

    stats = db.get_referral_stats(user["id"])
    invite_link = f"https://t.me/{bot_info.get('bot_username')}?start={stats['invite_code']}"

    rs = db.get_referral_settings()
    if not rs["paid_purchase_required"]:
        condition_text = f"ℹ️ به‌ازای هر دوستی که با لینک شما عضو شود، {rs['reward_amount']:,} تومان بلافاصله و بدون نیاز به خرید به کیف پول شما آزاد می‌شود."
    elif rs["min_volume_enabled"]:
        condition_text = f"ℹ️ به‌ازای هر دوستی که با لینک شما عضو شود و یک خرید {rs['min_volume_gb']} گیگ یا بیشتر انجام دهد، {rs['reward_amount']:,} تومان به‌صورت خودکار و بدون نیاز به هیچ اقدام دیگری به کیف پول شما آزاد می‌شود.\n❌ خریدهای کمتر از {rs['min_volume_gb']} گیگ و تست رایگان پاداش را آزاد نمی‌کنند."
    else:
        condition_text = f"ℹ️ به‌ازای هر دوستی که با لینک شما عضو شود و هر خرید پولی انجام دهد، {rs['reward_amount']:,} تومان به‌صورت خودکار و بدون نیاز به هیچ اقدام دیگری به کیف پول شما آزاد می‌شود."

    text = (
        f"👥 دعوت دوستان و کسب درآمد 💸\n\n"
        f"دوستانتو دعوت کن و به‌ازای هر دعوت موفق، {rs['reward_amount']:,} تومان پاداش نقدی بگیر! 🎁\n"
        f"کافیه لینک اختصاصی‌ت رو برای دوستات، گروه‌ها یا کانال‌هایی که توشون عضوی بفرستی.\n\n"
        f"🔗 لینک اختصاصی شما:\n{invite_link}\n\n"
        f"🔑 کد اختصاصی: {stats['invite_code']}\n\n"
        f"👤 تعداد دعوت: {stats['invited_count']}\n"
        f"✅ دعوت‌های موفق: {stats['successful_invites']}\n"
        f"🔓 مبلغ آزاد شده: {stats['released_amount']:,} تومان\n"
        f"🔒 مبلغ در انتظار: {user['locked_wallet']:,} تومان\n\n"
        f"{condition_text}\n\n"
        f"⚠️ لطفاً فقط لینک را برای افراد واقعی ارسال کنید؛ استفاده از اکانت‌های فیک تقلب محسوب شده و جایزه شما لغو می‌شود."
    )
    await show_menu_with_sticker(message.bot, message.chat.id, "referral", text, reply_markup=referral_menu())


@router.message(lambda message: message.text == ui_editor.get_button("main_reply", "profile", "👤 پروفایل من"))
async def menu_profile(message: types.Message, state: FSMContext):
    await state.clear()
    user = db.get_user(message.from_user.id)
    if user is None:
        await message.answer(ui_editor.get_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."))
        return

    configs_count = len(db.get_configs(user["id"]))

    text = (
        f"👤 پروفایل حرفه‌ای شما\n\n"
        f"📛 نام: {user['name']}\n"
        f"🆔 آیدی: {user['telegram_id']}\n\n"
        f"👛 موجودی قابل استفاده: {user['wallet']:,} تومان\n"
        f"🔒 موجودی در انتظار: {user['locked_wallet']:,} تومان\n\n"
        f"📦 تعداد سرویس: {configs_count}\n"
        f"🛒 کل خرید: {user['total_purchase']:,} تومان\n"
        f"📅 تاریخ عضویت: {user['joined']}\n\n"
        f"👥 تعداد دعوت: {user['invited_count']} | دعوت موفق: {user['successful_invites']}"
    )
    await show_menu_with_sticker(message.bot, message.chat.id, "profile", text, reply_markup=profile_menu())


@router.message(lambda message: message.text == ui_editor.get_button("main_reply", "guides", "📚 راهنما"))
async def menu_user_guides(message: types.Message, state: FSMContext):
    await state.clear()
    guides = db.get_guides()
    if not guides:
        await show_menu_with_sticker(
            message.bot, message.chat.id, "guides_empty",
            "📚 راهنما و اموزش‌ها\n\nهنوز هیچ راهنمایی ثبت نشده. به‌زودی محتوای آموزشی اینجا قرار می‌گیرد.",
            reply_markup=back_button("back", "🏠 بازگشت به منوی اصلی", screen="guides_empty"),
        )
        return
    await show_menu_with_sticker(
        message.bot, message.chat.id, "guides_has",
        "📚 راهنما و اموزش‌ها\n\nیکی از موارد زیر را برای مشاهده انتخاب کنید 👇",
        reply_markup=user_guides_menu(guides),
    )


@router.callback_query(F.data == "user_guides")
async def menu_user_guides_callback(callback: types.CallbackQuery):
    guides = db.get_guides()
    if not guides:
        await show_menu_with_sticker(callback.bot, callback.message.chat.id, "guides_empty", 
            "📚 راهنما و اموزش‌ها\n\nهنوز هیچ راهنمایی ثبت نشده.", reply_markup=back_button("back", "🏠 بازگشت به منوی اصلی", screen="guides_empty")
        )
        await callback.answer()
        return
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "guides_has", 
        "📚 راهنما و اموزش‌ها\n\nیکی از موارد زیر را برای مشاهده انتخاب کنید 👇",
        reply_markup=user_guides_menu(guides),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("guideopen_"))
async def menu_user_guide_open(callback: types.CallbackQuery):
    guide_id = int(callback.data.replace("guideopen_", ""))
    guide = db.get_guide(guide_id)
    if guide is None:
        await callback.answer(ui_editor.get_alert_text("msg_1318898fb7", "❌ این راهنما دیگر موجود نیست."), show_alert=True)
        return

    caption = f"📚 {guide['title']}"
    if guide.get("body_text"):
        caption += f"\n\n{guide['body_text']}"

    try:
        if guide["content_type"] == "photo" and guide.get("file_id"):
            await callback.message.answer_photo(guide["file_id"], caption=caption, reply_markup=user_guide_detail_keyboard())
        elif guide["content_type"] == "video" and guide.get("file_id"):
            await callback.message.answer_video(guide["file_id"], caption=caption, reply_markup=user_guide_detail_keyboard())
        else:
            await callback.message.answer(caption, reply_markup=user_guide_detail_keyboard())
    except Exception:
        await callback.message.answer(caption, reply_markup=user_guide_detail_keyboard())
    await callback.answer()


@router.message(lambda message: message.text == ui_editor.get_button("main_reply", "support", "👨‍💻 پشتیبانی"))
async def menu_ticket(message: types.Message, state: FSMContext):
    await state.clear()
    await show_menu_with_sticker(
        message.bot, message.chat.id, "support",
        "👨‍💻 پشتیبانی\n\nمی‌تونی مستقیم تیکت بزنی یا از کانال اصلی و پشتیبان استفاده کنی 👇",
        reply_markup=support_menu(),
    )


@router.message(lambda message: message.text == ui_editor.get_button("main_reply", "agent_manage", "🤝 مدیریت نمایندگی"))
async def menu_agent_manage(message: types.Message, state: FSMContext):
    agent = db.get_agent(message.from_user.id)
    if not agent:
        return
    await state.clear()
    cats = db.get_vip_categories()
    selected = set(db.get_agent_category_ids(message.from_user.id))
    scope = "همه دسته‌ها" if not selected else ", ".join(str(c.get("name")) for c in cats if int(c.get("id")) in selected)
    prefix = agent.get("custom_prefix") or "انتخاب نشده"
    text = (f"🤝 مدیریت نمایندگی\n\n💯 تخفیف: {agent.get('vip_discount_percent',0)}٪\n"
            f"🗂 دسته‌های مشمول: {scope}\n🏷 پیشوند ساخت کانفیگ: {prefix}\n\n"
            "از دکمه‌های زیر می‌توانی تنظیمات نمایندگی خودت را مدیریت کنی:")
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 آمار نمایندگی", callback_data="agentstats", style="primary")],
        [InlineKeyboardButton(text="🏷 انتخاب نام دلخواه برای ساخت کانفیگ", callback_data="agentprefix", style="success")],
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data="back", style="danger")],
    ])
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "agentstats")
async def menu_agent_stats(callback: types.CallbackQuery):
    agent = db.get_agent(callback.from_user.id)
    if not agent:
        await callback.answer("❌ نماینده نیستید.", show_alert=True); return
    user = db.get_user(callback.from_user.id)
    if not user:
        await callback.answer("❌ اطلاعات کاربری پیدا نشد.", show_alert=True); return
    cur = db.get_connection().cursor()
    cur.execute("SELECT COUNT(*) AS c, COALESCE(SUM(price),0) AS total FROM orders WHERE user_id=? AND status='paid'", (user['id'],))
    row = db._fetchone(cur) or {"c":0,"total":0}
    await callback.message.edit_text(
        f"📊 آمار نمایندگی\n\n👤 نماینده: {user.get('name','-')}\n"
        f"💯 تخفیف: {agent.get('vip_discount_percent',0)}٪\n"
        f"🛒 تعداد سفارش‌های ثبت‌شده: {row.get('c',0):,}\n"
        f"💰 مجموع مبلغ سفارش‌ها: {int(row.get('total',0) or 0):,} تومان",
        reply_markup=__import__('aiogram').types.InlineKeyboardMarkup(inline_keyboard=[[__import__('aiogram').types.InlineKeyboardButton(text="🔙 بازگشت", callback_data="back", style="danger")]])
    )
    await callback.answer()


@router.callback_query(F.data == "agentprefix")
async def menu_agent_prefix_start(callback: types.CallbackQuery, state: FSMContext):
    if not db.get_agent(callback.from_user.id):
        await callback.answer("❌ نماینده نیستید.", show_alert=True); return
    await state.set_state(UserStates.waiting_agent_prefix)
    await callback.message.answer("🏷 یک پیشوند انگلیسی برای نام سرویس‌ها بفرست (۱ تا 12 کاراکتر، فقط a-z و 0-9 و _):")
    await callback.answer()


@router.message(UserStates.waiting_agent_prefix)
async def menu_agent_prefix_save(message: types.Message, state: FSMContext):
    import re
    value=(message.text or '').strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{1,12}", value):
        await message.answer("❌ پیشوند نامعتبر است. فقط a-z، عدد و _ و حداکثر 12 کاراکتر.")
        return
    db.set_agent_prefix(message.from_user.id, value)
    await state.clear()
    await message.answer(f"✅ پیشوند «{value}» ذخیره شد. از این به بعد کد ساخت سرویس بعد از پیشوند اصلی ربات با این نام ساخته می‌شود.", reply_markup=main_reply_keyboard(message.from_user.id))


@router.message(lambda message: message.text == ui_editor.get_button("main_reply", "agency", "🤝 درخواست نمایندگی"))
async def menu_agency_request_start(message: types.Message, state: FSMContext):
    await state.clear()
    await show_menu_with_sticker(
        message.bot, message.chat.id, "agency_request",
        "🤝 درخواست نمایندگی\n\n"
        "درخواست و مشخصات خودتون (اسم، شماره تماس، میزان فعالیت/تعداد مشتری تقریبی و توضیحات) "
        "رو در یک پیام بنویسید و ارسال کنید؛ مستقیم برای پشتیبانی فرستاده می‌شه و به‌زودی بررسی و پاسخ داده می‌شه 👇",
        reply_markup=back_button("back", "🔙 انصراف", screen="ticket_write"),
    )
    await state.set_state(UserStates.waiting_agency_request_message)


@router.message(UserStates.waiting_agency_request_message)
async def menu_agency_request_send(message: types.Message, state: FSMContext):
    uid = str(message.from_user.id)
    text = (message.text or "").strip()
    if not text:
        await message.answer(ui_editor.get_text("msg_b940e468ec", "❌ لطفاً درخواستتون رو به‌صورت متن ارسال کنید:"))
        return

    await message.bot.send_message(
        ADMIN_ID,
        f"🤝 درخواست نمایندگی جدید\n👤 {message.from_user.full_name}\n🆔 {uid}\n\n💬 {text}",
    )
    await message.answer(
        ui_editor.get_text("msg_8331e7233a", "✅ درخواست شما برای پشتیبانی ارسال شد. به‌زودی بررسی و باهاتون تماس گرفته می‌شه."),
        reply_markup=main_reply_keyboard(message.from_user.id),
    )
    await state.clear()


# ---------------------------------------------------------------------------
# منوی ادمین
# ---------------------------------------------------------------------------
@router.message(F.text == "📊 آمار")
async def menu_admin_stats(message: types.Message, state: FSMContext):
    if not _has_admin_permission(message.from_user.id, "stats"):
        return
    await state.clear()
    text = (
        f"📊 آمار ربات\n\n"
        f"💰 فروش امروز: {db.sales_since(1):,} تومان\n"
        f"💰 فروش هفته: {db.sales_since(7):,} تومان\n"
        f"💰 فروش ماه: {db.sales_since(30):,} تومان\n"
        f"💰 کل فروش: {db.total_sales():,} تومان\n\n"
        f"👥 تعداد کاربران: {db.count_users()}\n"
        f"🟢 کاربران فعال (۳۰ روز اخیر): {db.count_active_users(30)}"
    )
    await message.answer(text, reply_markup=admin_back_button())


@router.message(F.text == "👥 لیست کاربران")
async def menu_admin_userlist(message: types.Message, state: FSMContext):
    if not _has_admin_permission(message.from_user.id, "users"):
        return
    await state.clear()
    text = (
        f"👥 مدیریت کاربران\n\n"
        f"👥 کل کاربران ثبت‌نامی: {db.count_users()}\n"
        f"🟢 مشتریانی که خرید داشته‌اند: {db.count_customers()}\n\n"
        f"یکی از گزینه‌های زیر را انتخاب کنید 👇"
    )
    await message.answer(text, reply_markup=admin_userlist_menu())


@router.message(F.text == "📢 پیام همگانی")
async def menu_admin_broadcast(message: types.Message, state: FSMContext):
    if not _has_admin_permission(message.from_user.id, "broadcast"):
        return
    await message.answer(
        ui_editor.get_text("msg_74e05ed696", "📢 پیامی که می‌خواهید برای همه کاربران ارسال شود را بنویسید:"),
        reply_markup=admin_back_button(),
    )
    await state.set_state(UserStates.waiting_broadcast)


@router.message(F.text == "🎟 مدیریت تخفیف")
async def menu_admin_discount(message: types.Message, state: FSMContext):
    if not _has_admin_permission(message.from_user.id, "discounts"):
        return
    await state.clear()
    discounts = db.get_all_discounts()
    if not discounts:
        text = "🎟 هیچ کد تخفیفی هنوز ثبت نشده.\n\nبرای ساخت کد جدید، دکمه‌ی زیر را بزنید 👇"
    else:
        text = "🎟 کدهای تخفیف فعال:\n\nبرای مشاهده و ویرایش جزئیات هر کد، روی آن بزنید 👇"
    await message.answer(text, reply_markup=admin_discount_menu(discounts))


REFERRERS_PER_PAGE = 10


@router.message(F.text == "🤝 مدیریت دعوت‌ها")
async def menu_admin_referrals(message: types.Message, state: FSMContext):
    if not _has_admin_permission(message.from_user.id, "referrals"):
        return
    await state.clear()
    total = db.count_referrers()
    if total == 0:
        text = "🤝 هنوز هیچ دعوتی ثبت نشده."
    else:
        text = (
            f"🤝 مدیریت دعوت‌شده‌ها — مرتب‌شده بر اساس بیشترین دعوت\n\n"
            f"👥 تعداد کل دعوت‌کننده‌ها: {total}\n\n"
            f"روی هر کدام بزنید تا لیست دعوت‌شده‌هایش و وضعیت کیف پولش رو ببینید 👇"
        )
    users = db.get_referrers_page(0, REFERRERS_PER_PAGE)
    has_next = total > REFERRERS_PER_PAGE
    await message.answer(text, reply_markup=admin_referrers_page_keyboard(users, 0, has_next))


@router.message(F.text == "📚 مدیریت راهنما")
async def menu_admin_guides(message: types.Message, state: FSMContext):
    if not _has_admin_permission(message.from_user.id, "guides"):
        return
    await state.clear()
    guides = db.get_guides()
    await message.answer(
        f"📚 مدیریت راهنما و اموزش‌ها\n\nتعداد: {len(guides)}\n\n"
        "از اینجا می‌تونید راهنما/آموزش جدید اضافه کنید یا موردهای موجود را ویرایش کنید:",
        reply_markup=admin_guides_menu(guides),
    )


@router.message(F.text == "💾 بکاپ")
async def menu_admin_backup(message: types.Message, state: FSMContext):
    if not _has_admin_permission(message.from_user.id, "backup"):
        return
    await state.clear()
    try:
        if db.USE_TURSO:
            export_path = "/tmp/backup_export.json"
            db.export_backup_json(export_path)
            backup_file = FSInputFile(export_path, filename="backup.json")
            caption = "💾 بکاپ دیتابیس (Turso — JSON)"
        else:
            backup_file = FSInputFile(DATABASE_PATH)
            caption = "💾 بکاپ دیتابیس"
        await message.answer_document(backup_file, caption=caption)
    except Exception:
        await message.answer(ui_editor.get_text("msg_91e4a4bd2f", "❌ خطا در ساخت بکاپ."))


