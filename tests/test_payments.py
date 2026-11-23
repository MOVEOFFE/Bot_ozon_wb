import unittest
from sqlalchemy import select

import database
from tests.common import DatabaseTestCase


class PaymentsTests(DatabaseTestCase):

    async def test_extra_balance_used_after_main_quota(self):
        await self.create_user()
        await database.add_extra_replies(42, 1)

        async with database.async_session() as session:
            user = (
                await session.execute(
                    select(database.User).where(database.User.user_id == 42)
                )
            ).scalar_one()
            user.replies_quota_left = 0
            await session.commit()

        self.assertTrue(await database.reserve_reply_quota(42))
        self.assertFalse(await database.reserve_reply_quota(42))

        async with database.async_session() as session:
            user = (
                await session.execute(
                    select(database.User).where(database.User.user_id == 42)
                )
            ).scalar_one()
            self.assertEqual(user.extra_replies_balance, 0)
            self.assertEqual(user.replies_quota_left, 0)

    async def test_promo_code_single_use(self):
        await self.create_user()
        await database.create_promo_code(
            "WELCOME", promo_type="replies", value=50, max_uses=1
        )

        self.assertIsNotNone(await database.use_promo_code("welcome", 42))
        self.assertIsNone(await database.use_promo_code("WELCOME", 42))

        promo = await database.get_promo_code("WELCOME")
        self.assertEqual(promo.used_count, 1)

    async def test_promo_code_per_user_redemption(self):
        await self.create_user(42)
        await self.create_user(43)
        await database.create_promo_code(
            "SHARED", promo_type="replies", value=10, max_uses=100
        )

        self.assertIsNotNone(await database.use_promo_code("shared", 42))
        self.assertIsNotNone(await database.use_promo_code("shared", 43))
        self.assertIsNone(await database.use_promo_code("shared", 42))

        promo = await database.get_promo_code("SHARED")
        self.assertEqual(promo.used_count, 2)

    async def test_pending_payment_lifecycle(self):
        await self.create_user()
        payment = await database.create_pending_payment(
            "pay-1", 42, "plan", "start", 890
        )

        self.assertEqual(len(await database.get_pending_payments("pending")), 1)

        await database.update_pending_payment(payment.id, status="succeeded")

        self.assertEqual(len(await database.get_pending_payments("pending")), 0)
        self.assertEqual(
            (await database.get_pending_payment_by_payment_id("pay-1")).status,
            "succeeded",
        )

    async def test_grant_subscription_rejects_unknown_plan(self):
        await self.create_user()

        self.assertFalse(await database.grant_subscription(42, "agency", 30))
        self.assertTrue(await database.grant_subscription(42, "start", 30))

        async with database.async_session() as session:
            user = (
                await session.execute(
                    select(database.User).where(database.User.user_id == 42)
                )
            ).scalar_one()
            self.assertEqual(user.plan, "start")
            self.assertEqual(user.replies_quota_left, 200)


if __name__ == "__main__":
    unittest.main()
