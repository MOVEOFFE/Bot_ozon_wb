from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
import database
from tests.common import DatabaseTestCase, callback


class ModerationTests(DatabaseTestCase):
    async def test_negative_review_goes_to_moderation_not_published(self):
        shop = await self.create_running_shop(marketplace="ozon")
        await database.update_shop(shop.id, reply_1_star=True, moderate_negative=True)
        user_before = await database.get_or_create_user(42)

        review = {
            "id": "ozon-neg-1",
            "sku": 123,
            "rating": 1,
            "text": "плохо",
            "published_at": datetime.now(timezone.utc).isoformat(),
            "photos_amount": 0,
            "videos_amount": 0,
            "comments_amount": 0,
            "status": "UNPROCESSED",
        }
        client = SimpleNamespace(
            get_unprocessed_reviews=AsyncMock(return_value={"reviews": [review], "has_next": False, "last_id": ""}),
            get_product_names=AsyncMock(return_value={123: "Product"}),
            publish_reply=AsyncMock(),
        )
        ai = SimpleNamespace(generate_reply=AsyncMock(return_value="Draft reply"))
        telegram = SimpleNamespace(send_message=AsyncMock())
        with patch.object(bot, "OzonClient", return_value=client), patch.object(
            bot, "interruptible_sleep", AsyncMock(return_value=True)
        ):
            result = await bot.process_ozon_queue(telegram, shop, ai)

        self.assertTrue(result)
        client.publish_reply.assert_not_awaited()
        pendings = await database.get_pending_approvals(shop.id, "pending")
        self.assertEqual(len(pendings), 1)
        self.assertEqual(pendings[0].review_id, "ozon-neg-1")
        self.assertEqual(pendings[0].proposed_reply, "Draft reply")
        user_after = await database.get_or_create_user(42)
        self.assertEqual(user_after.replies_quota_left, user_before.replies_quota_left)
        telegram.send_message.assert_awaited_once()

    async def test_positive_review_auto_publishes_despite_moderation(self):
        shop = await self.create_running_shop(marketplace="ozon")
        await database.update_shop(shop.id, moderate_negative=True)

        review = {
            "id": "ozon-pos-1",
            "sku": 123,
            "rating": 5,
            "text": "отлично",
            "published_at": datetime.now(timezone.utc).isoformat(),
            "photos_amount": 0,
            "videos_amount": 0,
            "comments_amount": 0,
            "status": "UNPROCESSED",
        }
        client = SimpleNamespace(
            get_unprocessed_reviews=AsyncMock(return_value={"reviews": [review], "has_next": False, "last_id": ""}),
            get_product_names=AsyncMock(return_value={123: "Product"}),
            publish_reply=AsyncMock(),
        )
        ai = SimpleNamespace(generate_reply=AsyncMock(return_value="Reply"))
        telegram = SimpleNamespace(send_message=AsyncMock())
        with patch.object(bot, "OzonClient", return_value=client), patch.object(
            bot, "interruptible_sleep", AsyncMock(return_value=True)
        ):
            await bot.process_ozon_queue(telegram, shop, ai)

        client.publish_reply.assert_awaited_once()
        pendings = await database.get_pending_approvals(shop.id, "pending")
        self.assertEqual(len(pendings), 0)

    async def test_custom_instructions_passed_to_ai(self):
        shop = await self.create_running_shop(marketplace="ozon")
        await database.update_shop(shop.id, custom_instructions="особенность товара X")

        review = {
            "id": "ozon-c-1",
            "sku": 123,
            "rating": 5,
            "text": "хорошо",
            "published_at": datetime.now(timezone.utc).isoformat(),
            "photos_amount": 0,
            "videos_amount": 0,
            "comments_amount": 0,
            "status": "UNPROCESSED",
        }
        client = SimpleNamespace(
            get_unprocessed_reviews=AsyncMock(return_value={"reviews": [review], "has_next": False, "last_id": ""}),
            get_product_names=AsyncMock(return_value={123: "Product"}),
            publish_reply=AsyncMock(),
        )
        ai = SimpleNamespace(generate_reply=AsyncMock(return_value="Reply"))
        telegram = SimpleNamespace(send_message=AsyncMock())
        with patch.object(bot, "OzonClient", return_value=client), patch.object(
            bot, "interruptible_sleep", AsyncMock(return_value=True)
        ):
            await bot.process_ozon_queue(telegram, shop, ai)

        self.assertEqual(ai.generate_reply.await_args.kwargs.get("custom_instructions"), "особенность товара X")

    async def test_publish_pending_reply_approves_and_logs(self):
        shop = await self.create_running_shop(marketplace="ozon")
        approval = await database.create_pending_approval(
            shop_id=shop.id,
            user_id=42,
            marketplace="ozon",
            review_id="ozon-r1",
            product_name="P",
            rating=2,
            review_text="bad",
            proposed_reply="draft",
        )
        client = SimpleNamespace(publish_reply=AsyncMock())
        with patch.object(bot, "OzonClient", return_value=client):
            ok, msg = await bot.publish_pending_reply(approval, "Final text")

        self.assertTrue(ok)
        client.publish_reply.assert_awaited_once_with(review_id="ozon-r1", text="Final text")
        updated = await database.get_pending_approval(approval.id)
        self.assertEqual(updated.status, "approved")
        self.assertEqual(await database.get_shop_reply_count(shop.id), 1)
        self.assertEqual((await database.get_or_create_user(42)).replies_quota_left, 49)

    async def test_pending_action_skip_marks_skipped(self):
        shop = await database.create_shop(42, "wildberries", "Shop", wb_token="secret")
        approval = await database.create_pending_approval(
            shop_id=shop.id,
            user_id=42,
            marketplace="wb",
            review_id="r1",
            product_name="P",
            rating=2,
            review_text="bad",
            proposed_reply="draft",
        )
        call = callback(f"pa_{approval.id}_skip", user_id=42)
        await bot.cb_pending_action(call, None)
        updated = await database.get_pending_approval(approval.id)
        self.assertEqual(updated.status, "skipped")
        self.assertTrue(await database.is_review_processed(shop.id, "wb", "r1"))

    async def test_negative_review_skipped_when_star_toggle_off(self):
        shop = await self.create_running_shop(marketplace="ozon")
        await database.update_shop(shop.id, moderate_negative=True)
        shop = await database.get_shop_by_id(shop.id)

        review = {
            "id": "ozon-neg-3",
            "sku": 123,
            "rating": 1,
            "text": "плохо",
            "published_at": datetime.now(timezone.utc).isoformat(),
            "photos_amount": 0,
            "videos_amount": 0,
            "comments_amount": 0,
            "status": "UNPROCESSED",
        }
        client = SimpleNamespace(
            get_unprocessed_reviews=AsyncMock(return_value={"reviews": [review], "has_next": False}),
            get_product_names=AsyncMock(return_value={123: "Product"}),
            publish_reply=AsyncMock(),
        )
        ai = SimpleNamespace(generate_reply=AsyncMock(return_value="Draft"))
        telegram = SimpleNamespace(send_message=AsyncMock())
        with patch.object(bot, "OzonClient", return_value=client), patch.object(
            bot, "interruptible_sleep", AsyncMock(return_value=True)
        ):
            await bot.process_ozon_queue(telegram, shop, ai)

        client.publish_reply.assert_not_awaited()
        ai.generate_reply.assert_not_awaited()
        pendings = await database.get_pending_approvals(shop.id, "pending")
        self.assertEqual(pendings, [])

    async def test_negative_review_goes_to_moderation_when_star_toggle_on(self):
        shop = await self.create_running_shop(marketplace="ozon")
        await database.update_shop(shop.id, moderate_negative=True, reply_1_star=True)
        shop = await database.get_shop_by_id(shop.id)

        review = {
            "id": "ozon-neg-4",
            "sku": 123,
            "rating": 1,
            "text": "плохо",
            "published_at": datetime.now(timezone.utc).isoformat(),
            "photos_amount": 0,
            "videos_amount": 0,
            "comments_amount": 0,
            "status": "UNPROCESSED",
        }
        client = SimpleNamespace(
            get_unprocessed_reviews=AsyncMock(return_value={"reviews": [review], "has_next": False}),
            get_product_names=AsyncMock(return_value={123: "Product"}),
            publish_reply=AsyncMock(),
        )
        ai = SimpleNamespace(generate_reply=AsyncMock(return_value="Draft"))
        telegram = SimpleNamespace(send_message=AsyncMock())
        with patch.object(bot, "OzonClient", return_value=client), patch.object(
            bot, "interruptible_sleep", AsyncMock(return_value=True)
        ):
            await bot.process_ozon_queue(telegram, shop, ai)

        client.publish_reply.assert_not_awaited()
        pendings = await database.get_pending_approvals(shop.id, "pending")
        self.assertEqual(len(pendings), 1)
        self.assertEqual(pendings[0].review_id, "ozon-neg-4")
