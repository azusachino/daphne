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

    async def test_oversized_video_cards_before_downloading(self):
        # TikWM reports a size over the limit: the gate cards without fetching
        # a single media byte (the old path streamed up to the cap first).
        api = MagicMock(status_code=200)
        api.json.return_value = {
            "code": 0,
            "data": {
                "id": "123",
                "play": "https://cdn.example/a.mp4",
                "size": 2 * 1024 * 1024,
            },
        }
        with (
            self._client(api),
            patch("daphne.bot.video_upload_limit_mb", return_value=1),
            patch("daphne.tiktok.stream_media", new=AsyncMock()) as stream,
            patch("daphne.bot.rbac_service") as rbac,
            patch("daphne.bot.send_video_card", autospec=True) as card,
        ):
            access = MagicMock()
            access.is_allowed.return_value = True
            rbac.check_access.return_value = access
            await handle_tiktok_links(self.update, self.context)
        card.assert_awaited_once()
        update, video, _sender, reason = card.await_args.args
        self.assertIs(update, self.update)
        self.assertIsInstance(video, Video)
        self.assertEqual(video.platform, "tiktok")
        self.assertEqual(video.canonical_url, PAGE)
        self.assertEqual(reason, "Video is over 1 MB")
        stream.assert_not_called()
        self.update.message.delete.assert_not_awaited()

    async def test_a_tikwm_size_that_lied_cards_the_real_bytes(self):
        # The gate approved a size under the cap, but the stream crossed it:
        # the real-bytes backstop cards the priced video mid-download.
        api = MagicMock(status_code=200)
        api.json.return_value = {
            "code": 0,
            "data": {
                "id": "123",
                "play": "https://cdn.example/a.mp4",
                "size": 512 * 1024,
            },
        }
        with (
            self._client(api),
            patch("daphne.bot.video_upload_limit_mb", return_value=1),
            patch(
                "daphne.tiktok.stream_media", new=AsyncMock(side_effect=MediaTooLarge())
            ),
            patch("daphne.bot.rbac_service") as rbac,
            patch("daphne.bot.send_video_card", autospec=True) as card,
        ):
            access = MagicMock()
            access.is_allowed.return_value = True
            rbac.check_access.return_value = access
            await handle_tiktok_links(self.update, self.context)
        card.assert_awaited_once()
        _update, video, _sender, reason = card.await_args.args
        self.assertEqual(video.estimated_bytes, 512 * 1024)
        self.assertEqual(reason, "Video is over 1 MB")
        self.update.message.delete.assert_not_awaited()

    async def test_a_priced_stream_failure_cards_instead_of_a_second_cycle(self):
        # TikWM priced the video and the gate approved it, but the stream or
        # send failed: the card's Download button runs the full pipeline on
        # demand. No silent second resolve+download cycle.
        api = MagicMock(status_code=200)
        api.json.return_value = {
            "code": 0,
            "data": {
                "id": "123",
                "play": "https://cdn.example/a.mp4",
                "size": 512 * 1024,
            },
        }
        with (
            self._client(api),
            patch("daphne.bot.video_upload_limit_mb", return_value=1),
            patch(
                "daphne.tiktok.stream_media", new=AsyncMock(side_effect=RuntimeError())
            ),
            patch("daphne.bot.rbac_service") as rbac,
            patch("daphne.bot.send_video_card", autospec=True) as card,
            patch("daphne.bot.handle_video_url", autospec=True) as fallback,
        ):
            access = MagicMock()
            access.is_allowed.return_value = True
            rbac.check_access.return_value = access
            await handle_tiktok_links(self.update, self.context)
        fallback.assert_not_called()
        card.assert_awaited_once()
        _update, video, _sender, reason = card.await_args.args
        self.assertEqual(video.estimated_bytes, 512 * 1024)
        self.assertIn("TikWM download failed", reason)
        self.update.message.delete.assert_not_awaited()

    async def test_an_upload_charges_the_preview_quota(self):
        api = MagicMock(status_code=200)
        api.json.return_value = {
            "code": 0,
            "data": {
                "id": "123",
                "play": "https://cdn.example/a.mp4",
                "size": 512 * 1024,
            },
        }
        with (
            self._client(api, MagicMock(content=b"small")),
            patch("daphne.bot.video_upload_limit_mb", return_value=1),
            patch("daphne.tiktok.stream_media", new=AsyncMock()),
            patch("daphne.bot.rbac_service") as rbac,
            patch("daphne.tiktok.probe_video_dimensions", return_value=(720, 1280, 10)),
            patch("builtins.open", unittest.mock.mock_open()),
        ):
            self.context.bot.send_video = AsyncMock()
            access = MagicMock()
            access.is_allowed.return_value = True
            rbac.check_access.return_value = access
            await handle_tiktok_links(self.update, self.context)
        # Dry-run decides, the charge follows only on Upload.
        dry = rbac.check_access.call_args_list[0].kwargs.get("dry_run")
        self.assertTrue(dry)
        rbac.check_access.assert_any_call(
            self.update.effective_user.id,
            self.update.effective_chat.id,
            "preview_video",
        )
        self.context.bot.send_video.assert_awaited_once()

    async def test_preview_denied_cards_without_downloading(self):
        api = MagicMock(status_code=200)
        api.json.return_value = {
            "code": 0,
            "data": {
                "id": "123",
                "play": "https://cdn.example/a.mp4",
                "size": 512 * 1024,
            },
        }
        with (
            self._client(api),
            patch("daphne.bot.video_upload_limit_mb", return_value=1),
            patch("daphne.tiktok.stream_media", new=AsyncMock()) as stream,
            patch("daphne.bot.rbac_service") as rbac,
            patch("daphne.bot.send_video_card", autospec=True) as card,
        ):
            denied = MagicMock()
            denied.is_allowed.return_value = False
            denied.is_rate_limited.return_value = False
            rbac.check_access.return_value = denied
            await handle_tiktok_links(self.update, self.context)
        reason = card.await_args.args[3]
        self.assertEqual(reason, "Preview Not Allowed (direct download only)")
        stream.assert_not_called()
