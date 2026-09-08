import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from daphne import main as main_module


class TestBotLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_run_bot_closes_session_if_startup_fails(self) -> None:
        bot = MagicMock()
        bot.session.close = AsyncMock()
        dispatcher = MagicMock()
        dispatcher.start_polling = AsyncMock()

        with (
            patch.dict(os.environ, {main_module.ENV_BOT_TOKEN: "token"}),
            patch.object(main_module, "build_bot", return_value=bot),
            patch.object(main_module, "build_dispatcher", return_value=dispatcher),
            patch.object(
                main_module,
                "register_bot_commands",
                new=AsyncMock(side_effect=RuntimeError("registration failed")),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "registration failed"):
                await main_module.run_bot()

        bot.session.close.assert_awaited_once_with()
        dispatcher.start_polling.assert_not_awaited()
