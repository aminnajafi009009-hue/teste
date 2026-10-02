"""
handlers/wallet.py
نمایش کیف پول (موجودی آزاد / موجودی در انتظار)، تاریخچه تراکنش‌ها،
و فرایند شارژ کیف پول (انتخاب مبلغ یا مبلغ دلخواه + ارسال رسید).
"""

import ui_editor


import logging

from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext

import database as db
import uniquepay
import alerts
import bot_info
from utils import is_duplicate_action, format_deadline_time, progress_bar, apply_random_amount_variation
from utils import show_menu_with_sticker
from states import UserStates
from config import (
    ADMIN_ID,
    UNIQUEPAY_ENABLED,
    ONLINE_PAYMENT_MIN_AMOUNT,
)
from keyboards import (
    wallet_menu,
    charge_amount_keyboard,
    charge_payment_method_keyboard,
    online_payment_wallet_keyboard,
    back_button,
    admin_charge_approval_keyboard,
    card_payment_actions_keyboard,
    receipt_submitted_keyboard,
)

logger = logging.getLogger(__name__)

router = Router(name="wallet")


def _wallet_topup_error(amount: int) -> str | None:
    """اعتبارسنجی متمرکز مبلغ شارژ کیف پول (تومان).
    💡 حداقل/حداکثر دیگر مقدار ثابت در .env نیستند؛ از تنظیمات «مدیریت شارژ
    کیف‌پول» در دیتابیس خوانده می‌شوند تا ادمین بتواند بدون ری‌دیپلوی تغییرشان دهد."""
    limits = db.get_wallet_settings()
    if amount < limits["min_topup"]:
        return f"❌ حداقل مبلغ شارژ کیف پول {limits['min_topup']:,} تومان است."
    if amount > limits["max_topup"]:
        return f"❌ حداکثر مبلغ شارژ کیف پول {limits['max_topup']:,} تومان است."
    return None


async def _reject_invalid_wallet_topup(target, amount: int) -> bool:
    """اگر مبلغ خارج از بازه باشد پیام خطا می‌دهد و True برمی‌گرداند."""
    error = _wallet_topup_error(amount)
    if not error:
        return False
    if isinstance(target, types.CallbackQuery):
        await target.answer(error, show_alert=True)
    else:
        await target.answer(error)
    return True


def _get_user_row(telegram_id):
    """کاربر را برمی‌گرداند؛ اگر هنوز ساخته نشده، می‌سازد (محافظتی)."""
    user = db.get_user(telegram_id)
    if user is None:
        return None
    return user


@router.callback_query(F.data == "wallet")
async def wallet_overview(callback: types.CallbackQuery):
    user = _get_user_row(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return

    text = (
        f"💰 کیف پول شما\n\n"
        f"💰 موجودی قابل استفاده: {user['wallet']:,} تومان\n"
        f"🔒 موجودی در انتظار: {user['locked_wallet']:,} تومان\n\n"
        + (f"ℹ️ موجودی در انتظار، پس از خرید حجم {db.get_referral_settings()['min_volume_gb']} گیگ یا بیشتر توسط فردی که با لینک شما عضو شده، به‌صورت خودکار آزاد می‌شود."
         if db.get_referral_settings()["paid_purchase_required"] and db.get_referral_settings()["min_volume_enabled"]
         else ("ℹ️ پاداش دعوت پس از هر خرید پولی توسط فرد دعوت‌شده به‌صورت خودکار آزاد می‌شود."
               if db.get_referral_settings()["paid_purchase_required"]
               else "ℹ️ پاداش دعوت بدون نیاز به خرید، بلافاصله پس از عضویت فرد دعوت‌شده آزاد می‌شود."))
    )
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "wallet", text, reply_markup=wallet_menu())
    await callback.answer()


@router.callback_query(F.data == "wallet_free")
async def wallet_free(callback: types.CallbackQuery):
    user = _get_user_row(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return
    text = f"💰 موجودی قابل استفاده شما\n\n{user['wallet']:,} تومان\n\nاین مبلغ را می‌توانید برای خرید سرویس استفاده کنید."
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "wallet_free", text, reply_markup=back_button("profile", "🔙 بازگشت", screen="wallet_free"))
    await callback.answer()


@router.callback_query(F.data == "wallet_locked")
async def wallet_locked(callback: types.CallbackQuery):
    user = _get_user_row(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return
    text = (
        f"🔒 موجودی در انتظار شما\n\n{user['locked_wallet']:,} تومان\n\n"
        + (f"این مبلغ از دعوت دوستان به‌دست آمده و پس از خرید حجم {db.get_referral_settings()['min_volume_gb']} گیگ یا بیشتر توسط آن‌ها، به‌صورت خودکار به موجودی قابل‌استفاده شما اضافه می‌شود."
         if db.get_referral_settings()["paid_purchase_required"] and db.get_referral_settings()["min_volume_enabled"]
         else ("این مبلغ پس از هر خرید پولی فرد دعوت‌شده آزاد می‌شود."
               if db.get_referral_settings()["paid_purchase_required"]
               else "این مبلغ بدون نیاز به خرید فرد دعوت‌شده، پس از عضویت او آزاد می‌شود."))
    )
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "wallet_locked", text, reply_markup=back_button("profile", "🔙 بازگشت", screen="wallet_locked"))
    await callback.answer()


@router.callback_query(F.data == "transactions")
async def transactions(callback: types.CallbackQuery):
    user = _get_user_row(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return

    txs = db.get_transactions(user["id"], limit=10)
    if not txs:
        text = "📋 هنوز تراکنشی ندارید."
    else:
        icon_map = {
            "charge": "✅",
            "purchase": "🛒",
            "referral_locked": "🔒",
            "referral_release": "🔓",
        }
        # نوع‌هایی که واقعاً واریز به حساب هستند (سبز/+)؛ بقیه خروج از حساب یا
        # در انتظار محسوب می‌شوند (قرمز/بدون علامت). این دقیقاً همان منطقی است
        # که Mini App (webapp_api._signed_tx_amount) استفاده می‌کند تا نمایش
        # تراکنش‌ها بین ربات و Mini App یکسان باشد.
        positive_types = ("charge", "referral_release")
        text = "📋 تراکنش‌های اخیر:\n\n"
        for tx in txs:
            icon = icon_map.get(tx["type"], "•")
            sign = "+" if tx["type"] in positive_types else "-"
            text += f"{icon} {tx['description']} | {sign}{tx['amount']:,} تومان | {tx['created_at']}\n"

    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "wallet_transactions", text, reply_markup=back_button("wallet", "🔙 بازگشت", screen="wallet_transactions"))
    await callback.answer()


# ---------------------------------------------------------------------------
# فرایند شارژ کیف پول
# ---------------------------------------------------------------------------
@router.callback_query(F.data == "charge")
async def charge(callback: types.CallbackQuery):
    limits = db.get_wallet_settings()
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "wallet_charge", 
        f"💳 مبلغ شارژ را انتخاب کنید:\n\n📌 حداقل شارژ: {limits['min_topup']:,} تومان\n📌 حداکثر شارژ: {limits['max_topup']:,} تومان",
        reply_markup=charge_amount_keyboard(limits["quick_amounts"])
    )
    await callback.answer()


async def _offer_charge_payment_method(target, amount: int, state: FSMContext):
    """پس از مشخص‌شدن مبلغ شارژ (چه از دکمه‌های سریع، چه مبلغ دلخواه)، اگر
    درگاه آنلاین فعال باشد و مبلغ بیشتر از ONLINE_PAYMENT_MIN_AMOUNT باشد،
    روش پرداخت را از کاربر می‌پرسد (آنلاین یا کارت‌به‌کارت)؛ در غیر این
    صورت دقیقاً مثل قبل مستقیم به مرحله‌ی کارت‌به‌کارت می‌رود."""
    if await _reject_invalid_wallet_topup(target, amount):
        await state.clear()
        return

    if UNIQUEPAY_ENABLED and amount > ONLINE_PAYMENT_MIN_AMOUNT:
        text = f"💳 مبلغ: {amount:,} تومان\n\n💰 روش پرداخت را انتخاب کنید:"
        markup = charge_payment_method_keyboard(amount)
        if isinstance(target, types.CallbackQuery):
            await show_menu_with_sticker(target.bot, target.message.chat.id, "walletcharge_method", text, reply_markup=markup)
        else:
            await show_menu_with_sticker(target.bot, target.chat.id, "walletcharge_method", text, reply_markup=markup)
        return

    await state.update_data(amount=amount)
    await state.set_state(UserStates.waiting_charge_receipt)
    text = (
        f"💳 مبلغ: {amount:,} تومان\n\n"
        f"💳 شماره کارت:\n{bot_info.get('card_number')}\n\n"
        f"👤 {bot_info.get('card_holder')}\n\n"
        f"📸 عکس رسید را ارسال کنید."
    )
    if isinstance(target, types.CallbackQuery):
        await show_menu_with_sticker(target.bot, target.message.chat.id, "walletcharge_pay_card", text)
    else:
        await show_menu_with_sticker(target.bot, target.chat.id, "walletcharge_pay_card", text)


@router.callback_query(F.data.startswith("charge_"))
async def charge_amount(callback: types.CallbackQuery, state: FSMContext):
    action = callback.data.replace("charge_", "")

    if action == "custom":
        limits = db.get_wallet_settings()
        await state.set_state(UserStates.waiting_custom_charge)
        await show_menu_with_sticker(
            callback.bot, callback.message.chat.id, "wallet_charge",
            f"💵 مبلغ دلخواه را به تومان ارسال کنید:\n\n📌 حداقل: {limits['min_topup']:,} تومان\n📌 حداکثر: {limits['max_topup']:,} تومان"
        )
    else:
        amount = int(action)
        await _offer_charge_payment_method(callback, amount, state)
    await callback.answer()


@router.message(UserStates.waiting_custom_charge)
async def custom_charge_amount(message: types.Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer(ui_editor.get_text("msg_39be0509db", "❌ فقط عدد ارسال کنید."))
        return

    amount = int(message.text)
    await _offer_charge_payment_method(message, amount, state)


@router.callback_query(F.data.startswith("chargepay_card_"))
async def charge_pay_with_card(callback: types.CallbackQuery, state: FSMContext):
    try:
        amount = int(callback.data.replace("chargepay_card_", ""))
    except ValueError:
        await callback.answer(ui_editor.get_alert_text("msg_4b6223ac73", "❌ درخواست نامعتبر است."), show_alert=True)
        return

    if await _reject_invalid_wallet_topup(callback, amount):
        return

    invoicing_user = db.get_user(callback.from_user.id)
    invoice = db.create_invoice(
        user_id=invoicing_user["id"] if invoicing_user else None,
        telegram_id=str(callback.from_user.id),
        kind="wallet_card",
        label="شارژ کیف پول",
        price=amount,
    )
    deadline_str = format_deadline_time(invoice["expires_at"])
    await state.update_data(amount=amount, wallet_card_invoice_id=invoice["id"])
    await state.set_state(UserStates.waiting_charge_receipt)
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "walletcharge_pay_card", 
        progress_bar(1, 2) +
        f"💳 مبلغ: {amount:,} تومان\n\n"
        f"💳 شماره کارت:\n{bot_info.get('card_number')}\n\n"
        f"👤 {bot_info.get('card_holder')}\n\n"
        f"📸 عکس رسید را ارسال کنید.\n\n"
        f"⏱ این شماره کارت و مبلغ تا ساعت {deadline_str} (۳۰ دقیقه) معتبر است. لطفاً تا این ساعت رسید پرداخت را ارسال کنید، وگرنه این فاکتور به‌طور خودکار منقضی و حذف می‌شود.\n\n"
        f"*⚠️ دقیقاً همین مبلغ ({amount:,} تومان) را واریز کنید تا پرداختتان شناسایی و بلافاصله تایید شود.*",
        parse_mode="Markdown",
        reply_markup=card_payment_actions_keyboard(bot_info.get('card_number'), amount, f"changepay_wallet_{amount}"),
    )
    await callback.answer()


# ✅ دکمه‌ی «انتخاب روش پرداخت دیگر» روی صفحه‌ی پرداخت کارت‌به‌کارت شارژ کیف پول:
# فاکتور فعلی را منقضی می‌کند و دوباره مرحله‌ی انتخاب روش پرداخت را نمایش می‌دهد.
@router.callback_query(F.data.startswith("changepay_wallet_"))
async def change_payment_method_wallet(callback: types.CallbackQuery, state: FSMContext):
    try:
        amount = int(callback.data.replace("changepay_wallet_", ""))
    except ValueError:
        await callback.answer(ui_editor.get_alert_text("msg_4b6223ac73", "❌ درخواست نامعتبر است."), show_alert=True)
        return

    if await _reject_invalid_wallet_topup(callback, amount):
        return

    data = await state.get_data()
    invoice_id = data.get("wallet_card_invoice_id")
    if invoice_id:
        db.delete_invoice(invoice_id)
    await state.update_data(wallet_card_invoice_id=None)
    await state.set_state(None)
    await _offer_charge_payment_method(callback, amount, state)
    await callback.answer()


@router.callback_query(F.data.startswith("chargepay_online_"))
async def charge_pay_online(callback: types.CallbackQuery):
    if not UNIQUEPAY_ENABLED:
        await callback.answer(ui_editor.get_alert_text("msg_ab9b7bfc88", "این روش پرداخت در حال حاضر فعال نیست."), show_alert=True)
        return

    try:
        amount = int(callback.data.replace("chargepay_online_", ""))
    except ValueError:
        await callback.answer(ui_editor.get_alert_text("msg_4b6223ac73", "❌ درخواست نامعتبر است."), show_alert=True)
        return

    if await _reject_invalid_wallet_topup(callback, amount):
        return

    if amount <= ONLINE_PAYMENT_MIN_AMOUNT:
        await callback.answer(
            ui_editor.get_text("msg_b46b807d8b", "❌ برای مبالغ ۵۰ هزار تومان و کمتر امکان استفاده از درگاه پرداخت آنلاین نیست."
            " لطفاً از کارت‌به‌کارت یا کیف پول استفاده کنید."),
            show_alert=True,
        )
        return

    if is_duplicate_action(f"onlinecharge_{callback.from_user.id}_{amount}"):
        await callback.answer(ui_editor.get_alert_text("msg_8e4c82514a", "⚠️ این درخواست در حال پردازش/ثبت‌شده است."), show_alert=True)
        return

    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return

    await callback.answer(ui_editor.get_alert_text("msg_75ef70c650", "⏳ در حال ساخت لینک پرداخت..."))

    hash_id = uniquepay.new_hash_id("charge")
    invoice = await uniquepay.create_invoice(hash_id, amount)
    if invoice is None or not invoice.get("paymentLink"):
        await alerts.report_uniquepay_create_failure(callback.bot, ADMIN_ID)
        await show_menu_with_sticker(callback.bot, callback.message.chat.id, "walletcharge_pay_online", 
            "❌ برای مبالغ ۵۰ هزار تومان و کمتر امکان استفاده از درگاه پرداخت آنلاین نیست."
            " لطفاً از کارت‌به‌کارت یا کیف پول استفاده کنید.",
            reply_markup=charge_payment_method_keyboard(amount),
        )
        return

    alerts.report_uniquepay_create_success()

    payment_link = invoice.get("paymentLink")
    payment_id = db.create_online_payment(
        user_id=user["id"],
        telegram_id=str(callback.from_user.id),
        hash_id=hash_id,
        plan_name="شارژ کیف پول",
        price=amount,
        order_type="wallet_charge",
        kind="wallet_charge",
        payment_link=payment_link,
        ref_id=str(invoice.get("refId")),
    )

    white_label = uniquepay.white_label_data(invoice)
    payable_text = (
        f"💰 مبلغ دقیق واریز: {int(white_label['payableAmount']):,} تومان"
        if white_label and str(white_label['payableAmount']).isdigit()
        else f"💰 مبلغ سفارش: {amount:,} تومان"
    )
    card_text = f"\n💳 شماره کارت مقصد: {white_label['cardNumber']}\n" if white_label else ""
    await show_menu_with_sticker(callback.bot, callback.message.chat.id, "walletcharge_pay_online", 
        progress_bar(1, 2) +
        f"🌐 پرداخت آنلاین (کارت‌به‌کارت خودکار)\n\n"
        + payable_text + card_text + "\n"
        f"روی دکمه‌ی پرداخت بزنید یا از اطلاعات وایت‌لیبل بالا استفاده کنید؛ سپس همینجا روی «بررسی کن» بزنید.\n"
        f"⏱ به‌محض تأیید نهایی UniquePay، کیف پول شما به‌طور خودکار شارژ می‌شود.\n\n"
        f"⚠️ این فاکتور تا ۳۰ دقیقه دیگر معتبر است. اگر تا این مهلت پرداخت تایید نشود، به‌طور خودکار منقضی و حذف خواهد شد.",
        reply_markup=online_payment_wallet_keyboard(payment_link, payment_id, white_label),
    )


async def finalize_wallet_charge_online_payment(bot, payment: dict) -> int | None:
    """اینوویس پرداخت‌شده‌ی یونیک‌پی برای «شارژ کیف پول» را نهایی می‌کند.
    معادل finalize_online_payment در handlers/plans.py، با این تفاوت که به‌جای
    ساخت سفارش/سرویس، مستقیماً مبلغ به کیف پول کاربر اضافه می‌شود. از همان
    قفل اتمیک claim_online_payment_for_finalize استفاده می‌شود تا اگر پولر
    پس‌زمینه‌ی ربات (bot.py) و دکمه‌ی «بررسی کن» هم‌زمان صدا زده شوند، کیف
    پول فقط یک‌بار شارژ شود."""
    amount = int(payment["price"])
    if _wallet_topup_error(amount):
        logger.error(
            "Rejected out-of-range wallet online payment during finalization: user=%s amount=%s",
            payment.get("telegram_id"), amount,
        )
        return None

    if payment["status"] == "paid":
        return payment["id"]

    if not db.claim_online_payment_for_finalize(payment["id"]):
        fresh = db.get_online_payment(payment["id"])
        if fresh and fresh["status"] == "paid":
            return fresh["id"]
        return None

    try:
        db.add_to_wallet(payment["user_id"], payment["price"], "شارژ کیف پول (پرداخت آنلاین)")
        db.mark_online_payment_paid(payment["id"], None)
    except Exception:
        db.set_online_payment_status(payment["id"], "pending")
        raise

    try:
        await bot.send_message(
            ADMIN_ID,
            f"💳 شارژ کیف پول (پرداخت آنلاین - یونیک‌پی)!\n\n"
            f"🆔 {payment['telegram_id']}\n"
            f"💰 {payment['price']:,} تومان",
        )
    except Exception:
        logger.exception("ارسال پیام اطلاع‌رسانی شارژ آنلاین به ادمین ناموفق بود")

    return payment["id"]


@router.message(UserStates.waiting_charge_receipt, F.photo)
async def receive_receipt(message: types.Message, state: FSMContext):
    uid = str(message.from_user.id)
    data = await state.get_data()
    amount = data.get("amount")
    wallet_card_invoice_id = data.get("wallet_card_invoice_id")

    if amount is None:
        await message.answer(progress_bar(2, 2) + "❌ مشکلی پیش آمد، لطفاً دوباره از منوی شارژ شروع کنید.")
        await state.clear()
        return

    error = _wallet_topup_error(int(amount))
    if error:
        await message.answer(error)
        await state.clear()
        return

    if not wallet_card_invoice_id or db.consume_invoice(wallet_card_invoice_id) is None:
        await message.answer(
            ui_editor.get_text("msg_d77397f5d3", "⏰ مهلت ۳۰ دقیقه‌ای پرداخت این فاکتور به پایان رسیده و به‌طور خودکار منقضی شد. لطفاً دوباره از منوی شارژ شروع کنید.")
        )
        await state.clear()
        return

    user = db.get_user(uid)
    receipt_id = None
    try:
        receipt_id = db.create_pending_receipt("charge", uid, user["id"] if user else None, "شارژ کیف پول", amount)
    except Exception:
        pass

    if wallet_card_invoice_id:
        db.delete_invoice(wallet_card_invoice_id)

    await message.bot.forward_message(ADMIN_ID, message.chat.id, message.message_id)
    await message.bot.send_message(
        ADMIN_ID,
        f"📩 رسید شارژ\n👤 {message.from_user.full_name}\n🆔 {uid}\n💰 {amount:,} تومان",
        reply_markup=admin_charge_approval_keyboard(uid, amount),
    )
    tracking_code = receipt_id if receipt_id is not None else wallet_card_invoice_id
    await message.answer(
        f"✅ رسید پرداخت با موفقیت دریافت شد\n\n"
        f"🧾 کد پیگیری: `{tracking_code}`\n"
        f"📦سرویس: شارژ کیف پول\n"
        f"💳 مبلغ: {amount:,} تومان\n"
        f"🟡 وضعیت: در حال بررسی\n\n"
        f"رسید شما ثبت شده و نیازی به ارسال مجدد آن نیست.\n\n"
        f"پس از تأیید پرداخت، لینک سرویس و آموزش اتصال از طریق همین ربات برایت ارسال می‌شود.",
        parse_mode="Markdown",
        reply_markup=receipt_submitted_keyboard(),
    )
    await state.clear()


@router.message(UserStates.waiting_charge_receipt)
async def receipt_wrong_format(message: types.Message):
    # اگر کاربر به‌جای عکس، متن فرستاد
    await message.answer(ui_editor.get_text("msg_21b282b8dc", "📸 لطفاً عکس رسید پرداخت را ارسال کنید (نه متن)."))
