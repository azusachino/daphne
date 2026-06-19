import datetime
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx

from image_fetch import (
    get_yesterday_date,
    parse_popular_api,
    format_caption,
    fetch_popular_image,
)


class TestImageFetch(unittest.IsolatedAsyncioTestCase):
    def test_date_extraction(self):
        # Test standard date extraction
        today = datetime.date(2026, 6, 20)
        day, month, year = get_yesterday_date(today)
        self.assertEqual((day, month, year), (19, 6, 2026))

        # Test month boundary (Jan 1)
        today_jan_1 = datetime.date(2026, 1, 1)
        day, month, year = get_yesterday_date(today_jan_1)
        self.assertEqual((day, month, year), (31, 12, 2025))

    def test_parse_popular_api_yandere(self):
        # Empty input
        self.assertIsNone(parse_popular_api([], "yandere"))
        self.assertIsNone(parse_popular_api(None, "yandere"))

        # Valid input
        posts = [
            {
                "id": 12345,
                "file_url": "https://files.yande.re/image/12345.jpg",
                "tags": "solo 1girl",
            },
            {
                "id": 67890,
                "file_url": "https://files.yande.re/image/67890.png",
                "tags": "2girls",
            },
        ]
        res = parse_popular_api(posts, "yandere")
        self.assertIsNotNone(res)
        self.assertEqual(res["id"], 12345)
        self.assertEqual(res["file_url"], "https://files.yande.re/image/12345.jpg")
        self.assertEqual(res["tags"], "solo 1girl")

    def test_parse_popular_api_danbooru(self):
        # Empty input
        self.assertIsNone(parse_popular_api([], "danbooru"))

        # Valid input with file_url
        posts = [
            {
                "id": 111,
                "file_url": "https://danbooru.donmai.us/111.jpg",
                "tag_string": "original",
            }
        ]
        res = parse_popular_api(posts, "danbooru")
        self.assertIsNotNone(res)
        self.assertEqual(res["id"], 111)
        self.assertEqual(res["file_url"], "https://danbooru.donmai.us/111.jpg")

        # Valid input fallback to large_file_url
        posts = [
            {
                "id": 222,
                "large_file_url": "https://danbooru.donmai.us/large_222.jpg",
                "tag_string": "original",
            }
        ]
        res = parse_popular_api(posts, "danbooru")
        self.assertIsNotNone(res)
        self.assertEqual(res["id"], 222)
        self.assertEqual(res["file_url"], "https://danbooru.donmai.us/large_222.jpg")

    def test_format_caption(self):
        caption_y = format_caption("yandere", 123, "2026-06-19", "tag1 tag2")
        self.assertIn("yande.re Daily Popular", caption_y)
        self.assertIn("Post #123", caption_y)
        self.assertIn("tag1 tag2", caption_y)

        caption_d = format_caption("danbooru", 456, "2026-06-19", "tag3 tag4")
        self.assertIn("danbooru Daily Popular", caption_d)
        self.assertIn("Post #456", caption_d)
        self.assertIn("tag3 tag4", caption_d)

    @patch("image_fetch.download_image")
    @patch("httpx.AsyncClient")
    async def test_fetch_popular_image_yandere_success(
        self, mock_client_class, mock_download
    ):
        # Mock API response
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "id": 999,
                "file_url": "https://yande.re/999.png",
                "tags": "test_tag",
            }
        ]

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_class.return_value.__aenter__.return_value = mock_client

        # Mock image download bytes
        mock_download.return_value = b"fake_image_bytes"

        # Mock Bot
        mock_bot = MagicMock()
        mock_bot.send_photo = AsyncMock()

        today = datetime.date(2026, 6, 20)
        await fetch_popular_image(
            mock_bot, "yandere", "@test_channel", today_date=today
        )

        # Check API call URL and parameters
        mock_client.get.assert_called_once()
        call_url = mock_client.get.call_args[0][0]
        self.assertIn("https://yande.re/post/popular_by_day.json", call_url)
        self.assertIn("day=19", call_url)
        self.assertIn("month=6", call_url)
        self.assertIn("year=2026", call_url)

        # Check image download was called with correct URL and referer
        mock_download.assert_called_once_with(
            "https://yande.re/999.png", "https://yande.re/"
        )

        # Check Telegram bot called send_photo
        mock_bot.send_photo.assert_called_once()
        kwargs = mock_bot.send_photo.call_args[1]
        self.assertEqual(kwargs["chat_id"], "@test_channel")
        self.assertEqual(kwargs["parse_mode"], "HTML")
        self.assertIn("Post #999", kwargs["caption"])

    @patch("image_fetch.download_image")
    @patch("httpx.AsyncClient")
    async def test_fetch_popular_image_danbooru_success(
        self, mock_client_class, mock_download
    ):
        # Mock API response
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "id": 888,
                "large_file_url": "https://danbooru.donmai.us/888.jpg",
                "tag_string": "test_danbooru_tag",
            }
        ]

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client_class.return_value.__aenter__.return_value = mock_client

        # Mock image download bytes
        mock_download.return_value = b"fake_danbooru_bytes"

        # Mock Bot
        mock_bot = MagicMock()
        mock_bot.send_photo = AsyncMock()

        today = datetime.date(2026, 6, 20)
        await fetch_popular_image(
            mock_bot, "danbooru", "@test_channel", today_date=today
        )

        # Check API call URL and parameters
        mock_client.get.assert_called_once()
        call_url = mock_client.get.call_args[0][0]
        self.assertIn("https://danbooru.donmai.us/explore/posts/popular.json", call_url)
        self.assertIn("day=19", call_url)
        self.assertIn("month=6", call_url)
        self.assertIn("year=2026", call_url)

        # Check image download was called with correct URL and referer
        mock_download.assert_called_once_with(
            "https://danbooru.donmai.us/888.jpg", "https://danbooru.donmai.us/"
        )

        # Check Telegram bot called send_photo
        mock_bot.send_photo.assert_called_once()
        kwargs = mock_bot.send_photo.call_args[1]
        self.assertEqual(kwargs["chat_id"], "@test_channel")
        self.assertEqual(kwargs["parse_mode"], "HTML")
        self.assertIn("Post #888", kwargs["caption"])

    @patch("image_fetch.download_image")
    @patch("httpx.AsyncClient")
    async def test_send_photo_retry(self, mock_client_class, mock_download):
        # We also want to verify send_photo_with_retry works with retry logic
        from image_fetch import send_photo_with_retry

        mock_bot = MagicMock()
        # Fail first two times, then succeed
        mock_bot.send_photo = AsyncMock()
        mock_bot.send_photo.side_effect = [
            Exception("Telegram Error 1"),
            Exception("Telegram Error 2"),
            None,
        ]

        # Call with initial_delay=0.01 to speed up test execution
        await send_photo_with_retry(
            mock_bot,
            "@channel",
            b"fake_bytes",
            "caption",
            "photo.jpg",
            retries=3,
            initial_delay=0.01,
        )

        self.assertEqual(mock_bot.send_photo.call_count, 3)


if __name__ == "__main__":
    unittest.main()
