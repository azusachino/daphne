import unittest
from unittest.mock import AsyncMock, MagicMock, patch


from daphne.instagram import (
    contains_instagram_link,
    extract_instagram_link,
    handle_instagram_links,
    send_carousel_media,
)
from daphne.video import from_metadata

PAGE = "https://www.instagram.com/p/DRZSlC7D3OT"

# Real link that triggered the fail-fast bug in production: parth-dl's
# extractor raised DownloadError, and the code used to fall back to
# handle_video_url's yt-dlp chain, which burned ~15s only to
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

    @patch("daphne.downloader.fetch_instagram_fallback_media", return_value=None)
    @patch(
        "parth_dl.extractors.MediaExtractor", side_effect=RuntimeError("init failed")
    )
    async def test_constructor_failure_still_tries_fallback(
        self, mock_extractor, mock_fallback
    ):
        await handle_instagram_links(self.update, self.context)
        mock_fallback.assert_called_once_with("https://www.instagram.com/p/DRZSlC7D3OT")
        self.context.bot.send_message.assert_awaited_once()

    async def test_carousel_videos_are_streamed_before_grouping(self):
        async def stream(client, url, output, limit, **kwargs):
            output.write(b"video")

        self.context.bot.send_media_group = AsyncMock()
        videos = [
            from_metadata(u, {"webpage_url": PAGE, "size": 1024}, platform="instagram")
            for u in ("https://cdn.test/one", "https://cdn.test/two")
        ]
        with (
            patch(
                "daphne.instagram.stream_media", new=AsyncMock(side_effect=stream)
            ) as download,
            patch("daphne.bot._charge_preview", autospec=True) as charge,
        ):
            await send_carousel_media(
                self.context.bot, 7, videos, "caption", self.update
            )
        self.assertEqual(download.await_count, 2)
        media = self.context.bot.send_media_group.await_args.kwargs["media"]
        self.assertEqual([item.type for item in media], ["video", "video"])
        self.assertEqual(media[0].caption, "caption")
        # The group delivered: one charge, at send time.
        charge.assert_awaited_once_with(self.update)

    async def test_a_refused_album_charges_per_single_send(self):
        # Telegram refused the album: the items go out one by one, and each
        # successful send charges the preview quota once.
        from aiogram.exceptions import TelegramBadRequest

        self.context.bot.send_media_group = AsyncMock(
            side_effect=TelegramBadRequest(method="sendMediaGroup", message="refused")
        )
        self.context.bot.send_video = AsyncMock()
        videos = [
            from_metadata(u, {"webpage_url": PAGE, "size": 1024}, platform="instagram")
            for u in ("https://cdn.test/one", "https://cdn.test/two")
        ]

        async def stream(client, url, output, limit, **kwargs):
            output.write(b"video")

        with (
            patch("daphne.instagram.stream_media", new=AsyncMock(side_effect=stream)),
            patch("daphne.bot._charge_preview", autospec=True) as charge,
        ):
            await send_carousel_media(self.context.bot, 7, videos, "", self.update)
        self.assertEqual(self.context.bot.send_video.await_count, 2)
        charge.assert_awaited_with(self.update)
        self.assertEqual(charge.await_count, 2)

    @patch("daphne.instagram.try_delete_message", new_callable=AsyncMock)
    @patch("daphne.instagram.send_carousel_media", new_callable=AsyncMock)
    @patch("daphne.instagram.send_photos", new_callable=AsyncMock)
    @patch("daphne.bot.rbac_service")
    @patch("daphne.media_http.content_length", new=AsyncMock(return_value=512 * 1024))
    @patch("daphne.bot.video_upload_limit_mb", return_value=1)
    @patch("parth_dl.extractors.MediaExtractor.extract")
    async def test_carousel_send_failure_keeps_original(
        self, extract, _limit, rbac, photos, videos, delete
    ):
        allowed = MagicMock()
        allowed.is_allowed.return_value = True
        rbac.check_access.return_value = allowed
        extract.return_value = {
            "id": "ABC",
            "type": "carousel",
            "entries": [
                {"kind": "image", "formats": [{"url": "https://cdn.test/1.jpg"}]},
                {"kind": "video", "formats": [{"url": "https://cdn.test/1.mp4"}]},
            ],
        }
        videos.side_effect = RuntimeError("upload failed")
        await handle_instagram_links(self.update, self.context)
        delete.assert_not_awaited()
        self.assertIn(
            "original link was kept",
            self.context.bot.send_message.await_args.kwargs["text"],
        )

    @patch("daphne.instagram.try_delete_message", new_callable=AsyncMock)
    @patch("daphne.instagram.send_carousel_media", new_callable=AsyncMock)
    @patch("daphne.instagram.send_photos", new_callable=AsyncMock)
    @patch("daphne.bot.rbac_service")
    @patch("daphne.media_http.content_length", new=AsyncMock(return_value=512 * 1024))
    @patch("daphne.bot.video_upload_limit_mb", return_value=1)
    @patch("parth_dl.extractors.MediaExtractor.extract")
    async def test_mixed_carousel_sends_every_entry(
        self, extract, _limit, rbac, photos, videos, delete
    ):
        allowed = MagicMock()
        allowed.is_allowed.return_value = True
        rbac.check_access.return_value = allowed
        extract.return_value = {
            "id": "ABC",
            "type": "carousel",
            "entries": [
                {"kind": "image", "formats": [{"url": "https://cdn.test/1.jpg"}]},
                {
                    "kind": "video",
                    "formats": [
                        {"url": "https://cdn.test/1.mp4"},
                        {"url": "https://cdn.test/alt.mp4"},
                    ],
                },
                {"kind": "video", "formats": [{"url": "https://cdn.test/2.mp4"}]},
            ],
        }
        await handle_instagram_links(self.update, self.context)
        delivered = videos.await_args.args[2]
        # The photos ride along with the video delivery now: nothing is sent
        # before every video byte is on disk.
        self.assertEqual(videos.await_args.args[5], ["https://cdn.test/1.jpg"])
        self.assertEqual(
            [v.fetch_url for v in delivered],
            ["https://cdn.test/1.mp4", "https://cdn.test/2.mp4"],
        )
        # The post is what every item's card would print, not the CDN file.
        for video in delivered:
            self.assertEqual(video.canonical_url, PAGE)
        delete.assert_awaited_once()

    @patch("daphne.instagram.try_delete_message", new_callable=AsyncMock)
    @patch("daphne.instagram.send_carousel_media", new_callable=AsyncMock)
    @patch("daphne.instagram.send_photos", new_callable=AsyncMock)
    @patch("daphne.bot.send_video_card", autospec=True)
    @patch("daphne.bot.rbac_service")
    @patch(
        "daphne.media_http.content_length", new=AsyncMock(return_value=2 * 1024 * 1024)
    )
    @patch("daphne.bot.video_upload_limit_mb", return_value=1)
    @patch("parth_dl.extractors.MediaExtractor.extract")
    async def test_an_over_limit_carousel_item_cards_the_video_set(
        self, extract, _limit, rbac, card, photos, videos, delete
    ):
        # One oversized video item cards the whole video set: a half-delivered
        # group is worse than a card. Photos still go out.
        allowed = MagicMock()
        allowed.is_allowed.return_value = True
        rbac.check_access.return_value = allowed
        extract.return_value = {
            "id": "ABC",
            "type": "carousel",
            "entries": [
                {"kind": "image", "formats": [{"url": "https://cdn.test/1.jpg"}]},
                {"kind": "video", "formats": [{"url": "https://cdn.test/1.mp4"}]},
                {"kind": "video", "formats": [{"url": "https://cdn.test/2.mp4"}]},
            ],
        }
        await handle_instagram_links(self.update, self.context)
        photos.assert_awaited_once()
        videos.assert_not_awaited()
        card.assert_awaited_once()
        _update, video, _sender, reason = card.await_args.args
        self.assertEqual(video.fetch_url, "https://cdn.test/1.mp4")
        self.assertEqual(reason, "Video is over 1 MB")
        delete.assert_not_awaited()

    @patch("daphne.instagram.try_delete_message", autospec=True)
    @patch("daphne.instagram.send_photos", new_callable=AsyncMock)
    @patch("daphne.instagram.stream_media", autospec=True)
    @patch("daphne.bot.send_video_card", autospec=True)
    @patch("daphne.bot.rbac_service")
    @patch("daphne.media_http.content_length", new=AsyncMock(return_value=512 * 1024))
    @patch("daphne.bot.video_upload_limit_mb", return_value=1)
    @patch("parth_dl.extractors.MediaExtractor.extract")
    async def test_a_carousel_stream_over_the_cap_cards_the_whole_video_set(
        self, extract, _limit, rbac, card, stream, photos, delete
    ):
        # The gate approved a price under the cap, but the second item's real
        # bytes crossed it mid-stream: every video in the set gets a card --
        # the refused one with the size reason, the fetched ones with a plain
        # card -- so nothing is silently dropped. No partial group, no failure
        # note; the photos still go out. The original message is kept.
        from daphne.media_http import MediaTooLarge

        self.context.bot.send_media_group = AsyncMock()
        allowed = MagicMock()
        allowed.is_allowed.return_value = True
        rbac.check_access.return_value = allowed

        async def stream_side_effect(client, url, output, limit, **kwargs):
            if url.endswith("2.mp4"):
                raise MediaTooLarge("over")
            output.write(b"video")

        stream.side_effect = stream_side_effect
        extract.return_value = {
            "id": "ABC",
            "type": "carousel",
            "entries": [
                {"kind": "image", "formats": [{"url": "https://cdn.test/1.jpg"}]},
                {"kind": "video", "formats": [{"url": "https://cdn.test/1.mp4"}]},
                {"kind": "video", "formats": [{"url": "https://cdn.test/2.mp4"}]},
            ],
        }
        await handle_instagram_links(self.update, self.context)
        self.context.bot.send_media_group.assert_not_awaited()
        self.assertEqual(card.await_count, 2)
        carded = {call.args[1].fetch_url: call.args[3] for call in card.await_args_list}
        self.assertEqual(
            carded,
            {
                "https://cdn.test/1.mp4": "Video Details",
                "https://cdn.test/2.mp4": "Video is over 1 MB",
            },
        )
        photos.assert_awaited_once()
        delete.assert_not_awaited()

    @patch("daphne.instagram.try_delete_message", autospec=True)
    @patch("daphne.instagram.send_photos", new_callable=AsyncMock)
    @patch("daphne.instagram.stream_media", autospec=True)
    @patch("daphne.bot.rbac_service")
    @patch("daphne.media_http.content_length", new=AsyncMock(return_value=512 * 1024))
    @patch("daphne.bot.video_upload_limit_mb", return_value=1)
    @patch("parth_dl.extractors.MediaExtractor.extract")
    async def test_a_transport_failure_before_photos_leaves_them_unsent(
        self, extract, _limit, rbac, stream, photos, delete
    ):
        # Photos ride along AFTER the video sends: a transport error on the
        # album means no video and no photos went out, which is exactly what
        # the failure note claims (no half-delivery in either direction).
        self.context.bot.send_media_group = AsyncMock(side_effect=RuntimeError("net"))
        self.context.bot.send_video = AsyncMock(side_effect=RuntimeError("net"))
        allowed = MagicMock()
        allowed.is_allowed.return_value = True
        rbac.check_access.return_value = allowed

        async def stream_side_effect(client, url, output, limit, **kwargs):
            output.write(b"video")

        stream.side_effect = stream_side_effect
        extract.return_value = {
            "id": "ABC",
            "type": "carousel",
            "entries": [
                {"kind": "image", "formats": [{"url": "https://cdn.test/1.jpg"}]},
                {"kind": "video", "formats": [{"url": "https://cdn.test/1.mp4"}]},
            ],
        }
        await handle_instagram_links(self.update, self.context)
        photos.assert_not_awaited()
        self.context.bot.send_message.assert_awaited_once()
        delete.assert_not_awaited()

    @patch("daphne.instagram.try_delete_message", autospec=True)
    @patch("daphne.instagram.stream_media", autospec=True)
    @patch("daphne.bot.send_video_card", autospec=True)
    @patch("daphne.bot.rbac_service")
    @patch(
        "daphne.media_http.content_length", new=AsyncMock(return_value=2 * 1024 * 1024)
    )
    @patch("daphne.bot.video_upload_limit_mb", return_value=1)
    @patch("parth_dl.extractors.MediaExtractor.extract")
    async def test_oversized_video_cards_before_downloading(
        self, extract, _limit, rbac, card, stream, delete
    ):
        # The CDN states a size over the limit: the gate cards without fetching
        # a single media byte (the old path streamed to the cap first).
        allowed = MagicMock()
        allowed.is_allowed.return_value = True
        rbac.check_access.return_value = allowed
        extract.return_value = {
            "id": "ABC",
            "type": "video",
            "title": "Video by someone",
            "uploader": "someone",
            "formats": [{"url": "https://cdn.test/1.mp4"}],
        }
        await handle_instagram_links(self.update, self.context)
        card.assert_awaited_once()
        update, video, _sender, reason = card.await_args.args
        self.assertIs(update, self.update)
        self.assertEqual(video.platform, "instagram")
        self.assertEqual(video.canonical_url, "https://www.instagram.com/p/DRZSlC7D3OT")
        self.assertEqual(reason, "Video is over 1 MB")
        stream.assert_not_called()
        delete.assert_not_awaited()

    @patch("daphne.instagram.try_delete_message", autospec=True)
    @patch("daphne.instagram.stream_media", autospec=True)
    @patch("daphne.bot.send_video_card", autospec=True)
    @patch("daphne.bot.rbac_service")
    @patch("daphne.media_http.content_length", new=AsyncMock(return_value=None))
    @patch("daphne.bot.video_upload_limit_mb", return_value=1)
    @patch("parth_dl.extractors.MediaExtractor.extract")
    async def test_an_unpriceable_video_cards_instead_of_guessing(
        self, extract, _limit, rbac, card, stream, delete
    ):
        # No HEAD size: unknown means card with a Download button, not a blind
        # download; and the yt-dlp pipeline is not burned retrying it.
        allowed = MagicMock()
        allowed.is_allowed.return_value = True
        rbac.check_access.return_value = allowed
        extract.return_value = {
            "id": "ABC",
            "type": "video",
            "title": "Video by someone",
            "uploader": "someone",
            "formats": [{"url": "https://cdn.test/1.mp4"}],
        }
        with patch("daphne.bot.handle_video_url", autospec=True) as fallback:
            await handle_instagram_links(self.update, self.context)
        card.assert_awaited_once()
        _update, _video, _sender, reason = card.await_args.args
        self.assertEqual(reason, "Video Details")
        stream.assert_not_called()
        fallback.assert_not_called()
        delete.assert_not_awaited()

    @patch("daphne.bot.handle_video_url", autospec=True)
    @patch("daphne.instagram.stream_media", autospec=True)
    @patch("daphne.bot.rbac_service")
    @patch("daphne.media_http.content_length", new=AsyncMock(return_value=512 * 1024))
    @patch("daphne.bot.video_upload_limit_mb", return_value=1)
    @patch("parth_dl.extractors.MediaExtractor.extract")
    async def test_a_cdnc_price_that_lied_cards_the_real_bytes(
        self, extract, _limit, rbac, stream, fallback
    ):
        # The gate approved a price under the cap, but the stream crossed it:
        # the real-bytes backstop cards the priced video mid-download.
        from daphne.media_http import MediaTooLarge

        allowed = MagicMock()
        allowed.is_allowed.return_value = True
        rbac.check_access.return_value = allowed
        stream.side_effect = MediaTooLarge()
        extract.return_value = {
            "id": "ABC",
            "type": "video",
            "title": "Video by someone",
            "uploader": "someone",
            "formats": [{"url": "https://cdn.test/1.mp4"}],
        }
        with patch("daphne.bot.send_video_card", autospec=True) as card:
            await handle_instagram_links(self.update, self.context)
        card.assert_awaited_once()
        _update, video, _sender, reason = card.await_args.args
        self.assertEqual(video.estimated_bytes, 512 * 1024)
        self.assertEqual(reason, "Video is over 1 MB")
        fallback.assert_not_called()

    @patch("daphne.bot.send_video_card", autospec=True)
    @patch("daphne.instagram.stream_media", autospec=True)
    @patch("daphne.bot.rbac_service")
    @patch("daphne.media_http.content_length", new=AsyncMock(return_value=512 * 1024))
    @patch("daphne.bot.video_upload_limit_mb", return_value=1)
    @patch("parth_dl.extractors.MediaExtractor.extract")
    async def test_direct_download_failure_cards_instead_of_a_second_cycle(
        self, extract, _limit, rbac, stream, card
    ):
        # The gate approved the price, but the CDN stream died. The card's
        # Download button runs the full pipeline on demand; there is no silent
        # second resolve+download cycle anymore.
        with patch("daphne.bot.handle_video_url", autospec=True) as fallback:
            allowed = MagicMock()
            allowed.is_allowed.return_value = True
            rbac.check_access.return_value = allowed
            stream.side_effect = RuntimeError("boom")
            extract.return_value = {
                "id": "ABC",
                "type": "video",
                "title": "Video by someone",
                "uploader": "someone",
                "formats": [{"url": "https://cdn.test/1.mp4"}],
            }
            await handle_instagram_links(self.update, self.context)
        fallback.assert_not_called()
        card.assert_awaited_once()
        _update, video, _sender, reason = card.await_args.args
        self.assertEqual(video.estimated_bytes, 512 * 1024)
        self.assertIn("Direct download failed", reason)

    @patch("daphne.bot.send_video_card", autospec=True)
    @patch("daphne.bot.handle_video_url", autospec=True)
    @patch("daphne.bot.rbac_service")
    @patch("parth_dl.extractors.MediaExtractor.extract")
    async def test_a_non_public_cdn_url_cards_instead_of_raising(
        self, extract, rbac, fallback, card
    ):
        # check_public_url rejects the URL; pricing must come back None (not an
        # exception), so the gate cards NO_PLAN and nothing retries.
        allowed = MagicMock()
        allowed.is_allowed.return_value = True
        rbac.check_access.return_value = allowed
        extract.return_value = {
            "id": "ABC",
            "type": "video",
            "title": "Video by someone",
            "uploader": "someone",
            "formats": [{"url": "http://127.0.0.1/1.mp4"}],
        }
        await handle_instagram_links(self.update, self.context)
        fallback.assert_not_called()
        card.assert_awaited_once()
        self.assertEqual(card.await_args.args[3], "Video Details")

    @patch("daphne.bot.handle_video_url", autospec=True)
    @patch("daphne.downloader.fetch_instagram_fallback_media", return_value=None)
    @patch("parth_dl.extractors.MediaExtractor.extract")
    async def test_extraction_failure_fails_fast_no_video_fallback(
        self, mock_extract, mock_yt_dlp_fallback, mock_handle_video_url
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
        mock_handle_video_url.assert_not_called()

        self.context.bot.send_message.assert_called_once()
        _, kwargs = self.context.bot.send_message.call_args
        self.assertEqual(kwargs["chat_id"], -1002058191932)
        self.assertIn("Couldn't fetch this Instagram post", kwargs["text"])
        self.assertIn("https://www.instagram.com/p/DRZSlC7D3OT", kwargs["text"])

        self.update.message.set_reaction.assert_called_once()

    @patch("daphne.bot.handle_video_url", autospec=True)
    @patch("daphne.downloader.fetch_instagram_fallback_media", return_value=None)
    @patch("parth_dl.extractors.MediaExtractor.extract", return_value=None)
    async def test_no_data_returned_fails_fast_no_video_fallback(
        self, mock_extract, mock_yt_dlp_fallback, mock_handle_video_url
    ):
        # Same fail-fast contract when extract() returns falsy instead of
        # raising, and the yt-dlp fallback also comes up empty.
        await handle_instagram_links(self.update, self.context)

        mock_handle_video_url.assert_not_called()
        self.context.bot.send_message.assert_called_once()

    @patch("daphne.bot.handle_video_url", autospec=True)
    @patch("parth_dl.extractors.MediaExtractor.extract", return_value=None)
    async def test_parth_dl_failure_recovers_via_yt_dlp_image_fallback(
        self, mock_extract, mock_handle_video_url
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

        mock_handle_video_url.assert_not_called()
        self.context.bot.send_message.assert_not_called()
        self.context.bot.send_photo.assert_called_once()
        _, kwargs = self.context.bot.send_photo.call_args
        self.assertEqual(kwargs["chat_id"], -1002058191932)
        self.assertEqual(kwargs["photo"], "https://instagram.fna.fbcdn.net/photo.jpg")
        # Uploader name with a "." (a real production value) collapses into
        # one author hashtag rather than splitting or being dropped.
        self.assertIn("#instagram #nyarumaa_cosplay", kwargs["caption"])

    @patch("daphne.bot.handle_video_url", autospec=True)
    @patch("parth_dl.extractors.MediaExtractor.extract", return_value=None)
    async def test_no_uploader_skips_author_hashtag(
        self, mock_extract, mock_handle_video_url
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

    @patch("daphne.bot.handle_video_url", autospec=True)
    @patch("parth_dl.extractors.MediaExtractor.extract", return_value=None)
    async def test_multi_word_and_non_ascii_uploader_hashtag(
        self, mock_extract, mock_handle_video_url
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
