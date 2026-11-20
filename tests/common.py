import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

import database


class DatabaseTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_engine = database.engine
        self.original_session = database.async_session
        self.engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        database.engine = self.engine
        database.async_session = self.Session
        await database.init_db()

    async def asyncTearDown(self):
        database.async_session = self.original_session
        database.engine = self.original_engine
        await self.engine.dispose()

    async def create_user(self, user_id: int = 42):
        return await database.get_or_create_user(user_id, is_admin=False)

    async def create_running_shop(self, user_id: int = 42, marketplace: str = "ozon"):
        await self.create_user(user_id)
        if marketplace == "ozon":
            shop = await database.create_shop(
                user_id=user_id,
                marketplace="ozon",
                name="Test <Shop>",
                ozon_client_id="client",
                ozon_api_key="api-secret",
            )
        else:
            shop = await database.create_shop(
                user_id=user_id,
                marketplace="wildberries",
                name="Test <Shop>",
                wb_token="wb-secret",
            )
        return await database.update_shop(shop.id, is_running=True)


def callback(data: str, user_id: int = 42):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
        answer=AsyncMock(),
    )
