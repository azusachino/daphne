import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import io
from telegram import Update
from telegram.ext import ContextTypes

from daphne.twitter import (
    contains_twitter_link,
    extract_twitter_link,
    handle_twitter_links,
)


class TestTwitterExtraction(unittest.TestCase):
    def test_contains_twitter_link(self):
        self.assertTrue(
            contains_twitter_link("Check this out: https://twitter.com/jack/status/20")
        )
        self.assertTrue(
            contains_twitter_link("Check this out: https://x.com/jack/status/20?s=19")
        )
        self.assertTrue(
            contains_twitter_link(
                "Check this out: https://fxtwitter.com/jack/status/20"
            )
        )
        self.assertTrue(
            contains_twitter_link(
                "Check this out: https://vxtwitter.com/jack/status/20"
            )
        )
        self.assertTrue(
            contains_twitter_link("Check this out: https://fixupx.com/jack/status/20")
        )
        self.assertTrue(
            contains_twitter_link("Check this out: https://www.x.com/jack/status/20")
        )
        self.assertFalse(
            contains_twitter_link("Check this out: https://google.com/jack/status/20")
        )
        self.assertFalse(
            contains_twitter_link("Check this out: https://twitter.com/jack")
        )

    def test_extract_twitter_link(self):
        res = extract_twitter_link(
            "Check this out: https://twitter.com/jack/status/20?s=20"
        )
        self.assertIsNotNone(res)
        domain, username, tweet_id = res
        self.assertEqual(domain, "twitter.com")
        self.assertEqual(username, "jack")
        self.assertEqual(tweet_id, "20")

        res = extract_twitter_link("https://x.com/some_user/status/1234567890")
        self.assertIsNotNone(res)
        domain, username, tweet_id = res
        self.assertEqual(domain, "x.com")
        self.assertEqual(username, "some_user")
        self.assertEqual(tweet_id, "1234567890")


class TestTwitterHandler(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.update = MagicMock(spec=Update)
        self.update.message = MagicMock()
        self.update.message.chat_id = 123456
        self.update.message.reply_to_message = None
        self.update.message.is_topic_message = False
        self.update.message.delete = AsyncMock()

        self.user = MagicMock()
        self.user.username = "test_user"
        self.user.full_name = "Test User Full Name"
        self.update.effective_user = self.user

        self.context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
        self.context.bot = MagicMock()
        self.context.bot.send_photo = AsyncMock()
        self.context.bot.send_video = AsyncMock()
        self.context.bot.send_animation = AsyncMock()
        self.context.bot.send_media_group = AsyncMock()
        self.context.bot.send_message = AsyncMock()

    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_handle_no_media_sends_fallback_url(self, mock_get):
        # API succeeds, but tweet has no media
        self.update.message.text = "Here: https://x.com/jack/status/20"

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": 200,
            "tweet": {
                "id": "20",
                "text": "just setting up my twttr",
                "author": {"screen_name": "jack", "name": "jack"},
                "media": {"photos": [], "videos": []},
            },
        }
        mock_get.return_value = mock_response

        await handle_twitter_links(self.update, self.context)

        # It should send the fallback fxtwitter URL
        self.context.bot.send_message.assert_called_once_with(
            chat_id=123456, text="https://fxtwitter.com/jack/status/20"
        )
        self.update.message.delete.assert_called_once()

    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_handle_single_photo_success(self, mock_get):
        self.update.message.text = "Check: https://twitter.com/nasa/status/999"

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": 200,
            "tweet": {
                "id": "999",
                "text": "Beautiful space view #NASA #Hubble",
                "author": {"screen_name": "nasa", "name": "NASA"},
                "media": {
                    "photos": [{"url": "https://pbs.twimg.com/media/test.jpg"}],
                    "videos": [],
                },
            },
        }
        mock_get.return_value = mock_response

        await handle_twitter_links(self.update, self.context)

        # Should send single photo
        self.context.bot.send_photo.assert_called_once()
        kwargs = self.context.bot.send_photo.call_args[1]
        self.assertEqual(kwargs["chat_id"], 123456)
        self.assertEqual(kwargs["photo"], "https://pbs.twimg.com/media/test.jpg")
        self.assertEqual(kwargs["parse_mode"], "HTML")
        self.assertIn("Beautiful space view #NASA #Hubble", kwargs["caption"])
        self.assertIn("#twitter #nasa #hubble", kwargs["caption"])
        self.assertIn("via @test_user", kwargs["caption"])
        self.assertIn("https://twitter.com/nasa/status/999", kwargs["caption"])
        self.update.message.delete.assert_called_once()

    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_handle_multi_photo_success(self, mock_get):
        self.update.message.text = "https://x.com/nasa/status/999"

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": 200,
            "tweet": {
                "id": "999",
                "text": "Check these photos",
                "author": {"screen_name": "nasa", "name": "NASA"},
                "media": {
                    "photos": [
                        {"url": "https://pbs.twimg.com/media/1.jpg"},
                        {"url": "https://pbs.twimg.com/media/2.jpg"},
                    ],
                    "videos": [],
                },
            },
        }
        mock_get.return_value = mock_response

        await handle_twitter_links(self.update, self.context)

        # Should send media group
        self.context.bot.send_media_group.assert_called_once()
        kwargs = self.context.bot.send_media_group.call_args[1]
        self.assertEqual(kwargs["chat_id"], 123456)
        media_group = kwargs["media"]
        self.assertEqual(len(media_group), 2)
        self.assertEqual(media_group[0].media, "https://pbs.twimg.com/media/1.jpg")
        self.assertEqual(media_group[1].media, "https://pbs.twimg.com/media/2.jpg")
        self.assertIn("Check these photos", media_group[0].caption)
        self.update.message.delete.assert_called_once()

    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_handle_video_success(self, mock_get):
        self.update.message.text = "https://x.com/nasa/status/999"

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": 200,
            "tweet": {
                "id": "999",
                "text": "Rocket launch video",
                "author": {"screen_name": "nasa", "name": "NASA"},
                "media": {
                    "photos": [],
                    "videos": [
                        {"url": "https://video.twimg.com/test.mp4", "type": "video"}
                    ],
                },
            },
        }
        mock_get.return_value = mock_response

        await handle_twitter_links(self.update, self.context)

        # Should send video
        self.context.bot.send_video.assert_called_once()
        kwargs = self.context.bot.send_video.call_args[1]
        self.assertEqual(kwargs["chat_id"], 123456)
        self.assertEqual(kwargs["video"], "https://video.twimg.com/test.mp4")
        self.update.message.delete.assert_called_once()

    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_handle_mixed_media_sends_photos_and_video(self, mock_get):
        self.update.message.text = "https://x.com/nasa/status/999"

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": 200,
            "tweet": {
                "id": "999",
                "text": "Mixed media #NASA",
                "url": "https://twitter.com/nasa/status/999",
                "media": {
                    "all": [
                        {"url": "https://pbs.twimg.com/media/1.jpg", "type": "photo"},
                        {"url": "https://pbs.twimg.com/media/2.jpg", "type": "photo"},
                        {"url": "https://video.twimg.com/test.mp4", "type": "video"},
                    ]
                },
            },
        }
        mock_get.return_value = mock_response

        await handle_twitter_links(self.update, self.context)

        self.context.bot.send_media_group.assert_called_once()
        self.context.bot.send_video.assert_called_once()
        media_group = self.context.bot.send_media_group.call_args[1]["media"]
        self.assertIn("Mixed media #NASA", media_group[0].caption)
        self.assertEqual(
            self.context.bot.send_video.call_args[1]["video"],
            "https://video.twimg.com/test.mp4",
        )
        self.assertEqual(self.context.bot.send_video.call_args[1]["caption"], "")
        self.update.message.delete.assert_called_once()

    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_handle_gif_success(self, mock_get):
        self.update.message.text = "https://x.com/nasa/status/999"

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": 200,
            "tweet": {
                "id": "999",
                "text": "Rocket launch gif",
                "author": {"screen_name": "nasa", "name": "NASA"},
                "media": {
                    "photos": [],
                    "videos": [
                        {"url": "https://video.twimg.com/test.gif", "type": "gif"}
                    ],
                },
            },
        }
        mock_get.return_value = mock_response

        await handle_twitter_links(self.update, self.context)

        # Should send animation
        self.context.bot.send_animation.assert_called_once()
        kwargs = self.context.bot.send_animation.call_args[1]
        self.assertEqual(kwargs["chat_id"], 123456)
        self.assertEqual(kwargs["animation"], "https://video.twimg.com/test.gif")
        self.update.message.delete.assert_called_once()

    @patch("daphne.twitter.logger.warning")
    @patch("daphne.twitter.httpx.AsyncClient.get")
    @patch("daphne.twitter.download_bytes")
    async def test_handle_media_send_by_url_fails_downloads_bytes(
        self, mock_download, mock_get, mock_log_warn
    ):
        self.update.message.text = "https://x.com/nasa/status/999"

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": 200,
            "tweet": {
                "id": "999",
                "text": "A photo",
                "author": {"screen_name": "nasa", "name": "NASA"},
                "media": {
                    "photos": [{"url": "https://pbs.twimg.com/media/test.jpg"}],
                    "videos": [],
                },
            },
        }
        mock_get.return_value = mock_response

        # First call to send_photo raises exception, second succeeds
        self.context.bot.send_photo.side_effect = [
            Exception("Failed to send by URL"),
            None,
        ]
        mock_download.return_value = b"image_data"

        await handle_twitter_links(self.update, self.context)

        # Verified that send_photo is called twice
        self.assertEqual(self.context.bot.send_photo.call_count, 2)
        # First call tried with URL
        self.assertEqual(
            self.context.bot.send_photo.call_args_list[0][1]["photo"],
            "https://pbs.twimg.com/media/test.jpg",
        )
        # Second call tried with downloaded BytesIO
        photo_arg = self.context.bot.send_photo.call_args_list[1][1]["photo"]
        self.assertTrue(isinstance(photo_arg, io.BytesIO))
        self.assertEqual(photo_arg.getvalue(), b"image_data")
        self.assertEqual(photo_arg.name, "photo.jpg")

        mock_download.assert_called_once_with("https://pbs.twimg.com/media/test.jpg")
        self.update.message.delete.assert_called_once()

    @patch("daphne.twitter.logger.warning")
    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_handle_api_404_sends_fallback_url(self, mock_get, mock_log_warn):
        self.update.message.text = "https://x.com/nasa/status/999"

        # API returns 404
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        await handle_twitter_links(self.update, self.context)

        # Sends fallback URL
        self.context.bot.send_message.assert_called_once_with(
            chat_id=123456, text="https://fxtwitter.com/nasa/status/999"
        )
        self.update.message.delete.assert_called_once()

    @patch("daphne.twitter.logger.exception")
    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_handle_api_exception_sends_fallback_url(
        self, mock_get, mock_log_exc
    ):
        self.update.message.text = "https://x.com/nasa/status/999"

        # API raises connection error
        mock_get.side_effect = httpx.RequestError("Connection failed")

        await handle_twitter_links(self.update, self.context)

        # Sends fallback URL
        self.context.bot.send_message.assert_called_once_with(
            chat_id=123456, text="https://fxtwitter.com/nasa/status/999"
        )
        self.update.message.delete.assert_called_once()

    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_reply_prevents_message_deletion(self, mock_get):
        self.update.message.text = "https://x.com/jack/status/20"
        self.update.message.reply_to_message = MagicMock()  # This message is a reply

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": 200,
            "tweet": {
                "id": "20",
                "text": "No media tweet",
                "author": {"screen_name": "jack", "name": "jack"},
                "media": {},
            },
        }
        mock_get.return_value = mock_response

        await handle_twitter_links(self.update, self.context)

        self.context.bot.send_message.assert_called_once()
        self.update.message.delete.assert_not_called()  # delete must not be called

    @patch("daphne.twitter.httpx.AsyncClient.get")
    async def test_topic_message_prevents_message_deletion(self, mock_get):
        self.update.message.text = "https://x.com/jack/status/20"
        self.update.message.is_topic_message = True  # This is a topic message

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "code": 200,
            "tweet": {
                "id": "20",
                "text": "No media tweet",
                "author": {"screen_name": "jack", "name": "jack"},
                "media": {},
            },
        }
        mock_get.return_value = mock_response

        await handle_twitter_links(self.update, self.context)

        self.context.bot.send_message.assert_called_once()
        self.update.message.delete.assert_not_called()  # delete must not be called


if __name__ == "__main__":
    unittest.main()
