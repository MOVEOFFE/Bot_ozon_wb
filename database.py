import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import List, Tuple, Optional
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, ForeignKey, BigInteger, select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncAttrs
from sqlalchemy.orm import declarative_base, relationship

DATA_DIR = os.getenv("DATA_DIR", "/app/data" if os.path.exists("/app") else ".")
os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = Path(DATA_DIR) / "vellio_bot.db"
DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH.as_posix()}"

engine = create_async_engine(DATABASE_URL, echo=False, future=True)
async_session = async_sessionmaker(engine, expire_on_commit=False)
Base = declarative_base()


class User(AsyncAttrs, Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(BigInteger, unique=True, index=True, nullable=False)
    username = Column(String, nullable=True)
    is_admin = Column(Boolean, default=False)
    
    plan = Column(String, default="trial")  # trial, start, business, pro, unlimited
    plan_replies_limit = Column(Integer, default=50)
    replies_quota_left = Column(Integer, default=50)      # Остаток из месячного тарифа
    extra_replies_balance = Column(Integer, default=0)    # Купленные разовые пакеты S, M, L
    
    subscription_until = Column(DateTime, nullable=True)
    expiry_notification_sent = Column(Boolean, default=False)
    
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

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

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="shops")


class ReplyLog(AsyncAttrs, Base):
    __tablename__ = "reply_logs"

    id = Column(Integer, primary_key=True, index=True)
    shop_id = Column(Integer, ForeignKey("shops.id"), nullable=False)
    marketplace = Column(String, nullable=False)
    product_name = Column(Text, nullable=True)
    rating = Column(Integer, nullable=False)
    review_text = Column(Text, nullable=True)
    reply_text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


async def init_db():
    if DB_PATH.exists():
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(users)")
            columns = [row[1] for row in cursor.fetchall()]
            
            if "username" not in columns:
                cursor.execute("ALTER TABLE users ADD COLUMN username TEXT")
            if "extra_replies_balance" not in columns:
                cursor.execute("ALTER TABLE users ADD COLUMN extra_replies_balance INTEGER DEFAULT 0")
            if "plan_replies_limit" not in columns:
                cursor.execute("ALTER TABLE users ADD COLUMN plan_replies_limit INTEGER DEFAULT 50")
                
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Migration error: {e}")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_or_create_user(user_id: int, is_admin: bool = False, username: str = None) -> User:
    async with async_session() as session:
        stmt = select(User).where(User.user_id == user_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()
        
        if not user:
            user = User(
                user_id=user_id,
                username=username,
                is_admin=is_admin, 
                plan="unlimited" if is_admin else "trial",
                plan_replies_limit=99999 if is_admin else 50,
                replies_quota_left=99999 if is_admin else 50, 
                extra_replies_balance=0,
                subscription_until=datetime.now(timezone.utc) + timedelta(days=7) if not is_admin else None
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
        else:
            updated = False
            if username and user.username != username:
                user.username = username
                updated = True
            if user.is_admin != is_admin:
                user.is_admin = is_admin
                if is_admin:
                    user.plan = "unlimited"
                    user.plan_replies_limit = 99999
                    user.replies_quota_left = 99999
                updated = True
            if updated:
                await session.commit()
                await session.refresh(user)
                
        return user


async def can_user_reply(user_id: int) -> bool:
    user = await get_or_create_user(user_id)
    if user.is_admin or user.plan == "unlimited" or user_id in [590568180]:
        return True
    
    now = datetime.now(timezone.utc)
    if user.subscription_until and user.subscription_until >= now and user.replies_quota_left > 0:
        return True
    
    if user.extra_replies_balance > 0:
        return True
        
    return False


async def deduct_reply_quota(user_id: int) -> bool:
    async with async_session() as session:
        stmt = select(User).where(User.user_id == user_id)
        res = await session.execute(stmt)
        user = res.scalar_one_or_none()
        if not user:
            return False
            
        if user.is_admin or user.plan == "unlimited" or user_id in [590568180]:
            return True

        now = datetime.now(timezone.utc)
        if user.subscription_until and user.subscription_until >= now and user.replies_quota_left > 0:
            user.replies_quota_left -= 1
            await session.commit()
            return True
        elif user.extra_replies_balance > 0:
            user.extra_replies_balance -= 1
            await session.commit()
            return True
            
        return False


async def get_user_shops(user_id: int, include_archived: bool = False) -> List[Shop]:
    async with async_session() as session:
        if include_archived:
            stmt = select(Shop).where(Shop.user_id == user_id)
        else:
            stmt = select(Shop).where(Shop.user_id == user_id, Shop.is_archived == False)
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def get_running_shops() -> List[Shop]:
    async with async_session() as session:
        stmt = select(Shop).where(Shop.is_running == True, Shop.is_archived == False)
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def create_shop(user_id: int, marketplace: str, name: str, ozon_client_id: str = None, ozon_api_key: str = None, wb_token: str = None) -> Shop:
    async with async_session() as session:
        shop = Shop(
            user_id=user_id,
            marketplace=marketplace,
            name=name,
            ozon_client_id=ozon_client_id,
            ozon_api_key_enc=ozon_api_key,
            wb_token_enc=wb_token,
            is_running=False,
            is_archived=False,
            max_age_days=3,
            reply_5_star=True,
            tone_style="standard"
        )
        session.add(shop)
        await session.commit()
        await session.refresh(shop)
        return shop


async def update_shop(shop_id: int, **kwargs) -> Optional[Shop]:
    async with async_session() as session:
        shop = await session.get(Shop, shop_id)
        if not shop:
            return None
        for key, value in kwargs.items():
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
        
        limits = {"trial": 1, "start": 2, "business": 5, "pro": 15, "unlimited": 99}
        max_slots = 99 if (user.is_admin or user.plan == "unlimited") else limits.get(user.plan, 1)

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
        
        logs_stmt = select(ReplyLog).where(ReplyLog.shop_id == shop_id)
        logs_res = await session.execute(logs_stmt)
        for log in logs_res.scalars().all():
            await session.delete(log)

        await session.delete(shop)
        await session.commit()
        return True


async def save_reply_log(shop_id: int, marketplace: str, product_name: str, rating: int, review_text: str, reply_text: str):
    async with async_session() as session:
        log = ReplyLog(
            shop_id=shop_id,
            marketplace=marketplace,
            product_name=product_name,
            rating=rating,
            review_text=review_text,
            reply_text=reply_text
        )
        session.add(log)
        await session.commit()


async def get_recent_logs(shop_id: int, limit: int = 5, offset: int = 0) -> List[ReplyLog]:
    async with async_session() as session:
        stmt = (
            select(ReplyLog)
            .where(ReplyLog.shop_id == shop_id)
            .order_by(ReplyLog.id.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def get_shop_reply_count(shop_id: int) -> int:
    async with async_session() as session:
        stmt = select(func.count(ReplyLog.id)).where(ReplyLog.shop_id == shop_id)
        result = await session.execute(stmt)
        return result.scalar() or 0


async def check_shop_creation_allowed(user_id: int) -> Tuple[bool, int, int]:
    user = await get_or_create_user(user_id)
    shops = await get_user_shops(user_id, include_archived=False)
    count = len(shops)

    if user.is_admin or user.plan == "unlimited" or user_id in [590568180]:
        return True, count, 999

    limits = {"trial": 1, "start": 2, "business": 5, "pro": 15, "unlimited": 999}
    max_allowed = limits.get(user.plan, 1)
    return count < max_allowed, count, max_allowed


async def check_shop_launch_allowed(user_id: int, shop_to_launch_id: int) -> Tuple[bool, int, int]:
    user = await get_or_create_user(user_id)
    shops = await get_user_shops(user_id, include_archived=False)
    
    running_shops = [s for s in shops if s.is_running and s.id != shop_to_launch_id]
    running_count = len(running_shops)

    if user.is_admin or user.plan == "unlimited" or user_id in [590568180]:
        return True, running_count, 999

    limits = {"trial": 1, "start": 2, "business": 5, "pro": 15, "unlimited": 999}
    max_allowed = limits.get(user.plan, 1)
    return running_count < max_allowed, running_count, max_allowed


async def get_running_shops_stats(user_id: int) -> Tuple[int, int]:
    user = await get_or_create_user(user_id)
    shops = await get_user_shops(user_id, include_archived=False)
    running_count = sum(1 for s in shops if s.is_running)
    
    if user.is_admin:
        return running_count, 99

    limits = {"trial": 1, "start": 2, "business": 5, "pro": 15}
    return running_count, limits.get(user.plan, 1)


async def get_user_profile_data(user_id: int) -> dict:
    user = await get_or_create_user(user_id)
    shops = await get_user_shops(user_id, include_archived=False)
    
    total_replies = 0
    for s in shops:
        total_replies += await get_shop_reply_count(s.id)

    return {
        "is_admin": user.is_admin,
        "plan": user.plan,
        "quota_left": user.replies_quota_left,
        "extra_balance": getattr(user, "extra_replies_balance", 0),
        "subscription_until": user.subscription_until,
        "shops_count": len(shops),
        "total_replies": total_replies
    }


async def downgrade_expired_users() -> List[dict]:
    reports = []
    now = datetime.now(timezone.utc)
    async with async_session() as session:
        stmt = select(User).where(User.subscription_until != None, User.subscription_until < now, User.plan != "trial")
        result = await session.execute(stmt)
        expired_users = list(result.scalars().all())

        for user in expired_users:
            old_plan = user.plan
            user.plan = "trial"
            user.subscription_until = None
            
            shops = await get_user_shops(user.user_id, include_archived=False)
            archived_names = []
            for idx, s in enumerate(shops):
                if idx >= 1:
                    s.is_archived = True
                    s.is_running = False
                    archived_names.append(s.name)

            reports.append({
                "user_id": user.user_id,
                "old_plan": old_plan,
                "archived_shops": archived_names
            })

        await session.commit()
    return reports


async def get_users_expiring_soon() -> List[User]:
    now = datetime.now(timezone.utc)
    target_date = now + timedelta(days=2)
    async with async_session() as session:
        stmt = select(User).where(
            User.subscription_until != None,
            User.subscription_until > now,
            User.subscription_until <= target_date,
            User.expiry_notification_sent == False
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
        users_res = await session.execute(select(User))
        users = list(users_res.scalars().all())
        
        shops_res = await session.execute(select(Shop))
        shops = list(shops_res.scalars().all())
        
        logs_res = await session.execute(select(ReplyLog))
        logs = list(logs_res.scalars().all())

        active_shops_count = sum(1 for s in shops if s.is_running and not s.is_archived)

        return {
            "total_users": len(users),
            "total_shops": len(shops),
            "active_shops": active_shops_count,
            "total_replies": len(logs)
        }


async def grant_subscription(user_id: int, plan: str, days: int) -> bool:
    async with async_session() as session:
        stmt = select(User).where(User.user_id == user_id)
        res = await session.execute(stmt)
        user = res.scalar_one_or_none()
        if not user:
            return False

        now = datetime.now(timezone.utc)
        if user.subscription_until and user.subscription_until > now:
            base_date = user.subscription_until
        else:
            base_date = now

        user.plan = plan
        user.subscription_until = base_date + timedelta(days=days)
        user.expiry_notification_sent = False
        
        quotas = {"start": 300, "business": 1000, "pro": 3000, "unlimited": 99999}
        assigned_quota = quotas.get(plan, 300)
        user.plan_replies_limit = assigned_quota
        user.replies_quota_left = assigned_quota

        await session.commit()
        return True


async def add_extra_replies(user_id: int, amount: int) -> bool:
    async with async_session() as session:
        stmt = select(User).where(User.user_id == user_id)
        res = await session.execute(stmt)
        user = res.scalar_one_or_none()
        if not user:
            return False

        current_extra = getattr(user, "extra_replies_balance", 0) or 0
        user.extra_replies_balance = current_extra + amount
        await session.commit()
        return True


async def get_all_user_ids() -> List[int]:
    async with async_session() as session:
        stmt = select(User.user_id)
        res = await session.execute(stmt)
        return [row[0] for row in res.all()]


async def get_all_users_admin() -> List[dict]:
    async with async_session() as session:
        res_users = await session.execute(select(User).order_by(User.id.asc()))
        users = res_users.scalars().all()

        user_list = []
        for u in users:
            res_shops = await session.execute(
                select(func.count(Shop.id)).where(Shop.user_id == u.user_id, Shop.is_archived == False)
            )
            shops_cnt = res_shops.scalar() or 0

            user_list.append({
                "user_id": u.user_id,
                "username": getattr(u, "username", None),
                "plan": u.plan,
                "replies_quota_left": u.replies_quota_left,
                "extra_balance": getattr(u, "extra_replies_balance", 0),
                "is_admin": u.is_admin,
                "shops_count": shops_cnt,
                "created_at": u.created_at
            })

        return user_list
