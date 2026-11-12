import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from daphne.media_http import MediaTooLarge
from daphne.tiktok import handle_tiktok_links
from daphne.video import Video

PAGE = "https://www.tiktok.com/@user/video/123"


class TestTikTokHandler(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.update = MagicMock()
        self.update.message.text = PAGE
        self.update.message.chat_id = 7
        self.update.message.delete = AsyncMock()
        self.context = MagicMock()
        self.context.bot.send_chat_action = AsyncMock()

    def _client(self, *responses):
        client = AsyncMock()
        client.get.side_effect = list(responses)
        client_context = MagicMock()
        client_context.__aenter__ = AsyncMock(return_value=client)
        client_context.__aexit__ = AsyncMock(return_value=None)
        return patch("daphne.tiktok.httpx.AsyncClient", return_value=client_context)

    async def test_failed_fallback_does_not_delete_original(self):
        with (
            self._client(MagicMock(status_code=500)),
            patch("daphne.bot.handle_video_url", autospec=True) as fallback,
        ):
            await handle_tiktok_links(self.update, self.context)
        # The seam, its arity and the platform it reports are all pinned.
        fallback.assert_awaited_once_with(self.update, self.context, PAGE, "tiktok")
        self.update.message.delete.assert_not_awaited()

    async def test_oversized_video_sends_card_and_keeps_original(self):
        api = MagicMock(status_code=200)
        api.json.return_value = {
            "code": 0,
            "data": {"id": "123", "play": "https://cdn.example/a.mp4"},
        }
        with (
            self._client(api, MagicMock(content=b"large")),
            patch("daphne.tiktok.video_upload_limit_mb", return_value=1),
            patch(
                "daphne.tiktok.stream_media", new=AsyncMock(side_effect=MediaTooLarge())
            ),
            patch("daphne.bot.send_video_card", autospec=True) as card,
        ):
            await handle_tiktok_links(self.update, self.context)
        card.assert_awaited_once()
        update, video, _sender, reason = card.await_args.args
        self.assertIs(update, self.update)
        self.assertIsInstance(video, Video)
        self.assertEqual(video.platform, "tiktok")
        self.assertEqual(video.canonical_url, PAGE)
        self.assertEqual(video.fetch_url, PAGE)
        self.assertEqual(reason, "Video is over 1 MB")
        self.update.message.delete.assert_not_awaited()
