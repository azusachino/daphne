import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from daphne import bot as bot_module
from daphne.bot import extract_video_url, help_command, handle_video_link, audio_command
from daphne.config import load_config
from daphne.messages import PARSE_MODE_HTML
from daphne.rbac import RbacService, get_rbac_config_path


class TestRbac(unittest.TestCase):
    def setUp(self):
        self.config_data = {
            "public_commands": ["help"],
            "roles": {
                "admin": {"permissions": ["*"]},
                "standard": {"permissions": ["fix"]},
            },
            "users": {"123456789": "admin", "12345": "standard"},
            "chats": {"-1002058191932": "standard"},
        }
        self.rbac = RbacService(self.config_data)

    def test_public_command(self):
        self.assertTrue(self.rbac.check_access(111, 222, "help").is_allowed())
        self.assertTrue(self.rbac.check_access(111, 222, "/help").is_allowed())

    def test_admin_bypass(self):
        self.assertTrue(self.rbac.check_access(123456789, 999, "fix").is_allowed())

    def test_whitelist_enforcement(self):
        self.assertTrue(self.rbac.check_access(999, -1002058191932, "fix").is_denied())
        self.assertTrue(self.rbac.check_access(12345, 999, "fix").is_denied())

    def test_intersection_logic(self):
        self.assertTrue(
            self.rbac.check_access(12345, -1002058191932, "fix").is_allowed()
        )

    def test_missing_rbac_toml_fallback(self):
        with patch("os.path.exists", return_value=False):
            svc = RbacService.load("non_existent_file.toml")
            self.assertTrue(svc.check_access(111, 222, "help").is_allowed())
            self.assertTrue(svc.check_access(111, 222, "fix").is_denied())


class TestPathsAndXDG(unittest.TestCase):
    def tearDown(self):
        load_config.cache_clear()

    def test_rbac_cwd_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("os.path.exists", lambda path: path == "rbac.toml"):
                self.assertEqual(get_rbac_config_path(), "rbac.toml")

    def test_rbac_xdg_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("os.path.exists", return_value=False):
                self.assertEqual(
                    get_rbac_config_path(),
                    os.path.expanduser("~/.config/daphne/rbac.toml"),
                )


class TestApplicationBuilder(unittest.TestCase):
    def tearDown(self):
        load_config.cache_clear()

    def test_build_application_uses_default_bot_api(self):
        app = MagicMock()
        builder = MagicMock()
        builder.token.return_value = builder
        builder.job_queue.return_value = builder
        builder.build.return_value = app

        with (
            patch.dict(os.environ, {"DAPHNE_BOT_TOKEN": "token"}, clear=True),
            patch("daphne.bot.telegram_api_url", return_value=None),
            patch("daphne.bot.Application.builder", return_value=builder),
        ):
            built = bot_module.build_application()

        self.assertIs(built, app)
        builder.token.assert_called_once_with("token")
        builder.job_queue.assert_called_once_with(None)
        builder.base_url.assert_not_called()
        self.assertEqual(app.add_handler.call_count, 3)

    def test_build_application_uses_local_bot_api(self):
        app = MagicMock()
        builder = MagicMock()
        builder.token.return_value = builder
        builder.job_queue.return_value = builder
        builder.base_url.return_value = builder
        builder.base_file_url.return_value = builder
        builder.local_mode.return_value = builder
        builder.media_write_timeout.return_value = builder
        builder.build.return_value = app

        with (
            patch.dict(os.environ, {"DAPHNE_BOT_TOKEN": "token"}, clear=True),
            patch(
                "daphne.bot.telegram_api_url",
                return_value="http://telegram-bot-api:8081/",
            ),
            patch("daphne.bot.Application.builder", return_value=builder),
        ):
            built = bot_module.build_application()

        self.assertIs(built, app)
        builder.job_queue.assert_called_once_with(None)
        builder.base_url.assert_called_once_with("http://telegram-bot-api:8081/bot")
        builder.base_file_url.assert_called_once_with(
            "http://telegram-bot-api:8081/file/bot"
        )
        builder.local_mode.assert_called_once_with(True)
        builder.media_write_timeout.assert_called_once_with(7200)


class TestBotCommands(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.update = MagicMock()
        self.update.effective_user.id = 123456789
        self.update.effective_user.username = "haru"
        self.update.effective_user.full_name = "Haru"
        self.update.effective_chat.id = -1002058191932
        self.update.message.reply_text = AsyncMock()
        self.context = MagicMock()

    async def test_help_command(self):
        await help_command(self.update, self.context)
        self.update.message.reply_text.assert_called_once()
        text = self.update.message.reply_text.call_args[0][0]
        self.assertIn("Twitter/X", text)
        self.assertEqual(
            self.update.message.reply_text.call_args[1]["parse_mode"], PARSE_MODE_HTML
        )

    @patch("daphne.bot.check_access_and_reply", return_value=True)
    async def test_audio_command_no_link(self, mock_check):
        self.update.message.reply_to_message = None
        self.context.args = []
        await audio_command(self.update, self.context)
        self.update.message.reply_text.assert_called_once()
        text = self.update.message.reply_text.call_args[0][0]
        self.assertIn("Please provide a link", text)

    @patch("daphne.bot.check_access_and_reply", return_value=True)
    @patch("daphne.bot.video_upload_limit_mb", return_value=512)
    @patch("daphne.bot.fetch_video_metadata")
    @patch("daphne.bot.download_audio")
    @patch("os.path.getsize", return_value=1024)
    async def test_audio_command_success(
        self, mock_getsize, mock_download, mock_metadata, mock_limit, mock_check
    ):
        mock_metadata.return_value = {
            "title": "Audio Title",
            "uploader": "Uploader",
            "duration": 180,
            "webpage_url": "https://www.youtube.com/watch?v=abc",
        }
        mock_download.return_value = "/tmp/audio.mp3"

        self.update.message.reply_to_message = None
        self.context.args = ["https://www.youtube.com/watch?v=abc"]
        self.context.bot.send_audio = AsyncMock()
        self.context.bot.send_chat_action = AsyncMock()

        # Mock status message
        status_msg = MagicMock()
        status_msg.delete = AsyncMock()
        status_msg.edit_text = AsyncMock()
        self.update.message.reply_text.return_value = status_msg
        self.update.message.delete = AsyncMock()

        with patch("builtins.open", unittest.mock.mock_open()):
            await audio_command(self.update, self.context)

        self.context.bot.send_audio.assert_called_once()
        kwargs = self.context.bot.send_audio.call_args[1]
        self.assertEqual(kwargs["title"], "Audio Title")
        self.assertEqual(kwargs["performer"], "Uploader")
        self.assertEqual(kwargs["duration"], 180)
        self.update.message.delete.assert_called_once()


class TestVideoHandler(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.update = MagicMock()
        self.update.effective_user.username = "haru"
        self.update.effective_user.full_name = "Haru"
        self.update.effective_chat.id = -1002058191932
        self.update.message.reply_text = AsyncMock()
        self.update.message.delete = AsyncMock()
        self.status = MagicMock()
        self.status.delete = AsyncMock()
        self.status.edit_text = AsyncMock()
        self.update.message.reply_text.return_value = self.status
        self.context = MagicMock()
        self.context.bot.send_video = AsyncMock()

    def test_extract_video_url_sanitizes_bilibili(self):
        self.assertEqual(
            extract_video_url(
                "https://www.bilibili.com/video/BV1abc/?share_source=copy_web&p=1"
            ),
            "https://www.bilibili.com/video/BV1abc",
        )

    def test_extract_video_url_tiktok_and_instagram(self):
        self.assertEqual(
            extract_video_url("Check this: https://www.tiktok.com/@user/video/12345"),
            "https://www.tiktok.com/@user/video/12345",
        )
        self.assertEqual(
            extract_video_url("https://www.instagram.com/reel/C12345/"),
            "https://www.instagram.com/reel/C12345/",
        )

    @patch("daphne.bot.video_upload_limit_mb", return_value=512)
    @patch("daphne.bot.fetch_video_metadata")
    async def test_handle_video_link_over_configured_limit_sends_card(
        self, mock_metadata, mock_limit
    ):
        mock_metadata.return_value = {
            "title": "Huge Video",
            "uploader": "Uploader",
            "duration": 120,
            "webpage_url": "https://www.bilibili.com/video/BV1abc",
            "filesize_approx": 513 * 1024 * 1024,
        }

        await handle_video_link(
            self.update, self.context, "https://www.bilibili.com/video/BV1abc"
        )

        self.context.bot.send_video.assert_not_called()
        self.assertEqual(self.update.message.reply_text.call_count, 2)
        text = self.update.message.reply_text.call_args_list[-1][0][0]
        kwargs = self.update.message.reply_text.call_args_list[-1][1]
        self.assertIn("Video is over 512 MB", text)
        self.assertIn("Huge Video", text)
        self.assertEqual(
            kwargs["reply_markup"].inline_keyboard[0][0].text, "Open source"
        )
        self.update.message.delete.assert_called_once()

    @patch("daphne.bot.download_video")
    @patch("daphne.bot.fetch_video_metadata")
    async def test_handle_video_link_unknown_size_sends_card_without_download(
        self, mock_metadata, mock_download
    ):
        mock_metadata.return_value = {
            "title": "Unknown Size Video",
            "uploader": "Uploader",
            "duration": 90,
            "webpage_url": "https://www.bilibili.com/video/BV1abc",
        }

        await handle_video_link(
            self.update, self.context, "https://www.bilibili.com/video/BV1abc"
        )

        mock_download.assert_not_called()
        self.context.bot.send_video.assert_not_called()
        self.assertEqual(self.update.message.reply_text.call_count, 2)
        text = self.update.message.reply_text.call_args_list[-1][0][0]
        kwargs = self.update.message.reply_text.call_args_list[-1][1]
        self.assertIn("Video size is unknown", text)
        self.assertIn("Unknown Size Video", text)
        button = kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.text, "Open source")
        self.assertEqual(button.url, "https://www.bilibili.com/video/BV1abc")
        self.update.message.delete.assert_called_once()


class TestMainInit(unittest.TestCase):
    @patch("os.path.exists", return_value=False)
    @patch("os.makedirs")
    @patch("builtins.open")
    def test_run_init(self, mock_open, mock_makedirs, mock_exists):
        from daphne import main

        # Test global mode
        main.run_init(local=False)
        mock_makedirs.assert_called_with(os.path.expanduser("~/.config/daphne"), exist_ok=True)

        # Test local mode
        mock_makedirs.reset_mock()
        main.run_init(local=True)
        mock_makedirs.assert_called_with("./config", exist_ok=True)

        write_calls = [
            call for call in mock_open.call_args_list
            if (len(call.args) > 1 and "w" in call.args[1]) or "w" in call.kwargs.get("mode", "")
        ]
        # 2 files written for local=False, 2 files written for local=True
        self.assertEqual(len(write_calls), 4)


if __name__ == "__main__":
    unittest.main()
