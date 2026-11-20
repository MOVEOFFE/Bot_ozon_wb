import asyncio
import html
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from config import ADMIN_IDS, APP_VERSION
from database import get_admin_metrics, grant_subscription, get_all_user_ids, create_promo_code


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


admin_router = Router()
_broadcast_lock = asyncio.Lock()
MENU_TEXTS = {"🎛 Главная панель", "🏪 Мои магазины", "💬 Ответы", "👤 Мой профиль", "❓ Помощь и FAQ", "💳 Тарифы", "🛡 Супер-Админка"}


class AdminStates(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_days = State()
    waiting_for_broadcast_text = State()
    waiting_for_promo_code = State()


def _build_admin_dashboard(metrics: dict) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "👑 <b>Панель супер-администратора Vellio</b>\n\n"
        f"👥 Всего пользователей: <b>{metrics['total_users']}</b>\n"
        f"🏪 Всего магазинов: <b>{metrics['total_shops']}</b>\n"
        f"🟢 Активных магазинов в работе: <b>{metrics['active_shops']}</b>\n"
        f"💬 Всего сгенерировано ответов: <b>{metrics['total_replies']}</b>\n"
        f"🏷 Версия: <b>{APP_VERSION}</b>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Выдать подписку", callback_data="admin_grant_sub")],
            [InlineKeyboardButton(text="🎟 Создать промокод", callback_data="admin_create_promo")],
            [InlineKeyboardButton(text="📢 Рассылка пользователям", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="🔄 Обновить метрики", callback_data="admin_refresh_metrics")],
            [InlineKeyboardButton(text="🔙 Режим селлера", callback_data="admin_to_seller")],
        ]
    )
    return text, kb


@admin_router.message(F.text == "🛡 Супер-Админка")
async def btn_admin_panel(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    metrics = await get_admin_metrics()
    text, kb = _build_admin_dashboard(metrics)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@admin_router.callback_query(F.data == "admin_refresh_metrics")
async def cb_refresh_admin_metrics(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await state.clear()
    metrics = await get_admin_metrics()
    text, kb = _build_admin_dashboard(metrics)
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await call.answer("Метрики обновлены")
    except Exception:
        await call.answer()


@admin_router.callback_query(F.data == "admin_to_seller")
async def cb_admin_to_seller(call: CallbackQuery, state: FSMContext):
    await state.clear()
    from bot import get_main_dashboard_view

    text, kb = await get_main_dashboard_view(call.from_user.id)
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer("Переключено в режим селлера")


@admin_router.callback_query(F.data == "admin_grant_sub")
async def cb_admin_grant_sub(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await state.set_state(AdminStates.waiting_for_user_id)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_refresh_metrics")]]
    )
    await call.message.edit_text(
        "👤 Введите Telegram ID пользователя для выдачи подписки:", reply_markup=kb
    )
    await call.answer()


@admin_router.message(AdminStates.waiting_for_user_id, F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_TEXTS))
async def process_admin_user_id(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    raw_id = (message.text or "").strip()
    if not raw_id.isdigit():
        await message.answer("❌ Введите корректный числовой ID:")
        return

    target_id = int(raw_id)
    await state.update_data(target_user_id=target_id)
    await state.set_state(AdminStates.waiting_for_days)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Старт (1 маг)", callback_data="admin_plan_start"),
                InlineKeyboardButton(text="Базовый (2 маг)", callback_data="admin_plan_basic"),
            ],
            [
                InlineKeyboardButton(text="Бизнес (4 маг)", callback_data="admin_plan_business"),
                InlineKeyboardButton(text="PRO (8 маг)", callback_data="admin_plan_pro"),
            ],
            [InlineKeyboardButton(text="Безлимит", callback_data="admin_plan_unlimited")],
            [InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_refresh_metrics")],
        ]
    )
    await message.answer(
        f"📦 Выберите тариф для пользователя <code>{target_id}</code>:",
        reply_markup=kb,
        parse_mode="HTML",
    )


@admin_router.callback_query(F.data.startswith("admin_plan_"))
async def cb_admin_choose_plan(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    plan = call.data.replace("admin_plan_", "")
    await state.update_data(selected_plan=plan)
    await state.set_state(AdminStates.waiting_for_days)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="30 дней", callback_data="admin_days_30"),
                InlineKeyboardButton(text="90 дней", callback_data="admin_days_90"),
            ],
            [InlineKeyboardButton(text="365 дней", callback_data="admin_days_365")],
            [InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_refresh_metrics")],
        ]
    )
    await call.message.edit_text(
        f"⏳ Выберите срок действия для тарифа <b>{html.escape(plan.upper())}</b>:",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await call.answer()


@admin_router.callback_query(F.data.startswith("admin_days_"))
async def cb_admin_choose_days(call: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    days = int(call.data.replace("admin_days_", ""))
    data = await state.get_data()
    target_id = data.get("target_user_id")
    plan = data.get("selected_plan", "start")
    await state.clear()

    if not target_id:
        await call.message.edit_text("❌ Не выбран пользователь. Попробуйте снова.")
        await call.answer()
        return

    success = await grant_subscription(target_id, plan, days)
    if success:
        try:
            await bot.send_message(
                target_id,
                f"🎉 <b>Ваша подписка успешно обновлена!</b>\n\n"
                f"📦 Тариф: <b>{html.escape(plan.upper())}</b>\n"
                f"⏳ Срок: <b>{days} дней</b>\n\n"
                "Приятной работы с Vellio!",
                parse_mode="HTML",
            )
        except Exception:
            pass
        await call.message.edit_text(
            f"✅ Пользователю <code>{target_id}</code> успешно начислен тариф "
            f"<b>{html.escape(plan.upper())}</b> на <b>{days}</b> дней.",
            parse_mode="HTML",
        )
    else:
        await call.message.edit_text("❌ Ошибка: пользователь не найден в базе данных.")
    await call.answer()


@admin_router.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await state.set_state(AdminStates.waiting_for_broadcast_text)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_refresh_metrics")]]
    )
    await call.message.edit_text(
        "📢 Отправьте текст для рассылки всем пользователям бота:", reply_markup=kb
    )
    await call.answer()


@admin_router.message(AdminStates.waiting_for_broadcast_text, F.text, ~F.text.startswith("/"), ~F.text.in_(MENU_TEXTS))
async def process_admin_broadcast(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    broadcast_text = (message.text or "").strip()
    if not broadcast_text:
        await message.answer("❌ Отправьте текстовое сообщение для рассылки.")
        return

    await state.clear()

    user_ids = await get_all_user_ids()
    sent_count = 0
    error_count = 0

    progress_msg = await message.answer(f"⏳ Отправка сообщений (всего {len(user_ids)})...")
    safe_text = html.escape(broadcast_text)

    async with _broadcast_lock:
        for uid in user_ids:
            try:
                await bot.send_message(uid, safe_text, parse_mode="HTML")
                sent_count += 1
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after)
                try:
                    await bot.send_message(uid, safe_text, parse_mode="HTML")
                    sent_count += 1
                except Exception:
                    error_count += 1
            except TelegramForbiddenError:
                error_count += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                error_count += 1
            await asyncio.sleep(0.05)

    suffix = f", ошибок: <b>{error_count}</b>" if error_count else ""
    await progress_msg.edit_text(
        f"✅ Рассылка завершена! Доставлено: <b>{sent_count}/{len(user_ids)}</b> пользователям{suffix}.",
        parse_mode="HTML",
    )


@admin_router.callback_query(F.data == "admin_create_promo")
async def cb_admin_create_promo(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await state.set_state(AdminStates.waiting_for_promo_code)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_refresh_metrics")]
    ])
    await call.message.edit_text(
        "🎟 <b>Создать промокод</b>\n\n"
        "Введите одной строкой через пробел:\n"
        "<code>КОД тип значение</code>\n\n"
        "• тип <code>plan</code> → <code>КОД plan ТАРИФ ДНИ</code>, напр. <code>MYCODE plan pro 30</code>\n"
        "• тип <code>replies</code> → <code>КОД replies ЧИСЛО</code>, напр. <code>BONUS replies 100</code>",
        reply_markup=kb,
    )
    await call.answer()


@admin_router.message(AdminStates.waiting_for_promo_code)
async def process_admin_promo_code(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer("Неверный формат. Введите: КОД тип значение (например: MYCODE plan pro 30).")
        return
    code = parts[0]
    promo_type = parts[1].lower()
    if promo_type == "plan":
        if len(parts) < 4:
            await message.answer("Для plan укажите: КОД plan ТАРИФ ДНИ (например: MYCODE plan pro 30).")
            return
        plan_code_val = parts[2].lower()
        try:
            value = int(parts[3])
        except ValueError:
            await message.answer("Дни должны быть числом.")
            return
    elif promo_type == "replies":
        try:
            value = int(parts[2])
        except ValueError:
            await message.answer("Число ответов должно быть числом.")
            return
        plan_code_val = None
    else:
        await message.answer("Тип должен быть plan или replies.")
        return

    await create_promo_code(code, promo_type, plan_code_val, value)
    await state.clear()
    await message.answer(f"✅ Промокод <code>{html.escape(code.upper())}</code> создан.")
