import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from daphne.tiktok import handle_tiktok_links
from daphne.media_http import MediaTooLarge


class TestTikTokHandler(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.update = MagicMock()
        self.update.message.text = "https://www.tiktok.com/@user/video/123"
        self.update.message.chat_id = 7
        self.update.message.delete = AsyncMock()
        self.context = MagicMock()
        self.context.bot.send_chat_action = AsyncMock()

    async def test_failed_fallback_does_not_delete_original(self):
        response = MagicMock(status_code=500)
        client = AsyncMock()
        client.get.return_value = response
        client_context = MagicMock()
        client_context.__aenter__ = AsyncMock(return_value=client)
        client_context.__aexit__ = AsyncMock(return_value=None)
        with (
            patch("daphne.tiktok.httpx.AsyncClient", return_value=client_context),
            patch("daphne.bot.handle_video_link", new=AsyncMock()) as fallback,
        ):
            await handle_tiktok_links(self.update, self.context)
        fallback.assert_awaited_once()
        self.update.message.delete.assert_not_awaited()

    async def test_oversized_video_sends_card_and_keeps_original(self):
        api = MagicMock(status_code=200)
        api.json.return_value = {
            "code": 0,
            "data": {"id": "123", "play": "https://cdn.example/a.mp4"},
        }
        video = MagicMock(content=b"large")
        client = AsyncMock()
        client.get.side_effect = [api, video]
        client_context = MagicMock()
        client_context.__aenter__ = AsyncMock(return_value=client)
        client_context.__aexit__ = AsyncMock(return_value=None)
        with (
            patch("daphne.tiktok.httpx.AsyncClient", return_value=client_context),
            patch("daphne.tiktok.video_upload_limit_mb", return_value=1),
            patch(
                "daphne.tiktok.stream_media", new=AsyncMock(side_effect=MediaTooLarge())
            ),
            patch("daphne.bot.send_video_card", new=AsyncMock()) as card,
        ):
            await handle_tiktok_links(self.update, self.context)
        card.assert_awaited_once()
        self.update.message.delete.assert_not_awaited()
