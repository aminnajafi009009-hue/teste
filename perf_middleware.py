"""
perf_middleware.py — لایه‌های performance برای aiogram

🎯 هدف: كاهش زمان پاسخ به زیر 100ms

اجزا: 
1. CallbackAnswerMiddleware — فوری قبل از handler، callback.answer() صدا می‌زنه
   → کاربر فوری پاسخ می‌گیرد (دور شدن ساعت شن)

2. UserCacheMiddleware — در outer middleware یک‌بار get_user() صدا می‌زنه
   و نتیجه رو در data["user"] می‌گذارد
   → همه handlerها با data.get("user") به‌جای db.get_user() بخونن

3. PerformanceLogMiddleware — روی requestهای کند (>100ms) لاگ می‌گیرد
"""

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

import db_async

logger = logging.getLogger(__name__)


class CallbackAnswerMiddleware(BaseMiddleware):
    """
    فوری callback.answer() صدا می‌زنه تا کاربر فوری پاسخ بگیرد.
    Handlerها می‌تونن با answer_cb_later=True این را اورراید کنن.
    """
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, CallbackQuery):
            # فوری قبل از هر بررسی، callback رو جواب ده (ساعت شن خاموش می‌شه)
            # Handlerهایی که می‌خوانن خودشون answer() بدن باید "_cb_answered": True بگذارن
            if not data.get("_cb_answered"):
                asyncio.create_task(event.answer())  # fire-and-forget
                data["_cb_answered"] = True
        return await handler(event, data)


class UserCacheMiddleware(BaseMiddleware):
    """
    یک‌بار get_user رو صدا می‌زنه و نتیجه رو در data["user"] می‌گذارد.
    Handlerها باید data.get("user") بخونن به‌جای db.get_user() صدا کردن.
    """
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        telegram_id = None
        if isinstance(event, (Message, CallbackQuery)):
            telegram_id = event.from_user.id if event.from_user else None

        if telegram_id:
            try:
                user = await db_async.get_user(telegram_id)
                data["user"] = user
            except Exception:
                logger.exception("خطا در UserCacheMiddleware")
                data["user"] = None
        else:
            data["user"] = None

        return await handler(event, data)


class PerformanceLogMiddleware(BaseMiddleware):
    """
    requestهای کندتر از 100ms رو لاگ می‌گیرد.
    """
    SLOW_THRESHOLD_MS = 100

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        t0 = time.monotonic()
        result = await handler(event, data)
        elapsed_ms = (time.monotonic() - t0) * 1000

        if elapsed_ms > self.SLOW_THRESHOLD_MS:
            # لاگ کردن تا بدونیم کجا کند است
            desc = ""
            if isinstance(event, CallbackQuery):
                desc = f"callback={event.data!r:.60s}"
            elif isinstance(event, Message):
                desc = f"msg={event.text!r:.60s}" if event.text else "msg=(media)"
            logger.warning(
                "⚠️ SLOW [%.0fms] %s",
                elapsed_ms,
                desc,
            )
        return result
