import os
import unittest
import tempfile
import shutil
import datetime
from unittest.mock import AsyncMock, MagicMock, patch
import httpx

from database import init_db, get_latest_exchange_rate
from exchange import fetch_rate, fetch_and_save_rates, format_rates_message
from scheduler import update_and_report_rates, setup_scheduler

class TestExchangeAndScheduler(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.test_dir, "test_exchange.db")

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    @patch("httpx.AsyncClient")
    async def test_fetch_rate_success(self, mock_client_class):
        # Create a mock response
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"source": "USD", "target": "JPY", "value": 161.355, "time": 1781820000000},
            {"source": "USD", "target": "JPY", "value": 161.26, "time": 1781823600000},
            {"source": "USD", "target": "JPY", "value": 161.305, "time": 1781827200000}
        ]
        
        # Setup the mock client
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        
        # Setup context manager
        mock_client_class.return_value.__aenter__.return_value = mock_client
        
        rate = await fetch_rate("USD", "JPY")
        self.assertEqual(rate, 161.305)
        
        # Verify URL called
        mock_client.get.assert_called_once_with(
            "https://wise.com/rates/history?source=USD&target=JPY&length=1&resolution=hourly&unit=day",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        )

    @patch("httpx.AsyncClient")
    async def test_fetch_rate_invalid_json(self, mock_client_class):
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {"error": "not a list"}
        
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_class.return_value.__aenter__.return_value = mock_client
        
        with self.assertRaises(ValueError):
            await fetch_rate("USD", "JPY")

    @patch("exchange.fetch_rate")
    async def test_fetch_and_save_rates(self, mock_fetch_rate):
        # Initialize DB
        await init_db(self.db_path)
        
        # Mock responses
        async def side_effect(source, target):
            if source == "JPY" and target == "CNY":
                return 0.04567
            if source == "USD" and target == "JPY":
                return 161.355
            return 0.0

        mock_fetch_rate.side_effect = side_effect
        
        rates = await fetch_and_save_rates(self.db_path)
        
        # Verify rates dict returned
        self.assertEqual(rates, {
            "JPY_CNY": 0.04567,
            "USD_JPY": 161.355
        })
        
        # Verify rates saved to database
        jpy_cny_record = await get_latest_exchange_rate(self.db_path, "JPY", "CNY")
        self.assertIsNotNone(jpy_cny_record)
        self.assertEqual(jpy_cny_record["rate"], 0.04567)
        self.assertEqual(jpy_cny_record["source_currency"], "JPY")
        self.assertEqual(jpy_cny_record["target_currency"], "CNY")
        
        usd_jpy_record = await get_latest_exchange_rate(self.db_path, "USD", "JPY")
        self.assertIsNotNone(usd_jpy_record)
        self.assertEqual(usd_jpy_record["rate"], 161.355)
        self.assertEqual(usd_jpy_record["source_currency"], "USD")
        self.assertEqual(usd_jpy_record["target_currency"], "JPY")

    def test_format_rates_message(self):
        rates = {
            "JPY_CNY": 0.045678,
            "USD_JPY": 161.354
        }
        
        message = format_rates_message(rates)
        
        # Should contain current local date/time (e.g. YYYY-MM-DD)
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        self.assertIn(today_str, message)
        
        # Verify format (JPY_CNY: {:.4f}, USD_JPY: {:.2f})
        # 0.045678 -> 0.0457
        # 161.354 -> 161.35
        self.assertIn("JPY_CNY: 0.0457", message)
        self.assertIn("USD_JPY: 161.35", message)

    @patch("scheduler.fetch_and_save_rates")
    @patch("scheduler.format_rates_message")
    async def test_update_and_report_rates(self, mock_format, mock_fetch_and_save):
        mock_fetch_and_save.return_value = {
            "JPY_CNY": 0.04567,
            "USD_JPY": 161.35
        }
        mock_format.return_value = "Formatted Message"
        
        # Create a mock bot
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock()
        
        await update_and_report_rates(mock_bot, self.db_path, "123456")
        
        mock_fetch_and_save.assert_called_once_with(self.db_path)
        mock_format.assert_called_once_with({
            "JPY_CNY": 0.04567,
            "USD_JPY": 161.35
        })
        mock_bot.send_message.assert_called_once_with(
            chat_id="123456",
            text="Formatted Message"
        )

    def test_setup_scheduler(self):
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        scheduler = AsyncIOScheduler()
        mock_bot = MagicMock()
        
        setup_scheduler(mock_bot, self.db_path, "123456", scheduler=scheduler)
        
        jobs = scheduler.get_jobs()
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        
        self.assertEqual(job.func, update_and_report_rates)
        self.assertEqual(job.args, (mock_bot, self.db_path, "123456"))
        
        fields = {f.name: str(f) for f in job.trigger.fields}
        self.assertEqual(fields["minute"], "59")
        self.assertEqual(fields["second"], "39")

if __name__ == "__main__":
    unittest.main()
