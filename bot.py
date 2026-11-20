import asyncio
import html
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Optional, Union

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from admin_router import MENU_TEXTS, admin_router
from ai_generator import AIGenerationError, DeepSeekReplyGenerator, limit_reply_length
from config import ADMIN_IDS, AI_MAX_REPLY_LENGTH, APP_VERSION, BOT_TOKEN, DATABASE_URL, DEEPSEEK_API_KEY
from database import (
    ADD_ON_PACKAGES,
    PLAN_QUOTAS,
    Shop,
    ShopLimitError,
    TARIFFS,
    add_extra_replies,
    archive_shop,
    check_shop_creation_allowed,
    check_shop_launch_allowed,
    create_pending_approval,
    create_pending_payment,
    create_shop,
    decrement_user_quota,
    delete_shop,
    downgrade_expired_users,
    get_all_user_ids,
    get_decrypted_ozon_key,
    get_decrypted_wb_token,
    get_or_create_user,
    get_pending_approval,
    get_pending_approvals,
    get_pending_approvals_count,
    get_pending_payment_by_payment_id,
    get_pending_payments,
    get_promo_code,
    get_recent_logs,
    get_reply_counts_for_shops,
    get_running_shops,
    get_running_shops_stats,
    get_shop_by_id,
    get_shop_reply_count,
    get_user_shops,
    get_users_expiring_soon,
    grant_subscription,
    init_db,
    is_review_pending,
    is_review_processed,
    mark_review_processed,
    mark_user_expiry_notified,
    record_published_reply,
    release_reply_quota,
    reserve_reply_quota,
    save_reply_log,
    update_pending_approval,
    update_pending_payment,
    update_shop,
    use_promo_code,
)
from ozon_client import OzonClient
from wb_client import WildberriesClient
from yookassa_service import create_payment, get_payment_status

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

router = Router()
get_all_running_shops = get_running_shops

AGE_LABELS = {
    1: "1 день",
    3: "3 дня",
    7: "7 дней",
    14: "14 дней",
    30: "1 месяц",
    90: "3 месяца",
    180: "6 месяцев",
    0: "За все время",
}

TONE_LABELS = {
    "standard": "Нейтральный",
    "friendly": "Дружелюбный",
    "formal": "Деловой / Строгий",
    "grateful": "С благодарностью",
    "expert": "Экспертный",
}


class AddShopStates(StatesGroup):
    choose_marketplace = State()
    enter_name = State()
    ozon_client_id = State()
    ozon_api_key = State()
    wb_api_token = State()


class EditSettingsStates(StatesGroup):
    edit_signature = State()
    edit_stop_words = State()
    edit_instructions = State()


class EditPendingStates(StatesGroup):
    waiting_text = State()


class PaymentStates(StatesGroup):
    promo_code = State()


def escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def shorten(value: Any, length: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= length:
        return text
    return text[: max(0, length - 1)].rstrip() + "…"


def parse_review_date(date_str: Any) -> Optional[datetime]:
    if not date_str:
        return None
    raw = str(date_str).strip()
    for candidate in (raw, raw.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
        except (TypeError, ValueError):
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    if raw.isdigit():
        try:
            return datetime.fromtimestamp(int(raw), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def is_date_fresh(date_str: str, max_days: int) -> bool:
    if not max_days or not date_str:
        return True
    parsed = parse_review_date(date_str)
    if parsed is None:
        logging.warning(f"Не удалось разобрать дату отзыва {date_str!r} — отзыв считается свежим")
        return True
    return parsed >= datetime.now(timezone.utc) - timedelta(days=max_days)


def media_count(value: Any) -> int:
    if not value:
        return 0
    if isinstance(value, (list, tuple)):
        return len(value)
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 1


def stop_words_for(shop: Shop) -> list[str]:
    return [word.strip().lower() for word in (shop.stop_words or "").split(",") if word.strip()]


def has_stop_word(text: str, shop: Shop) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in stop_words_for(shop))


async def interruptible_sleep(duration: int, shop_id: int) -> bool:
    waited = 0
    while waited < duration:
        await asyncio.sleep(min(2, duration - waited))
        waited += 2
        shop = await get_shop_by_id(shop_id)
        if not shop or not shop.is_running or shop.is_archived:
            return False
    return True


def get_main_menu_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="🎛 Главная панель"), KeyboardButton(text="🏪 Мои магазины")],
        [KeyboardButton(text="💬 Ответы"), KeyboardButton(text="👤 Мой профиль")],
        [KeyboardButton(text="❓ Помощь и FAQ"), KeyboardButton(text="💳 Тарифы")],
    ]
    if is_admin:
        buttons.append([KeyboardButton(text="🛡 Супер-Админка")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


async def get_main_dashboard_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    user_shops = await get_user_shops(user_id, include_archived=False)
    running_count, max_allowed = await get_running_shops_stats(user_id)
    running_display = f"{running_count}/{max_allowed}" if max_allowed != 99 else f"{running_count}/∞"

    if not user_shops:
        text = (
            "🤖 <b>Добро пожаловать в Vellio Bot!</b>\n\n"
            f"⚡️ <b>Слоты в работе:</b> <code>{running_display}</code>\n\n"
            "У вас пока не подключено ни одного магазина.\n"
            "Перейдите в раздел «🏪 Мои магазины», чтобы привязать первый магазин Ozon или Wildberries."
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🏪 Перейти в Мои магазины", callback_data="goto_shops_catalog")],
                [InlineKeyboardButton(text="❓ Помощь и FAQ", callback_data="faq_menu")],
            ]
        )
        return text, kb

    total_all_replies = 0
    status_lines = []
    reply_counts = await get_reply_counts_for_shops([shop.id for shop in user_shops])
    total_all_replies = sum(reply_counts.values())
    for index, shop in enumerate(user_shops):
        marketplace = "🔵 Ozon" if shop.marketplace == "ozon" else "🟣 WB"
        status = "🟢 В работе" if shop.is_running else "🔴 Пауза"
        replies = reply_counts.get(shop.id, 0)
        if index < 20:
            status_lines.append(
                f"  • {marketplace} <b>{escape(shorten(shop.name, 40))}</b>: {status} — 💬 <b>{replies} шт.</b>"
            )
    if len(user_shops) > 20:
        status_lines.append(f"  • …и ещё {len(user_shops) - 20} магазинов")

    text = (
        "🤖 <b>Сводная панель мониторинга Vellio</b>\n\n"
        f"⚡️ <b>Слоты в работе:</b> <code>{running_display}</code>\n"
        f"🏪 <b>Активных магазинов:</b> <code>{len(user_shops)}</code>\n"
        f"🚀 <b>Всего ответов бота:</b> <code>{total_all_replies}</code> шт.\n\n"
        f"📊 <b>Текущее состояние маркетплейсов:</b>\n{chr(10).join(status_lines)}\n\n"
        "Воспользуйтесь общими переключателями или перейдите в «Мои магазины» для настройки:"
    )

    rows = []
    if len(user_shops) >= 2:
        if running_count > 0:
            rows.append([InlineKeyboardButton(text="⏸ Поставить ВСЕ магазины на паузу", callback_data="pause_all_shops")])
        if running_count < len(user_shops):
            rows.append([InlineKeyboardButton(text="▶️ Запустить ВСЕ магазины", callback_data="resume_all_shops")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить статус", callback_data="refresh_main_dashboard")])
    rows.append([InlineKeyboardButton(text="🏪 Управление магазинами", callback_data="goto_shops_catalog")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def get_shop_logs(shop_id: int, limit: int = 5, offset: int = 0):
    return await get_recent_logs(shop_id, limit=limit, offset=offset)


def effective_review_text(raw_text: str, rating: int) -> str:
    return raw_text if raw_text else f"[Покупатель поставил оценку {rating} из 5 звёзд без текстового отзыва]"


def build_pending_keyboard(approval_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"pa_{approval_id}_pub"),
            InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"pa_{approval_id}_edit"),
            InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"pa_{approval_id}_skip"),
        ]
    ])


def build_pending_text(approval) -> str:
    marketplace = "🔵 Ozon" if approval.marketplace == "ozon" else "🟣 WB"
    return (
        f"🛡 <b>Отзыв на модерации</b> ({marketplace})\n\n"
        f"⭐️ Оценка: <b>{approval.rating}⭐️</b> | Товар: <b>{escape(shorten(approval.product_name, 60))}</b>\n"
        f"💬 <b>Отзыв:</b> <i>{escape(shorten(approval.review_text, 500))}</i>\n\n"
        f"🤖 <b>Черновой ответ:</b>\n{escape(shorten(approval.proposed_reply, 800))}"
    )


async def send_pending_approval(bot: Bot, approval) -> bool:
    text = build_pending_text(approval)
    kb = build_pending_keyboard(approval.id)
    try:
        await bot.send_message(approval.user_id, text, reply_markup=kb, parse_mode="HTML")
        return True
    except Exception as exc:
        logging.error(f"Не удалось отправить отзыв на модерацию пользователю {approval.user_id}: {exc}")
        return False


async def publish_pending_reply(approval, reply_text: str) -> tuple[bool, str]:
    shop = await get_shop_by_id(approval.shop_id)
    if not shop:
        return False, "Магазин не найден"
    if not await reserve_reply_quota(shop.user_id):
        return False, "Недостаточно квоты ответов"
    try:
        if approval.marketplace == "ozon":
            client = OzonClient(client_id=shop.ozon_client_id, api_key=get_decrypted_ozon_key(shop))
            await client.publish_reply(review_id=approval.review_id, text=reply_text)
        else:
            client = WildberriesClient(api_token=get_decrypted_wb_token(shop))
            await client.publish_reply(feedback_id=approval.review_id, text=reply_text)
    except asyncio.CancelledError:
        await release_reply_quota(shop.user_id)
        raise
    except Exception as exc:
        await release_reply_quota(shop.user_id)
        status_code = getattr(exc, "status_code", 0)
        if 400 <= status_code < 500 and status_code not in (408, 429):
            await update_pending_approval(approval.id, status="failed")
            return False, f"Ошибка публикации ({status_code}): отзыв снят с модерации"
        return False, f"Ошибка публикации: {exc}"

    try:
        await record_published_reply(
            shop.id, approval.marketplace, approval.review_id,
            approval.product_name, approval.rating,
            approval.review_text or "Без текста", reply_text,
        )
    except Exception:
        logging.critical(
            f"[{shop.name}] Ответ на отзыв {approval.review_id} опубликован, но не записан локально",
            exc_info=True,
        )
    await update_pending_approval(approval.id, status="approved")
    return True, "Опубликовано"


async def process_ozon_queue(bot: Bot, shop: Shop, ai: DeepSeekReplyGenerator, session_stats: Optional[dict] = None) -> bool:
    fresh_shop = await get_shop_by_id(shop.id)
    if not fresh_shop or not fresh_shop.is_running or fresh_shop.is_archived:
        return False
    shop = fresh_shop

    api_key = get_decrypted_ozon_key(shop)
    if not shop.ozon_client_id or not api_key:
        logging.error(f"[{shop.name}] Ozon: отсутствуют Client ID или API Key")
        return False

    ozon = OzonClient(client_id=shop.ozon_client_id, api_key=api_key)
    allowed_stars = [star for star in range(1, 6) if getattr(shop, f"reply_{star}_star", False)]
    if not allowed_stars:
        return False

    target_reviews = []
    offset = 0

    for _ in range(50):
        try:
            data = await ozon.get_unprocessed_reviews(limit=100, offset=offset)
        except Exception as exc:
            logging.error(f"[{shop.name}] Ошибка API Ozon: {exc}")
            break

        batch = data.get("reviews", [])
        if not batch:
            break

        stop_paging = False
        for review in batch:
            review_id = str(review.get("id") or "")
            if not review_id:
                continue
            if await is_review_processed(shop.id, "ozon", review_id) or await is_review_pending(shop.id, "ozon", review_id):
                continue

            if shop.max_age_days > 0 and not is_date_fresh(review.get("published_at", ""), shop.max_age_days):
                logging.info(
                    f"[{shop.name}] Ozon: отзыв {review_id} от {review.get('published_at')} старше "
                    f"{shop.max_age_days} дн. — пагинация остановлена"
                )
                stop_paging = True
                break
            if review.get("rating") not in allowed_stars:
                continue
            if review.get("comments_amount", 0) > 0 or review.get("status") == "PROCESSED":
                await mark_review_processed(shop.id, "ozon", review_id)
                continue

            raw_text = (review.get("text") or "").strip()
            if raw_text and has_stop_word(raw_text, shop):
                continue

            target_reviews.append(review)
            if len(target_reviews) >= 15:
                stop_paging = True
                break

        offset += len(batch)
        if stop_paging or not data.get("has_next"):
            break

    logging.info(
        f"[{shop.name}] Ozon: к ответу {len(target_reviews)} отз. "
        f"(звёзды: {allowed_stars}, глубина: {shop.max_age_days} дн.)"
    )
    if not target_reviews:
        return False

    skus = [review.get("sku") for review in target_reviews if review.get("sku") is not None]
    names = await ozon.get_product_names(skus)
    tone = shop.tone_style or "standard"
    signature = shop.signature_text or f"С уважением, {shop.name}"
    replied_in_cycle = 0

    for review in target_reviews:
        current_shop = await get_shop_by_id(shop.id)
        if not current_shop or not current_shop.is_running or current_shop.is_archived:
            return True

        user = await get_or_create_user(shop.user_id)
        if not user.is_admin and user.plan != "unlimited" and user.replies_quota_left <= 0:
            await update_shop(shop.id, is_running=False)
            return False

        review_id = str(review.get("id") or "")
        if not review_id or await is_review_processed(shop.id, "ozon", review_id):
            continue

        sku = review.get("sku")
        product_name = names.get(sku, "наш товар")
        rating = int(review.get("rating") or 0)
        raw_text = (review.get("text") or "").strip()
        try:
            reply = await ai.generate_reply(
                brand_name=shop.name,
                product_name=product_name,
                rating=rating,
                review_text=effective_review_text(raw_text, rating),
                photos_count=media_count(review.get("photos_amount")),
                videos_count=media_count(review.get("videos_amount")),
                tone_style=tone,
                signature=signature,
                custom_instructions=shop.custom_instructions,
            )
        except AIGenerationError as exc:
            logging.error(f"[{shop.name}] Генерация ответа Ozon отложена: {exc}")
            return False
        reply = limit_reply_length(reply, signature, AI_MAX_REPLY_LENGTH)

        # Премодерация негативных отзывов (1-3★)
        if shop.moderate_negative and rating <= 3:
            if not await is_review_pending(shop.id, "ozon", review_id):
                approval = await create_pending_approval(
                    shop_id=shop.id,
                    user_id=shop.user_id,
                    marketplace="ozon",
                    review_id=review_id,
                    product_name=product_name,
                    rating=rating,
                    review_text=raw_text or "",
                    proposed_reply=reply,
                    sku=str(sku) if sku is not None else None,
                )
                await send_pending_approval(bot, approval)
            if not await interruptible_sleep(10, shop.id):
                return True
            continue

        current_shop = await get_shop_by_id(shop.id)
        if not current_shop or not current_shop.is_running or current_shop.is_archived:
            return True
        if not await reserve_reply_quota(shop.user_id):
            await update_shop(shop.id, is_running=False)
            return False

        try:
            await ozon.publish_reply(review_id=review_id, text=reply)
        except asyncio.CancelledError:
            await release_reply_quota(shop.user_id)
            raise
        except Exception as exc:
            await release_reply_quota(shop.user_id)
            status_code = getattr(exc, "status_code", 0)
            if 400 <= status_code < 500 and status_code not in (408, 429):
                await mark_review_processed(shop.id, "ozon", review_id)
                logging.error(f"[{shop.name}] Постоянная ошибка Ozon {status_code}: отзыв {review_id} пропущен")
            else:
                logging.error(f"[{shop.name}] Временная ошибка публикации Ozon: {exc}")
        else:
            try:
                recorded = await asyncio.shield(record_published_reply(
                    shop.id, "ozon", review_id, product_name, rating, raw_text or "Без текста", reply
                ))
            except Exception:
                logging.critical(
                    f"[{shop.name}] Ответ Ozon {review_id} опубликован, но не записан локально. Магазин остановлен для сверки",
                    exc_info=True,
                )
                await update_shop(shop.id, is_running=False)
                return False
            if not recorded:
                logging.warning(f"[{shop.name}] Отзыв Ozon {review_id} уже был зафиксирован параллельно")
            else:
                replied_in_cycle += 1
                if session_stats is not None:
                    session_stats["replies"] = session_stats.get("replies", 0) + 1
                logging.info(f"[{shop.name}] Ответ Ozon опубликован на отзыв {review_id} ({rating}⭐️)")

        if not await interruptible_sleep(45, shop.id):
            return True

    if replied_in_cycle > 0:
        try:
            await bot.send_message(
                shop.user_id,
                "✅ <b>Цикл обработки завершён.</b>\n\n"
                f"🏪 Магазин: <b>[🔵 Ozon] {escape(shop.name)}</b>\n"
                f"💬 Отвечено за сеанс: <b>{replied_in_cycle} шт.</b>\n\n"
                "Если остались новые отзывы, я продолжу через минуту.",
                parse_mode="HTML",
            )
        except Exception:
            pass
    return True


async def process_wb_queue(bot: Bot, shop: Shop, ai: DeepSeekReplyGenerator, session_stats: Optional[dict] = None) -> bool:
    fresh_shop = await get_shop_by_id(shop.id)
    if not fresh_shop or not fresh_shop.is_running or fresh_shop.is_archived:
        return False
    shop = fresh_shop

    token = get_decrypted_wb_token(shop)
    if not token:
        logging.error(f"[{shop.name}] WB: отсутствует API-токен")
        return False

    wb = WildberriesClient(api_token=token)
    allowed_stars = [star for star in range(1, 6) if getattr(shop, f"reply_{star}_star", False)]
    if not allowed_stars:
        logging.info(f"[{shop.name}] WB: не выбрана ни одна звезда для ответов")
        return False

    target_feedbacks = []
    page_size = 30
    stop_paging = False

    for page in range(10):
        try:
            feedbacks = await wb.get_unprocessed_feedbacks(take=page_size, skip=page * page_size)
        except Exception as exc:
            logging.error(f"[{shop.name}] Ошибка API WB при получении отзывов: {exc}")
            break
        if not feedbacks:
            break

        for feedback in feedbacks:
            feedback_id = str(feedback.get("id") or "")
            if not feedback_id or await is_review_processed(shop.id, "wb", feedback_id) or await is_review_pending(shop.id, "wb", feedback_id):
                continue
            if shop.max_age_days > 0 and not is_date_fresh(feedback.get("createdDate", ""), shop.max_age_days):
                # API возвращает отзывы от новых к старым (dateDesc).
                logging.info(
                    f"[{shop.name}] WB: отзыв {feedback_id} от {feedback.get('createdDate')} старше "
                    f"{shop.max_age_days} дн. — пагинация остановлена"
                )
                stop_paging = True
                break

            rating = feedback.get("productValuation") or feedback.get("valuation") or 0
            if rating not in allowed_stars:
                continue

            combined_text = " ".join(
                part for part in (feedback.get("text"), feedback.get("pros"), feedback.get("cons")) if part
            ).strip()
            if combined_text and has_stop_word(combined_text, shop):
                continue

            target_feedbacks.append(feedback)
            if len(target_feedbacks) >= 15:
                stop_paging = True
                break

        if stop_paging or len(feedbacks) < page_size:
            break

    logging.info(
        f"[{shop.name}] WB: к ответу {len(target_feedbacks)} отз. "
        f"(звёзды: {allowed_stars}, глубина: {shop.max_age_days} дн.)"
    )
    if not target_feedbacks:
        return False

    tone = shop.tone_style or "standard"
    signature = shop.signature_text or f"С уважением, {shop.name}"
    replied_in_cycle = 0

    for feedback in target_feedbacks:
        current_shop = await get_shop_by_id(shop.id)
        if not current_shop or not current_shop.is_running or current_shop.is_archived:
            return True

        user = await get_or_create_user(shop.user_id)
        if not user.is_admin and user.plan != "unlimited" and user.replies_quota_left <= 0:
            await update_shop(shop.id, is_running=False)
            return False

        feedback_id = str(feedback.get("id") or "")
        if not feedback_id or await is_review_processed(shop.id, "wb", feedback_id):
            continue

        details = feedback.get("productDetails") or {}
        product_name = details.get("productName") or "наш товар"
        rating = int(feedback.get("productValuation") or feedback.get("valuation") or 0)
        raw_text = " ".join(
            part for part in (feedback.get("text"), feedback.get("pros"), feedback.get("cons")) if part
        ).strip()
        try:
            reply = await ai.generate_reply(
                brand_name=shop.name,
                product_name=product_name,
                rating=rating,
                review_text=effective_review_text(raw_text, rating),
                photos_count=media_count(feedback.get("photoLinks")),
                videos_count=media_count(feedback.get("video")),
                tone_style=tone,
                signature=signature,
                custom_instructions=shop.custom_instructions,
            )
        except AIGenerationError as exc:
            logging.error(f"[{shop.name}] Генерация ответа WB отложена: {exc}")
            return False
        reply = limit_reply_length(reply, signature, AI_MAX_REPLY_LENGTH)

        # Премодерация негативных отзывов (1-3★)
        if shop.moderate_negative and rating <= 3:
            if not await is_review_pending(shop.id, "wb", feedback_id):
                approval = await create_pending_approval(
                    shop_id=shop.id,
                    user_id=shop.user_id,
                    marketplace="wb",
                    review_id=feedback_id,
                    product_name=product_name,
                    rating=rating,
                    review_text=raw_text or "",
                    proposed_reply=reply,
                )
                await send_pending_approval(bot, approval)
            if not await interruptible_sleep(10, shop.id):
                return True
            continue

        current_shop = await get_shop_by_id(shop.id)
        if not current_shop or not current_shop.is_running or current_shop.is_archived:
            return True
        if not await reserve_reply_quota(shop.user_id):
            await update_shop(shop.id, is_running=False)
            return False

        try:
            await wb.publish_reply(feedback_id=feedback_id, text=reply)
        except asyncio.CancelledError:
            await release_reply_quota(shop.user_id)
            raise
        except Exception as exc:
            await release_reply_quota(shop.user_id)
            status_code = getattr(exc, "status_code", 0)
            if 400 <= status_code < 500 and status_code not in (408, 429):
                await mark_review_processed(shop.id, "wb", feedback_id)
                logging.error(f"[{shop.name}] Постоянная ошибка WB {status_code}: отзыв {feedback_id} пропущен")
            else:
                logging.error(f"[{shop.name}] Временная ошибка публикации WB: {exc}")
        else:
            try:
                recorded = await asyncio.shield(record_published_reply(
                    shop.id, "wb", feedback_id, product_name, rating, raw_text or "Без текста", reply
                ))
            except Exception:
                logging.critical(
                    f"[{shop.name}] Ответ WB {feedback_id} опубликован, но не записан локально. Магазин остановлен для сверки",
                    exc_info=True,
                )
                await update_shop(shop.id, is_running=False)
                return False
            if not recorded:
                logging.warning(f"[{shop.name}] Отзыв WB {feedback_id} уже был зафиксирован параллельно")
            else:
                replied_in_cycle += 1
                if session_stats is not None:
                    session_stats["replies"] = session_stats.get("replies", 0) + 1
                logging.info(f"[{shop.name}] Ответ WB опубликован на отзыв {feedback_id} ({rating}⭐️)")

        if not await interruptible_sleep(20, shop.id):
            return True

    if replied_in_cycle > 0:
        try:
            await bot.send_message(
                shop.user_id,
                "✅ <b>Цикл обработки завершён.</b>\n\n"
                f"🏪 Магазин: <b>[🟣 WB] {escape(shop.name)}</b>\n"
                f"💬 Отвечено за сеанс: <b>{replied_in_cycle} шт.</b>\n\n"
                "Если остались новые отзывы, я продолжу через минуту.",
                parse_mode="HTML",
            )
        except Exception:
            pass
    return True


async def process_single_shop(bot: Bot, shop: Shop, ai: DeepSeekReplyGenerator, session_stats: Optional[dict] = None):
    try:
        marketplace = str(shop.marketplace or "").strip().lower()
        if marketplace == "ozon":
            await process_ozon_queue(bot, shop, ai, session_stats)
        elif marketplace in ("wb", "wildberries"):
            await process_wb_queue(bot, shop, ai, session_stats)
        else:
            logging.warning(f"[{shop.name}] Неизвестный маркетплейс: '{marketplace}'")
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.exception(f"Worker Error ({shop.name})")


SHOP_WORKER_TASKS: dict[int, asyncio.Task] = {}


async def stop_shop_worker(shop_id: int):
    task = SHOP_WORKER_TASKS.pop(shop_id, None)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _shop_reviews_loop(bot: Bot, shop_id: int, ai: DeepSeekReplyGenerator):
    try:
        while True:
            shop = await get_shop_by_id(shop_id)
            if not shop or not shop.is_running or shop.is_archived:
                return
            await process_single_shop(bot, shop, ai, {})
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.exception(f"Фоновый поток магазина {shop_id} завершился с ошибкой")


async def background_reviews_worker(bot: Bot):
    ai = DeepSeekReplyGenerator(api_key=DEEPSEEK_API_KEY)
    try:
        while True:
            try:
                running_shops = await get_all_running_shops()
                running_ids = {shop.id for shop in running_shops}

                for shop_id in list(SHOP_WORKER_TASKS):
                    if shop_id not in running_ids:
                        task = SHOP_WORKER_TASKS.pop(shop_id)
                        task.cancel()

                for shop in running_shops:
                    task = SHOP_WORKER_TASKS.get(shop.id)
                    if task is None or task.done():
                        SHOP_WORKER_TASKS[shop.id] = asyncio.create_task(
                            _shop_reviews_loop(bot, shop.id, ai), name=f"reviews-shop-{shop.id}"
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Global Worker Loop Error")
            await asyncio.sleep(10)
    finally:
        tasks = list(SHOP_WORKER_TASKS.values())
        SHOP_WORKER_TASKS.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def send_subscription_warning(bot: Bot, user_id: int, date_label: str) -> bool:
    text = (
        f"⏳ Ваша подписка Vellio заканчивается <b>{escape(date_label)}</b>. "
        "Продлите её, чтобы магазины продолжили отвечать на отзывы."
    )
    for attempt in range(3):
        try:
            await bot.send_message(user_id, text, parse_mode="HTML")
            return True
        except TelegramRetryAfter as exc:
            if attempt == 2:
                return False
            await asyncio.sleep(exc.retry_after)
        except TelegramForbiddenError:
            # Пользователь заблокировал бота: временные повторы бессмысленны.
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception(f"Не удалось предупредить пользователя {user_id} об окончании подписки")
            return False
    return False


async def background_subscription_worker(bot: Bot):
    while True:
        try:
            for user in await get_users_expiring_soon():
                date_label = user.subscription_until.strftime("%d.%m.%Y") if user.subscription_until else "скоро"
                if await send_subscription_warning(bot, user.user_id, date_label):
                    await mark_user_expiry_notified(user.user_id)

            for report in await downgrade_expired_users():
                archived = ", ".join(report["archived_shops"]) or "нет"
                try:
                    await bot.send_message(
                        report["user_id"],
                        "⌛️ Срок действия вашей подписки истёк. Активирован пробный тариф.\n"
                        f"Прежний тариф: <b>{escape(report['old_plan'])}</b>.\n"
                        f"Магазины перенесены в архив: <b>{escape(archived)}</b>.",
                        parse_mode="HTML",
                    )
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(exc.retry_after)
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Subscription Worker Error")
        await asyncio.sleep(3600)


async def safe_callback_answer(call: CallbackQuery, text: Optional[str] = None, show_alert: bool = False):
    try:
        await call.answer(text=text, show_alert=show_alert)
    except TelegramBadRequest:
        pass


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = await get_or_create_user(message.from_user.id)
    is_admin = user.is_admin or message.from_user.id in ADMIN_IDS
    text, kb = await get_main_dashboard_view(message.from_user.id)
    await message.answer(
        "👋 Рады приветствовать вас в <b>Vellio</b> — умном автоответчике для маркетплейсов!",
        reply_markup=get_main_menu_keyboard(is_admin),
    )
    await message.answer(text, reply_markup=kb)


@router.message(F.text == "🎛 Главная панель")
async def nav_main_panel(message: Message, state: FSMContext):
    await state.clear()
    text, kb = await get_main_dashboard_view(message.from_user.id)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "refresh_main_dashboard")
async def cb_refresh_main_dashboard(call: CallbackQuery):
    text, kb = await get_main_dashboard_view(call.from_user.id)
    try:
        await call.message.edit_text(text, reply_markup=kb)
        await safe_callback_answer(call, "Статус обновлен!")
    except TelegramBadRequest:
        await safe_callback_answer(call)


@router.message(F.text == "👤 Мой профиль")
async def nav_profile(message: Message, state: FSMContext):
    await state.clear()
    user = await get_or_create_user(message.from_user.id)
    shops = await get_user_shops(message.from_user.id, include_archived=False)
    running_count, _ = await get_running_shops_stats(message.from_user.id)

    total_replies = sum((await get_reply_counts_for_shops([shop.id for shop in shops])).values())
    unlimited = user.is_admin or user.plan == "unlimited"
    plan_label = "👑 Безлимит" if unlimited else user.plan.capitalize()
    quota_label = "Безлимитно" if unlimited else f"{user.replies_quota_left} шт."
    if user.subscription_until:
        subscription_label = user.subscription_until.strftime("%d.%m.%Y %H:%M UTC")
    elif unlimited:
        subscription_label = "Не ограничен"
    else:
        subscription_label = "Пробный тариф"

    text = (
        "👤 <b>Личный кабинет селлера</b>\n\n"
        f"🆔 <b>Ваш ID:</b> <code>{message.from_user.id}</code>\n"
        f"📦 <b>Текущий тариф:</b> {escape(plan_label)}\n"
        f"⏳ <b>Срок действия:</b> {subscription_label}\n"
        f"💬 <b>Остаток ответов:</b> {quota_label}\n\n"
        f"🏪 <b>Активных магазинов:</b> {len(shops)} шт.\n"
        f"⚡️ <b>Магазинов в работе:</b> {running_count} шт.\n"
        f"🚀 <b>Всего ответов бота:</b> {total_replies} шт."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Тарифы и оплата", callback_data="tariffs_menu")],
    ])
    await message.answer(text, reply_markup=kb)


@router.message(F.text == "🏪 Мои магазины")
async def nav_shops(message: Message, state: FSMContext):
    await state.clear()
    await show_shops_catalog(message.from_user.id, message)


@router.callback_query(F.data == "goto_shops_catalog")
async def cb_goto_shops(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_shops_catalog(call.from_user.id, call.message, is_edit=True)
    await safe_callback_answer(call)


@router.callback_query(F.data.startswith("shops_page_"))
async def cb_shops_page(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        offset = max(0, int(call.data.rsplit("_", 1)[1]))
    except (ValueError, IndexError):
        await safe_callback_answer(call, "Некорректная страница", show_alert=True)
        return
    await show_shops_catalog(call.from_user.id, call.message, is_edit=True, offset=offset)
    await safe_callback_answer(call)


async def build_shops_catalog(user_id: int, offset: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    shops = await get_user_shops(user_id, include_archived=False)
    page_size = 20
    offset = max(0, min(offset, max(0, ((len(shops) - 1) // page_size) * page_size)))
    visible_shops = shops[offset:offset + page_size]
    counts = await get_reply_counts_for_shops([shop.id for shop in visible_shops])
    buttons = []
    for shop in visible_shops:
        marketplace = "🔵 Ozon" if shop.marketplace == "ozon" else "🟣 WB"
        status = "🟢" if shop.is_running else "🔴"
        count = counts.get(shop.id, 0)
        buttons.append([
            InlineKeyboardButton(
                text=f"{status} {marketplace} {shorten(shop.name, 40)} ({count} отв.)",
                callback_data=f"shop_view_{shop.id}",
            )
        ])

    navigation = []
    if offset > 0:
        navigation.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"shops_page_{max(0, offset - page_size)}"))
    if offset + page_size < len(shops):
        navigation.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"shops_page_{offset + page_size}"))
    if navigation:
        buttons.append(navigation)

    allowed, current, maximum = await check_shop_creation_allowed(user_id)
    if allowed:
        buttons.append([InlineKeyboardButton(text="➕ Подключить магазин", callback_data="add_shop_start")])
    else:
        buttons.append([
            InlineKeyboardButton(text=f"⚠️ Достигнут лимит магазинов ({current}/{maximum})", callback_data="limit_alert")
        ])

    text = (
        f"🏪 <b>Ваши подключенные магазины ({len(shops)}):</b>\n"
        "Выберите магазин для управления или привяжите новый:"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


async def show_shops_catalog(
    user_id: int,
    message_or_call: Union[Message, CallbackQuery],
    is_edit: bool = False,
    offset: int = 0,
):
    text, kb = await build_shops_catalog(user_id, offset=offset)
    message = message_or_call.message if isinstance(message_or_call, CallbackQuery) else message_or_call
    if is_edit:
        try:
            await message.edit_text(text, reply_markup=kb)
            return
        except TelegramBadRequest:
            pass
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "limit_alert")
async def cb_limit_alert(call: CallbackQuery):
    allowed, current, maximum = await check_shop_creation_allowed(call.from_user.id)
    text = "Лимит уже не действует" if allowed else f"Достигнут лимит магазинов ({current}/{maximum})"
    await safe_callback_answer(call, text, show_alert=True)


async def build_shop_view(shop: Shop) -> tuple[str, InlineKeyboardMarkup]:
    marketplace = "🔵 Ozon" if shop.marketplace == "ozon" else "🟣 WB"
    status_text = "🟢 В работе (отвечает)" if shop.is_running else "🔴 На паузе"
    toggle_text = "⏸ Поставить на паузу" if shop.is_running else "▶️ Запустить бота"
    count = await get_shop_reply_count(shop.id)
    stars_row = [
        InlineKeyboardButton(
            text=f"{'✅' if getattr(shop, f'reply_{star}_star', False) else '❌'} {star}⭐️",
            callback_data=f"toggle_star_{shop.id}_{star}",
        )
        for star in range(1, 6)
    ]
    age_label = AGE_LABELS.get(shop.max_age_days, f"{shop.max_age_days} дн.")
    tone_label = TONE_LABELS.get(shop.tone_style, "Нейтральный")
    pending_count = await get_pending_approvals_count(shop.id, "pending")
    moderation_label = "ВКЛ" if shop.moderate_negative else "ВЫКЛ"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_text, callback_data=f"toggle_run_{shop.id}")],
            stars_row,
            [InlineKeyboardButton(text="⚡️ Выбрать все 1-5⭐️ / Сбросить", callback_data=f"toggle_all_stars_{shop.id}")],
            [
                InlineKeyboardButton(text=f"Глубина: 📅 {age_label}", callback_data=f"change_age_{shop.id}"),
                InlineKeyboardButton(text=f"Стиль: 😊 {tone_label}", callback_data=f"change_tone_{shop.id}"),
            ],
            [
                InlineKeyboardButton(text="✍️ Изменить подпись", callback_data=f"edit_sig_{shop.id}"),
                InlineKeyboardButton(text="🛑 Стоп-слова", callback_data=f"edit_stops_{shop.id}"),
            ],
            [
                InlineKeyboardButton(text="🧠 Доп. инструкции для ИИ", callback_data=f"edit_instr_{shop.id}"),
                InlineKeyboardButton(text=f"🛡 Премодерация 1-3★: {moderation_label}", callback_data=f"toggle_moderation_{shop.id}"),
            ],
            [
                InlineKeyboardButton(text="📋 Логи ответов", callback_data=f"logs_{shop.id}_0"),
                InlineKeyboardButton(text="🔄 Обновить", callback_data=f"shop_view_{shop.id}"),
            ],
            [InlineKeyboardButton(text=f"📥 Модерация: {pending_count} в очереди", callback_data=f"pending_list_{shop.id}_0")],
            [
                InlineKeyboardButton(text="📦 В архив", callback_data=f"confirm_archive_{shop.id}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"confirm_delete_{shop.id}"),
            ],
            [InlineKeyboardButton(text="⬅️ К списку магазинов", callback_data="goto_shops_catalog")],
        ]
    )
    text = (
        f"⚙️ <b>Настройки магазина: {marketplace} {escape(shop.name)}</b>\n\n"
        f"Статус: {status_text}\n"
        f"Отвечено ботом: 💬 <b>{count} отзывов</b>\n"
        f"Глубина проверки: 📅 <b>{age_label}</b>\n\n"
        f"🤖 <b>Параметры ИИ:</b>\n"
        f"• Стиль: <b>{tone_label}</b>\n"
        f"• Подпись: <code>{escape(shop.signature_text or 'По умолчанию')}</code>\n"
        f"• Стоп-слова: <code>{escape(shop.stop_words or 'Нет')}</code>\n"
        f"• Доп. инструкции: <code>{escape(shorten(shop.custom_instructions or '', 60)) if shop.custom_instructions else 'Не заданы'}</code>\n"
        f"• Премодерация 1-3★: <b>{moderation_label}</b>\n"
        f"📥 В очереди на модерацию: <b>{pending_count}</b>"
    )
    if shop.moderate_negative and not any(getattr(shop, f"reply_{star}_star", False) for star in (1, 2, 3)):
        text += (
            "\n\n⚠️ <i>Премодерация включена, но оценки 1-3⭐ не выбраны — "
            "негативные отзывы бот не обрабатывает.</i>"
        )
    return text, kb


async def edit_shop_view(call: CallbackQuery, shop: Shop):
    text, kb = await build_shop_view(shop)
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("shop_view_"))
async def cb_shop_view(call: CallbackQuery, state: Optional[FSMContext] = None):
    if state:
        await state.clear()
    try:
        shop_id = int(call.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await safe_callback_answer(call, "Некорректный запрос", show_alert=True)
        return
    shop = await get_shop_by_id(shop_id, user_id=call.from_user.id)
    if not shop:
        await safe_callback_answer(call, "Магазин не найден или недоступен", show_alert=True)
        return
    await edit_shop_view(call, shop)
    await safe_callback_answer(call, "Статус обновлен")


@router.callback_query(F.data.startswith("toggle_run_"))
async def cb_toggle_run(call: CallbackQuery):
    try:
        shop_id = int(call.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await safe_callback_answer(call, "Некорректный запрос", show_alert=True)
        return
    shop = await get_shop_by_id(shop_id, user_id=call.from_user.id)
    if not shop:
        await safe_callback_answer(call, "Магазин не найден или недоступен", show_alert=True)
        return

    if not shop.is_running:
        allowed, current, maximum = await check_shop_launch_allowed(call.from_user.id, shop.id)
        if not allowed:
            await safe_callback_answer(call, f"⚠️ Превышен лимит слотов ({current}/{maximum})", show_alert=True)
            return
        shop = await update_shop(shop.id, user_id=call.from_user.id, is_running=True)
        await safe_callback_answer(call, "Магазин запущен!")
    else:
        shop = await update_shop(shop.id, user_id=call.from_user.id, is_running=False)
        await stop_shop_worker(shop_id)
        await safe_callback_answer(call, "Магазин на паузе!")
    if shop:
        await edit_shop_view(call, shop)


@router.callback_query(F.data.startswith("toggle_star_"))
async def cb_toggle_star(call: CallbackQuery):
    parts = call.data.split("_")
    if len(parts) != 4:
        await safe_callback_answer(call, "Некорректный запрос", show_alert=True)
        return
    try:
        shop_id, star = int(parts[2]), int(parts[3])
    except ValueError:
        await safe_callback_answer(call, "Некорректный запрос", show_alert=True)
        return
    if star not in range(1, 6):
        await safe_callback_answer(call, "Некорректная оценка", show_alert=True)
        return

    shop = await get_shop_by_id(shop_id, user_id=call.from_user.id)
    if not shop:
        await safe_callback_answer(call, "Магазин не найден или недоступен", show_alert=True)
        return
    shop = await update_shop(
        shop.id,
        user_id=call.from_user.id,
        **{f"reply_{star}_star": not getattr(shop, f"reply_{star}_star", False)},
    )
    if shop:
        await edit_shop_view(call, shop)
    await safe_callback_answer(call)


@router.callback_query(F.data.startswith("toggle_all_stars_"))
async def cb_toggle_all_stars(call: CallbackQuery):
    try:
        shop_id = int(call.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await safe_callback_answer(call, "Некорректный запрос", show_alert=True)
        return
    shop = await get_shop_by_id(shop_id, user_id=call.from_user.id)
    if not shop:
        await safe_callback_answer(call, "Магазин не найден или недоступен", show_alert=True)
        return
    new_value = not all(getattr(shop, f"reply_{star}_star", False) for star in range(1, 6))
    shop = await update_shop(
        shop.id,
        user_id=call.from_user.id,
        **{f"reply_{star}_star": new_value for star in range(1, 6)},
    )
    if shop:
        await edit_shop_view(call, shop)
    await safe_callback_answer(call)


@router.callback_query(F.data.startswith("change_age_"))
async def cb_change_age_menu(call: CallbackQuery):
    try:
        shop_id = int(call.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await safe_callback_answer(call, "Некорректный запрос", show_alert=True)
        return
    if not await get_shop_by_id(shop_id, user_id=call.from_user.id):
        await safe_callback_answer(call, "Магазин не найден или недоступен", show_alert=True)
        return

    buttons = []
    row = []
    for days, label in AGE_LABELS.items():
        row.append(InlineKeyboardButton(text=label, callback_data=f"set_age_{shop_id}_{days}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(text="⬅️ Назад в настройки", callback_data=f"shop_view_{shop_id}")])
    await call.message.edit_text(
        "📅 <b>Выберите глубину проверки отзывов:</b>\n"
        "Бот будет сканировать и отвечать только на отзывы за выбранный период:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await safe_callback_answer(call)


@router.callback_query(F.data.startswith("set_age_"))
async def cb_set_age(call: CallbackQuery):
    parts = call.data.split("_")
    try:
        shop_id, days = int(parts[2]), int(parts[3])
    except (ValueError, IndexError):
        await safe_callback_answer(call, "Некорректный запрос", show_alert=True)
        return
    if days not in AGE_LABELS:
        await safe_callback_answer(call, "Некорректный период", show_alert=True)
        return
    shop = await update_shop(shop_id, user_id=call.from_user.id, max_age_days=days)
    if not shop:
        await safe_callback_answer(call, "Магазин не найден или недоступен", show_alert=True)
        return
    await safe_callback_answer(call, "Глубина проверки обновлена!")
    await edit_shop_view(call, shop)


@router.callback_query(F.data.startswith("change_tone_"))
async def cb_change_tone_menu(call: CallbackQuery):
    try:
        shop_id = int(call.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await safe_callback_answer(call, "Некорректный запрос", show_alert=True)
        return
    if not await get_shop_by_id(shop_id, user_id=call.from_user.id):
        await safe_callback_answer(call, "Магазин не найден или недоступен", show_alert=True)
        return

    buttons = [
        [InlineKeyboardButton(text=f"😊 {label}", callback_data=f"set_tone_{shop_id}_{key}")]
        for key, label in TONE_LABELS.items()
    ]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад в настройки", callback_data=f"shop_view_{shop_id}")])
    await call.message.edit_text(
        "🎭 <b>Выберите тональность и стиль ответов ИИ:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await safe_callback_answer(call)


@router.callback_query(F.data.startswith("set_tone_"))
async def cb_set_tone(call: CallbackQuery):
    parts = call.data.split("_", 3)
    if len(parts) != 4:
        await safe_callback_answer(call, "Некорректный запрос", show_alert=True)
        return
    try:
        shop_id = int(parts[2])
    except ValueError:
        await safe_callback_answer(call, "Некорректный запрос", show_alert=True)
        return
    tone_key = parts[3]
    if tone_key not in TONE_LABELS:
        await safe_callback_answer(call, "Неизвестный стиль", show_alert=True)
        return
    shop = await update_shop(shop_id, user_id=call.from_user.id, tone_style=tone_key)
    if not shop:
        await safe_callback_answer(call, "Магазин не найден или недоступен", show_alert=True)
        return
    await safe_callback_answer(call, f"Стиль изменен на: {TONE_LABELS[tone_key]}")
    await edit_shop_view(call, shop)


@router.callback_query(F.data.startswith("edit_sig_"))
async def cb_edit_signature(call: CallbackQuery, state: FSMContext):
    try:
        shop_id = int(call.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await safe_callback_answer(call, "Некорректный запрос", show_alert=True)
        return
    if not await get_shop_by_id(shop_id, user_id=call.from_user.id):
        await safe_callback_answer(call, "Магазин не найден или недоступен", show_alert=True)
        return
    await state.update_data(target_shop_id=shop_id)
    await state.set_state(EditSettingsStates.edit_signature)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"shop_view_{shop_id}")]]
    )
    await call.message.edit_text(
        "✍️ <b>Введите подпись для ответов:</b>\n\n"
        "Например: <code>С уважением, команда Бренда</code>\n"
        "Или отправьте <code>-</code>, чтобы убрать подпись.",
        reply_markup=kb,
    )
    await safe_callback_answer(call)


async def send_shop_view(message: Message, shop_id: int, user_id: int):
    shop = await get_shop_by_id(shop_id, user_id=user_id)
    if shop:
        text, kb = await build_shop_view(shop)
        await message.answer(text, reply_markup=kb)
    else:
        await message.answer("Магазин не найден или недоступен.")


@router.message(EditSettingsStates.edit_signature)
async def process_new_signature(message: Message, state: FSMContext):
    if message.text is None:
        await message.answer("Отправьте текстовую подпись.")
        return
    data = await state.get_data()
    shop_id = data.get("target_shop_id")
    if not shop_id or not await get_shop_by_id(shop_id, user_id=message.from_user.id):
        await state.clear()
        await message.answer("Магазин не найден или недоступен.")
        return
    new_signature = "" if message.text.strip() == "-" else message.text.strip()
    if len(new_signature) > 300:
        await message.answer("Подпись слишком длинная. Максимум 300 символов.")
        return
    await update_shop(shop_id, user_id=message.from_user.id, signature_text=new_signature)
    await state.clear()
    await message.answer("✅ Подпись успешно обновлена!")
    await send_shop_view(message, shop_id, message.from_user.id)


@router.callback_query(F.data.startswith("edit_stops_"))
async def cb_edit_stops(call: CallbackQuery, state: FSMContext):
    try:
        shop_id = int(call.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await safe_callback_answer(call, "Некорректный запрос", show_alert=True)
        return
    if not await get_shop_by_id(shop_id, user_id=call.from_user.id):
        await safe_callback_answer(call, "Магазин не найден или недоступен", show_alert=True)
        return
    await state.update_data(target_shop_id=shop_id)
    await state.set_state(EditSettingsStates.edit_stop_words)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=f"shop_view_{shop_id}")]]
    )
    await call.message.edit_text(
        "🛑 <b>Введите стоп-слова через запятую:</b>\n\n"
        "Если отзыв содержит любое из этих слов, бот не станет отвечать на него автоматически.\n"
        "Например: <code>брак, возврат, разбито, подделка</code>\n\n"
        "Отправьте <code>-</code>, чтобы очистить список стоп-слов.",
        reply_markup=kb,
    )
    await safe_callback_answer(call)


@router.message(EditSettingsStates.edit_stop_words)
async def process_new_stop_words(message: Message, state: FSMContext):
    if message.text is None:
        await message.answer("Отправьте список стоп-слов текстом.")
        return
    data = await state.get_data()
    shop_id = data.get("target_shop_id")
    if not shop_id or not await get_shop_by_id(shop_id, user_id=message.from_user.id):
        await state.clear()
        await message.answer("Магазин не найден или недоступен.")
        return
    stop_words = "" if message.text.strip() == "-" else message.text.strip()
    if len(stop_words) > 1000:
        await message.answer("Список слишком длинный. Максимум 1000 символов.")
        return
    await update_shop(shop_id, user_id=message.from_user.id, stop_words=stop_words)
    await state.clear()
    await message.answer("✅ Список стоп-слов обновлен!")
    await send_shop_view(message, shop_id, message.from_user.id)


@router.message(F.text == "💬 Ответы")
async def nav_answers_logs(message: Message, state: FSMContext):
    await state.clear()
    shops = await get_user_shops(message.from_user.id, include_archived=False)
    if not shops:
        await message.answer("У вас нет подключенных магазинов для просмотра логов.")
        return
    buttons = [
        [InlineKeyboardButton(text=f"{'🔵' if shop.marketplace == 'ozon' else '🟣'} {shorten(shop.name, 40)}", callback_data=f"logs_{shop.id}_0")]
        for shop in shops
    ]
    await message.answer(
        "📋 Выберите магазин для просмотра истории ответов:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("logs_"))
async def cb_show_logs(call: CallbackQuery):
    parts = call.data.split("_")
    try:
        shop_id = int(parts[1])
        offset = int(parts[2]) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        await safe_callback_answer(call, "Некорректный запрос", show_alert=True)
        return
    if offset < 0:
        offset = 0

    shop = await get_shop_by_id(shop_id, user_id=call.from_user.id)
    if not shop:
        await safe_callback_answer(call, "Магазин не найден или недоступен", show_alert=True)
        return
    limit = 3
    logs = await get_shop_logs(shop_id, limit=limit, offset=offset)
    if not logs and offset == 0:
        await safe_callback_answer(call, "В этом магазине еще нет опубликованных ответов", show_alert=True)
        return

    text = f"📋 <b>История ответов: {escape(shop.name)}</b>\n\n"
    for log in logs:
        text += (
            f"⭐️ <b>Оценка:</b> {log.rating}⭐️ | <b>Товар:</b> {escape(shorten(log.product_name, 80))}\n"
            f"💬 <b>Отзыв:</b> <i>{escape(shorten(log.review_text, 200))}</i>\n\n"
            f"🤖 <b>Ответ:</b>\n{escape(shorten(log.reply_text, 500))}\n"
            "──────────────────\n"
        )
    if not logs:
        text += "На этой странице записей больше нет.\n"
    if len(text) > 4000:
        cut = text.rfind("──────────────────\n", 0, 4000)
        if cut == -1:
            text = text[:4000]
        else:
            text = text[:cut + len("──────────────────\n")]
        text += "…"

    nav_buttons = []
    if offset > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"logs_{shop_id}_{max(0, offset - limit)}"))
    if len(logs) == limit:
        nav_buttons.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"logs_{shop_id}_{offset + limit}"))
    rows = [nav_buttons] if nav_buttons else []
    rows.append([InlineKeyboardButton(text="⬅️ Назад в настройки", callback_data=f"shop_view_{shop_id}")])
    try:
        await call.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except TelegramBadRequest:
        pass
    await safe_callback_answer(call)


@router.callback_query(F.data.startswith("confirm_archive_"))
async def cb_confirm_archive(call: CallbackQuery):
    try:
        shop_id = int(call.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await safe_callback_answer(call, "Некорректный запрос", show_alert=True)
        return
    if not await get_shop_by_id(shop_id, user_id=call.from_user.id):
        await safe_callback_answer(call, "Магазин не найден или недоступен", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚠️ Да, архивировать", callback_data=f"do_archive_{shop_id}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"shop_view_{shop_id}")],
        ]
    )
    await call.message.edit_text(
        "Вы уверены, что хотите перенести магазин в архив? Он перестанет отвечать на отзывы.",
        reply_markup=kb,
    )
    await safe_callback_answer(call)


@router.callback_query(F.data.startswith("do_archive_"))
async def cb_do_archive(call: CallbackQuery):
    try:
        shop_id = int(call.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await safe_callback_answer(call, "Некорректный запрос", show_alert=True)
        return
    success = await archive_shop(call.from_user.id, shop_id)
    await safe_callback_answer(call, "Магазин перенесен в архив" if success else "Магазин не найден или недоступен", show_alert=not success)
    if success:
        await stop_shop_worker(shop_id)
        await show_shops_catalog(call.from_user.id, call.message, is_edit=True)


@router.callback_query(F.data.startswith("confirm_delete_"))
async def cb_confirm_delete(call: CallbackQuery):
    try:
        shop_id = int(call.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await safe_callback_answer(call, "Некорректный запрос", show_alert=True)
        return
    if not await get_shop_by_id(shop_id, user_id=call.from_user.id):
        await safe_callback_answer(call, "Магазин не найден или недоступен", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Да, полностью удалить", callback_data=f"do_delete_{shop_id}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"shop_view_{shop_id}")],
        ]
    )
    await call.message.edit_text(
        "⚠️ <b>Внимание:</b> магазин и вся история ответов будут удалены безвозвратно. Продолжить?",
        reply_markup=kb,
    )
    await safe_callback_answer(call)


@router.callback_query(F.data.startswith("do_delete_"))
async def cb_do_delete(call: CallbackQuery):
    try:
        shop_id = int(call.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await safe_callback_answer(call, "Некорректный запрос", show_alert=True)
        return
    success = await delete_shop(call.from_user.id, shop_id)
    await safe_callback_answer(call, "Магазин успешно удален" if success else "Магазин не найден или недоступен", show_alert=not success)
    if success:
        await stop_shop_worker(shop_id)
        await show_shops_catalog(call.from_user.id, call.message, is_edit=True)


@router.callback_query(F.data == "pause_all_shops")
async def cb_pause_all(call: CallbackQuery):
    shops = await get_user_shops(call.from_user.id, include_archived=False)
    changed = 0
    for shop in shops:
        if shop.is_running:
            await update_shop(shop.id, user_id=call.from_user.id, is_running=False)
            await stop_shop_worker(shop.id)
            changed += 1
    await safe_callback_answer(call, f"Магазинов поставлено на паузу: {changed}")
    text, kb = await get_main_dashboard_view(call.from_user.id)
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "resume_all_shops")
async def cb_resume_all(call: CallbackQuery):
    shops = await get_user_shops(call.from_user.id, include_archived=False)
    resumed, skipped = 0, 0
    for shop in shops:
        if not shop.is_running:
            allowed, _, _ = await check_shop_launch_allowed(call.from_user.id, shop.id)
            if not allowed:
                skipped += 1
                continue
            await update_shop(shop.id, user_id=call.from_user.id, is_running=True)
            resumed += 1
    message = f"Запущено магазинов: {resumed}"
    if skipped:
        message += f"; пропущено из-за лимита тарифа: {skipped}"
    await safe_callback_answer(call, message, show_alert=bool(skipped))
    text, kb = await get_main_dashboard_view(call.from_user.id)
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


@router.message(F.text == "❓ Помощь и FAQ")
@router.callback_query(F.data == "faq_menu")
async def show_faq(event: Union[Message, CallbackQuery], state: Optional[FSMContext] = None):
    if state:
        await state.clear()
    faq_text = (
        "❓ <b>Помощь и FAQ</b>\n\n"
        "<b>Что умеет бот?</b>\n"
        "Автоматически отвечает на отзывы покупателей на Ozon и Wildberries с помощью AI-агента: "
        "учитывает оценку, текст отзыва и ваши инструкции, подбирает тон и подпись.\n\n"
        "<b>Возможности:</b>\n"
        "• Ответы по фильтру оценок (1–5⭐);\n"
        "• Глубина проверки (от 1 дня до «за всё время»);\n"
        "• Стоп-слова, подпись, стиль общения;\n"
        "• «Доп. инструкции для ИИ» — особенности вашего товара;\n"
        "• Премодерация отзывов 1–3⭐ (одобрение перед публикацией);\n"
        "• Журнал всех отправленных ответов.\n\n"
        "<b>Безопасно ли это для аккаунта?</b>\n"
        "Все запросы идут через официальный API Ozon Seller и Wildberries OpenAPI с безопасными интервалами.\n\n"
        "<b>Что такое глубина проверки?</b>\n"
        "Период в прошлое (от 1 дня до «за всё время»), в пределах которого бот ищет неотвеченные отзывы.\n\n"
        "<b>Тарифы (30 дней):</b>\n"
        "• Старт — 890 ₽: 1 слот, 200 ответов;\n"
        "• Базовый — 1690 ₽: 2 слота, 400 ответов;\n"
        "• Бизнес — 2490 ₽: 4 слота, 800 ответов, инструкции для ИИ и аналитика;\n"
        "• PRO — 4990 ₽: 8 слотов, 2000 ответов, все функции.\n"
        "Плюс разовые пакеты ответов S/M/L (не сгорают).\n\n"
        "Подробнее и оплата — в разделе «💳 Тарифы»."
    )
    support_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Написать в поддержку", url="https://t.me/vellio_help")],
    ])
    if isinstance(event, CallbackQuery):
        await event.message.answer(faq_text, reply_markup=support_kb)
        await safe_callback_answer(event)
    else:
        await event.answer(faq_text, reply_markup=support_kb)


@router.callback_query(F.data == "add_shop_start")
async def cb_add_shop_start(call: CallbackQuery, state: FSMContext):
    await state.clear()
    allowed, current, maximum = await check_shop_creation_allowed(call.from_user.id)
    if not allowed:
        await safe_callback_answer(call, f"Достигнут лимит магазинов ({current}/{maximum})", show_alert=True)
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔵 Ozon", callback_data="add_mp_ozon")],
            [InlineKeyboardButton(text="🟣 Wildberries", callback_data="add_mp_wb")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="goto_shops_catalog")],
        ]
    )
    await call.message.edit_text("🏪 <b>Выберите маркетплейс для подключения:</b>", reply_markup=kb)
    await safe_callback_answer(call)


@router.callback_query(F.data.startswith("add_mp_"))
async def cb_choose_marketplace(call: CallbackQuery, state: FSMContext):
    marketplace = {"add_mp_ozon": "ozon", "add_mp_wb": "wildberries"}.get(call.data)
    if not marketplace:
        await safe_callback_answer(call, "Неизвестный маркетплейс", show_alert=True)
        return
    await state.update_data(chosen_marketplace=marketplace)
    await state.set_state(AddShopStates.enter_name)
    marketplace_name = "Ozon" if marketplace == "ozon" else "Wildberries"
    await call.message.edit_text(
        f"🏷 <b>Введите внутреннее название для вашего магазина {marketplace_name}:</b>\n\n"
        f"Например: <code>Косметика Ozon</code> или <code>{marketplace_name} Основной</code>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="goto_shops_catalog")]]
        ),
    )
    await safe_callback_answer(call)


@router.message(AddShopStates.enter_name)
async def process_shop_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Введите название магазина текстом.")
        return
    if len(name) > 100:
        await message.answer("Название слишком длинное. Максимум 100 символов.")
        return
    await state.update_data(shop_name=name)
    data = await state.get_data()
    if data.get("chosen_marketplace") == "ozon":
        await state.set_state(AddShopStates.ozon_client_id)
        await message.answer(
            "🔑 <b>Введите Client ID от API Ozon:</b>\n\n"
            "Его можно скопировать в ЛК Ozon Seller → Настройки → API ключи."
        )
    else:
        await state.set_state(AddShopStates.wb_api_token)
        await message.answer(
            "🔑 <b>Введите API Токен Wildberries (с правами «Отзывы и вопросы»):</b>\n\n"
            "Его можно сгенерировать в ЛК WB Партнёры → Настройки → Доступ к API."
        )


@router.message(AddShopStates.ozon_client_id)
async def process_ozon_client_id(message: Message, state: FSMContext):
    client_id = (message.text or "").strip()
    if not client_id or len(client_id) > 200:
        await message.answer("Введите корректный Client ID (до 200 символов).")
        return
    await state.update_data(ozon_client_id=client_id)
    await state.set_state(AddShopStates.ozon_api_key)
    await message.answer("🔐 Теперь введите <b>API Key Ozon</b>:")


@router.message(AddShopStates.ozon_api_key)
async def process_ozon_api_key(message: Message, state: FSMContext):
    key = (message.text or "").strip()
    if not key or len(key) > 1000:
        await message.answer("Введите корректный API Key Ozon.")
        return
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    client_id = data.get("ozon_client_id")
    name = data.get("shop_name")
    if not client_id or not name or data.get("chosen_marketplace") != "ozon":
        await state.clear()
        await message.answer("Данные подключения устарели. Начните добавление магазина заново.")
        return

    status_message = await message.answer("🔄 Проверяем API-ключи Ozon...")
    ok, error = await OzonClient(client_id=client_id, api_key=key).verify_credentials()
    if not ok:
        await status_message.edit_text(
            f"❌ Ошибка валидации ключей Ozon: {escape(error)}\nПопробуйте снова или нажмите /start."
        )
        return

    allowed, current, maximum = await check_shop_creation_allowed(message.from_user.id)
    if not allowed:
        await state.clear()
        await status_message.edit_text(f"❌ Достигнут лимит магазинов ({current}/{maximum}).")
        return

    try:
        shop = await create_shop(
            user_id=message.from_user.id,
            marketplace="ozon",
            name=name,
            ozon_client_id=client_id,
            ozon_api_key=key,
        )
    except ShopLimitError as exc:
        await state.clear()
        await status_message.edit_text(f"❌ {escape(str(exc))}.")
        return
    await state.clear()
    await status_message.edit_text(f"🎉 Магазин <b>{escape(shop.name)}</b> успешно подключен и готов к работе!")
    await show_shops_catalog(message.from_user.id, message)


@router.message(AddShopStates.wb_api_token)
async def process_wb_token(message: Message, state: FSMContext):
    token = (message.text or "").strip()
    if not token or len(token) > 5000:
        await message.answer("Введите корректный API-токен Wildberries.")
        return
    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    name = data.get("shop_name")
    if not name or data.get("chosen_marketplace") != "wildberries":
        await state.clear()
        await message.answer("Данные подключения устарели. Начните добавление магазина заново.")
        return

    status_message = await message.answer("🔄 Проверяем токен Wildberries...")
    ok, error = await WildberriesClient(api_token=token).verify_credentials()
    if not ok:
        await status_message.edit_text(
            f"❌ Ошибка валидации токена WB: {escape(error)}\nПопробуйте снова или нажмите /start."
        )
        return

    allowed, current, maximum = await check_shop_creation_allowed(message.from_user.id)
    if not allowed:
        await state.clear()
        await status_message.edit_text(f"❌ Достигнут лимит магазинов ({current}/{maximum}).")
        return

    try:
        shop = await create_shop(
            user_id=message.from_user.id,
            marketplace="wildberries",
            name=name,
            wb_token=token,
        )
    except ShopLimitError as exc:
        await state.clear()
        await status_message.edit_text(f"❌ {escape(str(exc))}.")
        return
    await state.clear()
    await status_message.edit_text(f"🎉 Магазин <b>{escape(shop.name)}</b> успешно привязан!")
    await show_shops_catalog(message.from_user.id, message)


@router.callback_query(F.data.startswith("pa_"))
async def cb_pending_action(call: CallbackQuery, state: FSMContext):
    parts = call.data.split("_")
    if len(parts) != 3 or not parts[1].isdigit():
        await safe_callback_answer(call, "Некорректный запрос", show_alert=True)
        return
    approval_id = int(parts[1])
    action = parts[2]
    approval = await get_pending_approval(approval_id)
    if not approval or approval.user_id != call.from_user.id:
        await safe_callback_answer(call, "Отзыв не найден или недоступен", show_alert=True)
        return

    if action == "pub":
        ok, message = await publish_pending_reply(approval, approval.proposed_reply or "")
        await safe_callback_answer(call, message, show_alert=not ok)
        if ok:
            try:
                await call.message.edit_text(
                    build_pending_text(approval) + "\n\n✅ <b>Опубликовано</b>",
                    reply_markup=None,
                )
            except Exception:
                pass
    elif action == "skip":
        await mark_review_processed(approval.shop_id, approval.marketplace, approval.review_id)
        await update_pending_approval(approval.id, status="skipped")
        await safe_callback_answer(call, "Отзыв пропущен")
        try:
            await call.message.edit_text(
                build_pending_text(approval) + "\n\n⏭ <b>Пропущен</b>",
                reply_markup=None,
            )
        except Exception:
            pass
    elif action == "edit":
        await state.update_data(pending_approval_id=approval.id)
        await state.set_state(EditPendingStates.waiting_text)
        await call.message.answer(
            "✏️ <b>Введите новый текст ответа на отзыв:</b>\n\n"
            "Просто отправьте текст — бот его опубликует.\n"
            "Для отмены нажмите любую кнопку меню или /start.",
        )
        await safe_callback_answer(call)
    else:
        await safe_callback_answer(call, "Неизвестное действие", show_alert=True)


@router.message(EditPendingStates.waiting_text)
async def process_pending_edit(message: Message, state: FSMContext):
    data = await state.get_data()
    approval_id = data.get("pending_approval_id")
    if not approval_id:
        await state.clear()
        return
    approval = await get_pending_approval(approval_id)
    if not approval or approval.user_id != message.from_user.id:
        await state.clear()
        await message.answer("Отзыв не найден.")
        return
    new_text = (message.text or "").strip()
    if not new_text:
        await message.answer("Отправьте текст ответа.")
        return
    if len(new_text) > 1000:
        await message.answer("Текст слишком длинный (максимум 1000 символов).")
        return
    await state.clear()
    ok, result = await publish_pending_reply(approval, new_text)
    await message.answer("✅ Ответ опубликован!" if ok else f"❌ {escape(result)}")


@router.callback_query(F.data.startswith("toggle_moderation_"))
async def cb_toggle_moderation(call: CallbackQuery):
    try:
        shop_id = int(call.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await safe_callback_answer(call, "Некорректный запрос", show_alert=True)
        return
    shop = await get_shop_by_id(shop_id, user_id=call.from_user.id)
    if not shop:
        await safe_callback_answer(call, "Магазин не найден или недоступен", show_alert=True)
        return
    new_value = not bool(shop.moderate_negative)
    shop = await update_shop(shop.id, user_id=call.from_user.id, moderate_negative=new_value)
    if shop:
        await edit_shop_view(call, shop)
    await safe_callback_answer(call, "Премодерация включена" if new_value else "Премодерация выключена")


@router.callback_query(F.data.startswith("edit_instr_"))
async def cb_edit_instructions(call: CallbackQuery, state: FSMContext):
    try:
        shop_id = int(call.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await safe_callback_answer(call, "Некорректный запрос", show_alert=True)
        return
    shop = await get_shop_by_id(shop_id, user_id=call.from_user.id)
    if not shop:
        await safe_callback_answer(call, "Магазин не найден или недоступен", show_alert=True)
        return
    await state.update_data(target_shop_id=shop_id)
    await state.set_state(EditSettingsStates.edit_instructions)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"shop_view_{shop_id}")]
    ])
    preview = escape(shorten(shop.custom_instructions or "Не заданы", 300))
    await call.message.edit_text(
        f"🧠 <b>Доп. инструкции для ИИ:</b>\n\n"
        f"Текущий текст (сокращённо):\n<code>{preview}</code>\n\n"
        "Введите новый текст инструкций (особенности товара, правила ответов).\n"
        "Отправьте <code>-</code>, чтобы очистить.",
        reply_markup=kb,
    )
    await safe_callback_answer(call)


@router.message(EditSettingsStates.edit_instructions)
async def process_new_instructions(message: Message, state: FSMContext):
    if message.text is None:
        await message.answer("Отправьте текст инструкций.")
        return
    data = await state.get_data()
    shop_id = data.get("target_shop_id")
    if not shop_id or not await get_shop_by_id(shop_id, user_id=message.from_user.id):
        await state.clear()
        await message.answer("Магазин не найден или недоступен.")
        return
    instructions = "" if message.text.strip() == "-" else message.text.strip()
    if len(instructions) > 5000:
        await message.answer("Текст слишком длинный (максимум 5000 символов).")
        return
    await update_shop(shop_id, user_id=message.from_user.id, custom_instructions=instructions)
    await state.clear()
    await message.answer("✅ Инструкции для ИИ обновлены!")
    await send_shop_view(message, shop_id, message.from_user.id)


@router.callback_query(F.data.startswith("pending_list_"))
async def cb_pending_list(call: CallbackQuery):
    parts = call.data.split("_")
    try:
        shop_id = int(parts[2])
        offset = int(parts[3]) if len(parts) > 3 else 0
    except (ValueError, IndexError):
        await safe_callback_answer(call, "Некорректный запрос", show_alert=True)
        return
    shop = await get_shop_by_id(shop_id, user_id=call.from_user.id)
    if not shop:
        await safe_callback_answer(call, "Магазин не найден или недоступен", show_alert=True)
        return
    pendings = await get_pending_approvals(shop_id, "pending")
    if not pendings:
        await safe_callback_answer(call, "Нет отзывов, ожидающих модерации", show_alert=True)
        return
    offset = max(0, min(offset, len(pendings) - 1))
    approval = pendings[offset]
    text = build_pending_text(approval) + f"\n\n📥 <b>{offset + 1} из {len(pendings)}</b>"
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"pending_list_{shop_id}_{offset - 1}"))
    if offset < len(pendings) - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"pending_list_{shop_id}_{offset + 1}"))
    nav.append(InlineKeyboardButton(text="🔙 В магазин", callback_data=f"shop_view_{shop_id}"))
    rows = build_pending_keyboard(approval.id).inline_keyboard + [nav]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass
    await safe_callback_answer(call)


def build_tariffs_text(plan: str) -> str:
    plan_name = "👑 Безлимит" if plan == "unlimited" else TARIFFS.get(plan, {}).get("name", plan.capitalize())
    text = (
        "💳 <b>Тарифы Vellio</b>\n\n"
        f"Ваш текущий тариф: <b>{escape(plan_name)}</b>\n\n"
        "<b>Ежемесячные подписки:</b>\n"
    )
    for code, t in TARIFFS.items():
        text += (
            f"• <b>{t['name']}</b> — {t['price']} ₽/мес: {t['slots']} слот(а), {t['replies']} ответов\n"
            f"  <i>{escape(t['desc'])}</i>\n"
        )
    text += "\n<b>Разовые пакеты ответов (не сгорают):</b>\n"
    for code, t in ADD_ON_PACKAGES.items():
        text += f"• <b>{t['name']}</b> — {t['price']} ₽: +{t['replies']} ответов\n"
    return text


def build_tariffs_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for code, t in TARIFFS.items():
        rows.append([InlineKeyboardButton(text=f"Оплатить «{t['name']}» — {t['price']} ₽", callback_data=f"pay_plan_{code}")])
    for code, t in ADD_ON_PACKAGES.items():
        rows.append([InlineKeyboardButton(text=f"Купить «{t['name']}» — {t['price']} ₽", callback_data=f"pay_addon_{code}")])
    rows.append([InlineKeyboardButton(text="🎟 Активировать промокод", callback_data="promo_activate")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_tariffs(event: Union[Message, CallbackQuery]):
    user = await get_or_create_user(event.from_user.id)
    text = build_tariffs_text(user.plan)
    kb = build_tariffs_keyboard()
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            await event.message.answer(text, reply_markup=kb)
        await safe_callback_answer(event)
    else:
        await event.answer(text, reply_markup=kb)


@router.message(F.text == "💳 Тарифы")
async def nav_tariffs(message: Message, state: FSMContext):
    await state.clear()
    await show_tariffs(message)


@router.callback_query(F.data == "tariffs_menu")
async def cb_tariffs_menu(call: CallbackQuery):
    await show_tariffs(call)


async def start_payment(call: CallbackQuery, item_type: str, item_code: str, item_name: str, amount_rub: int):
    payment = await create_payment(amount_rub, f"Vellio: {item_name}", {"item_type": item_type, "item_code": item_code})
    if not payment or not payment.get("payment_url"):
        await safe_callback_answer(call, "Не удалось создать платёж. Попробуйте позже.", show_alert=True)
        return
    await create_pending_payment(payment["payment_id"], call.from_user.id, item_type, item_code, amount_rub)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Перейти к оплате", url=payment["payment_url"])],
    ])
    await call.message.answer(
        f"💳 <b>Оплата «{escape(item_name)}»</b>\n\n"
        f"Сумма: <b>{amount_rub} ₽</b>\n\n"
        "Нажмите кнопку и оплатите. После успешной оплаты подписка активируется автоматически (в течение минуты).",
        reply_markup=kb,
    )
    await safe_callback_answer(call)


@router.callback_query(F.data.startswith("pay_plan_"))
async def cb_pay_plan(call: CallbackQuery):
    code = call.data.replace("pay_plan_", "")
    t = TARIFFS.get(code)
    if not t:
        await safe_callback_answer(call, "Тариф не найден", show_alert=True)
        return
    await start_payment(call, "plan", code, t["name"], t["price"])


@router.callback_query(F.data.startswith("pay_addon_"))
async def cb_pay_addon(call: CallbackQuery):
    code = call.data.replace("pay_addon_", "")
    t = ADD_ON_PACKAGES.get(code)
    if not t:
        await safe_callback_answer(call, "Пакет не найден", show_alert=True)
        return
    await start_payment(call, "addon", code, t["name"], t["price"])


@router.callback_query(F.data == "promo_activate")
async def cb_promo_activate(call: CallbackQuery, state: FSMContext):
    await state.set_state(PaymentStates.promo_code)
    await call.message.answer("🎟 Введите промокод:")
    await safe_callback_answer(call)


@router.message(PaymentStates.promo_code, ~F.text.in_(MENU_TEXTS))
async def process_promo_code(message: Message, state: FSMContext):
    code = (message.text or "").strip()
    if not code:
        await message.answer("Введите промокод.")
        return
    promo = await get_promo_code(code)
    if not promo or not promo.is_active or (promo.max_uses > 0 and promo.used_count >= promo.max_uses):
        await state.clear()
        await message.answer("❌ Промокод не найден или недействителен.")
        return
    if promo.promo_type == "plan" and promo.plan_code not in PLAN_QUOTAS:
        await state.clear()
        await message.answer("❌ Промокод недействителен: тариф не найден.")
        return
    redeemed = await use_promo_code(code)
    if not redeemed:
        await state.clear()
        await message.answer("❌ Не удалось применить промокод.")
        return
    await state.clear()
    if redeemed.promo_type == "replies":
        await add_extra_replies(message.from_user.id, redeemed.value)
        await message.answer(f"🎉 Промокод активирован! Добавлено <b>{redeemed.value}</b> ответов.")
    else:
        plan_name = TARIFFS.get(redeemed.plan_code, {}).get("name", redeemed.plan_code or "тариф")
        days = redeemed.value or 30
        await grant_subscription(message.from_user.id, redeemed.plan_code, days)
        await message.answer(f"🎉 Промокод активирован! Тариф <b>{escape(plan_name)}</b> на <b>{days}</b> дней.")


async def background_payment_worker(bot: Bot):
    while True:
        try:
            for payment in await get_pending_payments("pending"):
                try:
                    status_data = await get_payment_status(payment.payment_id)
                except Exception:
                    continue
                if not status_data:
                    continue
                status = status_data.get("status")
                if status == "succeeded":
                    fresh = await get_pending_payment_by_payment_id(payment.payment_id)
                    if not fresh or fresh.status != "pending":
                        continue
                    await get_or_create_user(payment.user_id)
                    if payment.item_type == "plan":
                        days = TARIFFS.get(payment.item_code, {}).get("days", 30)
                        await grant_subscription(payment.user_id, payment.item_code, days)
                    elif payment.item_type == "addon":
                        replies = ADD_ON_PACKAGES.get(payment.item_code, {}).get("replies", 0)
                        await add_extra_replies(payment.user_id, replies)
                    await update_pending_payment(payment.id, status="succeeded")
                    try:
                        await bot.send_message(
                            payment.user_id,
                            "🎉 <b>Оплата получена! Ваш тариф/пакет активирован.</b>",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                elif status == "canceled":
                    await update_pending_payment(payment.id, status="canceled")
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Payment Worker Error")
        await asyncio.sleep(30)


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан. Добавьте его в локальный файл .env.")

    logging.info(f"Vellio Bot {APP_VERSION} | База данных: {DATABASE_URL}")
    await init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(admin_router)
    dp.include_router(router)

    background_tasks = [
        asyncio.create_task(background_reviews_worker(bot), name="reviews-monitor"),
        asyncio.create_task(background_subscription_worker(bot), name="subscriptions-monitor"),
        asyncio.create_task(background_payment_worker(bot), name="payments-monitor"),
    ]
    logging.info("Start polling")
    try:
        await dp.start_polling(bot)
    finally:
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен пользователем")
