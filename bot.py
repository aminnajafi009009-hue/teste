"""
bot.py
فایل اصلی اجرای ربات. تمام Routerهای پوشه‌ی handlers اینجا به Dispatcher
وصل می‌شوند. یک سرور Flask کوچک هم کنارش اجرا می‌شود تا Render سرویس را
"زنده" تشخیص بدهد (لازمه‌ی سرویس‌های نوع Web Service).
"""

import asyncio
import logging
import os
import threading

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.types import ErrorEvent
from aiogram.exceptions import TelegramBadRequest
from aiogram.dispatcher.middlewares.base import BaseMiddleware

import database as db
import uniquepay
import alerts
import fsm_storage
import fsm_hybrid
import db_async
import perf_middleware
from config import TOKEN, UNIQUEPAY_ENABLED, ADMIN_ID
from handlers import menu, start, wallet, profile, referral, plans, ticket, admin, panel_admin
from handlers.plans import finalize_online_payment, finalize_custom_online_payment
from handlers.wallet import finalize_wallet_charge_online_payment
from alerts import check_usage_alerts, CHECK_INTERVAL_SECONDS
from sentry_setup import init_sentry

# 🆕 ادغام تک-سرویسی: به‌جای اجرای bot.py و webapp_api.py به‌صورت دو Web Service
# جدا روی Render (که با محدودیت زمانی/تعداد دیپلوی رایگان گیت‌هاب مشکل ایجاد
# می‌کرد)، اپ Flask کامل Mini App (تمام endpointهای /api/...) همینجا ایمپورت
# و در همان ترد Flask زیر اجرا می‌شود؛ یعنی با یک‌بار دیپلوی (یک Web Service)،
# هم ربات (long polling، در event loop اصلی) و هم بک‌اند Mini App (در یک ترد
# جدا) هر دو زنده و فعال می‌شوند و هر دو به همان یک آدرس Render متصل‌اند.
from webapp_api import app as flask_app

init_sentry()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _patch_global_user_text_editing():
    """تمام پیام‌های متنی مشتری را از یک نقطه از Editor عبور می‌دهد.

    این لایه برای پیام‌هایی است که در handlerها به‌صورت مستقیم answer/send_message
    شده‌اند و قبلاً اصلاً ui_key نداشتند؛ بنابراین ویرایشگر واقعاً سرتاسر ربات را
    پوشش می‌دهد، بدون دست‌زدن به پنل مدیریت.
    """
    try:
        import ui_editor
        import cache as _cache
        from aiogram import Bot as _Bot

        # کش 60ثانیه‌ای is_sub_admin — هر send_message یک DB call حذف می‌شه
        def _cached_is_sub_admin(telegram_id: str) -> bool:
            _k = f"subadmin:{telegram_id}"
            val, hit = _cache.get(_k)
            if hit:
                return val
            result = db.is_sub_admin(telegram_id)
            _cache.set(_k, result, ttl=60)
            return result
        from aiogram.types import CallbackQuery as _CallbackQuery
        if not getattr(_Bot, "_ui_editor_patched", False):
            _orig_send_message = _Bot.send_message
            async def _send_message(self, *args, **kwargs):
                # perf: اگر تماس‌گیرنده (مثل show_menu_with_sticker) از قبل
                # apply_auto_text را روی متن اجرا کرده، دیگر لازم نیست اینجا
                # دوباره کل ۲۷۹ الگوی متن دوباره چک بشه — این پرتکرارترین
                # مسیر ارسال پیام کل ربات است، پس این یک بهینه‌سازی واقعی است.
                if kwargs.pop("_skip_auto_text", False):
                    return await _orig_send_message(self, *args, **kwargs)
                try:
                    chat_id = kwargs.get("chat_id", args[0] if args else None)
                    text = kwargs.get("text", args[1] if len(args) > 1 else None)
                    if text and chat_id is not None:
                        is_admin = int(chat_id) == int(ADMIN_ID) or _cached_is_sub_admin(str(chat_id))
                        if not is_admin:
                            new_text, new_entities = ui_editor.apply_auto_text_with_entities(str(text))
                            if args and len(args) > 1:
                                args = list(args); args[1] = new_text; args = tuple(args)
                            else:
                                kwargs["text"] = new_text
                            if new_entities and not kwargs.get("entities"):
                                from aiogram.types import MessageEntity as _MessageEntity
                                kwargs["entities"] = [_MessageEntity(
                                    type=e["type"], offset=e["offset"], length=e["length"],
                                    custom_emoji_id=e.get("custom_emoji_id")
                                ) for e in new_entities]
                except Exception:
                    logger.exception("ui editor auto-text failed for send_message")
                return await _orig_send_message(self, *args, **kwargs)
            _Bot.send_message = _send_message
            _Bot._ui_editor_patched = True

        if not getattr(_Bot, "_ui_editor_edit_patched", False):
            # Message.edit_text/edit_caption eventually call these Bot methods.
            # Patch them too so customized messages do not revert when a later
            # step edits the same user-facing message (a common source of the
            # "start changed but next step reverted" bug).
            _orig_edit_message_text = _Bot.edit_message_text
            async def _edit_message_text(self, *args, **kwargs):
                try:
                    chat_id = kwargs.get("chat_id", args[0] if args else None)
                    text = kwargs.get("text", args[2] if len(args) > 2 else None)
                    is_admin = chat_id is not None and (int(chat_id) == int(ADMIN_ID) or _cached_is_sub_admin(str(chat_id)))
                    if text and not is_admin:
                        new_text, new_entities = ui_editor.apply_auto_text_with_entities(str(text))
                        if len(args) > 2:
                            args = list(args); args[2] = new_text; args = tuple(args)
                        else:
                            kwargs["text"] = new_text
                        if new_entities and not kwargs.get("entities"):
                            from aiogram.types import MessageEntity as _MessageEntity
                            kwargs["entities"] = [_MessageEntity(
                                type=e["type"], offset=e["offset"], length=e["length"],
                                custom_emoji_id=e.get("custom_emoji_id")
                            ) for e in new_entities]
                except Exception:
                    logger.exception("ui editor auto-text failed for edit_message_text")
                return await _orig_edit_message_text(self, *args, **kwargs)
            _Bot.edit_message_text = _edit_message_text

            _orig_edit_message_caption = getattr(_Bot, "edit_message_caption", None)
            if _orig_edit_message_caption:
                async def _edit_message_caption(self, *args, __original=_orig_edit_message_caption, **kwargs):
                    try:
                        chat_id = kwargs.get("chat_id", args[0] if args else None)
                        caption = kwargs.get("caption", args[2] if len(args) > 2 else None)
                        is_admin = chat_id is not None and (int(chat_id) == int(ADMIN_ID) or _cached_is_sub_admin(str(chat_id)))
                        if caption and not is_admin:
                            new_caption, new_entities = ui_editor.apply_auto_text_with_entities(str(caption))
                            if len(args) > 2:
                                args = list(args); args[2] = new_caption; args = tuple(args)
                            else:
                                kwargs["caption"] = new_caption
                            if new_entities and not kwargs.get("caption_entities"):
                                from aiogram.types import MessageEntity as _MessageEntity
                                kwargs["caption_entities"] = [_MessageEntity(
                                    type=e["type"], offset=e["offset"], length=e["length"],
                                    custom_emoji_id=e.get("custom_emoji_id")
                                ) for e in new_entities]
                    except Exception:
                        logger.exception("ui editor auto-text failed for edit_message_caption")
                    return await __original(self, *args, **kwargs)
                _Bot.edit_message_caption = _edit_message_caption
            _Bot._ui_editor_edit_patched = True

        if not getattr(_Bot, "_ui_editor_media_patched", False):
            for method_name in ("send_photo", "send_video", "send_document", "send_animation"):
                original = getattr(_Bot, method_name, None)
                if original is None:
                    continue
                async def _media(self, *args, __original=original, **kwargs):
                    try:
                        chat_id = kwargs.get("chat_id", args[0] if args else None)
                        caption = kwargs.get("caption")
                        is_admin = chat_id is not None and (int(chat_id) == int(ADMIN_ID) or _cached_is_sub_admin(str(chat_id)))
                        if caption and not is_admin:
                            new_caption, new_entities = ui_editor.apply_auto_text_with_entities(str(caption))
                            kwargs["caption"] = new_caption
                            if new_entities and not kwargs.get("caption_entities"):
                                from aiogram.types import MessageEntity as _MessageEntity
                                kwargs["caption_entities"] = [_MessageEntity(
                                    type=e["type"], offset=e["offset"], length=e["length"],
                                    custom_emoji_id=e.get("custom_emoji_id")
                                ) for e in new_entities]
                    except Exception:
                        logger.exception("ui editor auto-text failed for media")
                    return await __original(self, *args, **kwargs)
                setattr(_Bot, method_name, _media)
            _Bot._ui_editor_media_patched = True

        if not getattr(_CallbackQuery, "_ui_editor_patched", False):
            _orig_answer = _CallbackQuery.answer
            async def _answer(self, *args, **kwargs):
                try:
                    uid = getattr(getattr(self, "from_user", None), "id", None)
                    text = kwargs.get("text", args[0] if args else None)
                    is_admin = uid is not None and (int(uid) == int(ADMIN_ID) or _cached_is_sub_admin(str(uid)))
                    if text and not is_admin:
                        new_text, _new_entities = ui_editor.apply_auto_text_with_entities(str(text))
                        if args:
                            args = list(args); args[0] = new_text; args = tuple(args)
                        else:
                            kwargs["text"] = new_text
                except Exception:
                    logger.exception("ui editor auto-text failed for callback answer")
                return await _orig_answer(self, *args, **kwargs)
            _CallbackQuery.answer = _answer
            _CallbackQuery._ui_editor_patched = True
    except Exception:
        logger.exception("ui editor global text patch initialization failed")


_patch_global_user_text_editing()


class BlockedUserMiddleware(BaseMiddleware):
    """اعمال مسدودی روی تمام پیام‌ها و callbackها، نه فقط /start."""
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user and user.id != ADMIN_ID and db.is_user_blocked(user.id):
            text = "🚫 دسترسی شما به ربات مسدود شده است. با پشتیبانی در ارتباط باشید."
            if getattr(event, "answer", None):
                try:
                    if event.__class__.__name__ == "CallbackQuery":
                        await event.answer(text, show_alert=True)
                    else:
                        await event.answer(text)
                except Exception:
                    logger.exception("خطا در اعلام مسدودی به کاربر")
            return None
        return await handler(event, data)


# ---------------------------------------------------------------------------
# هندلر سراسری خطا — تا حالا اگر یک خطای پیش‌بینی‌نشده (باگ) وسط پردازش یک
# callback (مثلاً دکمه‌ی پرداخت) رخ می‌داد، چون هیچ‌جا callback.answer() صدا
# زده نمی‌شد، دکمه برای کاربر برای همیشه در حالت لودینگ/فریز می‌ماند و نه
# پیامی به کاربر می‌رسید و نه به ادمین — و ردیابی علتش هم سخت بود چون فقط در
# لاگ‌ها گم می‌شد. این هندلر تضمین می‌کند که:
# ۱) خطا با جزئیات کامل (traceback) در لاگ ثبت شود تا بعداً قابل پیگیری باشد.
# ۲) کاربر همیشه یک پیام/آلارم دریافت کند و دکمه از حالت لودینگ خارج شود،
#    به‌جای این‌که تا ابد فریز بماند.
# ---------------------------------------------------------------------------
async def global_error_handler(event: ErrorEvent):
    # 🐛 فیکس: خطای کاملاً بی‌ضرر و متداول تلگرام “محتوا تفاوتی ندارد” (message is not modified) را باید به‌طور جدایی مدیریت کرد: وقتی یک دکمه دوبار زده می‌شود یا handler دوباره همان متن/دکمه را edit می‌کند، تلگرام این خطا را برمی‌گرداند ولی هیچ مشکلی برای کاربر رخ نداده؛ قبلاً هم به لاگ و هم Sentry به‌عنوان خطای جدی ثبت می‌شد و به کاربر هم پیام خطای گمراه‌کننده نمایش داده می‌شد.
    exc = event.exception
    if isinstance(exc, TelegramBadRequest) and "message is not modified" in str(exc).lower():
        logger.info("نادیده‌گرفتن خطای بی‌ضرر message-is-not-modified (کلیک دوباره/محتوای یکسان)")
        try:
            update = event.update
            if update.callback_query:
                try:
                    await update.callback_query.answer()
                except Exception:
                    pass
        except Exception:
            pass
        return True

    logger.exception(
        "خطای پیش‌بینی‌نشده هنگام پردازش آپدیت: %s", event.exception, exc_info=event.exception
    )
    try:
        import sentry_sdk
        sentry_sdk.capture_exception(event.exception)
    except Exception:
        pass
    try:
        import traceback as _tb
        db.log_error(
            error_type=type(event.exception).__name__,
            message=str(event.exception),
            traceback_text="".join(_tb.format_exception(type(event.exception), event.exception, event.exception.__traceback__)),
            context="global_error_handler",
        )
    except Exception:
        pass
    update = event.update
    warning_text = "⚠️ خطایی پیش آمد. لطفاً دوباره تلاش کنید یا با پشتیبانی تماس بگیرید."
    try:
        if update.callback_query:
            try:
                await update.callback_query.answer(warning_text, show_alert=True)
            except Exception:
                # اگر این callback قبلاً یک‌بار answer شده باشد (مثلاً با پیام
                # "در حال دریافت..." قبل از شروع کار اصلی)، تلگرام دیگر اجازه‌ی
                # answer دوباره را نمی‌دهد و این except صدا زده می‌شود. قبلاً
                # همین‌جا خطا فقط لاگ می‌شد و کاربر هیچ پیامی نمی‌دید (دکمه فقط
                # در حالت لودینگ می‌ماند). حالا به‌جای answer، مستقیماً یک پیام
                # در همون چت می‌فرستیم تا کاربر همیشه یک نتیجه ببیند.
                logger.warning("امکان answer دوباره‌ی callback نبود؛ ارسال پیام مستقیم به چت.")
                if update.callback_query.message:
                    await update.callback_query.message.answer(warning_text)
        elif update.message:
            await update.message.answer(warning_text)
    except Exception:
        logger.exception("خطا حتی در تلاش برای اطلاع‌رسانی خطای اصلی به کاربر")
    return True


# ---------------------------------------------------------------------------
# Flask - همان اپ webapp_api (بک‌اند کامل Mini App) + یک مسیر health-check
# ساده برای اینکه Render سرویس را "زنده" تشخیص بدهد (لازمه‌ی Web Serviceها).
# ---------------------------------------------------------------------------
@flask_app.route("/")
def health_check():
    return "Bot + Mini App API هر دو در حال اجرا هستند ✅"


def run_flask():
    port = int(os.environ.get("PORT", 10000))
    # threaded=True: تا درخواست‌های هم‌زمان Mini App (مثلاً چند کاربر) یکدیگر را بلاک نکنند
    # و همزمان با پاسخگویی ربات (در event loop اصلی asyncio) پردازش شوند.
    flask_app.run(host="0.0.0.0", port=port, threaded=True)


# ---------------------------------------------------------------------------
# aiogram - ربات اصلی
# ---------------------------------------------------------------------------
async def run_bot():
    db.init_db()
    # 🐛 HybridFSMStorage باید قبل از warm_up ساخته شود؛ در غیر این صورت
    # به‌خاطر assignment داخل همین تابع، پایتون آن را local تشخیص می‌دهد.
    _hybrid_storage = fsm_hybrid.HybridFSMStorage()
    # 🔥 FSM warm-up + Cache warm-up (async)
    await _hybrid_storage.warm_up()
    _hybrid_storage.start_flush_task()
    await db_async.warm_cache()
    recovered = db.recover_stuck_online_payments()
    if recovered:
        logger.warning("%d پرداخت processing قدیمی برای پردازش مجدد بازیابی شد.", recovered)
    logger.info("Database initialized.")

    bot = Bot(token=TOKEN)
    # 🐛 فیکس: قبلاً MemoryStorage (فقط RAM) بود، با هر ری‌استارت state گم می‌شد.
    dp = Dispatcher(storage=_hybrid_storage)
    dp.errors.register(global_error_handler)
    blocked_middleware = BlockedUserMiddleware()
    dp.message.outer_middleware(blocked_middleware)
    dp.callback_query.outer_middleware(blocked_middleware)

    # 🚀 Performance middlewares
    _cb_answer_mw = perf_middleware.CallbackAnswerMiddleware()
    _user_cache_mw = perf_middleware.UserCacheMiddleware()
    _perf_log_mw = perf_middleware.PerformanceLogMiddleware()

    dp.message.outer_middleware(_user_cache_mw)
    dp.callback_query.outer_middleware(_cb_answer_mw)
    dp.callback_query.outer_middleware(_user_cache_mw)
    dp.message.outer_middleware(_perf_log_mw)
    dp.callback_query.outer_middleware(_perf_log_mw)

    fsm_storage.storage = dp.storage

    # ترتیب ثبت Routerها مهم است: handler خاص‌تر باید زودتر بیاید.
    # menu (منوی پایین صفحه) باید همیشه اول باشد تا دکمه‌های ثابت پایین صفحه
    # در هر شرایطی (حتی وسط یک FSM دیگر) همیشه در دسترس و فعال باشند.
    # admin باید بعد از آن باشد چون فیلتر سخت‌گیرانه‌تری (ADMIN_ID) دارد
    # و برخی callback_dataهای مشترک (مثل state یکسان) را زودتر می‌گیرد.
    dp.include_router(menu.router)
    dp.include_router(admin.router)
    dp.include_router(panel_admin.router)
    dp.include_router(start.router)
    dp.include_router(wallet.router)
    dp.include_router(profile.router)
    dp.include_router(referral.router)
    dp.include_router(plans.router)
    dp.include_router(ticket.router)

    asyncio.create_task(usage_alert_loop(bot))
    asyncio.create_task(invoice_expiry_loop(bot))
    asyncio.create_task(self_ping_loop())
    if UNIQUEPAY_ENABLED:
        asyncio.create_task(online_payment_poller(bot))

    logger.info("Bot starting polling...")
    await dp.start_polling(bot)


async def usage_alert_loop(bot: Bot):
    """هر ۳۰ دقیقه سرویس‌های VIP را برای هشدار ۸۰٪/۹۰٪ مصرف و ۲ روز به انقضا بررسی می‌کند.
    همچنین کانفیگ‌های منقضی‌شده را به‌صورت خودکار آرشیو می‌کند (deleted=1) تا
    از پنل کاربر حذف شوند."""
    while True:
        try:
            await check_usage_alerts(bot)
        except Exception:
            logger.exception("خطا در بررسی دوره‌ای هشدارهای مصرف/انقضا")
        try:
            archived = db.archive_expired_configs()
            if archived > 0:
                logger.info("کانفیگ‌های منقضی‌شده آرشیو شدند: %d مورد", archived)
        except Exception:
            logger.exception("خطا در آرشیو خودکار کانفیگ‌های منقضی‌شده")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


ONLINE_PAYMENT_POLL_SECONDS = 20


async def online_payment_poller(bot: Bot):
    """هر ۲۰ ثانیه اینوویس‌های در انتظار درگاه یونیک‌پی را چک می‌کند و به‌محض
    پرداخت‌شدن، بدون نیاز به این‌که کاربر دکمه‌ی «بررسی کن» را بزند، سفارش را
    خودکار ثبت کرده و به کاربر و ادمین اطلاع می‌دهد. این همان «تایید خودکار»
    درخواست‌شده برای درگاه پرداخت آنلاین است.
    علاوه‌براین، نرخ خطای هر چرخه را می‌سنجد و اگر درگاه قطعی/کند شده باشد
    (بیشتر چک‌ها fail شوند)، یک‌بار (با cooldown) به ادمین هشدار می‌دهد."""
    while True:
        checked = 0
        failed = 0
        try:
            pending = db.get_pending_online_payments(limit=50)
            for payment in pending:
                checked += 1
                try:
                    invoice = await uniquepay.check_invoice(payment["hash_id"])
                    if invoice and invoice.get("isPaid") and invoice.get("isVerified"):
                        payment_kind = payment.get("kind")
                        if payment_kind == "custom":
                            result = await finalize_custom_online_payment(bot, payment)
                        elif payment_kind == "wallet_charge":
                            result = await finalize_wallet_charge_online_payment(bot, payment)
                        else:
                            result = await finalize_online_payment(bot, payment)
                        if result is None:
                            # یک فراخوانی هم‌زمان دیگر (مثلاً مینی‌اپ) همین الان
                            # در حال پردازش این پرداخت است؛ برای جلوگیری از پیام
                            # تکراری به کاربر، این چرخه کاری انجام نمی‌دهد.
                            continue
                        try:
                            if payment_kind == "wallet_charge":
                                confirm_text = (
                                    f"✅ پرداخت آنلاین شما تأیید شد و کیف پول شما به مبلغ "
                                    f"{payment['price']:,} تومان شارژ شد."
                                )
                            else:
                                confirm_text = (
                                    f"✅ پرداخت آنلاین شما برای «{payment['plan_name']}» تأیید شد "
                                    f"و سفارش ثبت گردید. سرویس شما به‌زودی ارسال می‌شود."
                                )
                            await bot.send_message(int(payment["telegram_id"]), confirm_text)
                        except Exception:
                            logger.exception("ارسال پیام تایید پرداخت خودکار به کاربر ناموفق بود")
                except Exception:
                    failed += 1
                    logger.exception("خطا در بررسی خودکار اینوویس %s", payment.get("hash_id"))
            if checked:
                await alerts.report_uniquepay_check_cycle(bot, ADMIN_ID, checked, failed)
        except Exception:
            logger.exception("خطا در حلقه‌ی پولر پرداخت آنلاین")
        await asyncio.sleep(ONLINE_PAYMENT_POLL_SECONDS)


INVOICE_EXPIRY_POLL_SECONDS = 60


async def invoice_expiry_loop(bot: Bot):
    """هر ۶۰ ثانیه فاکتورهای کارت‌به‌کارت (پلن/بساز-سرویس/کیف‌پول) و پرداخت‌های آنلاین پرداخت‌نشده که مهلت ۳۰ دقیقه‌ایشان تمام شده را حذف می‌کند
    و به کاربر پیامی می‌فرستد که برای دوباره سفارش ثبت کند."""
    while True:
        try:
            expired_invoices = db.expire_due_invoices()
            for inv in expired_invoices:
                try:
                    await bot.send_message(
                        int(inv["telegram_id"]),
                        f"⏰ مهلت ۳۰ دقیقه‌ای پرداخت فاکتور تان برای «{inv['label']}» به پایان رسید و به‌طور خودکار منقضی شد. لطفاً دوباره از منوی سرویس‌ها سفارش تان را ثبت کنید.\n\n🧾 کد پیگیری: `{inv['id']}`",
                        parse_mode="Markdown",
                    )
                except Exception:
                    logger.exception("ارسال پیام انقضای فاکتور به کاربر ناموفق بود")
        except Exception:
            logger.exception("خطا در حلقه‌ی انقضای فاکتورها")
        try:
            expired_online = db.expire_due_online_payments()
            for pay in expired_online:
                try:
                    await bot.send_message(
                        int(pay["telegram_id"]),
                        f"⏰ مهلت ۳۰ دقیقه‌ای پرداخت این فاکتور به پایان رسیده و به‌طور خودکار منقضی شد. لطفاً دوباره از منوی سرویس‌ها سفارش تان را ثبت کنید.\n\n🧾 کد پیگیری: `{pay['id']}`",
                        parse_mode="Markdown",
                    )
                except Exception:
                    logger.exception("ارسال پیام انقضای پرداخت آنلاین به کاربر ناموفق بود")
        except Exception:
            logger.exception("خطا در حلقه‌ی انقضای پرداخت‌های آنلاین")
        await asyncio.sleep(INVOICE_EXPIRY_POLL_SECONDS)


SELF_PING_INTERVAL_SECONDS = 120  # ۲ دقیقه؛ زیر آستانهٔ خواب ۱۵ دقیقه‌ای Render رایگان


async def self_ping_loop():
    """هر ۲ دقیقه یک درخواست GET به آدرس خودش می‌زند تا در پلن رایگان Render
    (که بعد از ۱۵ دقیقه بی‌فعالیتی سرویس را می‌خواباند) ربات/مینی‌اپ همیشه بیدار بماند.
    آدرس را یا از SELF_PING_URL (اگر دستی ست شده باشد) یا از RENDER_EXTERNAL_URL
    (که خود Render به‌صورت خودکار در Web Serviceها ست می‌کند) می‌خواند.
    اگر هیچکدام تنظیم نشده باشد (مثلاً اجرای محلی/غیرRender)، حلقه بی‌ضرر غیرفعال می‌ماند."""
    ping_url = os.environ.get("SELF_PING_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if not ping_url:
        logger.warning("SELF_PING_URL/RENDER_EXTERNAL_URL تنظیم نشده؛ حلقهٔ self-ping غیرفعال ماند.")
        return
    async with aiohttp.ClientSession() as session:
        while True:
            await asyncio.sleep(SELF_PING_INTERVAL_SECONDS)
            try:
                async with session.get(ping_url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    logger.info("self-ping انجام شد (وضعیت %s).", resp.status)
            except Exception:
                logger.exception("self-ping ناموفق بود؛ در چرخهٔ بعدی دوباره تلاش می‌شود.")


def main():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Flask keep-alive server started.")

    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
