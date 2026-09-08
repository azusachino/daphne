import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from aiogram import Bot
from aiogram.types import Chat, Message, Update, User

from daphne import bot as bot_module
from daphne.tg import TelegramMessage, build_bot


class TestTelegramBoundary(unittest.IsolatedAsyncioTestCase):
    async def test_reply_text_translates_preview_option_and_wraps_result(self):
        raw_message = MagicMock()
        raw_message.answer = AsyncMock(return_value=MagicMock())
        message = TelegramMessage(raw_message)

        result = await message.reply_text("hello", disable_web_page_preview=True)

        raw_message.answer.assert_awaited_once()
        kwargs = raw_message.answer.await_args.kwargs
        self.assertTrue(kwargs["link_preview_options"].is_disabled)
        self.assertIsInstance(result, TelegramMessage)

    def test_build_bot_uses_single_local_api_base(self):
        raw_bot = MagicMock()
        session = MagicMock()
        api = MagicMock()
        with (
            patch("daphne.tg.Bot", return_value=raw_bot) as bot_class,
            patch("daphne.tg.AiohttpSession", return_value=session) as session_class,
            patch(
                "daphne.tg.TelegramAPIServer.from_base", return_value=api
            ) as from_base,
            patch("daphne.tg.telegram_api_url", return_value="http://telegram:8081/"),
        ):
            bot = build_bot("token")

        from_base.assert_called_once_with("http://telegram:8081", is_local=True)
        session_class.assert_called_once()
        self.assertEqual(session_class.call_args.kwargs["api"], api)
        bot_class.assert_called_once()
        self.assertIs(bot.raw, raw_bot)

    async def test_dispatcher_feeds_native_update_to_compatibility_handler(self):
        message = Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=7, type="private"),
            from_user=User(id=9, is_bot=False, first_name="Test"),
            text="/start",
        )
        raw_bot = Bot("123:token")
        with (
            patch.object(bot_module, "start_command", new_callable=AsyncMock) as start,
            patch.object(bot_module, "log_update", new_callable=AsyncMock),
        ):
            await bot_module.build_dispatcher().feed_update(
                raw_bot, Update(update_id=1, message=message)
            )

        start.assert_awaited_once()
        adapted_update = start.await_args.args[0]
        self.assertEqual(adapted_update.message.text, "/start")
        await raw_bot.session.close()


if __name__ == "__main__":
    unittest.main()
