import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from daphne.instagram import (
    contains_instagram_link,
    extract_instagram_link,
    handle_instagram_links,
)

# Real link that triggered the fail-fast bug in production: parth-dl's
# extractor raised DownloadError, and the code used to fall back to
# handle_video_link's yt-dlp/you-get/lux chain, which burned ~15s only to
# report "There is no video in this post" (it's an image post).
FAILING_INSTAGRAM_URL = "https://www.instagram.com/p/DRZSlC7D3OT/?igsh=MTJqbzAyN2QweDQ5"


class TestInstagramExtraction(unittest.TestCase):
    def test_contains_instagram_link(self):
        self.assertTrue(contains_instagram_link(FAILING_INSTAGRAM_URL))
        self.assertFalse(contains_instagram_link("https://google.com/p/abc"))

    def test_extract_instagram_link_strips_query_params(self):
        res = extract_instagram_link(f"look at this: {FAILING_INSTAGRAM_URL}")
        self.assertEqual(res, "https://www.instagram.com/p/DRZSlC7D3OT")


class TestInstagramHandler(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.update = MagicMock()
        self.update.message = MagicMock()
        self.update.message.text = FAILING_INSTAGRAM_URL
        self.update.message.chat_id = -1002058191932
        self.update.message.set_reaction = AsyncMock()

        self.user = MagicMock()
        self.user.username = "haru"
        self.user.full_name = "Haru"
        self.update.effective_user = self.user

        self.context = MagicMock()
        self.context.bot = MagicMock()
        self.context.bot.send_chat_action = AsyncMock()
        self.context.bot.send_message = AsyncMock()
        self.context.bot.send_photo = AsyncMock()

    @patch("daphne.bot.handle_video_link", new_callable=AsyncMock)
    @patch("daphne.downloader.fetch_instagram_fallback_media", return_value=None)
    @patch("parth_dl.extractors.MediaExtractor.extract")
    async def test_extraction_failure_fails_fast_no_video_fallback(
        self, mock_extract, mock_yt_dlp_fallback, mock_handle_video_link
    ):
        from parth_dl.utils import DownloadError

        mock_extract.side_effect = DownloadError(
            "All extraction methods failed. Content might be private or unavailable."
        )

        await handle_instagram_links(self.update, self.context)

        # Must not burn the full multi-engine video pipeline on a post
        # parth-dl already failed to read — yt-dlp's own Instagram extractor
        # hits the same public endpoints and would just retry into the same
        # wall.
        mock_handle_video_link.assert_not_called()

        self.context.bot.send_message.assert_called_once()
        _, kwargs = self.context.bot.send_message.call_args
        self.assertEqual(kwargs["chat_id"], -1002058191932)
        self.assertIn("Couldn't fetch this Instagram post", kwargs["text"])
        self.assertIn("https://www.instagram.com/p/DRZSlC7D3OT", kwargs["text"])

        self.update.message.set_reaction.assert_called_once()

    @patch("daphne.bot.handle_video_link", new_callable=AsyncMock)
    @patch("daphne.downloader.fetch_instagram_fallback_media", return_value=None)
    @patch("parth_dl.extractors.MediaExtractor.extract", return_value=None)
    async def test_no_data_returned_fails_fast_no_video_fallback(
        self, mock_extract, mock_yt_dlp_fallback, mock_handle_video_link
    ):
        # Same fail-fast contract when extract() returns falsy instead of
        # raising, and the yt-dlp fallback also comes up empty.
        await handle_instagram_links(self.update, self.context)

        mock_handle_video_link.assert_not_called()
        self.context.bot.send_message.assert_called_once()

    @patch("daphne.bot.handle_video_link", new_callable=AsyncMock)
    @patch("parth_dl.extractors.MediaExtractor.extract", return_value=None)
    async def test_parth_dl_failure_recovers_via_yt_dlp_image_fallback(
        self, mock_extract, mock_handle_video_link
    ):
        # This is the actual production case: parth-dl comes up empty for an
        # image post, but yt-dlp's own Instagram extractor (called with
        # --ignore-no-formats-error) can still read the post anonymously and
        # hand back the photo URL.
        fallback_data = {
            "id": "DRZSlC7D3OT",
            "title": "Video by nyarumaa.cosplay",
            "uploader": "nyarumaa.cosplay",
            "type": "image",
            "images": [{"url": "https://instagram.fna.fbcdn.net/photo.jpg"}],
            "formats": [],
            "thumbnail": "https://instagram.fna.fbcdn.net/photo.jpg",
        }
        with patch(
            "daphne.downloader.fetch_instagram_fallback_media",
            return_value=fallback_data,
        ):
            await handle_instagram_links(self.update, self.context)

        mock_handle_video_link.assert_not_called()
        self.context.bot.send_message.assert_not_called()
        self.context.bot.send_photo.assert_called_once()
        _, kwargs = self.context.bot.send_photo.call_args
        self.assertEqual(kwargs["chat_id"], -1002058191932)
        self.assertEqual(kwargs["photo"], "https://instagram.fna.fbcdn.net/photo.jpg")
        # Uploader name with a "." (a real production value) collapses into
        # one author hashtag rather than splitting or being dropped.
        self.assertIn("#instagram #nyarumaa_cosplay", kwargs["caption"])

    @patch("daphne.bot.handle_video_link", new_callable=AsyncMock)
    @patch("parth_dl.extractors.MediaExtractor.extract", return_value=None)
    async def test_no_uploader_skips_author_hashtag(
        self, mock_extract, mock_handle_video_link
    ):
        fallback_data = {
            "id": "DRZSlC7D3OT",
            "title": "An image post",
            "uploader": "unknown",
            "type": "image",
            "images": [{"url": "https://instagram.fna.fbcdn.net/photo.jpg"}],
            "formats": [],
            "thumbnail": "https://instagram.fna.fbcdn.net/photo.jpg",
        }
        with patch(
            "daphne.downloader.fetch_instagram_fallback_media",
            return_value=fallback_data,
        ):
            await handle_instagram_links(self.update, self.context)

        self.context.bot.send_photo.assert_called_once()
        _, kwargs = self.context.bot.send_photo.call_args
        self.assertIn("#instagram", kwargs["caption"])
        self.assertNotIn("#unknown", kwargs["caption"])

    @patch("daphne.bot.handle_video_link", new_callable=AsyncMock)
    @patch("parth_dl.extractors.MediaExtractor.extract", return_value=None)
    async def test_multi_word_and_non_ascii_uploader_hashtag(
        self, mock_extract, mock_handle_video_link
    ):
        fallback_data = {
            "id": "DRZSlC7D3OT",
            "title": "An image post",
            "uploader": "Ali Taha 山田",
            "type": "image",
            "images": [{"url": "https://instagram.fna.fbcdn.net/photo.jpg"}],
            "formats": [],
            "thumbnail": "https://instagram.fna.fbcdn.net/photo.jpg",
        }
        with patch(
            "daphne.downloader.fetch_instagram_fallback_media",
            return_value=fallback_data,
        ):
            await handle_instagram_links(self.update, self.context)

        self.context.bot.send_photo.assert_called_once()
        _, kwargs = self.context.bot.send_photo.call_args
        self.assertIn("#instagram #ali_taha_山田", kwargs["caption"])


if __name__ == "__main__":
    unittest.main()
