from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
import database
from tests.common import DatabaseTestCase, callback


class BotTests(DatabaseTestCase):
    async def test_shop_view_escapes_external_text(self):
        shop = await database.create_shop(
            42,
            "wildberries",
            "Name <b>& _",
            wb_token="secret",
        )
        shop = await database.update_shop(
            shop.id,
            signature_text="Sig <script>",
            stop_words="брак & <возврат>",
        )
        text, keyboard = await bot.build_shop_view(shop)
        self.assertIn("Name &lt;b&gt;&amp; _", text)
        self.assertIn("Sig &lt;script&gt;", text)
        self.assertIn("брак &amp; &lt;возврат&gt;", text)
        self.assertNotIn("Name <b>", text)
        self.assertTrue(keyboard.inline_keyboard)

    async def test_set_tone_uses_no_callback_mutation(self):
        shop = await database.create_shop(42, "wildberries", "Shop", wb_token="secret")
        call = callback(f"set_tone_{shop.id}_friendly")
        await bot.cb_set_tone(call)
        self.assertEqual(call.data, f"set_tone_{shop.id}_friendly")
        updated = await database.get_shop_by_id(shop.id)
        self.assertEqual(updated.tone_style, "friendly")
        call.message.edit_text.assert_awaited_once()

    async def test_foreign_user_cannot_change_or_delete_shop(self):
        shop = await database.create_shop(42, "wildberries", "Shop", wb_token="secret")
        call = callback(f"set_tone_{shop.id}_expert", user_id=999)
        await bot.cb_set_tone(call)
        updated = await database.get_shop_by_id(shop.id)
        self.assertEqual(updated.tone_style, "standard")
        self.assertTrue(call.answer.await_args.kwargs["show_alert"])

        call = callback(f"do_delete_{shop.id}", user_id=999)
        await bot.cb_do_delete(call)
        self.assertIsNotNone(await database.get_shop_by_id(shop.id))
        self.assertTrue(call.answer.await_args.kwargs["show_alert"])

    async def test_delete_shop_by_owner(self):
        shop = await database.create_shop(42, "wildberries", "Shop", wb_token="secret")
        call = callback(f"do_delete_{shop.id}", user_id=42)
        await bot.cb_do_delete(call)
        self.assertIsNone(await database.get_shop_by_id(shop.id))
        call.message.edit_text.assert_awaited()

    async def test_resume_all_respects_trial_limit(self):
        await self.create_user(42)
        await database.grant_subscription(42, "pro", 30)
        first = await database.create_shop(42, "wildberries", "First", wb_token="one")
        second = await database.create_shop(42, "ozon", "Second", ozon_client_id="id", ozon_api_key="two")
        await database.grant_subscription(42, "trial", 30)
        call = callback("resume_all_shops")
        await bot.cb_resume_all(call)
        shops = await database.get_user_shops(42)
        self.assertEqual(sum(shop.is_running for shop in shops), 1)
        self.assertIn("пропущено из-за лимита", call.answer.await_args.kwargs["text"])
        self.assertEqual((await database.get_shop_by_id(first.id)).is_running, True)
        self.assertEqual((await database.get_shop_by_id(second.id)).is_running, False)

    async def test_wb_token_connection_regression(self):
        call_state = SimpleNamespace(
            get_data=AsyncMock(return_value={"shop_name": "WB Shop", "chosen_marketplace": "wildberries"}),
            clear=AsyncMock(),
        )
        status_message = SimpleNamespace(edit_text=AsyncMock())
        message = SimpleNamespace(
            text="secret-token",
            from_user=SimpleNamespace(id=42),
            delete=AsyncMock(),
            answer=AsyncMock(side_effect=[status_message, None]),
        )
        with patch.object(bot.WildberriesClient, "verify_credentials", AsyncMock(return_value=(True, "OK"))):
            await bot.process_wb_token(message, call_state)
        shops = await database.get_user_shops(42)
        self.assertEqual(len(shops), 1)
        self.assertEqual(shops[0].marketplace, "wildberries")
        self.assertEqual(database.get_decrypted_wb_token(shops[0]), "secret-token")
        message.delete.assert_awaited_once()

    async def test_ozon_queue_publishes_logs_and_decrements_quota_once(self):
        shop = await self.create_running_shop(marketplace="ozon")
        user_before = await database.get_or_create_user(42)
        review = {
            "id": "ozon-review-1",
            "sku": 123,
            "rating": 5,
            "text": "",
            "published_at": datetime.now(timezone.utc).isoformat(),
            "photos_amount": 0,
            "videos_amount": 0,
            "comments_amount": 0,
            "status": "UNPROCESSED",
        }
        client = SimpleNamespace(
            get_unprocessed_reviews=AsyncMock(return_value={"reviews": [review], "has_next": False, "last_id": ""}),
            get_product_names=AsyncMock(return_value={123: "Product <One>"}),
            publish_reply=AsyncMock(),
        )
        ai = SimpleNamespace(generate_reply=AsyncMock(return_value="Generated reply <safe>"))
        telegram = SimpleNamespace(send_message=AsyncMock())
        with patch.object(bot, "OzonClient", return_value=client), patch.object(bot, "interruptible_sleep", AsyncMock(return_value=True)):
            result = await bot.process_ozon_queue(telegram, shop, ai)
            self.assertTrue(result)
            client.publish_reply.assert_awaited_once_with(review_id="ozon-review-1", text="Generated reply <safe>")
            self.assertIn("без текстового отзыва", ai.generate_reply.await_args.kwargs["review_text"])
            self.assertEqual(await database.get_shop_reply_count(shop.id), 1)
            self.assertTrue(await database.is_review_processed(shop.id, "ozon", "ozon-review-1"))
            user_after = await database.get_or_create_user(42)
            self.assertEqual(user_after.replies_quota_left, user_before.replies_quota_left - 1)

            await bot.process_ozon_queue(telegram, shop, ai)
            self.assertEqual(client.publish_reply.await_count, 1)

    async def test_wb_queue_counts_dict_video_and_decrements_quota(self):
        shop = await self.create_running_shop(marketplace="wildberries")
        feedback = {
            "id": "wb-review-1",
            "text": "Хорошо",
            "productValuation": 5,
            "createdDate": datetime.now(timezone.utc).isoformat(),
            "photoLinks": [],
            "video": {"url": "example"},
            "productDetails": {"productName": "WB Product"},
        }
        client = SimpleNamespace(
            get_unprocessed_feedbacks=AsyncMock(return_value=[feedback]),
            publish_reply=AsyncMock(),
        )
        ai = SimpleNamespace(generate_reply=AsyncMock(return_value="Reply"))
        telegram = SimpleNamespace(send_message=AsyncMock())
        with patch.object(bot, "WildberriesClient", return_value=client), patch.object(bot, "interruptible_sleep", AsyncMock(return_value=True)):
            result = await bot.process_wb_queue(telegram, shop, ai)
            self.assertTrue(result)
            self.assertEqual(ai.generate_reply.await_args.kwargs["videos_count"], 1)
            self.assertEqual(await database.get_shop_reply_count(shop.id), 1)
            self.assertEqual((await database.get_or_create_user(42)).replies_quota_left, 49)

    async def test_media_count_handles_numbers_lists_and_dictionaries(self):
        self.assertEqual(bot.media_count(3), 3)
        self.assertEqual(bot.media_count("2"), 2)
        self.assertEqual(bot.media_count([1, 2]), 2)
        self.assertEqual(bot.media_count({"url": "example"}), 1)
        self.assertEqual(bot.media_count(None), 0)

    async def test_date_filter_supports_naive_datetime(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        old = (datetime.now(timezone.utc) - timedelta(days=10)).replace(tzinfo=None).isoformat()
        self.assertTrue(bot.is_date_fresh(now, 3))
        self.assertFalse(bot.is_date_fresh(old, 3))

    async def test_signature_input_sends_new_view_instead_of_fake_callback(self):
        shop = await database.create_shop(42, "wildberries", "Shop", wb_token="secret")
        state = SimpleNamespace(
            get_data=AsyncMock(return_value={"target_shop_id": shop.id}),
            clear=AsyncMock(),
        )
        message = SimpleNamespace(
            text="С уважением, <Команда>",
            from_user=SimpleNamespace(id=42),
            answer=AsyncMock(),
        )
        with patch.object(bot, "send_shop_view", AsyncMock()) as send_view:
            await bot.process_new_signature(message, state)
        self.assertEqual((await database.get_shop_by_id(shop.id)).signature_text, "С уважением, <Команда>")
        send_view.assert_awaited_once_with(message, shop.id, 42)
