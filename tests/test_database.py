import asyncio
from datetime import timedelta
from sqlalchemy import func, select

import database
from database import ProcessedReview, ReplyLog, Shop, User
from tests.common import DatabaseTestCase


class DatabaseTests(DatabaseTestCase):
    async def test_secrets_are_encrypted_and_legacy_plaintext_is_readable(self):
        secret = "wb-secret-<value>"
        shop = await database.create_shop(42, "wildberries", "Shop", wb_token=secret)
        if database.TOKEN_ENCRYPTION_KEY:
            self.assertNotEqual(shop.wb_token_enc, secret)
        self.assertEqual(database.get_decrypted_wb_token(shop), secret)
        self.assertEqual(database.decrypt_secret("legacy-plain-token"), "legacy-plain-token")

        async with self.Session() as session:
            stored = await session.get(database.Shop, shop.id)
            stored.wb_token_enc = "legacy-plain-token"
            await session.commit()
        await database.migrate_legacy_secrets()
        migrated = await database.get_shop_by_id(shop.id)
        if database.TOKEN_ENCRYPTION_KEY:
            self.assertNotEqual(migrated.wb_token_enc, "legacy-plain-token")
        self.assertEqual(database.get_decrypted_wb_token(migrated), "legacy-plain-token")

    async def test_ownership_and_cascading_delete(self):
        await self.create_user(42)
        await self.create_user(777)
        shop = await database.create_shop(42, "wildberries", "Shop", wb_token="secret")
        await database.save_reply_log(shop.id, "wb", "Product", 5, "Review", "Reply")
        await database.mark_review_processed(shop.id, "wb", "review-1")

        self.assertIsNone(await database.get_shop_by_id(shop.id, user_id=777))
        self.assertIsNone(await database.update_shop(shop.id, user_id=777, is_running=True))
        self.assertFalse(await database.archive_shop(777, shop.id))
        self.assertFalse(await database.delete_shop(777, shop.id))
        self.assertIsNotNone(await database.get_shop_by_id(shop.id))

        self.assertTrue(await database.delete_shop(42, shop.id))
        async with self.Session() as session:
            self.assertEqual((await session.execute(select(func.count(ReplyLog.id)))).scalar_one(), 0)
            self.assertEqual((await session.execute(select(func.count(ProcessedReview.id)))).scalar_one(), 0)

    async def test_log_pagination_and_quota(self):
        shop = await database.create_shop(42, "wildberries", "Shop", wb_token="secret")
        for index in range(5):
            await database.save_reply_log(shop.id, "wb", "P", 5, f"R{index}", f"A{index}")
        logs = await database.get_recent_logs(shop.id, limit=2, offset=2)
        self.assertEqual([log.review_text for log in logs], ["R2", "R1"])
        self.assertEqual(await database.get_shop_reply_count(shop.id), 5)

        user = await self.create_user(42)
        self.assertEqual(user.replies_quota_left, 50)
        self.assertEqual(await database.decrement_user_quota(42), 49)
        self.assertEqual(await database.get_shop_reply_count(shop.id), 5)

    async def test_processed_review_is_idempotent(self):
        shop = await database.create_shop(42, "wildberries", "Shop", wb_token="secret")
        self.assertFalse(await database.is_review_processed(shop.id, "wb", "id-1"))
        await database.mark_review_processed(shop.id, "wb", "id-1")
        await database.mark_review_processed(shop.id, "wb", "id-1")
        self.assertTrue(await database.is_review_processed(shop.id, "wb", "id-1"))
        async with self.Session() as session:
            count = (await session.execute(select(func.count(ProcessedReview.id)))).scalar_one()
        self.assertEqual(count, 1)

    async def test_subscription_grant_and_downgrade(self):
        await self.create_user(42)
        await database.grant_subscription(42, "pro", 30)
        first = await database.create_shop(42, "wildberries", "First", wb_token="one")
        second = await database.create_shop(42, "ozon", "Second", ozon_client_id="id", ozon_api_key="two")
        await database.update_shop(first.id, is_running=True)
        await database.update_shop(second.id, is_running=True)

        self.assertTrue(await database.grant_subscription(42, "pro", 30))
        async with self.Session() as session:
            user = (await session.execute(select(User).where(User.user_id == 42))).scalar_one()
            self.assertEqual(user.replies_quota_left, 2000)
            user.subscription_until = database.utcnow_naive() - timedelta(days=1)
            await session.commit()

        reports = await database.downgrade_expired_users()
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["archived_shops"], ["Second"])
        first = await database.get_shop_by_id(first.id)
        second = await database.get_shop_by_id(second.id)
        self.assertFalse(first.is_archived)
        self.assertTrue(second.is_archived)
        self.assertFalse(second.is_running)

    async def test_expiring_users_excludes_admins_and_already_notified(self):
        user = await self.create_user(1001)
        await database.grant_subscription(1001, "start", 1)
        users = await database.get_users_expiring_soon()
        self.assertEqual([item.user_id for item in users], [1001])
        await database.mark_user_expiry_notified(1001)
        self.assertEqual(await database.get_users_expiring_soon(), [])

    async def test_plan_limits_cover_unlimited(self):
        user = await self.create_user(1002)
        user_id = user.user_id
        await database.grant_subscription(user_id, "unlimited", 30)
        running, limit = await database.get_running_shops_stats(user_id)
        self.assertEqual((running, limit), (0, 99))
        allowed, count, maximum = await database.check_shop_launch_allowed(user_id, 0)
        self.assertTrue(allowed)
        self.assertEqual(maximum, 999)

    async def test_reply_quota_reservation_is_atomic(self):
        await self.create_user(42)
        async with self.Session() as session:
            user = (await session.execute(select(User).where(User.user_id == 42))).scalar_one()
            user.replies_quota_left = 1
            await session.commit()

        results = await asyncio.gather(
            database.reserve_reply_quota(42),
            database.reserve_reply_quota(42),
        )
        self.assertCountEqual(results, [True, False])

        async with self.Session() as session:
            user = (await session.execute(select(User).where(User.user_id == 42))).scalar_one()
            self.assertEqual(user.replies_quota_left, 0)

        await database.release_reply_quota(42)
        async with self.Session() as session:
            user = (await session.execute(select(User).where(User.user_id == 42))).scalar_one()
            self.assertEqual(user.replies_quota_left, 1)
