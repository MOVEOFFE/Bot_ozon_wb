import asyncio
import html
import os
import tempfile
from datetime import datetime
from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from config import ADMIN_IDS, APP_VERSION
from sqlalchemy.exc import IntegrityError
from database import (
    get_admin_metrics,
    grant_subscription,
    get_all_user_ids,
    get_users_page,
    create_promo_code,
    create_consistent_db_backup,
    PLAN_QUOTAS,
)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


admin_router = Router()
_broadcast_lock = asyncio.Lock()
PAGE_SIZE = 10
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
            [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")],
            [InlineKeyboardButton(text="💳 Выдать подписку", callback_data="admin_grant_sub")],
            [InlineKeyboardButton(text="🎟 Создать промокод", callback_data="admin_create_promo")],
            [InlineKeyboardButton(text="📢 Рассылка пользователям", callback_data="admin_broadcast")],
            [InlineKeyboardButton(text="💾 Скачать бэкап БД", callback_data="admin_db_backup")],
            [InlineKeyboardButton(text="🔄 Обновить метрики", callback_data="admin_refresh_metrics")],
            [InlineKeyboardButton(text="🔙 Режим селлера", callback_data="admin_to_seller")],
        ]
    )
    return text, kb


def _build_users_page(users, total, offset, shop_counts):
    page = offset // PAGE_SIZE + 1
    total_pages = max(1, -(-total // PAGE_SIZE))
    lines = [f"👥 <b>Пользователи</b> ({total} чел.) — стр. {page}/{total_pages}\n"]
    for idx, u in enumerate(users, start=offset + 1):
        name = " ".join(p for p in (u.first_name, u.last_name) if p) or "—"
        uname = f"@{u.username}" if u.username else "—"
        shops = shop_counts.get(u.user_id, 0)
        if u.is_admin or u.plan == "unlimited":
            plan = "👑 Безлимит"
            quota = "∞"
        else:
            plan = (u.plan or "trial").capitalize()
            quota = str(u.replies_quota_left or 0)
        created = u.created_at.strftime("%d.%m.%Y") if u.created_at else "—"
        lines.append(
            f"{idx}. {html.escape(name)} ({html.escape(uname)})\n"
            f"   ID: <code>{u.user_id}</code> | 🏪 {shops} | {html.escape(plan)} ({quota}) | 📅 {created}"
        )
    text = "\n".join(lines)

    buttons = []
    for u in users:
        name = " ".join(p for p in (u.first_name, u.last_name) if p).strip()
        if name and u.username:
            label = f"{name} (@{u.username})"
        elif name:
            label = name
        elif u.username:
            label = f"@{u.username}"
        else:
            label = f"ID {u.user_id}"
        url = f"https://t.me/{u.username}" if u.username else f"tg://user?id={u.user_id}"
        buttons.append([InlineKeyboardButton(text=f"💬 {label}", url=url)])

    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_users_page:{offset - PAGE_SIZE}"))
    if offset + PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"admin_users_page:{offset + PAGE_SIZE}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🔙 В админку", callback_data="admin_refresh_metrics")])
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@admin_router.message(F.text == "🛡 Супер-Админка")
async def btn_admin_panel(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    metrics = await get_admin_metrics()
    text, kb = _build_admin_dashboard(metrics)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@admin_router.callback_query(F.data == "admin_users")
async def cb_admin_users(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await state.clear()
    users, total, counts = await get_users_page(0, PAGE_SIZE)
    text, kb = _build_users_page(users, total, 0, counts)
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@admin_router.callback_query(F.data.startswith("admin_users_page:"))
async def cb_admin_users_page(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    try:
        offset = int(call.data.split(":", 1)[1])
    except ValueError:
        await call.answer()
        return
    users, total, counts = await get_users_page(offset, PAGE_SIZE)
    text, kb = _build_users_page(users, total, offset, counts)
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await call.answer()
    await call.answer()


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


@admin_router.callback_query(F.data == "admin_db_backup")
async def cb_admin_db_backup(call: CallbackQuery, bot: Bot):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    await call.answer("Создаю бэкап...")
    dest = os.path.join(
        tempfile.gettempdir(),
        f"vellio_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.db",
    )
    try:
        await create_consistent_db_backup(dest)
        await bot.send_document(call.from_user.id, FSInputFile(dest))
    except Exception as exc:
        await call.message.answer(f"❌ Не удалось создать бэкап: {exc}")
    finally:
        try:
            os.remove(dest)
        except Exception:
            pass


@admin_router.callback_query(F.data == "admin_to_seller")
async def cb_admin_to_seller(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
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
        "<code>КОД тип значение [ЛИМИТ]</code>\n\n"
        "• тип <code>plan</code> → <code>КОД plan ТАРИФ ДНИ [ЛИМИТ]</code>, напр. <code>MYCODE plan pro 30 100</code>\n"
        "• тип <code>replies</code> → <code>КОД replies ЧИСЛО [ЛИМИТ]</code>, напр. <code>BONUS replies 100 50</code>\n\n"
        "ЛИМИТ — максимум активаций на разных пользователей (по умолчанию 1; 0 = без лимита).",
        reply_markup=kb,
    )
    await call.answer()


@admin_router.message(AdminStates.waiting_for_promo_code, ~F.text.startswith("/"), ~F.text.in_(MENU_TEXTS))
async def process_admin_promo_code(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer("Неверный формат. Введите: КОД тип значение [ЛИМИТ] (например: MYCODE plan pro 30 100).")
        return
    code = parts[0]
    promo_type = parts[1].lower()
    if promo_type == "plan":
        if len(parts) < 4:
            await message.answer("Для plan укажите: КОД plan ТАРИФ ДНИ [ЛИМИТ] (например: MYCODE plan pro 30 100).")
            return
        plan_code_val = parts[2].lower()
        if plan_code_val not in PLAN_QUOTAS:
            await message.answer(f"Неизвестный тариф «{html.escape(plan_code_val)}». Доступны: {', '.join(PLAN_QUOTAS)}.")
            return
        try:
            value = int(parts[3])
        except ValueError:
            await message.answer("Дни должны быть числом.")
            return
        if value <= 0:
            await message.answer("Дни должны быть положительным числом.")
            return
        tail_index = 4
    elif promo_type == "replies":
        try:
            value = int(parts[2])
        except ValueError:
            await message.answer("Число ответов должно быть числом.")
            return
        if value <= 0:
            await message.answer("Число ответов должно быть положительным.")
            return
        plan_code_val = None
        tail_index = 3
    else:
        await message.answer("Тип должен быть plan или replies.")
        return

    max_uses = 1
    if len(parts) > tail_index:
        try:
            max_uses = int(parts[tail_index])
        except ValueError:
            await message.answer("Лимит использований должен быть числом.")
            return
        if max_uses < 0:
            await message.answer("Лимит использований не может быть отрицательным (0 = без лимита).")
            return

    try:
        await create_promo_code(code, promo_type, plan_code_val, value, max_uses)
    except IntegrityError:
        await state.clear()
        await message.answer(f"❌ Промокод <code>{html.escape(code.upper())}</code> уже существует.")
        return
    await state.clear()
    limit_label = f" (лимит: {max_uses})" if max_uses else " (без лимита)"
    await message.answer(f"✅ Промокод <code>{html.escape(code.upper())}</code> создан{limit_label}.")
