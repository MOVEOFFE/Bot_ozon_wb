from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import admin_router
from tests.common import DatabaseTestCase


class AdminRouterTests(DatabaseTestCase):
    async def test_admin_broadcast_escapes_text_and_uses_asyncio(self):
        message = SimpleNamespace(
            text="Важно <b>для всех</b>",
            from_user=SimpleNamespace(id=admin_router.ADMIN_IDS[0]),
            answer=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock())),
        )
        state = SimpleNamespace(clear=AsyncMock())
        telegram = SimpleNamespace(send_message=AsyncMock())
        with patch.object(admin_router, "get_all_user_ids", AsyncMock(return_value=[1, 2])), patch.object(
            admin_router.asyncio, "sleep", AsyncMock()
        ):
            await admin_router.process_admin_broadcast(message, state, telegram)
        self.assertEqual(telegram.send_message.await_count, 2)
        for call in telegram.send_message.await_args_list:
            self.assertEqual(call.args[1], "Важно &lt;b&gt;для всех&lt;/b&gt;")
            self.assertEqual(call.kwargs["parse_mode"], "HTML")

    async def test_admin_to_seller_callback_is_handled(self):
        call = SimpleNamespace(
            from_user=SimpleNamespace(id=admin_router.ADMIN_IDS[0]),
            message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
            answer=AsyncMock(),
        )
        state = SimpleNamespace(clear=AsyncMock())
        with patch("bot.get_main_dashboard_view", AsyncMock(return_value=("Dashboard", None))):
            await admin_router.cb_admin_to_seller(call, state)
        call.message.edit_text.assert_awaited_once_with("Dashboard", reply_markup=None, parse_mode="HTML")
        state.clear.assert_awaited_once()
