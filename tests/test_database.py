import os
import unittest
import tempfile
import datetime
import shutil
from database import (
    get_db_path,
    init_db,
    save_exchange_rate,
    get_latest_exchange_rate,
    get_exchange_rate_history,
)


class TestDatabase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Create a temporary directory for test databases
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test_daphne.db")

    def tearDown(self):
        # Clean up temporary directory
        shutil.rmtree(self.test_dir)

    def test_get_db_path_default(self):
        # Test default fallback path
        orig_env = os.environ.get("DATABASE_URL")
        try:
            if "DATABASE_URL" in os.environ:
                del os.environ["DATABASE_URL"]

            # If daphne.db is not present, should default to ~/.local/share/daphne/daphne.db
            expected_fallback = os.path.expanduser("~/.local/share/daphne/daphne.db")

            # We mock the existence check to test both paths
            import unittest.mock as mock

            with mock.patch("os.path.exists", return_value=False):
                self.assertEqual(get_db_path(), expected_fallback)

            with mock.patch("os.path.exists", return_value=True):
                self.assertEqual(get_db_path(), "daphne.db")
        finally:
            if orig_env is not None:
                os.environ["DATABASE_URL"] = orig_env

    def test_get_db_path_with_env(self):
        # Test DATABASE_URL without sqlite:/// prefix
        orig_env = os.environ.get("DATABASE_URL")
        try:
            os.environ["DATABASE_URL"] = "custom_path.db"
            self.assertEqual(get_db_path(), "custom_path.db")
        finally:
            if orig_env is not None:
                os.environ["DATABASE_URL"] = orig_env

    def test_get_db_path_with_sqlite_prefix(self):
        # Test DATABASE_URL with sqlite:/// prefix
        orig_env = os.environ.get("DATABASE_URL")
        try:
            os.environ["DATABASE_URL"] = "sqlite:///custom_path.db"
            self.assertEqual(get_db_path(), "custom_path.db")

            # Test absolute path with sqlite:///
            os.environ["DATABASE_URL"] = "sqlite:////tmp/custom_path.db"
            self.assertEqual(get_db_path(), "/tmp/custom_path.db")
        finally:
            if orig_env is not None:
                os.environ["DATABASE_URL"] = orig_env

    async def test_init_db(self):
        # Test that init_db creates table and indexes
        await init_db(self.db_path)
        self.assertTrue(os.path.exists(self.db_path))

        # Verify schema by checking if table exists
        import aiosqlite

        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='exchange_rates';"
            ) as cursor:
                row = await cursor.fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row[0], "exchange_rates")

            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_exchange_rates_currencies';"
            ) as cursor:
                row = await cursor.fetchone()
                self.assertIsNotNone(row)

            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_exchange_rates_fetched_at';"
            ) as cursor:
                row = await cursor.fetchone()
                self.assertIsNotNone(row)

    async def test_save_and_get_latest_exchange_rate(self):
        await init_db(self.db_path)

        # Save exchange rate with a datetime object
        fetched_at_1 = datetime.datetime(2026, 6, 20, 12, 0, 0)
        row_id_1 = await save_exchange_rate(
            self.db_path, "usd", "eur", 0.92, fetched_at_1
        )
        self.assertIsNotNone(row_id_1)

        # Get latest exchange rate
        rate_entry = await get_latest_exchange_rate(self.db_path, "usd", "eur")
        self.assertIsNotNone(rate_entry)
        self.assertEqual(rate_entry["source_currency"], "USD")
        self.assertEqual(rate_entry["target_currency"], "EUR")
        self.assertEqual(rate_entry["rate"], 0.92)
        self.assertIsInstance(rate_entry["created_at"], datetime.datetime)
        self.assertIsInstance(rate_entry["fetched_at"], datetime.datetime)
        # Verify datetime values match
        self.assertEqual(rate_entry["fetched_at"].year, 2026)
        self.assertEqual(rate_entry["fetched_at"].month, 6)
        self.assertEqual(rate_entry["fetched_at"].day, 20)
        self.assertEqual(rate_entry["fetched_at"].hour, 12)

        # Save another rate that is newer
        fetched_at_2 = datetime.datetime(2026, 6, 20, 13, 0, 0)
        row_id_2 = await save_exchange_rate(
            self.db_path, "USD", "EUR", 0.93, fetched_at_2
        )

        # Test retrieving the latest
        rate_entry = await get_latest_exchange_rate(self.db_path, "USD", "EUR")
        self.assertEqual(rate_entry["id"], row_id_2)
        self.assertEqual(rate_entry["rate"], 0.93)

        # Save an older rate, should not become the latest
        fetched_at_old = datetime.datetime(2026, 6, 20, 11, 0, 0)
        await save_exchange_rate(self.db_path, "USD", "EUR", 0.91, fetched_at_old)

        # Should still return rate_entry with rate 0.93 (fetched_at_2 is newer)
        rate_entry = await get_latest_exchange_rate(self.db_path, "USD", "EUR")
        self.assertEqual(rate_entry["id"], row_id_2)
        self.assertEqual(rate_entry["rate"], 0.93)

    async def test_save_with_string_datetime(self):
        await init_db(self.db_path)

        # Save exchange rate with a string fetched_at
        fetched_at_str = "2026-06-20T14:30:00"
        await save_exchange_rate(self.db_path, "GBP", "USD", 1.25, fetched_at_str)

        rate_entry = await get_latest_exchange_rate(self.db_path, "GBP", "USD")
        self.assertIsNotNone(rate_entry)
        self.assertEqual(rate_entry["rate"], 1.25)
        self.assertIsInstance(rate_entry["fetched_at"], datetime.datetime)
        self.assertEqual(rate_entry["fetched_at"].hour, 14)
        self.assertEqual(rate_entry["fetched_at"].minute, 30)

    async def test_get_latest_exchange_rate_not_found(self):
        await init_db(self.db_path)
        rate_entry = await get_latest_exchange_rate(self.db_path, "GBP", "JPY")
        self.assertIsNone(rate_entry)

    async def test_get_exchange_rate_history(self):
        await init_db(self.db_path)

        # Insert 5 records
        base_time = datetime.datetime(2026, 6, 20, 10, 0, 0)
        for i in range(5):
            fetched_at = base_time + datetime.timedelta(hours=i)
            await save_exchange_rate(
                self.db_path, "USD", "EUR", 0.90 + (i * 0.01), fetched_at
            )

        # Get history of count = 3
        history = await get_exchange_rate_history(self.db_path, 3)
        self.assertEqual(len(history), 3)
        # Should be ordered by fetched_at DESC, so rates should be 0.94, 0.93, 0.92
        self.assertAlmostEqual(history[0]["rate"], 0.94)
        self.assertAlmostEqual(history[1]["rate"], 0.93)
        self.assertAlmostEqual(history[2]["rate"], 0.92)

        # Get history with larger count than records
        history = await get_exchange_rate_history(self.db_path, 10)
        self.assertEqual(len(history), 5)


if __name__ == "__main__":
    unittest.main()
