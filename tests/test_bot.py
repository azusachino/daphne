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
                "standard": {"permissions": ["convert_link"]},
            },
            "users": {"123456789": "admin", "12345": "standard"},
            "chats": {"-1002058191932": "standard"},
        }
        self.rbac = RbacService(self.config_data)

    def test_public_command(self):
        self.assertTrue(self.rbac.check_access(111, 222, "help").is_allowed())
        self.assertTrue(self.rbac.check_access(111, 222, "/help").is_allowed())

    def test_admin_bypass(self):
        self.assertTrue(
            self.rbac.check_access(123456789, 999, "convert_link").is_allowed()
        )

    def test_whitelist_enforcement(self):
        # Non-whitelisted user in non-whitelisted chat is denied
        self.assertTrue(self.rbac.check_access(999, 999, "convert_link").is_denied())
        # Whitelisted user in non-whitelisted chat is denied
        self.assertTrue(self.rbac.check_access(12345, 999, "convert_link").is_denied())
        # Non-whitelisted user in whitelisted chat requesting command not allowed by chat role is denied
        self.assertTrue(
            self.rbac.check_access(999, -1002058191932, "extract_audio").is_denied()
        )
        # Non-whitelisted user in whitelisted chat requesting command allowed by chat role is allowed (fallback)
        self.assertTrue(
            self.rbac.check_access(999, -1002058191932, "convert_link").is_allowed()
        )

    def test_intersection_logic(self):
        self.assertTrue(
            self.rbac.check_access(12345, -1002058191932, "convert_link").is_allowed()
        )

    def test_missing_rbac_toml_fallback(self):
        with patch("os.path.exists", return_value=False):
            svc = RbacService.load("non_existent_file.toml")
            self.assertTrue(svc.check_access(111, 222, "help").is_allowed())
            self.assertTrue(svc.check_access(111, 222, "convert_link").is_denied())


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
        self.assertEqual(app.add_handler.call_count, 4)

    def test_build_application_uses_local_bot_api(self):
        app = MagicMock()
        builder = MagicMock()
        builder.token.return_value = builder
        builder.job_queue.return_value = builder
        builder.base_url.return_value = builder
        builder.base_file_url.return_value = builder
        builder.local_mode.return_value = builder
        builder.media_write_timeout.return_value = builder
        builder.read_timeout.return_value = builder
        builder.connect_timeout.return_value = builder
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
        builder.read_timeout.assert_called_once_with(7200)
        builder.connect_timeout.assert_called_once_with(30.0)


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

    @patch("daphne.bot.check_access_and_reply", return_value=True)
    @patch("daphne.bot.video_upload_limit_mb", return_value=512)
    @patch("daphne.bot.fetch_video_metadata")
    @patch("daphne.bot.download_audio")
    @patch("os.path.getsize", return_value=1024)
    async def test_audio_command_success_schemeless(
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
        self.context.args = ["youtube.com/watch?v=abc"]
        self.context.bot.send_audio = AsyncMock()
        self.context.bot.send_chat_action = AsyncMock()

        status_msg = MagicMock()
        status_msg.delete = AsyncMock()
        status_msg.edit_text = AsyncMock()
        self.update.message.reply_text.return_value = status_msg
        self.update.message.delete = AsyncMock()

        with patch("builtins.open", unittest.mock.mock_open()):
            await audio_command(self.update, self.context)

        self.context.bot.send_audio.assert_called_once()
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
        self.assertEqual(
            extract_video_url("bilibili.com/video/BV1abc/?share_source=copy_web&p=1"),
            "https://www.bilibili.com/video/BV1abc",
        )

    def test_extract_instagram_and_tiktok_links(self):
        from daphne.instagram import contains_instagram_link, extract_instagram_link
        from daphne.tiktok import contains_tiktok_link, extract_tiktok_link

        # Instagram
        self.assertTrue(
            contains_instagram_link(
                "Check this: https://www.instagram.com/p/DXS4QzZAqxB/?hl=en"
            )
        )
        self.assertEqual(
            extract_instagram_link(
                "Check this: https://www.instagram.com/p/DXS4QzZAqxB/?hl=en"
            ),
            "https://www.instagram.com/p/DXS4QzZAqxB",
        )

        # TikTok
        self.assertTrue(
            contains_tiktok_link("Check this: https://www.tiktok.com/@user/video/12345")
        )
        self.assertEqual(
            extract_tiktok_link("Check this: https://www.tiktok.com/@user/video/12345"),
            "https://www.tiktok.com/@user/video/12345",
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
            "url": "https://direct-url.com/file.mp4",
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
        button_download = kwargs["reply_markup"].inline_keyboard[0][0]
        button_source = kwargs["reply_markup"].inline_keyboard[0][1]
        self.assertEqual(button_download.text, "Download Video")
        self.assertTrue(button_download.callback_data.startswith("dl:"))
        self.assertEqual(button_source.text, "Open source")
        self.assertEqual(button_source.url, "https://www.bilibili.com/video/BV1abc")
        self.update.message.delete.assert_called_once()

    @patch("daphne.bot.video_upload_limit_mb", return_value=512)
    @patch("daphne.bot.probe_video_dimensions", return_value=(1920, 1080, 90))
    @patch("daphne.bot.download_video", return_value="/tmp/video.mp4")
    @patch("os.path.getsize", return_value=1024 * 1024)
    @patch("daphne.bot.fetch_video_metadata")
    async def test_handle_video_link_unknown_size_downloads_and_sends_video(
        self, mock_metadata, mock_getsize, mock_download, mock_probe, mock_limit
    ):
        mock_metadata.return_value = {
            "title": "Unknown Size Video",
            "uploader": "Uploader",
            "duration": 90,
            "webpage_url": "https://www.bilibili.com/video/BV1abc",
        }

        with patch("builtins.open", unittest.mock.mock_open()):
            await handle_video_link(
                self.update, self.context, "https://www.bilibili.com/video/BV1abc"
            )

        mock_download.assert_called_once()
        self.context.bot.send_video.assert_called_once()
        kwargs = self.context.bot.send_video.call_args[1]
        self.assertEqual(kwargs["width"], 1920)
        self.assertEqual(kwargs["height"], 1080)
        self.assertEqual(kwargs["duration"], 90)
        self.update.message.delete.assert_called_once()

    def test_preprocess_text_links(self):
        from daphne.bot import preprocess_text_links

        # Without scheme
        self.assertEqual(
            preprocess_text_links("bilibili.com/video/BV1tEEz6uELi"),
            "https://bilibili.com/video/BV1tEEz6uELi",
        )
        # With scheme (http)
        self.assertEqual(
            preprocess_text_links("http://youtube.com/watch?v=123"),
            "http://youtube.com/watch?v=123",
        )
        # With scheme (https)
        self.assertEqual(
            preprocess_text_links("https://x.com/user/status/123"),
            "https://x.com/user/status/123",
        )
        # Multiple links and normal text
        self.assertEqual(
            preprocess_text_links(
                "Cool video at bilibili.com/video/BV1tEEz6uELi and youtube.com/watch?v=123"
            ),
            "Cool video at https://bilibili.com/video/BV1tEEz6uELi and https://youtube.com/watch?v=123",
        )
        # Email address (should not match because of lookbehind)
        self.assertEqual(
            preprocess_text_links("contact uploader@bilibili.com"),
            "contact uploader@bilibili.com",
        )


class TestMainInit(unittest.TestCase):
    @patch("os.path.exists", return_value=False)
    @patch("os.makedirs")
    @patch("builtins.open")
    def test_run_init(self, mock_open, mock_makedirs, mock_exists):
        from daphne import main

        # Test global mode
        main.run_init(local=False)
        mock_makedirs.assert_called_with(
            os.path.expanduser("~/.config/daphne"), exist_ok=True
        )

        # Test local mode
        mock_makedirs.reset_mock()
        main.run_init(local=True)
        mock_makedirs.assert_called_with("./config", exist_ok=True)

        write_calls = [
            call
            for call in mock_open.call_args_list
            if (len(call.args) > 1 and "w" in call.args[1])
            or "w" in call.kwargs.get("mode", "")
        ]
        # 2 files written for local=False, 2 files written for local=True
        self.assertEqual(len(write_calls), 4)


class TestVideoPermissionsAndQuota(unittest.TestCase):
    def setUp(self):
        self.config_data = {
            "public_commands": ["help"],
            "help_limit": 2,
            "convert_link_limit": 2,
            "extract_audio_limit": 2,
            "download_video_limit": 2,
            "preview_video_limit": 3,
            "roles": {
                "admin": {"permissions": ["*"]},
                "downloader_only": {"permissions": ["download_video"]},
                "preview_only": {"permissions": ["preview_video"]},
                "both": {
                    "permissions": [
                        "download_video",
                        "preview_video",
                        "convert_link",
                        "extract_audio",
                    ]
                },
            },
            "users": {
                "1": "admin",
                "2": "downloader_only",
                "3": "preview_only",
                "4": "both",
            },
            "chats": {
                "-1002058191932": "both",
                "-1001111111111": "downloader_only",
            },
        }
        self.rbac = RbacService(self.config_data)

    def test_quota_limits(self):
        # Admin is always allowed and does not consume quota
        for _ in range(5):
            self.assertTrue(
                self.rbac.check_access(1, -1002058191932, "download_video").is_allowed()
            )

        # User 2 (downloader_only): limit is 2
        self.assertTrue(
            self.rbac.check_access(2, -1002058191932, "download_video").is_allowed()
        )  # 1
        self.assertTrue(
            self.rbac.check_access(2, -1002058191932, "download_video").is_allowed()
        )  # 2
        self.assertTrue(
            self.rbac.check_access(
                2, -1002058191932, "download_video"
            ).is_rate_limited()
        )  # Exceeded

        # User 3 (preview_only): limit is 3
        self.assertTrue(
            self.rbac.check_access(3, -1002058191932, "preview_video").is_allowed()
        )  # 1
        self.assertTrue(
            self.rbac.check_access(3, -1002058191932, "preview_video").is_allowed()
        )  # 2
        self.assertTrue(
            self.rbac.check_access(3, -1002058191932, "preview_video").is_allowed()
        )  # 3
        self.assertTrue(
            self.rbac.check_access(3, -1002058191932, "preview_video").is_rate_limited()
        )  # Exceeded

        # User 4 (both): convert_link limit is 2
        self.assertTrue(
            self.rbac.check_access(4, -1002058191932, "convert_link").is_allowed()
        )  # 1
        self.assertTrue(
            self.rbac.check_access(4, -1002058191932, "convert_link").is_allowed()
        )  # 2
        self.assertTrue(
            self.rbac.check_access(4, -1002058191932, "convert_link").is_rate_limited()
        )  # Exceeded

        # Public command (help) hourly quota is 2
        # User 4 has limit 2 for help
        self.assertTrue(
            self.rbac.check_access(4, -1002058191932, "help").is_allowed()
        )  # 1
        self.assertTrue(
            self.rbac.check_access(4, -1002058191932, "help").is_allowed()
        )  # 2
        self.assertTrue(
            self.rbac.check_access(4, -1002058191932, "help").is_rate_limited()
        )  # Exceeded


class TestCallbackAndMessageHandler(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.update = MagicMock()
        self.update.effective_user.id = 12345
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

    @patch("daphne.bot.rbac_service")
    @patch("daphne.bot.fetch_video_metadata")
    @patch("daphne.bot.handle_video_link")
    @patch("daphne.bot.send_video_card")
    async def test_media_message_handler_preview_path(
        self, mock_send_card, mock_handle_video, mock_fetch_metadata, mock_rbac
    ):
        mock_access = MagicMock()
        mock_access.is_allowed.return_value = True
        mock_rbac.check_access.return_value = mock_access

        mock_fetch_metadata.return_value = {
            "title": "Small Video",
            "uploader": "Uploader",
            "filesize": 10 * 1024 * 1024,
            "webpage_url": "https://www.bilibili.com/video/BV1abc",
        }

        self.update.message.text = "https://www.bilibili.com/video/BV1abc"

        await bot_module.media_message_handler(self.update, self.context)

        mock_rbac.check_access.assert_any_call(12345, -1002058191932, "fetch_metadata")
        mock_rbac.check_access.assert_any_call(
            12345, -1002058191932, "preview_video", dry_run=True
        )
        mock_rbac.check_access.assert_any_call(12345, -1002058191932, "preview_video")

        mock_handle_video.assert_called_once_with(
            self.update,
            self.context,
            "https://www.bilibili.com/video/BV1abc",
            custom_metadata=mock_fetch_metadata.return_value,
        )
        mock_send_card.assert_not_called()

    @patch("daphne.bot.rbac_service")
    @patch("daphne.bot.fetch_video_metadata")
    @patch("daphne.bot.handle_video_link")
    @patch("daphne.bot.send_video_card")
    async def test_media_message_handler_card_path(
        self, mock_send_card, mock_handle_video, mock_fetch_metadata, mock_rbac
    ):
        mock_fetch_allowed = MagicMock()
        mock_fetch_allowed.is_allowed.return_value = True

        mock_preview_denied = MagicMock()
        mock_preview_denied.is_allowed.return_value = False
        mock_preview_denied.is_rate_limited.return_value = False

        def mock_check_access(user_id, chat_id, cmd, dry_run=False):
            if cmd == "fetch_metadata":
                return mock_fetch_allowed
            if cmd == "preview_video":
                return mock_preview_denied
            return MagicMock()

        mock_rbac.check_access.side_effect = mock_check_access

        mock_fetch_metadata.return_value = {
            "title": "Small Video No Permission",
            "uploader": "Uploader",
            "filesize": 10 * 1024 * 1024,
            "webpage_url": "https://www.bilibili.com/video/BV1abc",
        }

        self.update.message.text = "https://www.bilibili.com/video/BV1abc"

        await bot_module.media_message_handler(self.update, self.context)

        mock_rbac.check_access.assert_any_call(12345, -1002058191932, "fetch_metadata")
        mock_rbac.check_access.assert_any_call(
            12345, -1002058191932, "preview_video", dry_run=True
        )

        mock_handle_video.assert_not_called()
        mock_send_card.assert_called_once_with(
            self.update,
            "https://www.bilibili.com/video/BV1abc",
            mock_fetch_metadata.return_value,
            bot_module.sender_attribution(self.update.effective_user),
            "Video Details",
        )

    @patch("daphne.bot.rbac_service")
    @patch("daphne.bot.fetch_video_metadata")
    @patch("daphne.bot.download_video")
    @patch("daphne.bot.probe_video_dimensions", return_value=(1280, 720, 60))
    @patch("os.path.getsize", return_value=15 * 1024 * 1024)
    async def test_download_button_callback_success(
        self, mock_getsize, mock_probe, mock_download, mock_fetch_metadata, mock_rbac
    ):
        mock_access = MagicMock()
        mock_access.is_allowed.return_value = True
        mock_rbac.check_access.return_value = mock_access

        mock_fetch_metadata.return_value = {
            "title": "Downloaded Video",
            "uploader": "Uploader",
            "filesize": 15 * 1024 * 1024,
            "webpage_url": "https://www.bilibili.com/video/BV1abc",
        }
        mock_download.return_value = "/tmp/video.mp4"

        bot_module.CALLBACK_URL_CACHE["testshort"] = (
            "https://www.bilibili.com/video/BV1abc"
        )

        query = AsyncMock()
        query.data = "dl:testshort"
        query.from_user.id = 12345
        query.message.chat.id = -1002058191932
        query.edit_message_text = AsyncMock()
        query.message.delete = AsyncMock()
        query.message.reply_to_message = AsyncMock()

        callback_update = MagicMock()
        callback_update.callback_query = query

        with patch("builtins.open", unittest.mock.mock_open()):
            await bot_module.download_button_callback(callback_update, self.context)

        query.answer.assert_called_once()
        mock_rbac.check_access.assert_called_once_with(
            12345, -1002058191932, "download_video"
        )
        mock_download.assert_called_once()
        self.context.bot.send_video.assert_called_once()
        query.message.delete.assert_called_once()
        query.message.reply_to_message.delete.assert_called_once()


if __name__ == "__main__":
    unittest.main()
