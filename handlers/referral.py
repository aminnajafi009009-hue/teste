"""
handlers/referral.py
نمایش لینک دعوت اختصاصی، کد اختصاصی، و آمار دعوت دوستان
(تعداد دعوت، دعوت‌های موفق، مبلغ آزاد شده، مبلغ در انتظار).
"""

import ui_editor


from aiogram import Router, F, types

import database as db
import bot_info
from utils import show_menu_with_sticker

from keyboards import referral_menu

router = Router(name="referral")


@router.callback_query(F.data == "referral")
async def referral(callback: types.CallbackQuery):
    user = db.get_user(callback.from_user.id)
    if user is None:
        await callback.answer(ui_editor.get_alert_text("msg_18ae2939df", "ابتدا دستور /start را بزنید."), show_alert=True)
        return

    stats = db.get_referral_stats(user["id"])
    invite_link = f"https://t.me/{bot_info.get('bot_username')}?start={stats['invite_code']}"

    rs = db.get_referral_settings()
    # 🆕 این سه متن پیش‌فرض هستند و از پنل ادمین (ویرایش متن ← 👥 دعوت دوستان)
    # کاملاً قابل‌تغییرند؛ چون کل صفحه حالا از یک قالب با placeholder رندر
    # می‌شود (نگاه کن به SCREENS["referral"] در ui_editor.py)، ادمین می‌تواند
    # کل جمله‌ی شرط رفرال از جمله «مقدار خرید کاربر دعوت‌شده» را عیناً حذف کند
    # یا هر جور دلش خواست بازنویسی کند — چیزی هاردکد در پایتون قفل نشده.
    if not rs["paid_purchase_required"]:
        condition_text = f"ℹ️ به‌ازای هر دوستی که با لینک شما عضو شود، {rs['reward_amount']:,} تومان بلافاصله و بدون نیاز به خرید به کیف پول شما آزاد می‌شود."
    elif rs["min_volume_enabled"]:
        condition_text = f"ℹ️ به‌ازای هر دوستی که با لینک شما عضو شود و یک خرید {rs['min_volume_gb']} گیگ یا بیشتر انجام دهد، {rs['reward_amount']:,} تومان به‌صورت خودکار به کیف پول شما آزاد می‌شود.\n❌ تست رایگان و خریدهای زیر {rs['min_volume_gb']} گیگ پاداش را آزاد نمی‌کنند."
    else:
        condition_text = f"ℹ️ به‌ازای هر دوستی که با لینک شما عضو شود و هر خرید پولی انجام دهد، {rs['reward_amount']:,} تومان به‌صورت خودکار و بدون نیاز به هیچ اقدام دیگری به کیف پول شما آزاد می‌شود."

    template_values = {
        "reward_amount": f"{rs['reward_amount']:,}",
        "invite_link": invite_link,
        "invite_code": stats["invite_code"],
        "invited_count": stats["invited_count"],
        "successful_invites": stats["successful_invites"],
        "released_amount": f"{stats['released_amount']:,}",
        "locked_wallet": f"{user['locked_wallet']:,}",
        "condition_text": condition_text,
    }
    await show_menu_with_sticker(
        callback.bot, callback.message.chat.id, "referral", None,
        reply_markup=referral_menu(), template_values=template_values,
    )
    await callback.answer()
