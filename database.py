import base64
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple, Optional

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    Text,
    ForeignKey,
    BigInteger,
    UniqueConstraint,
    event,
    func,
    select,
    update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncAttrs
from sqlalchemy.orm import declarative_base, relationship

from config import DATABASE_URL, ADMIN_IDS, TOKEN_ENCRYPTION_KEY

engine = create_async_engine(DATABASE_URL, echo=False, future=True)
async_session = async_sessionmaker(engine, expire_on_commit=False)
Base = declarative_base()


def configure_sqlite_connection(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


event.listen(engine.sync_engine, "connect", configure_sqlite_connection)

PLAN_SHOP_LIMITS = {
    "trial": 1,
    "start": 1,
    "basic": 2,
    "business": 4,
    "pro": 8,
    "unlimited": 999,
}

PLAN_QUOTAS = {
    "trial": 50,
    "start": 200,
    "basic": 400,
    "business": 800,
    "pro": 2000,
    "unlimited": 99999,
}

# Каталог ежемесячных тарифов (подписка)
TARIFFS = {
    "start": {"name": "Старт", "price": 890, "days": 30, "replies": 200, "slots": 1,
              "desc": "Базовые настройки: стиль, глубина дат, стоп-слова, подпись."},
    "basic": {"name": "Базовый", "price": 1690, "days": 30, "replies": 400, "slots": 2,
              "desc": "Оптимально для работы с WB + Ozon."},
    "business": {"name": "Бизнес", "price": 2490, "days": 30, "replies": 800, "slots": 4,
              "desc": "Индивидуальные инструкции для ИИ, аналитика за 7 дней, еженедельный дайджест."},
    "pro": {"name": "PRO", "price": 4990, "days": 30, "replies": 2000, "slots": 8,
              "desc": "Все функции «Бизнес» и максимальные лимиты."},
}

# Разовые пакеты ответов (add-on, не сгорают)
ADD_ON_PACKAGES = {
    "s": {"name": "Пакет S", "price": 490, "replies": 150},
    "m": {"name": "Пакет M", "price": 890, "replies": 350},
    "l": {"name": "Пакет L", "price": 1690, "replies": 800},
}


class ShopLimitError(ValueError):
    pass


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _to_naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _get_fernet() -> Optional[Fernet]:
    if not TOKEN_ENCRYPTION_KEY:
        return None
    try:
        return Fernet(TOKEN_ENCRYPTION_KEY.encode())
    except Exception as e:
        logging.error(f"Некорректный TOKEN_ENCRYPTION_KEY: {e}")
        return None


def looks_like_fernet_token(value: Optional[str]) -> bool:
    if not value or not value.startswith("gAAAAA"):
        return False
    try:
        decoded = base64.urlsafe_b64decode(value.encode())
        return len(decoded) >= 73 and decoded[0] == 0x80
    except (ValueError, TypeError):
        return False


def encrypt_secret(value: Optional[str]) -> Optional[str]:
    if not value:
        return value
    fernet = _get_fernet()
    if not fernet:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY не задан или некорректен: новые API-ключи не сохранены")
    return fernet.encrypt(value.strip().encode()).decode()


def is_encrypted_secret(value: Optional[str]) -> bool:
    if not value:
        return False
    fernet = _get_fernet()
    if not fernet:
        return False
    try:
        fernet.decrypt(value.encode())
        return True
    except InvalidToken:
        return False


def decrypt_secret(value: Optional[str]) -> Optional[str]:
    if not value:
        return value
    fernet = _get_fernet()
    if not fernet:
        if looks_like_fernet_token(value):
            raise RuntimeError("Не задан рабочий TOKEN_ENCRYPTION_KEY для расшифровки API-ключа")
        return value
    try:
        return fernet.decrypt(value.encode()).decode()
    except InvalidToken as exc:
        if looks_like_fernet_token(value):
            raise RuntimeError(
                "API-ключ зашифрован другим TOKEN_ENCRYPTION_KEY. Верните прежний ключ или обновите токен магазина"
            ) from exc
        # Обратная совместимость со старыми ключами, хранившимися открытым текстом.
        return value


class User(AsyncAttrs, Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(BigInteger, unique=True, index=True, nullable=False)
    is_admin = Column(Boolean, default=False)

    plan = Column(String, default="trial")
    replies_quota_left = Column(Integer, default=50)
    extra_replies_balance = Column(Integer, default=0)
    subscription_until = Column(DateTime, nullable=True)
    expiry_notification_sent = Column(Boolean, default=False)

    created_at = Column(DateTime, default=utcnow_naive)

    shops = relationship("Shop", back_populates="user", cascade="all, delete-orphan")


class Shop(AsyncAttrs, Base):
    __tablename__ = "shops"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(BigInteger, ForeignKey("users.user_id"), nullable=False)
    marketplace = Column(String, nullable=False)
    name = Column(String, nullable=False)

    ozon_client_id = Column(String, nullable=True)
    ozon_api_key_enc = Column(String, nullable=True)
    wb_token_enc = Column(String, nullable=True)

    is_running = Column(Boolean, default=False)
    is_archived = Column(Boolean, default=False)

    max_age_days = Column(Integer, default=3)
    reply_1_star = Column(Boolean, default=False)
    reply_2_star = Column(Boolean, default=False)
    reply_3_star = Column(Boolean, default=False)
    reply_4_star = Column(Boolean, default=False)
    reply_5_star = Column(Boolean, default=True)

    tone_style = Column(String, default="standard")
    signature_text = Column(String, nullable=True)
    stop_words = Column(String, nullable=True)
    custom_instructions = Column(Text, nullable=True)
    moderate_negative = Column(Boolean, default=False)

    created_at = Column(DateTime, default=utcnow_naive)

    user = relationship("User", back_populates="shops")
    logs = relationship("ReplyLog", back_populates="shop", cascade="all, delete-orphan")
    processed_reviews = relationship(
        "ProcessedReview", back_populates="shop", cascade="all, delete-orphan"
    )
    pending_approvals = relationship(
        "PendingApproval", back_populates="shop", cascade="all, delete-orphan"
    )


class ReplyLog(AsyncAttrs, Base):
    __tablename__ = "reply_logs"

    id = Column(Integer, primary_key=True, index=True)
    shop_id = Column(Integer, ForeignKey("shops.id", ondelete="CASCADE"), nullable=False)
    marketplace = Column(String, nullable=False)
    product_name = Column(Text, nullable=True)
    rating = Column(Integer, nullable=False)
    review_text = Column(Text, nullable=True)
    reply_text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utcnow_naive)

    shop = relationship("Shop", back_populates="logs")


class ProcessedReview(AsyncAttrs, Base):
    __tablename__ = "processed_reviews"
    __table_args__ = (
        UniqueConstraint("shop_id", "marketplace", "review_id", name="uq_shop_mp_review"),
    )

    id = Column(Integer, primary_key=True, index=True)
    shop_id = Column(Integer, ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True)
    marketplace = Column(String, nullable=False)
    review_id = Column(String, nullable=False)
    created_at = Column(DateTime, default=utcnow_naive)

    shop = relationship("Shop", back_populates="processed_reviews")


class PendingApproval(AsyncAttrs, Base):
    __tablename__ = "pending_approvals"

    id = Column(Integer, primary_key=True, index=True)
    shop_id = Column(Integer, ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(BigInteger, nullable=False)
    marketplace = Column(String, nullable=False)
    review_id = Column(String, nullable=False)
    product_name = Column(Text, nullable=True)
    rating = Column(Integer, nullable=False)
    review_text = Column(Text, nullable=True)
    proposed_reply = Column(Text, nullable=True)
    review_created_at = Column(DateTime, nullable=True)
    status = Column(String, default="pending")
    created_at = Column(DateTime, default=utcnow_naive)
    sku = Column(String, nullable=True)

    shop = relationship("Shop", back_populates="pending_approvals")


class PendingPayment(AsyncAttrs, Base):
    __tablename__ = "pending_payments"

    id = Column(Integer, primary_key=True, index=True)
    payment_id = Column(String, nullable=False, index=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    item_type = Column(String, nullable=False)
    item_code = Column(String, nullable=False)
    amount = Column(Integer, nullable=False)
    status = Column(String, default="pending")
    created_at = Column(DateTime, default=utcnow_naive)


class PromoCode(AsyncAttrs, Base):
    __tablename__ = "promo_codes"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, unique=True, index=True, nullable=False)
    promo_type = Column(String, default="plan")
    plan_code = Column(String, nullable=True)
    value = Column(Integer, default=0)
    max_uses = Column(Integer, default=1)
    used_count = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow_naive)


def get_decrypted_ozon_key(shop: Shop) -> str:
    return decrypt_secret(shop.ozon_api_key_enc) or ""


def get_decrypted_wb_token(shop: Shop) -> str:
    return decrypt_secret(shop.wb_token_enc) or ""


def _ensure_legacy_schema(sync_conn):
    """Досоздаёт отсутствующие колонки в существующих таблицах и убирает несовместимые служебные таблицы."""
    dialect = sync_conn.dialect

    def existing_columns(table_name: str):
        try:
            rows = sync_conn.exec_driver_sql(f"PRAGMA table_info('{table_name}')").fetchall()
        except Exception:
            return set()
        return {row[1] for row in rows}

    required = {
        "pending_payments": {"payment_id", "user_id", "item_type", "item_code", "amount", "status", "created_at"},
        "promo_codes": {"code", "promo_type", "plan_code", "value", "max_uses", "used_count", "is_active", "created_at"},
    }
    for table_name, need in required.items():
        existing = existing_columns(table_name)
        if existing and not need.issubset(existing):
            logging.warning("Таблица %s несовместима со схемой — пересоздаём", table_name)
            sync_conn.exec_driver_sql(f"DROP TABLE '{table_name}'")

    for table in Base.metadata.sorted_tables:
        existing = existing_columns(table.name)
        if not existing:
            continue
        for column in table.columns:
            if column.name in existing:
                continue
            ddl_type = column.type.compile(dialect=dialect)
            default_sql = ""
            default = column.default
            if default is not None and getattr(default, "is_scalar", False):
                value = default.arg
                if isinstance(value, bool):
                    default_sql = f" DEFAULT {1 if value else 0}"
                elif isinstance(value, (int, float)):
                    default_sql = f" DEFAULT {value}"
                elif isinstance(value, str):
                    escaped = value.replace("'", "''")
                    default_sql = f" DEFAULT '{escaped}'"
            sync_conn.exec_driver_sql(
                f"ALTER TABLE '{table.name}' ADD COLUMN '{column.name}' {ddl_type}{default_sql}"
            )
            logging.info("Добавлена колонка %s.%s", table.name, column.name)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(_ensure_legacy_schema)
        await conn.run_sync(Base.metadata.create_all)
    await migrate_legacy_secrets()


async def migrate_legacy_secrets():
    if not _get_fernet():
        return
    async with async_session() as session:
        shops = list((await session.execute(select(Shop))).scalars().all())
        changed = False
        for shop in shops:
            for field in ("ozon_api_key_enc", "wb_token_enc"):
                value = getattr(shop, field)
                if not value or is_encrypted_secret(value):
                    continue
                if looks_like_fernet_token(value):
                    logging.error(
                        f"Магазин {shop.id}, поле {field}: ключ зашифрован другим Fernet-ключом; значение не перезаписано"
                    )
                    continue
                setattr(shop, field, encrypt_secret(value))
                changed = True
        if changed:
            await session.commit()
            logging.info("Старые API-ключи маркетплейсов зашифрованы")


async def get_or_create_user(user_id: int, is_admin: Optional[bool] = None) -> User:
    if is_admin is None:
        is_admin = user_id in ADMIN_IDS

    async with async_session() as session:
        create_stmt = sqlite_insert(User).values(
            user_id=user_id,
            is_admin=is_admin,
            replies_quota_left=99999 if is_admin else 50,
            plan="unlimited" if is_admin else "trial",
        ).on_conflict_do_nothing(index_elements=["user_id"])
        await session.execute(create_stmt)
        await session.commit()

        result = await session.execute(select(User).where(User.user_id == user_id))
        user = result.scalar_one()
        if user.is_admin != is_admin:
            was_admin = user.is_admin
            user.is_admin = is_admin
            if is_admin:
                user.plan = "unlimited"
                user.replies_quota_left = 99999
            elif was_admin and user.plan == "unlimited":
                user.plan = "trial"
                user.subscription_until = None
                user.expiry_notification_sent = False
                user.replies_quota_left = max(0, min(user.replies_quota_left or 0, PLAN_QUOTAS["trial"]))
            await session.commit()
            await session.refresh(user)
        return user


async def get_user_shops(user_id: int, include_archived: bool = False) -> List[Shop]:
    async with async_session() as session:
        if include_archived:
            stmt = select(Shop).where(Shop.user_id == user_id).order_by(Shop.id.asc())
        else:
            stmt = (
                select(Shop)
                .where(Shop.user_id == user_id, Shop.is_archived == False)
                .order_by(Shop.id.asc())
            )
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def get_shop_by_id(shop_id: int, user_id: Optional[int] = None) -> Optional[Shop]:
    async with async_session() as session:
        stmt = select(Shop).where(Shop.id == shop_id)
        if user_id is not None:
            stmt = stmt.where(Shop.user_id == user_id)
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def get_running_shops() -> List[Shop]:
    async with async_session() as session:
        stmt = select(Shop).where(Shop.is_running == True, Shop.is_archived == False)
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def create_shop(
    user_id: int,
    marketplace: str,
    name: str,
    ozon_client_id: Optional[str] = None,
    ozon_api_key: Optional[str] = None,
    wb_token: Optional[str] = None,
) -> Shop:
    async with async_session() as session:
        await session.execute(
            sqlite_insert(User).values(user_id=user_id).on_conflict_do_nothing(index_elements=["user_id"])
        )
        user = (
            await session.execute(select(User).where(User.user_id == user_id))
        ).scalar_one()
        count = (
            await session.execute(
                select(func.count(Shop.id)).where(Shop.user_id == user_id, Shop.is_archived == False)
            )
        ).scalar_one() or 0
        max_allowed = (
            999
            if user.is_admin or user.plan == "unlimited" or user_id in ADMIN_IDS
            else PLAN_SHOP_LIMITS.get(user.plan, 1)
        )
        if count >= max_allowed:
            raise ShopLimitError(f"Достигнут лимит магазинов по тарифу ({count}/{max_allowed})")

        shop = Shop(
            user_id=user_id,
            marketplace=marketplace,
            name=name.strip(),
            ozon_client_id=ozon_client_id.strip() if ozon_client_id else None,
            ozon_api_key_enc=encrypt_secret(ozon_api_key) if ozon_api_key else None,
            wb_token_enc=encrypt_secret(wb_token) if wb_token else None,
            is_running=False,
            is_archived=False,
            max_age_days=3,
            reply_5_star=True,
            tone_style="standard",
        )
        session.add(shop)
        await session.commit()
        await session.refresh(shop)
        return shop


async def update_shop(shop_id: int, user_id: Optional[int] = None, **kwargs) -> Optional[Shop]:
    async with async_session() as session:
        stmt = select(Shop).where(Shop.id == shop_id)
        if user_id is not None:
            stmt = stmt.where(Shop.user_id == user_id)
        result = await session.execute(stmt)
        shop = result.scalar_one_or_none()
        if not shop:
            return None

        for key, value in kwargs.items():
            if key == "ozon_api_key_enc" and value:
                value = encrypt_secret(value)
            elif key == "wb_token_enc" and value:
                value = encrypt_secret(value)
            if hasattr(shop, key):
                setattr(shop, key, value)

        await session.commit()
        await session.refresh(shop)
        return shop


async def archive_shop(user_id: int, shop_id: int) -> bool:
    async with async_session() as session:
        shop = await session.get(Shop, shop_id)
        if not shop or shop.user_id != user_id:
            return False
        shop.is_archived = True
        shop.is_running = False
        await session.commit()
        return True


async def unarchive_shop(user_id: int, shop_id: int) -> Tuple[bool, str]:
    async with async_session() as session:
        shop = await session.get(Shop, shop_id)
        if not shop or shop.user_id != user_id:
            return False, "Магазин не найден"

        user = await get_or_create_user(user_id)
        shops = await get_user_shops(user_id, include_archived=False)

        if user.is_admin or user.plan == "unlimited" or user_id in ADMIN_IDS:
            max_slots = 999
        else:
            max_slots = PLAN_SHOP_LIMITS.get(user.plan, 1)

        if len(shops) >= max_slots:
            return False, f"Достигнут лимит активных магазинов по тарифу ({max_slots})."

        shop.is_archived = False
        await session.commit()
        return True, "Магазин восстановлен!"


async def delete_shop(user_id: int, shop_id: int) -> bool:
    async with async_session() as session:
        shop = await session.get(Shop, shop_id)
        if not shop or shop.user_id != user_id:
            return False
        await session.delete(shop)
        await session.commit()
        return True


async def save_reply_log(
    shop_id: int,
    marketplace: str,
    product_name: str,
    rating: int,
    review_text: str,
    reply_text: str,
):
    async with async_session() as session:
        log = ReplyLog(
            shop_id=shop_id,
            marketplace=marketplace,
            product_name=product_name,
            rating=rating,
            review_text=review_text,
            reply_text=reply_text,
        )
        session.add(log)
        await session.commit()


async def is_review_processed(shop_id: int, marketplace: str, review_id: str) -> bool:
    async with async_session() as session:
        stmt = select(ProcessedReview.id).where(
            ProcessedReview.shop_id == shop_id,
            ProcessedReview.marketplace == marketplace,
            ProcessedReview.review_id == str(review_id),
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None


async def mark_review_processed(shop_id: int, marketplace: str, review_id: str):
    async with async_session() as session:
        stmt = sqlite_insert(ProcessedReview).values(
            shop_id=shop_id,
            marketplace=marketplace,
            review_id=str(review_id),
        ).on_conflict_do_nothing(index_elements=["shop_id", "marketplace", "review_id"])
        await session.execute(stmt)
        await session.commit()


async def record_published_reply(
    shop_id: int,
    marketplace: str,
    review_id: str,
    product_name: str,
    rating: int,
    review_text: str,
    reply_text: str,
) -> bool:
    """Атомарно фиксирует публикацию и сохраняет лог без повторных записей."""
    async with async_session() as session:
        insert_stmt = sqlite_insert(ProcessedReview).values(
            shop_id=shop_id,
            marketplace=marketplace,
            review_id=str(review_id),
        ).on_conflict_do_nothing(index_elements=["shop_id", "marketplace", "review_id"])
        result = await session.execute(insert_stmt)
        created = result.rowcount == 1
        if created:
            session.add(
                ReplyLog(
                    shop_id=shop_id,
                    marketplace=marketplace,
                    product_name=product_name,
                    rating=rating,
                    review_text=review_text,
                    reply_text=reply_text,
                )
            )
        await session.commit()
        return created


async def get_recent_logs(shop_id: int, limit: int = 5, offset: int = 0) -> List[ReplyLog]:
    async with async_session() as session:
        stmt = (
            select(ReplyLog)
            .where(ReplyLog.shop_id == shop_id)
            .order_by(ReplyLog.id.desc())
            .offset(max(0, offset))
            .limit(max(1, limit))
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def get_shop_reply_count(shop_id: int) -> int:
    async with async_session() as session:
        stmt = select(func.count(ReplyLog.id)).where(ReplyLog.shop_id == shop_id)
        result = await session.execute(stmt)
        return int(result.scalar_one() or 0)


async def decrement_user_quota(user_id: int, amount: int = 1) -> Optional[int]:
    # Оставлено для совместимости; воркеры используют reserve/release + record_published_reply.
    if amount < 0:
        raise ValueError("amount не может быть отрицательным")
    for _ in range(amount):
        if not await reserve_reply_quota(user_id):
            break
    async with async_session() as session:
        user = (
            await session.execute(select(User).where(User.user_id == user_id))
        ).scalar_one_or_none()
        return user.replies_quota_left if user else None


async def reserve_reply_quota(user_id: int) -> bool:
    """Атомарно резервирует один ответ, исключая превышение квоты параллельными магазинами."""
    async with async_session() as session:
        result = await session.execute(
            update(User)
            .where(
                User.user_id == user_id,
                User.is_admin == False,
                User.plan != "unlimited",
                User.replies_quota_left > 0,
            )
            .values(replies_quota_left=User.replies_quota_left - 1)
        )
        if result.rowcount == 1:
            await session.commit()
            return True

        result = await session.execute(
            update(User)
            .where(
                User.user_id == user_id,
                User.is_admin == False,
                User.plan != "unlimited",
                User.extra_replies_balance > 0,
            )
            .values(extra_replies_balance=User.extra_replies_balance - 1)
        )
        if result.rowcount == 1:
            await session.commit()
            return True

        user = (
            await session.execute(select(User).where(User.user_id == user_id))
        ).scalar_one_or_none()
        return bool(
            user and (user.is_admin or user.plan == "unlimited" or user_id in ADMIN_IDS)
        )


async def release_reply_quota(user_id: int):
    async with async_session() as session:
        await session.execute(
            update(User)
            .where(
                User.user_id == user_id,
                User.is_admin == False,
                User.plan != "unlimited",
            )
            .values(replies_quota_left=func.max(0, func.coalesce(User.replies_quota_left, 0) + 1))
        )
        await session.commit()


async def get_reply_counts_for_shops(shop_ids: List[int]) -> Dict[int, int]:
    if not shop_ids:
        return {}
    async with async_session() as session:
        result = await session.execute(
            select(ReplyLog.shop_id, func.count(ReplyLog.id))
            .where(ReplyLog.shop_id.in_(shop_ids))
            .group_by(ReplyLog.shop_id)
        )
        return {shop_id: int(count) for shop_id, count in result.all()}


async def check_shop_creation_allowed(user_id: int) -> Tuple[bool, int, int]:
    user = await get_or_create_user(user_id)
    shops = await get_user_shops(user_id, include_archived=False)
    count = len(shops)

    if user.is_admin or user.plan == "unlimited" or user_id in ADMIN_IDS:
        return True, count, 999

    max_allowed = PLAN_SHOP_LIMITS.get(user.plan, 1)
    return count < max_allowed, count, max_allowed


async def check_shop_launch_allowed(user_id: int, shop_to_launch_id: int) -> Tuple[bool, int, int]:
    user = await get_or_create_user(user_id)
    shops = await get_user_shops(user_id, include_archived=False)

    running_shops = [s for s in shops if s.is_running and s.id != shop_to_launch_id]
    running_count = len(running_shops)

    if user.is_admin or user.plan == "unlimited" or user_id in ADMIN_IDS:
        return True, running_count, 999

    max_allowed = PLAN_SHOP_LIMITS.get(user.plan, 1)
    return running_count < max_allowed, running_count, max_allowed


async def get_running_shops_stats(user_id: int) -> Tuple[int, int]:
    user = await get_or_create_user(user_id)
    shops = await get_user_shops(user_id, include_archived=False)
    running_count = sum(1 for s in shops if s.is_running)

    if user.is_admin or user.plan == "unlimited" or user_id in ADMIN_IDS:
        return running_count, 99

    return running_count, PLAN_SHOP_LIMITS.get(user.plan, 1)


async def get_user_profile_data(user_id: int) -> dict:
    user = await get_or_create_user(user_id)
    shops = await get_user_shops(user_id, include_archived=False)

    counts = await get_reply_counts_for_shops([shop.id for shop in shops])
    total_replies = sum(counts.values())

    return {
        "is_admin": user.is_admin,
        "plan": user.plan,
        "quota_left": user.replies_quota_left,
        "subscription_until": user.subscription_until,
        "shops_count": len(shops),
        "total_replies": total_replies,
    }


async def downgrade_expired_users() -> List[dict]:
    reports = []
    now = utcnow_naive()
    async with async_session() as session:
        candidates = list(
            (
                await session.execute(
                    select(User).where(
                        User.subscription_until != None,
                        User.subscription_until < now,
                        User.plan != "trial",
                        User.is_admin == False,
                    )
                )
            ).scalars().all()
        )

    for candidate in candidates:
        expected_until = candidate.subscription_until
        async with async_session() as session:
            # Если администратор уже продлил подписку, условное UPDATE ничего не изменит.
            result = await session.execute(
                update(User)
                .where(
                    User.id == candidate.id,
                    User.plan == candidate.plan,
                    User.subscription_until == expected_until,
                    User.subscription_until < now,
                    User.is_admin == False,
                )
                .values(
                    plan="trial",
                    subscription_until=None,
                    expiry_notification_sent=False,
                    replies_quota_left=func.min(
                        func.max(func.coalesce(User.replies_quota_left, 0), 0),
                        PLAN_QUOTAS["trial"],
                    ),
                )
            )
            if result.rowcount != 1:
                continue

            shops = list(
                (
                    await session.execute(
                        select(Shop)
                        .where(Shop.user_id == candidate.user_id, Shop.is_archived == False)
                        .order_by(Shop.is_running.desc(), Shop.id.asc())
                    )
                ).scalars().all()
            )
            archived_names = []
            for idx, shop in enumerate(shops):
                if idx >= 1:
                    shop.is_archived = True
                    shop.is_running = False
                    archived_names.append(shop.name)

            await session.commit()
            reports.append(
                {
                    "user_id": candidate.user_id,
                    "old_plan": candidate.plan,
                    "archived_shops": archived_names,
                }
            )
    return reports


async def get_users_expiring_soon() -> List[User]:
    now = utcnow_naive()
    target_date = now + timedelta(days=2)
    async with async_session() as session:
        stmt = select(User).where(
            User.subscription_until != None,
            User.subscription_until > now,
            User.subscription_until <= target_date,
            User.expiry_notification_sent == False,
            User.is_admin == False,
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def mark_user_expiry_notified(user_id: int):
    async with async_session() as session:
        stmt = select(User).where(User.user_id == user_id)
        res = await session.execute(stmt)
        user = res.scalar_one_or_none()
        if user:
            user.expiry_notification_sent = True
            await session.commit()


# --- АДМИНИСТРАТИВНЫЕ ФУНКЦИИ ---
async def get_admin_metrics() -> dict:
    async with async_session() as session:
        total_users = (await session.execute(select(func.count(User.id)))).scalar_one() or 0
        total_shops = (await session.execute(select(func.count(Shop.id)))).scalar_one() or 0
        active_shops = (
            await session.execute(
                select(func.count(Shop.id)).where(
                    Shop.is_running == True, Shop.is_archived == False
                )
            )
        ).scalar_one() or 0
        total_replies = (await session.execute(select(func.count(ReplyLog.id)))).scalar_one() or 0

        return {
            "total_users": int(total_users),
            "total_shops": int(total_shops),
            "active_shops": int(active_shops),
            "total_replies": int(total_replies),
        }


async def grant_subscription(user_id: int, plan: str, days: int) -> bool:
    if plan not in PLAN_QUOTAS or days <= 0:
        return False
    async with async_session() as session:
        stmt = select(User).where(User.user_id == user_id)
        res = await session.execute(stmt)
        user = res.scalar_one_or_none()
        if not user:
            return False

        now = utcnow_naive()
        sub_until = _to_naive_utc(user.subscription_until)
        if sub_until and sub_until > now:
            base_date = sub_until
        else:
            base_date = now

        user.plan = plan
        user.subscription_until = base_date + timedelta(days=days)
        user.expiry_notification_sent = False
        user.replies_quota_left = PLAN_QUOTAS.get(plan, 300)

        await session.commit()
        return True


async def get_all_user_ids() -> List[int]:
    async with async_session() as session:
        stmt = select(User.user_id)
        res = await session.execute(stmt)
        return [row[0] for row in res.all()]


# --- МОДЕРАЦИЯ ОТЗЫВОВ (премодерация) ---
async def create_pending_approval(
    shop_id: int,
    user_id: int,
    marketplace: str,
    review_id: str,
    product_name: str,
    rating: int,
    review_text: str,
    proposed_reply: str,
    review_created_at=None,
    sku=None,
) -> PendingApproval:
    async with async_session() as session:
        approval = PendingApproval(
            shop_id=shop_id,
            user_id=user_id,
            marketplace=marketplace,
            review_id=str(review_id),
            product_name=product_name,
            rating=rating,
            review_text=review_text,
            proposed_reply=proposed_reply,
            review_created_at=review_created_at,
            status="pending",
            sku=str(sku) if sku is not None else None,
        )
        session.add(approval)
        await session.commit()
        await session.refresh(approval)
        return approval


async def get_pending_approval(approval_id: int) -> Optional[PendingApproval]:
    async with async_session() as session:
        return await session.get(PendingApproval, approval_id)


async def get_pending_approvals(shop_id: Optional[int] = None, status: str = "pending") -> List[PendingApproval]:
    async with async_session() as session:
        stmt = select(PendingApproval).order_by(PendingApproval.id.desc())
        if shop_id is not None:
            stmt = stmt.where(PendingApproval.shop_id == shop_id)
        if status:
            stmt = stmt.where(PendingApproval.status == status)
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def get_pending_approvals_count(shop_id: Optional[int] = None, status: str = "pending") -> int:
    async with async_session() as session:
        stmt = select(func.count(PendingApproval.id))
        if shop_id is not None:
            stmt = stmt.where(PendingApproval.shop_id == shop_id)
        if status:
            stmt = stmt.where(PendingApproval.status == status)
        result = await session.execute(stmt)
        return int(result.scalar_one() or 0)


async def update_pending_approval(approval_id: int, **kwargs) -> Optional[PendingApproval]:
    async with async_session() as session:
        approval = await session.get(PendingApproval, approval_id)
        if not approval:
            return None
        for key, value in kwargs.items():
            if hasattr(approval, key):
                setattr(approval, key, value)
        await session.commit()
        await session.refresh(approval)
        return approval


async def is_review_pending(shop_id: int, marketplace: str, review_id: str) -> bool:
    async with async_session() as session:
        stmt = select(PendingApproval.id).where(
            PendingApproval.shop_id == shop_id,
            PendingApproval.marketplace == marketplace,
            PendingApproval.review_id == str(review_id),
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none() is not None


# --- ПЛАТЕЖИ И ПРОМОКОДЫ ---
async def create_pending_payment(payment_id: str, user_id: int, item_type: str, item_code: str, amount: int) -> PendingPayment:
    async with async_session() as session:
        payment = PendingPayment(
            payment_id=str(payment_id),
            user_id=user_id,
            item_type=item_type,
            item_code=item_code,
            amount=amount,
            status="pending",
        )
        session.add(payment)
        await session.commit()
        await session.refresh(payment)
        return payment


async def get_pending_payment_by_payment_id(payment_id: str) -> Optional[PendingPayment]:
    async with async_session() as session:
        stmt = select(PendingPayment).where(PendingPayment.payment_id == str(payment_id))
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def get_pending_payments(status: str = "pending") -> List[PendingPayment]:
    async with async_session() as session:
        stmt = select(PendingPayment).where(PendingPayment.status == status)
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def update_pending_payment(payment_row_id: int, **kwargs) -> Optional[PendingPayment]:
    async with async_session() as session:
        payment = await session.get(PendingPayment, payment_row_id)
        if not payment:
            return None
        for key, value in kwargs.items():
            if hasattr(payment, key):
                setattr(payment, key, value)
        await session.commit()
        await session.refresh(payment)
        return payment


async def get_promo_code(code: str) -> Optional[PromoCode]:
    async with async_session() as session:
        stmt = select(PromoCode).where(PromoCode.code == str(code).strip().upper())
        result = await session.execute(stmt)
        return result.scalar_one_or_none()


async def use_promo_code(code: str) -> Optional[PromoCode]:
    async with async_session() as session:
        stmt = select(PromoCode).where(PromoCode.code == str(code).strip().upper())
        result = await session.execute(stmt)
        promo = result.scalar_one_or_none()
        if not promo or not promo.is_active:
            return None
        if promo.max_uses > 0 and promo.used_count >= promo.max_uses:
            return None
        promo.used_count += 1
        await session.commit()
        await session.refresh(promo)
        return promo


async def add_extra_replies(user_id: int, amount: int):
    async with async_session() as session:
        await session.execute(
            update(User)
            .where(User.user_id == user_id)
            .values(extra_replies_balance=func.coalesce(User.extra_replies_balance, 0) + amount)
        )
        await session.commit()


async def create_promo_code(code: str, promo_type: str = "plan", plan_code: Optional[str] = None, value: int = 0, max_uses: int = 1, is_active: bool = True) -> PromoCode:
    async with async_session() as session:
        promo = PromoCode(
            code=str(code).strip().upper(),
            promo_type=promo_type,
            plan_code=plan_code,
            value=value,
            max_uses=max_uses,
            used_count=0,
            is_active=is_active,
        )
        session.add(promo)
        await session.commit()
        await session.refresh(promo)
        return promo
