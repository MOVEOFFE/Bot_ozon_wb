import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton
)

from database import (
    init_db,
    get_or_create_user,
    create_shop,
    get_user_shops,
    update_shop,
    delete_shop,
    archive_shop,
    unarchive_shop,
    save_reply_log,
    get_recent_logs,
    get_shop_reply_count,
    get_running_shops,
    get_running_shops_stats,
    check_shop_creation_allowed,
    check_shop_launch_allowed,
    Shop,
    User
)

# Синонимы и обёртки для полной совместимости
async def get_shop_by_id(shop_id: int):
    # update_shop без аргументов находит и возвращает актуальный объект Shop
    return await update_shop(shop_id)

get_all_running_shops = get_running_shops
async def get_shop_logs(shop_id: int, limit: int = 5, offset: int = 0):
    try:
        # Пытаемся передать оба параметра, если база поддерживает пагинацию
        return await get_recent_logs(shop_id, limit=limit, offset=offset)
    except TypeError:
        # Если в database.py нет offset, запрашиваем записи с запасом и делаем срез в памяти
        all_logs = await get_recent_logs(shop_id, limit=offset + limit)
        return all_logs[offset:offset + limit] if all_logs else []

from ozon_client import OzonClient, OzonAPIError
from wb_client import WildberriesClient, WBAPIError
from ai_generator import DeepSeekReplyGenerator

# ================= КОНФИГУРАЦИЯ =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
admin_ids_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(i.strip()) for i in admin_ids_raw.split(",") if i.strip().isdigit()]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

router = Router()
PROCESSED_REVIEW_IDS = set()

AGE_LABELS = {
    1: "1 день",
    3: "3 дня",
    7: "7 дней",
    14: "14 дней",
    30: "1 месяц",
    90: "3 месяца",
    180: "6 месяцев",
    0: "За все время"
}

TONE_LABELS = {
    "standard": "Нейтральный",
    "friendly": "Дружелюбный",
    "formal": "Деловой / Строгий",
    "grateful": "С благодарностью",
    "expert": "Экспертный"
}

# ================= СОСТОЯНИЯ FSM =================
class AddShopStates(StatesGroup):
    choose_marketplace = State()
    enter_name = State()
    ozon_client_id = State()
    ozon_api_key = State()
    wb_api_token = State()

class EditSettingsStates(StatesGroup):
    edit_signature = State()
    edit_stop_words = State()

class AdminStates(StatesGroup):
    target_user_id = State()
    add_quota_amount = State()

# ================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =================
def is_date_fresh(date_str: str, max_days: int) -> bool:
    if not max_days or max_days == 0 or not date_str:
        return True
    try:
        clean = date_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        now = datetime.now(timezone.utc)
        return dt >= (now - timedelta(days=max_days))
    except Exception:
        return True

async def interruptible_sleep(duration: int, shop_id: int) -> bool:
    step = 2
    waited = 0
    while waited < duration:
        await asyncio.sleep(step)
        waited += step
        shop = await get_shop_by_id(shop_id)
        if not shop or not shop.is_running or shop.is_archived:
            return False
    return True

def get_main_menu_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="🎛 Главная панель"), KeyboardButton(text="🏪 Мои магазины")],
        [KeyboardButton(text="💬 Ответы"), KeyboardButton(text="👤 Мой профиль")],
        [KeyboardButton(text="❓ Помощь и FAQ")]
    ]
    if is_admin:
        buttons.append([KeyboardButton(text="🛡 Супер-Админка")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

# ================= ГЛАВНАЯ ПАНЕЛЬ =================
async def get_main_dashboard_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    user_shops = await get_user_shops(user_id, include_archived=False)
    running_count, max_allowed = await get_running_shops_stats(user_id)
    running_display = f"{running_count}/{max_allowed}" if max_allowed != 99 else f"{running_count}/∞"

    if not user_shops:
        text = (
            "🤖 **Добро пожаловать в Vellio Bot!**\n\n"
            f"⚡️ **Слоты в работе:** `{running_display}`\n\n"
            "У вас пока не подключено ни одного магазина.\n"
            "Перейдите в раздел «🏪 Мои магазины», чтобы привязать первый магазин Ozon или Wildberries."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏪 Перейти в Мои магазины", callback_data="goto_shops_catalog")],
            [InlineKeyboardButton(text="❓ Помощь и FAQ", callback_data="faq_menu")]
        ])
        return text, kb

    status_summary_lines = []
    total_all_replies = 0

    for s in user_shops:
        mp = "🔵 Ozon" if s.marketplace == "ozon" else "🟣 WB"
        st = "🟢 В работе" if s.is_running else "🔴 Пауза"
        shop_replies = await get_shop_reply_count(s.id)
        total_all_replies += shop_replies
        status_summary_lines.append(f"  • {mp} **{s.name}**: {st} — 💬 **{shop_replies} шт.**")

    status_summary_text = "\n".join(status_summary_lines)

    text = (
        "🤖 **Сводная панель мониторинга Vellio**\n\n"
        f"⚡️ **Слоты в работе:** `{running_display}`\n"
        f"🏪 **Активных магазинов:** `{len(user_shops)}`\n"
        f"🚀 **Всего ответов бота:** `{total_all_replies}` шт.\n\n"
        f"📊 **Текущее состояние маркетплейсов:**\n{status_summary_text}\n\n"
        "Воспользуйтесь общими переключателями или перейдите в «Мои магазины» для настройки:"
    )

    keyboard_rows = []
    if len(user_shops) >= 2:
        if running_count > 0:
            keyboard_rows.append([
                InlineKeyboardButton(text="⏸ Поставить ВСЕ магазины на паузу", callback_data="pause_all_shops")
            ])
        if running_count < len(user_shops):
            keyboard_rows.append([
                InlineKeyboardButton(text="▶️ Запустить ВСЕ магазины", callback_data="resume_all_shops")
            ])

    keyboard_rows.append([
        InlineKeyboardButton(text="🔄 Обновить статус", callback_data="refresh_main_dashboard")
    ])
    keyboard_rows.append([
        InlineKeyboardButton(text="🏪 Управление магазинами", callback_data="goto_shops_catalog")
    ])

    return text, InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

# ================= ОЧЕРЕДИ МАРКЕТПЛЕЙСОВ =================
async def process_ozon_queue(bot: Bot, shop: Shop, ai: DeepSeekReplyGenerator, session_stats: dict) -> bool:
    fresh_shop = await get_shop_by_id(shop.id)
    if not fresh_shop or not fresh_shop.is_running or fresh_shop.is_archived:
        return False
    shop = fresh_shop

    ozon = OzonClient(client_id=shop.ozon_client_id, api_key=shop.ozon_api_key_enc)
    allowed_stars = [s for s in [1, 2, 3, 4, 5] if getattr(shop, f"reply_{s}_star", False)]
    if not allowed_stars:
        return False

    target_reviews = []
    last_id = ""

    for _ in range(50):
        try:
            data = await ozon.get_unprocessed_reviews(limit=100, last_id=last_id)
        except Exception as e:
            logging.error(f"[{shop.name}] Ошибка API Ozon: {e}")
            break

        batch = data.get("reviews", [])
        if not batch:
            break

        stop_paging = False
        for r in batch:
            rev_id = str(r.get("id"))

            if rev_id in PROCESSED_REVIEW_IDS:
                continue

            if shop.max_age_days > 0 and not is_date_fresh(r.get("published_at", ""), shop.max_age_days):
                stop_paging = True
                break

            if r.get("rating") not in allowed_stars:
                continue

            if r.get("comments_amount", 0) > 0 or r.get("status") == "PROCESSED":
                PROCESSED_REVIEW_IDS.add(rev_id)
                continue

            raw_text = (r.get("text") or "").strip()
            if not raw_text:
                continue

            stops = [s.strip().lower() for s in (shop.stop_words or "").split(",") if s.strip()]
            if any(s in raw_text.lower() for s in stops):
                continue

            target_reviews.append(r)
            if len(target_reviews) >= 15:
                stop_paging = True
                break

        if stop_paging or not data.get("has_next"):
            break
        last_id = data.get("last_id", "")

    if not target_reviews:
        return False

    skus = [r["sku"] for r in target_reviews]
    names = await ozon.get_product_names(skus)
    tone = getattr(shop, "tone_style", "standard") or "standard"
    sig = shop.signature_text or f"С уважением, {shop.name}"
    replied_in_cycle = 0

    for rev in target_reviews:
        cur_shop = await get_shop_by_id(shop.id)
        if not cur_shop or not cur_shop.is_running or cur_shop.is_archived:
            return True

        user = await get_or_create_user(shop.user_id)
        if not user.is_admin and user.replies_quota_left <= 0:
            await update_shop(shop.id, is_running=False)
            return False

        rev_id = str(rev.get("id"))
        p_name = names.get(rev["sku"], "наш товар")
        raw_text = (rev.get("text") or "").strip()

        reply = await ai.generate_reply(
            brand_name=shop.name,
            product_name=p_name,
            rating=rev["rating"],
            review_text=raw_text,
            photos_count=rev.get("photos_amount", 0),
            videos_count=rev.get("videos_amount", 0),
            tone_style=tone,
            signature=sig
        )

        try:
            await ozon.publish_reply(review_id=rev_id, text=reply)
            PROCESSED_REVIEW_IDS.add(rev_id)
            await save_reply_log(shop.id, "ozon", p_name, rev["rating"], raw_text, reply)
            replied_in_cycle += 1
            logging.info(f"[{shop.name}] Ответ Ozon опубликован на отзыв {rev_id} ({rev['rating']}⭐️)")
        except Exception as e:
            logging.error(f"[{shop.name}] Ошибка публикации ответа Ozon: {e}")

        if not await interruptible_sleep(45, shop.id):
            return True

    if replied_in_cycle > 0:
        try:
            await bot.send_message(
                shop.user_id,
                f"💤 **Отзывы закончились, ушел спать!**\n\n"
                f"🏪 Магазин: **[🔵 Ozon] {shop.name}**\n"
                f"✅ Отвечено за сеанс: **{replied_in_cycle} шт.**\n\n"
                f"Как только появятся новые отзывы, я сразу продолжу свою работу.",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    return True

async def process_wb_queue(bot: Bot, shop: Shop, ai: DeepSeekReplyGenerator, session_stats: dict) -> bool:
    fresh_shop = await get_shop_by_id(shop.id)
    if not fresh_shop or not fresh_shop.is_running or fresh_shop.is_archived:
        return False
    shop = fresh_shop

    wb = WildberriesClient(api_token=shop.wb_token_enc)
    allowed_stars = [s for s in [1, 2, 3, 4, 5] if getattr(shop, f"reply_{s}_star", False)]
    if not allowed_stars:
        logging.info(f"[{shop.name}] WB: В настройках магазина не выбрана ни одна звезда для ответов.")
        return False

    try:
        feedbacks = await wb.get_unprocessed_feedbacks(take=30)
        logging.info(f"[{shop.name}] WB: Запрос выполнен успешно. Получено неотвеченных отзывов: {len(feedbacks) if feedbacks else 0}")
    except Exception as e:
        logging.error(f"[{shop.name}] Ошибка API WB при получении отзывов: {e}")
        return False

    if not feedbacks:
        return False

    target_feedbacks = []
    for f in feedbacks:
        fb_id = str(f.get("id"))

        if fb_id in PROCESSED_REVIEW_IDS:
            continue

        if shop.max_age_days > 0 and not is_date_fresh(f.get("createdDate", ""), shop.max_age_days):
            logging.info(f"[{shop.name}] WB: Отзыв {fb_id} пропущен по дате (старше {shop.max_age_days} дн.)")
            continue

        val = f.get("productValuation") or f.get("valuation", 0)
        if val not in allowed_stars:
            logging.info(f"[{shop.name}] WB: Отзыв {fb_id} пропущен (оценка {val}⭐️ отключена в фильтре)")
            continue

        raw_text = (f.get("text") or "").strip()

        # Проверка стоп-слов (только если покупатель оставил текст)
        if raw_text:
            stops = [s.strip().lower() for s in (shop.stop_words or "").split(",") if s.strip()]
            if any(s in raw_text.lower() for s in stops):
                logging.info(f"[{shop.name}] WB: Отзыв {fb_id} содержит стоп-слово, пропущен.")
                continue

        target_feedbacks.append(f)
        if len(target_feedbacks) >= 15:
            break

    if not target_feedbacks:
        logging.info(f"[{shop.name}] WB: Нет подходящих отзывов под текущие фильтры.")
        return False

    tone = getattr(shop, "tone_style", "standard") or "standard"
    sig = shop.signature_text or f"С уважением, {shop.name}"
    replied_in_cycle = 0

    for fb in target_feedbacks:
        cur_shop = await get_shop_by_id(shop.id)
        if not cur_shop or not cur_shop.is_running or cur_shop.is_archived:
            return True

        user = await get_or_create_user(shop.user_id)
        if not user.is_admin and user.replies_quota_left <= 0:
            logging.warning(f"[{shop.name}] У пользователя исчерпан лимит ответов.")
            await update_shop(shop.id, is_running=False)
            return False

        fb_id = str(fb.get("id"))
        p_name = fb.get("productDetails", {}).get("productName") or "наш товар"
        val = fb.get("productValuation") or fb.get("valuation", 5)
        raw_text = (fb.get("text") or "").strip()

        # Если текста нет — подставляем маркер для ИИ, чтобы сгенерировать ответ на оценку
        effective_review_text = raw_text if raw_text else f"[Покупатель поставил оценку {val} из 5 звёзд без текстового отзыва]"

        reply = await ai.generate_reply(
            brand_name=shop.name,
            product_name=p_name,
            rating=val,
            review_text=effective_review_text,
            photos_count=len(fb.get("photoLinks") or []),
            videos_count=len(fb.get("video") or []),
            tone_style=tone,
            signature=sig
        )

        try:
            await wb.publish_reply(feedback_id=fb_id, text=reply)
            PROCESSED_REVIEW_IDS.add(fb_id)
            await save_reply_log(shop.id, "wb", p_name, val, raw_text or "Без текста", reply)
            replied_in_cycle += 1
            logging.info(f"[{shop.name}] Ответ WB опубликован на отзыв {fb_id} ({val}⭐️)")
        except Exception as e:
            logging.error(f"[{shop.name}] Ошибка публикации ответа WB: {e}")

        # Безопасный интервал между ответами
        if not await interruptible_sleep(20, shop.id):
            return True

    if replied_in_cycle > 0:
        try:
            await bot.send_message(
                shop.user_id,
                f"💤 **Отзывы закончились, ушел спать!**\n\n"
                f"🏪 Магазин: **[🟣 WB] {shop.name}**\n"
                f"✅ Отвечено за сеанс: **{replied_in_cycle} шт.**\n\n"
                f"Как только появятся новые отзывы, я сразу продолжу свою работу.",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    return True

# ================= ПАРАЛЛЕЛЬНЫЙ ВОРКЕР =================
async def process_single_shop(bot: Bot, shop: Shop, ai: DeepSeekReplyGenerator, session_stats: dict):
    try:
        mp = str(shop.marketplace or "").strip().lower()
        logging.info(f"[{shop.name}] Воркер запустил поток магазина (ID: {shop.id}, маркетплейс: {mp})")

        if mp == "ozon":
            await process_ozon_queue(bot, shop, ai, session_stats)
        elif mp in ["wb", "wildberries"]:
            await process_wb_queue(bot, shop, ai, session_stats)
        else:
            logging.warning(f"[{shop.name}] Неизвестный маркетплейс: '{mp}'")
    except Exception as e:
        logging.error(f"Worker Error ({shop.name}): {e}")

async def background_reviews_worker(bot: Bot):
    ai = DeepSeekReplyGenerator(api_key=DEEPSEEK_API_KEY)
    session_stats = {}

    while True:
        try:
            running_shops = await get_all_running_shops()
            if running_shops:
                tasks = [
                    process_single_shop(bot, shop, ai, session_stats)
                    for shop in running_shops
                ]
                await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            logging.error(f"Global Worker Loop Error: {e}")

        await asyncio.sleep(60)

# ================= ХЭНДЛЕРЫ НАВИГАЦИИ =================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = await get_or_create_user(message.from_user.id)
    is_admin = user.is_admin or message.from_user.id in ADMIN_IDS
    text, kb = await get_main_dashboard_view(message.from_user.id)
    await message.answer(
        "👋 Рады приветствовать вас в **Vellio** — умном автоответчике для маркетплейсов!",
        reply_markup=get_main_menu_keyboard(is_admin)
    )
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")

@router.message(F.text == "🎛 Главная панель")
async def nav_main_panel(message: Message, state: FSMContext):
    await state.clear()
    text, kb = await get_main_dashboard_view(message.from_user.id)
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data == "refresh_main_dashboard")
async def cb_refresh_main_dashboard(cb: CallbackQuery):
    text, kb = await get_main_dashboard_view(cb.from_user.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        await cb.answer("Статус обновлен!")
    except Exception:
        await cb.answer()

@router.message(F.text == "👤 Мой профиль")
async def nav_profile(message: Message):
    user = await get_or_create_user(message.from_user.id)
    shops = await get_user_shops(message.from_user.id, include_archived=False)
    running_count, _ = await get_running_shops_stats(message.from_user.id)

    total_replies = 0
    for s in shops:
        total_replies += await get_shop_reply_count(s.id)

    plan_label = "👑 Безлимит" if user.plan == "unlimited" or user.is_admin else user.plan.capitalize()
    quota_label = "Безлимитно" if user.is_admin or user.plan == "unlimited" else f"{user.replies_quota_left} шт."

    text = (
        "👤 **Личный кабинет селлера**\n\n"
        f"🆔 **Ваш ID:** `{message.from_user.id}`\n"
        f"📦 **Текущий тариф:** {plan_label}\n"
        f"⏳ **Срок действия:** Не ограничен\n"
        f"💬 **Остаток ответов:** {quota_label}\n\n"
        f"🏪 **Активных магазинов:** {len(shops)} шт.\n"
        f"⚡️ **Магазинов в работе:** {running_count} шт.\n"
        f"🚀 **Всего ответов бота:** {total_replies} шт."
    )
    await message.answer(text, parse_mode="Markdown")

@router.message(F.text == "🏪 Мои магазины")
async def nav_shops(message: Message, state: FSMContext):
    await state.clear()
    await show_shops_catalog(message.from_user.id, message)

@router.callback_query(F.data == "goto_shops_catalog")
async def cb_goto_shops(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_shops_catalog(cb.from_user.id, cb.message, is_edit=True)
    await cb.answer()

async def show_shops_catalog(user_id: int, message_or_cb, is_edit: bool = False):
    shops = await get_user_shops(user_id, include_archived=False)
    buttons = []

    for s in shops:
        mp = "🔵 Ozon" if s.marketplace == "ozon" else "🟣 WB"
        st = "🟢" if s.is_running else "🔴"
        count = await get_shop_reply_count(s.id)
        buttons.append([InlineKeyboardButton(
            text=f"{st} {mp} {s.name} ({count} отв.)",
            callback_data=f"shop_view_{s.id}"
        )])

    allowed, cur, max_l = await check_shop_creation_allowed(user_id)
    if allowed:
        buttons.append([InlineKeyboardButton(text="➕ Подключить магазин", callback_data="add_shop_start")])
    else:
        buttons.append([InlineKeyboardButton(text=f"⚠️ Достигнут лимит магазинов ({cur}/{max_l})", callback_data="limit_alert")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    text = "🏪 **Ваши подключенные магазины:**\nВыберите магазин для управления или привяжите новый:"

    if is_edit and isinstance(message_or_cb, Message):
        try:
            await message_or_cb.edit_text(text, reply_markup=kb, parse_mode="Markdown")
            return
        except Exception:
            pass
    await message_or_cb.answer(text, reply_markup=kb, parse_mode="Markdown")

# ================= КАРТОЧКА И НАСТРОЙКИ МАГАЗИНА =================
# ==================== КАРТОЧКА И НАСТРОЙКИ МАГАЗИНА ====================
@router.callback_query(F.data.startswith("shop_view_"))
async def cb_shop_view(cb: CallbackQuery):
    shop_id = int(cb.data.split("_")[2])
    shop = await get_shop_by_id(shop_id)
    if not shop:
        await cb.answer("Магазин не найден", show_alert=True)
        return

    mp = "🔵 Ozon" if shop.marketplace == "ozon" else "🟣 WB"
    st_text = "🟢 В работе (отвечает)" if shop.is_running else "🔴 На паузе"
    btn_toggle = "⏸ Поставить на паузу" if shop.is_running else "▶️ Запустить бота"
    count = await get_shop_reply_count(shop.id)

    stars_row = []
    for star in [1, 2, 3, 4, 5]:
        val = getattr(shop, f"reply_{star}_star", False)
        icon = "✅" if val else "❌"
        stars_row.append(InlineKeyboardButton(text=f"{icon} {star}⭐️", callback_data=f"toggle_star_{shop.id}_{star}"))

    # ... здесь продолжается твой код сборки кнопок kb и строки text ...

    # В САМОМ КОНЦЕ функции (после сборки text и kb):
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        pass
    await cb.answer("Статус обновлен")

    mp = "🔵 Ozon" if shop.marketplace == "ozon" else "🟣 WB"
    st_text = "🟢 В работе (отвечает)" if shop.is_running else "🔴 На паузе"
    btn_toggle = "⏸ Поставить на паузу" if shop.is_running else "▶️ Запустить бота"
    count = await get_shop_reply_count(shop.id)

    stars_row = []
    for star in [1, 2, 3, 4, 5]:
        val = getattr(shop, f"reply_{star}_star", False)
        icon = "✅" if val else "❌"
        stars_row.append(InlineKeyboardButton(text=f"{icon} {star}⭐️", callback_data=f"toggle_star_{shop.id}_{star}"))

    age_label = AGE_LABELS.get(shop.max_age_days, f"{shop.max_age_days} дн.")
    tone_label = TONE_LABELS.get(shop.tone_style, "Нейтральный")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn_toggle, callback_data=f"toggle_run_{shop.id}")],
        stars_row,
        [InlineKeyboardButton(text="⚡️ Выбрать все 1-5⭐️ / Сбросить", callback_data=f"toggle_all_stars_{shop.id}")],
        [
            InlineKeyboardButton(text=f"Глубина: 📅 {age_label}", callback_data=f"change_age_{shop.id}"),
            InlineKeyboardButton(text=f"Стиль: 😊 {tone_label}", callback_data=f"change_tone_{shop.id}")
        ],
        [
            InlineKeyboardButton(text="✍️ Изменить подпись", callback_data=f"edit_sig_{shop.id}"),
            InlineKeyboardButton(text="🛑 Стоп-слова", callback_data=f"edit_stops_{shop.id}")
        ],
        [
            InlineKeyboardButton(text="📋 Логи ответов", callback_data=f"logs_{shop.id}_0"),
            InlineKeyboardButton(text="🔄 Обновить", callback_data=f"shop_view_{shop.id}")
        ],
        [
            InlineKeyboardButton(text="📦 В архив", callback_data=f"confirm_archive_{shop.id}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"confirm_delete_{shop.id}")
        ],
        [InlineKeyboardButton(text="⬅️ К списку магазинов", callback_data="goto_shops_catalog")]
    ])

    text = (
        f"⚙️ **Настройки магазина: {mp} {shop.name}**\n\n"
        f"Статус: {st_text}\n"
        f"Отвечено ботом: 💬 **{count} отзывов**\n"
        f"Глубина проверки: 📅 **{age_label}**\n\n"
        f"🤖 **Параметры ИИ:**\n"
        f"• Стиль: **{tone_label}**\n"
        f"• Подпись: `{shop.signature_text or 'По умолчанию'}`\n"
        f"• Стоп-слова: `{shop.stop_words or 'Нет'}`"
    )
    try:
          await cb.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
         pass
    await cb.answer("Статус обновлен")

@router.callback_query(F.data.startswith("toggle_run_"))
async def cb_toggle_run(cb: CallbackQuery):
    shop_id = int(cb.data.split("_")[2])
    shop = await get_shop_by_id(shop_id)
    if not shop:
        return

    if not shop.is_running:
        allowed, cur, max_l = await check_shop_launch_allowed(cb.from_user.id, shop.id)
        if not allowed:
            await cb.answer(f"⚠️ Превышен лимит слотов ({cur}/{max_l})", show_alert=True)
            return
        await update_shop(shop.id, is_running=True)
        await cb.answer("Магазин запущен!")
    else:
        await update_shop(shop.id, is_running=False)
        await cb.answer("Магазин на паузе!")

    await cb_shop_view(cb)

@router.callback_query(F.data.startswith("toggle_star_"))
async def cb_toggle_star(cb: CallbackQuery):
    _, _, shop_id, star = cb.data.split("_")
    shop_id, star = int(shop_id), int(star)
    shop = await get_shop_by_id(shop_id)
    if shop:
        cur_val = getattr(shop, f"reply_{star}_star", False)
        kwargs = {f"reply_{star}_star": not cur_val}
        await update_shop(shop.id, **kwargs)
    await cb_shop_view(cb)

@router.callback_query(F.data.startswith("toggle_all_stars_"))
async def cb_toggle_all_stars(cb: CallbackQuery):
    shop_id = int(cb.data.split("_")[3])
    shop = await get_shop_by_id(shop_id)
    if shop:
        all_on = all(getattr(shop, f"reply_{i}_star", False) for i in range(1, 6))
        new_val = not all_on
        await update_shop(
            shop.id,
            reply_1_star=new_val,
            reply_2_star=new_val,
            reply_3_star=new_val,
            reply_4_star=new_val,
            reply_5_star=new_val
        )
    await cb_shop_view(cb)

@router.callback_query(F.data.startswith("change_age_"))
async def cb_change_age_menu(cb: CallbackQuery):
    shop_id = int(cb.data.split("_")[2])
    keys = list(AGE_LABELS.keys())
    buttons = []
    row = []
    for k in keys:
        row.append(InlineKeyboardButton(text=AGE_LABELS[k], callback_data=f"set_age_{shop_id}_{k}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="⬅️ Назад в настройки", callback_data=f"shop_view_{shop_id}")])

    await cb.message.edit_text(
        "📅 **Выберите глубину проверки отзывов:**\n"
        "Бот будет сканировать и отвечать только на отзывы за выбранный период:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )
    await cb.answer()

@router.callback_query(F.data.startswith("set_age_"))
async def cb_set_age(cb: CallbackQuery):
    parts = cb.data.split("_")
    shop_id = int(parts[2])
    days = int(parts[3])

    await update_shop(shop_id, max_age_days=days)
    await cb.answer("Глубина проверки обновлена!")

    # Безопасно клонируем CallbackQuery с новым data для Pydantic v2:
    fake_cb = cb.model_copy(update={"data": f"shop_view_{shop_id}"})
    await cb_shop_view(fake_cb)

@router.callback_query(F.data.startswith("change_tone_"))
async def cb_change_tone_menu(cb: CallbackQuery):
    shop_id = int(cb.data.split("_")[2])
    buttons = []
    for key, name in TONE_LABELS.items():
        buttons.append([InlineKeyboardButton(text=f"😊 {name}", callback_data=f"set_tone_{shop_id}_{key}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад в настройки", callback_data=f"shop_view_{shop_id}")])

    await cb.message.edit_text(
        "🎭 **Выберите тональность и стиль ответов ИИ:**",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )
    await cb.answer()

@router.callback_query(F.data.startswith("set_tone_"))
async def cb_set_tone(cb: CallbackQuery):
    _, _, shop_id, tone_key = cb.data.split("_")
    await update_shop(int(shop_id), tone_style=tone_key)
    await cb.answer(f"Стиль изменен на: {TONE_LABELS.get(tone_key)}")
    cb.data = f"shop_view_{shop_id}"
    await cb_shop_view(cb)

@router.callback_query(F.data.startswith("edit_sig_"))
async def cb_edit_signature(cb: CallbackQuery, state: FSMContext):
    shop_id = int(cb.data.split("_")[2])
    await state.update_data(target_shop_id=shop_id)
    await state.set_state(EditSettingsStates.edit_signature)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"shop_view_{shop_id}")]
    ])
    await cb.message.edit_text(
        "✍️ **Введите подпись для ответов:**\n\n"
        "Например: `С уважением, команда Бренда`\n"
        "Или отправьте `-`, чтобы убрать подпись.",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await cb.answer()

@router.message(EditSettingsStates.edit_signature)
async def process_new_signature(message: Message, state: FSMContext):
    data = await state.get_data()
    shop_id = data.get("target_shop_id")
    new_sig = "" if message.text.strip() == "-" else message.text.strip()
    await update_shop(shop_id, signature_text=new_sig)
    await state.clear()
    await message.answer("✅ Подпись успешно обновлена!")
    
    cb_dummy = CallbackQuery(
        id="0",
        from_user=message.from_user,
        chat_instance="0",
        message=message,
        data=f"shop_view_{shop_id}"
    )
    await cb_shop_view(cb_dummy)

@router.callback_query(F.data.startswith("edit_stops_"))
async def cb_edit_stops(cb: CallbackQuery, state: FSMContext):
    shop_id = int(cb.data.split("_")[2])
    await state.update_data(target_shop_id=shop_id)
    await state.set_state(EditSettingsStates.edit_stop_words)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"shop_view_{shop_id}")]
    ])
    await cb.message.edit_text(
        "🛑 **Введите стоп-слова через запятую:**\n\n"
        "Если отзыв содержит любое из этих слов, бот не станет отвечать на него автоматически.\n"
        "Например: `брак, возврат, разбито, подделка`\n\n"
        "Отправьте `-`, чтобы очистить список стоп-слов.",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await cb.answer()

@router.message(EditSettingsStates.edit_stop_words)
async def process_new_stop_words(message: Message, state: FSMContext):
    data = await state.get_data()
    shop_id = data.get("target_shop_id")
    stops = "" if message.text.strip() == "-" else message.text.strip()
    await update_shop(shop_id, stop_words=stops)
    await state.clear()
    await message.answer("✅ Список стоп-слов обновлен!")

    cb_dummy = CallbackQuery(
        id="0",
        from_user=message.from_user,
        chat_instance="0",
        message=message,
        data=f"shop_view_{shop_id}"
    )
    await cb_shop_view(cb_dummy)

# ================= ЛОГИ ОТВЕТОВ =================
@router.message(F.text == "💬 Ответы")
async def nav_answers_logs(message: Message):
    shops = await get_user_shops(message.from_user.id, include_archived=False)
    if not shops:
        await message.answer("У вас нет подключенных магазинов для просмотра логов.")
        return
    buttons = [
        [InlineKeyboardButton(text=f"{'🔵' if s.marketplace == 'ozon' else '🟣'} {s.name}", callback_data=f"logs_{s.id}_0")]
        for s in shops
    ]
    await message.answer("📋 Выберите магазин для просмотра истории ответов:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("logs_"))
async def cb_show_logs(cb: CallbackQuery):
    parts = cb.data.split("_")
    shop_id = int(parts[1])
    offset = int(parts[2]) if len(parts) > 2 else 0
    limit = 3  # Показываем по 3 отзыва, чтобы влезали полные тексты

    shop = await get_shop_by_id(shop_id)
    logs = await get_shop_logs(shop_id, limit=limit, offset=offset)

    if not logs and offset == 0:
        await cb.answer("В этом магазине еще нет опубликованных ответов", show_alert=True)
        return

    text = f"📋 **История ответов: {shop.name}**\n\n"
    for log in logs:
        text += (
            f"⭐️ **Оценка:** {log.rating}⭐️ | **Товар:** {log.product_name}\n"
            f"💬 **Отзыв:** _{log.review_text}_\n\n"
            f"🤖 **Ответ:**\n{log.reply_text}\n"
            f"──────────────────\n"
        )

    nav_btns = []
    if offset > 0:
        nav_btns.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"logs_{shop_id}_{max(0, offset - limit)}"))
    if len(logs) == limit:
        nav_btns.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"logs_{shop_id}_{offset + limit}"))

    rows = []
    if nav_btns:
        rows.append(nav_btns)
    rows.append([InlineKeyboardButton(text="⬅️ Назад в настройки", callback_data=f"shop_view_{shop_id}")])

    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="Markdown")
    except Exception:
        pass
    await cb.answer()

# ================= АРХИВАЦИЯ И УДАЛЕНИЕ =================
@router.callback_query(F.data.startswith("confirm_archive_"))
async def cb_confirm_archive(cb: CallbackQuery):
    shop_id = int(cb.data.split("_")[2])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ Да, архивировать", callback_data=f"do_archive_{shop_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"shop_view_{shop_id}")]
    ])
    await cb.message.edit_text("Вы уверены, что хотите перенести магазин в архив? Он перестанет отвечать на отзывы.", reply_markup=kb)
    await cb.answer()

@router.callback_query(F.data.startswith("do_archive_"))
async def cb_do_archive(cb: CallbackQuery):
    shop_id = int(cb.data.split("_")[2])
    await update_shop(shop_id, is_archived=True, is_running=False)
    await cb.answer("Магазин перенесен в архив")
    await show_shops_catalog(cb.from_user.id, cb.message, is_edit=True)

@router.callback_query(F.data.startswith("confirm_delete_"))
async def cb_confirm_delete(cb: CallbackQuery):
    shop_id = int(cb.data.split("_")[2])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, полностью удалить", callback_data=f"do_delete_{shop_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"shop_view_{shop_id}")]
    ])
    await cb.message.edit_text("⚠️ **Внимание:** магазин и вся история ответов будут удалены безвозвратно. Продолжить?", reply_markup=kb, parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data.startswith("do_delete_"))
async def cb_do_delete(cb: CallbackQuery):
    shop_id = int(cb.data.split("_")[2])
    await delete_shop(shop_id)
    await cb.answer("Магазин успешно удален")
    await show_shops_catalog(cb.from_user.id, cb.message, is_edit=True)

# ================= FSM: ПОДКЛЮЧЕНИЕ МАГАЗИНА =================
@router.callback_query(F.data == "add_shop_start")
async def cb_add_shop_start(cb: CallbackQuery, state: FSMContext):
    allowed, cur, max_l = await check_shop_creation_allowed(cb.from_user.id)
    if not allowed:
        await cb.answer(f"Достигнут лимит магазинов ({cur}/{max_l})", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔵 Ozon", callback_data="add_mp_ozon")],
        [InlineKeyboardButton(text="🟣 Wildberries", callback_data="add_mp_wb")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="goto_shops_catalog")]
    ])
    await cb.message.edit_text("🏪 **Выберите маркетплейс для подключения:**", reply_markup=kb, parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data.startswith("add_mp_"))
async def cb_choose_marketplace(cb: CallbackQuery, state: FSMContext):
    mp = "ozon" if cb.data == "add_mp_ozon" else "wildberries"
    await state.update_data(chosen_marketplace=mp)
    await state.set_state(AddShopStates.enter_name)

    mp_name = "Ozon" if mp == "ozon" else "Wildberries"
    await cb.message.edit_text(
        f"🏷 **Введите внутреннее название для вашего магазина {mp_name}:**\n\n"
        f"Например: {mp_name} Vellio",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="goto_shops_catalog")]
        ]),
        parse_mode="Markdown"
    )
    await cb.answer()

@router.message(AddShopStates.enter_name)
async def process_shop_name(message: Message, state: FSMContext):
    name = message.text.strip()
    await state.update_data(shop_name=name)
    data = await state.get_data()
    mp = data.get("chosen_marketplace")

    if mp == "ozon":
        await state.set_state(AddShopStates.ozon_client_id)
        await message.answer(
            "🔑 **Введите Client ID от API Ozon:**\n\n"
            "Его можно скопировать в:\n"
            "**ЛК Ozon Seller** → **Настройки** → **API ключи**",
        )
    else:
        await state.set_state(AddShopStates.wb_api_token)
        await message.answer(
            "🔑 **Введите API Токен Wildberries:**\n\n"
            "Где создать: **ЛК WB Партнёры** → **Настройки** → **Доступ к API** → **Создать новый токен**\n\n"
            "🛡️ **Необходимые права доступа:**\n"
            "• Выберите категорию: **«Вопросы и отзывы»**\n"
            "⚠️ **Важно:** Переключатель **«Только чтение»** должен быть **выключен** (иначе бот сможет читать отзывы, но не сможет публиковать на них ответы)."
        )

@router.message(AddShopStates.ozon_client_id)
async def process_ozon_client_id(message: Message, state: FSMContext):
    cid = message.text.strip()
    await state.update_data(ozon_client_id=cid)
    await state.set_state(AddShopStates.ozon_api_key)
    await message.answer("🔐 **Введите API ключ Ozon:**\n\n"
    "Где создать: **ЛК Ozon Seller** → **Настройки** → **API ключи** → **Сгенерировать ключ**\n\n"
    "🛡️ **Какие права выбрать:**\n"
    "• **Рекомендуется:** Роль **«Администратор»** (ключ будет работать без ограничений).\n"
    "• **Для максимальной безопасности:** Если не хотите давать полный доступ, выберите роль с ограниченными правами:\n"
    "   — **Отзывы (Reviews):** чтение и запись (для публикации ответов)\n"
    "   — **Товары (Products):** только чтение (Read-only, чтобы бот видел названия и артикулы товаров)")

@router.message(AddShopStates.ozon_api_key)
async def process_ozon_api_key(message: Message, state: FSMContext):
    key = message.text.strip()
    data = await state.get_data()
    cid = data.get("ozon_client_id")
    name = data.get("shop_name")

    status_msg = await message.answer("🔄 Проверяем API-ключи Ozon...")
    client = OzonClient(client_id=cid, api_key=key)
    ok, err = await client.verify_credentials()

    if not ok:
        await status_msg.edit_text(f"❌ Ошибка валидации ключей Ozon: {err}\nПопробуйте снова или нажмите /start.")
        return

    shop = await create_shop(
        user_id=message.from_user.id,
        marketplace="ozon",
        name=name,
        ozon_client_id=cid,
        ozon_api_key=key
    )
    await state.clear()
    await status_msg.edit_text(f"🎉 Магазин **{name}** успешно подключен и готов к работе!", parse_mode="Markdown")
    await show_shops_catalog(message.from_user.id, message)

@router.message(AddShopStates.wb_api_token)
async def process_wb_token(message: Message, state: FSMContext):
    token = message.text.strip()
    data = await state.get_data()
    name = data.get("shop_name")

    status_msg = await message.answer("🔄 Проверяем токен Wildberries...")
    client = WildberriesClient(api_token=token)
    ok, err = await client.verify_credentials()

    if not ok:
        await status_msg.edit_text(f"❌ Ошибка валидации токена WB: {err}\nПопробуйте снова или нажмите /start.")
        return

    shop = await create_shop(
        user_id=message.from_user.id,
        marketplace="wildberries",
        name=name,
        wb_token=token,
    )
    await state.clear()
    await status_msg.edit_text(f"🎉 Магазин **{name}** успешно привязан!", parse_mode="Markdown")
    await show_shops_catalog(message.from_user.id, message)

# ================= МАССОВЫЕ ДЕЙСТВИЯ =================
@router.callback_query(F.data == "pause_all_shops")
async def cb_pause_all(cb: CallbackQuery):
    shops = await get_user_shops(cb.from_user.id, include_archived=False)
    for s in shops:
        if s.is_running:
            await update_shop(s.id, is_running=False)
    await cb.answer("Все магазины поставлены на паузу")
    text, kb = await get_main_dashboard_view(cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

@router.callback_query(F.data == "resume_all_shops")
async def cb_resume_all(cb: CallbackQuery):
    shops = await get_user_shops(cb.from_user.id, include_archived=False)
    for s in shops:
        if not s.is_running:
            await update_shop(s.id, is_running=True)
    await cb.answer("Все магазины запущены")
    text, kb = await get_main_dashboard_view(cb.from_user.id)
    await cb.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")

# ================= FAQ И АДМИНКА =================
@router.message(F.text == "❓ Помощь и FAQ")
@router.callback_query(F.data == "faq_menu")
async def show_faq(event: Message | CallbackQuery):
    faq_text = (
        "❓ **Часто задаваемые вопросы (FAQ):**\n\n"
        "**1. Как бот отвечает на отзывы?**\n"
        "Бот с помощью ИИ DeepSeek анализирует оценку, текст покупателя, вложения и генерирует персонализированный вежливый ответ с вашей подписью.\n\n"
        "**2. Не заблокирует ли меня маркетплейс?**\n"
        "Нет. Все запросы выполняются строго через официальный API Ozon Seller и Wildberries OpenAPI с безопасными интервалами.\n\n"
        "**3. Что такое глубина проверки?**\n"
        "Это период в прошлое (от 1 дня до «За все время»), в пределах которого бот ищет неотвеченные отзывы.\n\n"
        "**4. Как настроить стоп-слова?**\n"
        "В настройках магазина укажите слова через запятую. Отзывы с такими словами бот оставит вам для ручного ответа."
    )
    if isinstance(event, CallbackQuery):
        await event.message.answer(faq_text, parse_mode="Markdown")
        await event.answer()
    else:
        await event.answer(faq_text, parse_mode="Markdown")

@router.message(F.text == "🛡 Супер-Админка")
async def admin_panel(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    all_running = await get_all_running_shops()
    text = (
        "🛡 **Панель супер-администратора:**\n\n"
        f"⚡️ Активных магазинов в работе сейчас: **{len(all_running)} шт.**\n"
        f"У вас полный безлимитный доступ без ограничений на тарифы и слоты."
    )
    await message.answer(text, parse_mode="Markdown")

# ================= ТОЧКА ВХОДА =================
async def main():
    await init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()
    dp.include_router(router)

    asyncio.create_task(background_reviews_worker(bot))

    logging.info("Start polling")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен пользователем")
