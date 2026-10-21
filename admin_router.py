import asyncio
import logging
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest

from database import get_admin_metrics, grant_subscription, get_all_user_ids, get_all_users_admin

ADMIN_IDS = [590568180]

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

admin_router = Router()

class AdminStates(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_days = State()
    waiting_for_broadcast_text = State()


def get_admin_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users_list")],
        [InlineKeyboardButton(text="💳 Выдать подписку", callback_data="admin_grant_sub")],
        [InlineKeyboardButton(text="📢 Рассылка пользователям", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🔄 Обновить метрики", callback_data="admin_refresh_metrics")],
        [InlineKeyboardButton(text="🔙 Режим селлера", callback_data="admin_to_seller")]
    ])


@admin_router.message(F.text == "🛡 Супер-Админка")
async def btn_admin_panel(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    metrics = await get_admin_metrics()
    text = (
        "👑 <b>Панель супер-администратора Vellio</b>\n\n"
        f"👥 Всего пользователей: <b>{metrics['total_users']}</b>\n"
        f"🏪 Всего магазинов: <b>{metrics['total_shops']}</b>\n"
        f"🟢 Активных магазинов в работе: <b>{metrics['active_shops']}</b>\n"
        f"💬 Всего сгенерировано ответов: <b>{metrics['total_replies']}</b>"
    )
    await message.answer(text, reply_markup=get_admin_main_kb(), parse_mode="HTML")


@admin_router.callback_query(F.data == "admin_refresh_metrics")
async def cb_refresh_admin_metrics(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.clear()
    metrics = await get_admin_metrics()
    text = (
        "👑 <b>Панель супер-администратора Vellio</b>\n\n"
        f"👥 Всего пользователей: <b>{metrics['total_users']}</b>\n"
        f"🏪 Всего магазинов: <b>{metrics['total_shops']}</b>\n"
        f"🟢 Активных магазинов в работе: <b>{metrics['active_shops']}</b>\n"
        f"💬 Всего сгенерировано ответов: <b>{metrics['total_replies']}</b>"
    )
    try:
        await call.message.edit_text(text, reply_markup=get_admin_main_kb(), parse_mode="HTML")
        await call.answer("Метрики обновлены")
    except Exception:
        await call.answer()


@admin_router.callback_query(F.data == "admin_users_list")
async def cb_admin_users_list(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.clear()

    try:
        users = await get_all_users_admin()
        if not users:
            await call.answer("Пользователей пока нет", show_alert=True)
            return

        text_lines = ["👥 <b>Список пользователей бота:</b>\n"]
        user_buttons = []

        for idx, u in enumerate(users, 1):
            uid = u["user_id"]
            uname = u.get("username")

            if uname:
                clean_uname = uname.replace("@", "")
                user_display = f'<a href="https://t.me/{clean_uname}">@{clean_uname}</a>'
                user_buttons.append([InlineKeyboardButton(text=f"💬 @{clean_uname}", url=f"https://t.me/{clean_uname}")])
            else:
                user_display = f"ID <code>{uid}</code>"

            plan_name = str(u.get("plan") or "trial").upper()
            shops_cnt = u.get("shops_count", 0)
            quota = u.get("replies_quota_left", 0)
            quota_display = "∞" if plan_name == "UNLIMITED" or uid in ADMIN_IDS else f"{quota} шт."

            text_lines.append(
                f"{idx}. {user_display} (<code>{uid}</code>)\n"
                f"   • Тариф: <b>{plan_name}</b> | Ответы: <b>{quota_display}</b>\n"
                f"   • Подключено магазинов: <b>{shops_cnt} шт.</b>\n"
            )

        control_buttons = [
            [InlineKeyboardButton(text="🔄 Обновить список", callback_data="admin_users_list")],
            [InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_refresh_metrics")]
        ]

        kb = InlineKeyboardMarkup(inline_keyboard=user_buttons + control_buttons)

        await call.message.edit_text(
            "\n".join(text_lines),
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
    except TelegramBadRequest:
        pass
    except Exception as e:
        logging.error(f"Ошибка в cb_admin_users_list: {e}")
    finally:
        await call.answer()


@admin_router.callback_query(F.data == "admin_to_seller")
async def cb_admin_to_seller(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Вы вернулись в главное меню селлера. Используйте клавиатуру внизу.")
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

    if message.text in ["🎛 Главная панель", "🏪 Мои магазины", "💬 Ответы", "👤 Мой профиль", "❓ Помощь и FAQ", "🛡 Супер-Админка"]:
        await state.clear()
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
    await message.answer(f"📦 Выберите тариф для пользователя <code>{target_id}</code>:", reply_markup=kb, parse_mode="HTML")


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
    await call.message.edit_text(f"⏳ Выберите срок действия для тарифа <b>{plan.upper()}</b>:", reply_markup=kb, parse_mode="HTML")
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
                f"🎉 <b>Ваша подписка успешно обновлена!</b>\n\n"
                f"📦 Тариф: <b>{plan.upper()}</b>\n"
                f"⏳ Срок: <b>{days} дней</b>\n\n"
                "Приятной работы с Vellio!",
                parse_mode="HTML"
            )
        except Exception:
            pass
        await call.message.edit_text(f"✅ Пользователю <code>{target_id}</code> успешно начислен тариф <b>{plan.upper()}</b> на <b>{days}</b> дней.", parse_mode="HTML")
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

    if message.text in ["🎛 Главная панель", "🏪 Мои магазины", "💬 Ответы", "👤 Мой профиль", "❓ Помощь и FAQ", "🛡 Супер-Админка"]:
        await state.clear()
        return

    broadcast_text = message.text
    await state.clear()

    user_ids = await get_all_user_ids()
    sent_count = 0

    progress_msg = await message.answer(f"⏳ Отправка сообщений (всего {len(user_ids)})...")
    for uid in user_ids:
        try:
            await bot.send_message(uid, broadcast_text, parse_mode="HTML")
            sent_count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass

    await progress_msg.edit_text(f"✅ Рассылка завершена! Доставлено: <b>{sent_count}/{len(user_ids)}</b> пользователям.", parse_mode="HTML")
