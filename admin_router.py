import logging
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from database import get_admin_metrics, grant_subscription, get_all_user_ids

ADMIN_IDS = [590568180]

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

admin_router = Router()

class AdminStates(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_days = State()
    waiting_for_broadcast_text = State()


@admin_router.message(F.text == "🛡 Супер-Админка")
async def btn_admin_panel(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    metrics = await get_admin_metrics()
    text = (
        "👑 **Панель супер-администратора Vellio**\n\n"
        f"👥 Всего пользователей: **{metrics['total_users']}**\n"
        f"🏪 Всего магазинов: **{metrics['total_shops']}**\n"
        f"🟢 Активных магазинов в работе: **{metrics['active_shops']}**\n"
        f"💬 Всего сгенерировано ответов: **{metrics['total_replies']}**"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Выдать подписку", callback_data="admin_grant_sub")],
        [InlineKeyboardButton(text="📢 Рассылка пользователям", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🔄 Обновить метрики", callback_data="admin_refresh_metrics")],
        [InlineKeyboardButton(text="🔙 Режим селлера", callback_data="admin_to_seller")]
    ])
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")


@admin_router.callback_query(F.data == "admin_refresh_metrics")
async def cb_refresh_admin_metrics(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    metrics = await get_admin_metrics()
    text = (
        "👑 **Панель супер-администратора Vellio**\n\n"
        f"👥 Всего пользователей: **{metrics['total_users']}**\n"
        f"🏪 Всего магазинов: **{metrics['total_shops']}**\n"
        f"🟢 Активных магазинов в работе: **{metrics['active_shops']}**\n"
        f"💬 Всего сгенерировано ответов: **{metrics['total_replies']}**"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Выдать подписку", callback_data="admin_grant_sub")],
        [InlineKeyboardButton(text="📢 Рассылка пользователям", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🔄 Обновить метрики", callback_data="admin_refresh_metrics")],
        [InlineKeyboardButton(text="🔙 Режим селлера", callback_data="admin_to_seller")]
    ])
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        await call.answer("Метрики обновлены")
    except Exception:
        await call.answer()


@admin_router.callback_query(F.data == "admin_grant_sub")
async def cb_admin_grant_sub(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.waiting_for_user_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_refresh_metrics")]
    ])
    await call.message.edit_text("👤 Введите Telegram ID пользователя для выдачи подписки:", reply_markup=kb)
    await call.answer()


@admin_router.message(AdminStates.waiting_for_user_id)
async def process_admin_user_id(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if not message.text.strip().isdigit():
        await message.answer("❌ Введите корректный числовой ID:")
        return

    target_id = int(message.text.strip())
    await state.update_data(target_user_id=target_id)
    await state.set_state(AdminStates.waiting_for_days)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Start (1 маг)", callback_data="admin_plan_start"),
         InlineKeyboardButton(text="Pro (3 маг)", callback_data="admin_plan_pro")],
        [InlineKeyboardButton(text="Agency (7 маг)", callback_data="admin_plan_agency"),
         InlineKeyboardButton(text="Безлимит", callback_data="admin_plan_unlimited")],
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_refresh_metrics")]
    ])
    await message.answer(f"📦 Выберите тариф для пользователя `{target_id}`:", reply_markup=kb, parse_mode="Markdown")


@admin_router.callback_query(F.data.startswith("admin_plan_"))
async def cb_admin_choose_plan(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    plan = call.data.replace("admin_plan_", "")
    await state.update_data(selected_plan=plan)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="30 дней", callback_data="admin_days_30"),
         InlineKeyboardButton(text="90 дней", callback_data="admin_days_90")],
        [InlineKeyboardButton(text="365 дней", callback_data="admin_days_365")],
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_refresh_metrics")]
    ])
    await call.message.edit_text(f"⏳ Выберите срок действия для тарифа **{plan.upper()}**:", reply_markup=kb, parse_mode="Markdown")
    await call.answer()


@admin_router.callback_query(F.data.startswith("admin_days_"))
async def cb_admin_choose_days(call: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(call.from_user.id):
        return
    days = int(call.data.replace("admin_days_", ""))
    data = await state.get_data()
    target_id = data.get("target_user_id")
    plan = data.get("selected_plan")
    await state.clear()

    success = await grant_subscription(target_id, plan, days)
    if success:
        try:
            await bot.send_message(
                target_id,
                f"🎉 **Ваша подписка успешно обновлена!**\n\n"
                f"📦 Тариф: **{plan.upper()}**\n"
                f"⏳ Срок: **{days} дней**\n\n"
                "Приятной работы с Vellio!",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        await call.message.edit_text(f"✅ Пользователю `{target_id}` успешно начислен тариф **{plan.upper()}** на **{days}** дней.", parse_mode="Markdown")
    else:
        await call.message.edit_text("❌ Ошибка: пользователь не найден в базе данных.")
    await call.answer()


@admin_router.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(AdminStates.waiting_for_broadcast_text)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_refresh_metrics")]
    ])
    await call.message.edit_text("📢 Отправьте текст для рассылки всем пользователям бота:", reply_markup=kb)
    await call.answer()


@admin_router.message(AdminStates.waiting_for_broadcast_text)
async def process_admin_broadcast(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    broadcast_text = message.text
    await state.clear()

    user_ids = await get_all_user_ids()
    sent_count = 0

    progress_msg = await message.answer(f"⏳ Отправка сообщений (всего {len(user_ids)})...")
    for uid in user_ids:
        try:
            await bot.send_message(uid, broadcast_text, parse_mode="Markdown")
            sent_count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass

    await progress_msg.edit_text(f"✅ Рассылка завершена! Доставлено: **{sent_count}/{len(user_ids)}** пользователям.")