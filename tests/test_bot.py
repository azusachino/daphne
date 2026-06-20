import os
import unittest
import tempfile
import shutil
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from daphne.rbac import RbacService, get_rbac_config_path
from daphne.database import get_db_path
from daphne.bot import help_command, rate_command
from daphne import main


class TestRbac(unittest.TestCase):
    def setUp(self):
        # Sample configuration dict for RBAC
        self.config_data = {
            "public_commands": ["help", "rate"],
            "roles": {
                "admin": {"permissions": ["*"]},
                "standard": {"permissions": ["rate", "dl"]},
            },
            "users": {"123456789": "admin", "12345": "standard", "67890": "restricted"},
            "chats": {"-1002058191932": "standard", "88888": "restricted"},
        }
        self.rbac = RbacService(self.config_data)

    def test_public_command(self):
        # Public commands should be allowed for anyone initially
        res = self.rbac.check_access(user_id=111, chat_id=222, command="help")
        self.assertTrue(res.is_allowed())

        # Test command with leading slash
        res = self.rbac.check_access(user_id=111, chat_id=222, command="/help")
        self.assertTrue(res.is_allowed())

    def test_public_command_rate_limit(self):
        # 10 calls within 60 seconds are allowed, 11th is limited
        for i in range(10):
            res = self.rbac.check_access(user_id=111, chat_id=222, command="help")
            self.assertTrue(res.is_allowed())

        # 11th call
        res = self.rbac.check_access(user_id=111, chat_id=222, command="help")
        self.assertTrue(res.is_rate_limited())

        # Test rate limit reset after 60s
        with patch(
            "time.time", return_value=self.rbac.rate_limiter[(111, "help")][1] + 61.0
        ):
            res = self.rbac.check_access(user_id=111, chat_id=222, command="help")
            self.assertTrue(res.is_allowed())

    def test_admin_bypass(self):
        # Admin can access non-public command or anything
        res = self.rbac.check_access(
            user_id=123456789, chat_id=999, command="restricted_cmd"
        )
        self.assertTrue(res.is_allowed())

    def test_whitelist_enforcement(self):
        # User not in whitelist
        res = self.rbac.check_access(user_id=999, chat_id=-1002058191932, command="dl")
        self.assertTrue(res.is_denied())
        self.assertEqual(res.reason, "User not in whitelist")

        # Chat not in whitelist
        res = self.rbac.check_access(user_id=12345, chat_id=999, command="dl")
        self.assertTrue(res.is_denied())
        self.assertEqual(res.reason, "Chat not in whitelist")

    def test_intersection_logic(self):
        # Both user and chat allow 'dl'
        res = self.rbac.check_access(
            user_id=12345, chat_id=-1002058191932, command="dl"
        )
        self.assertTrue(res.is_allowed())

        # User allows 'dl', but chat is restricted (has role, but role lacks 'dl')
        res = self.rbac.check_access(user_id=12345, chat_id=88888, command="dl")
        self.assertTrue(res.is_denied())
        self.assertEqual(res.reason, "Command not allowed by user or chat role")

    def test_missing_rbac_toml_fallback(self):
        # RbacService loads defaults gracefully if file doesn't exist
        with patch("os.path.exists", return_value=False):
            svc = RbacService.load("non_existent_file.toml")
            # Defaults should allow 'rate' and 'help'
            self.assertTrue(svc.check_access(111, 222, "rate").is_allowed())
            self.assertTrue(svc.check_access(111, 222, "help").is_allowed())
            # Non-public commands should be denied (since no users/chats in fallback configuration)
            self.assertTrue(svc.check_access(111, 222, "other").is_denied())


class TestPathsAndXDG(unittest.TestCase):
    def test_database_url_env(self):
        with patch.dict(os.environ, {"DAPHNE_DATABASE_URL": "sqlite:///my_db.db"}):
            self.assertEqual(get_db_path(), "my_db.db")

        with patch.dict(os.environ, {"DAPHNE_DATABASE_URL": "custom_path.db"}):
            self.assertEqual(get_db_path(), "custom_path.db")

    def test_database_cwd_fallback(self):
        # If DAPHNE_DATABASE_URL is not set, but daphne.db exists in CWD
        with patch.dict(os.environ, {}, clear=True):
            with patch("os.path.exists", lambda path: path == "daphne.db"):
                self.assertEqual(get_db_path(), "daphne.db")

    def test_database_xdg_fallback(self):
        # If DAPHNE_DATABASE_URL is not set, and daphne.db not in CWD, fallback to local/share
        with patch.dict(os.environ, {}, clear=True):
            with patch("os.path.exists", return_value=False):
                with patch("os.makedirs") as mock_makedirs:
                    path = get_db_path()
                    expected = os.path.expanduser("~/.local/share/daphne/daphne.db")
                    self.assertEqual(path, expected)
                    mock_makedirs.assert_called_once_with(
                        os.path.dirname(expected), exist_ok=True
                    )

    def test_rbac_path_env(self):
        with patch.dict(os.environ, {"DAPHNE_RBAC_CONFIG_PATH": "custom_rbac.toml"}):
            self.assertEqual(get_rbac_config_path(), "custom_rbac.toml")

    def test_rbac_cwd_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("os.path.exists", lambda path: path == "rbac.toml"):
                self.assertEqual(get_rbac_config_path(), "rbac.toml")

    def test_rbac_xdg_fallback(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("os.path.exists", return_value=False):
                path = get_rbac_config_path()
                expected = os.path.expanduser("~/.config/daphne/rbac.toml")
                self.assertEqual(path, expected)


class TestBotCommands(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test_bot.db")

        # Patch database path resolution to use our temp DB path
        self.get_db_path_patcher = patch(
            "daphne.bot.get_db_path", return_value=self.db_path
        )
        self.get_db_path_patcher.start()

        # Setup mock update and context
        self.update = MagicMock()
        self.update.effective_user.id = 123456789  # admin to pass RBAC
        self.update.effective_chat.id = -1002058191932
        self.update.message.reply_text = AsyncMock()
        self.context = MagicMock()

    def tearDown(self):
        self.get_db_path_patcher.stop()
        shutil.rmtree(self.test_dir)

    async def test_help_command(self):
        self.update.message.text = "/help"
        await help_command(self.update, self.context)

        self.update.message.reply_text.assert_called_once()
        reply = self.update.message.reply_text.call_args[0][0]
        self.assertIn("daphne - Wise Exchange Rate Bot", reply)
        self.assertIn("/rate - Show the latest JPY/CNY exchange rate", reply)

    @patch("daphne.bot.get_latest_exchange_rate")
    async def test_rate_command_no_args_stored(self, mock_get_latest):
        # Database returns a stored exchange rate record
        mock_get_latest.return_value = {
            "rate": 0.0456,
            "fetched_at": datetime.datetime(
                2026, 6, 20, 12, 0, 0, tzinfo=datetime.timezone.utc
            ),
        }

        self.update.message.text = "/rate"
        await rate_command(self.update, self.context)

        self.update.message.reply_text.assert_called_once()
        reply = self.update.message.reply_text.call_args[0][0]
        # JPY/CNY: {10000.0 * rate:.3f} (×10000)\nFetched: {fetched_at:%Y-%m-%d %H:%M}
        # 10000.0 * 0.0456 = 456.000
        self.assertIn("JPY/CNY: 456.000 (×10000)", reply)
        self.assertIn("Fetched: 2026-06-20", reply)

    @patch("daphne.bot.get_latest_exchange_rate", return_value=None)
    @patch("daphne.bot.fetch_rate")
    @patch("daphne.bot.save_exchange_rate")
    async def test_rate_command_no_args_live_fallback(
        self, mock_save, mock_fetch, mock_get_latest
    ):
        mock_fetch.return_value = 0.0457

        self.update.message.text = "/rate"
        await rate_command(self.update, self.context)

        mock_fetch.assert_called_once_with("JPY", "CNY")
        mock_save.assert_called_once()
        self.update.message.reply_text.assert_called_once()
        reply = self.update.message.reply_text.call_args[0][0]
        self.assertIn("JPY/CNY: 457.000 (×10000)", reply)

    @patch("daphne.bot.get_exchange_rate_history")
    async def test_rate_command_history(self, mock_history):
        mock_history.return_value = [
            {
                "source_currency": "JPY",
                "target_currency": "CNY",
                "rate": 0.0456,
                "fetched_at": datetime.datetime(
                    2026, 6, 20, 12, 0, 0, tzinfo=datetime.timezone.utc
                ),
            },
            {
                "source_currency": "JPY",
                "target_currency": "CNY",
                "rate": 0.0455,
                "fetched_at": datetime.datetime(
                    2026, 6, 20, 11, 0, 0, tzinfo=datetime.timezone.utc
                ),
            },
        ]

        self.update.message.text = "/rate history 5"
        await rate_command(self.update, self.context)

        self.update.message.reply_text.assert_called_once()
        reply = self.update.message.reply_text.call_args[0][0]
        self.assertIn("JPY/CNY history (×10000):", reply)
        self.assertIn("456.000", reply)
        self.assertIn("455.000", reply)

    @patch("daphne.bot.get_exchange_rate_history", return_value=[])
    async def test_rate_command_history_empty(self, mock_history):
        self.update.message.text = "/rate history"
        await rate_command(self.update, self.context)
        self.update.message.reply_text.assert_called_once_with(
            "No rate history available."
        )

    @patch("daphne.bot.fetch_rate")
    async def test_rate_command_on_demand_success(self, mock_fetch):
        mock_fetch.return_value = 161.35

        self.update.message.text = "/rate USD JPY"
        await rate_command(self.update, self.context)

        mock_fetch.assert_called_once_with("USD", "JPY")
        self.update.message.reply_text.assert_called_once_with("USD/JPY: 161.350000")

    @patch("daphne.bot.fetch_rate", side_effect=Exception("Wise down"))
    async def test_rate_command_on_demand_fail(self, mock_fetch):
        self.update.message.text = "/rate USD JPY"
        await rate_command(self.update, self.context)
        self.update.message.reply_text.assert_called_once_with(
            "Could not fetch rate for USD/JPY"
        )

    async def test_rate_command_invalid_args(self):
        self.update.message.text = "/rate a b c"
        await rate_command(self.update, self.context)
        self.update.message.reply_text.assert_called_once()
        reply = self.update.message.reply_text.call_args[0][0]
        self.assertIn("Invalid arguments", reply)


class TestMainInit(unittest.TestCase):
    @patch("os.makedirs")
    @patch("builtins.open")
    @patch("shutil.which", return_value="/usr/local/bin/daphne")
    def test_run_init(self, mock_which, mock_open, mock_makedirs):
        main.run_init()

        # Verify creating folders
        self.assertEqual(mock_makedirs.call_count, 2)
        # Verify writing rbac.toml, daphne.env, and daphne.service
        self.assertEqual(mock_open.call_count, 3)


if __name__ == "__main__":
    unittest.main()
