import json
import os
import stat
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from daphne import pixiv
from daphne.pixiv import (
    PixivInfo,
    build_caption,
    extract_pixiv_id,
    fetch_artwork_info,
    fetch_pixiv_image,
    handle_pixiv_links,
    to_telegram_tag,
)


class TestPixiv(unittest.IsolatedAsyncioTestCase):
    def test_extract_pixiv_id(self):
        self.assertEqual(
            extract_pixiv_id("https://www.pixiv.net/en/artworks/12345678?foo=bar"),
            "12345678",
        )
        self.assertEqual(
            extract_pixiv_id("check https://pixiv.net/artworks/87654321"),
            "87654321",
        )
        self.assertIsNone(extract_pixiv_id("https://www.pixiv.net/users/12345678"))

    def test_to_telegram_tag(self):
        self.assertEqual(to_telegram_tag("#fantasy art"), "#fantasy_art")
        self.assertEqual(to_telegram_tag("R-18"), "#R_18")
        self.assertEqual(to_telegram_tag("女の子"), "#女の子")
        self.assertEqual(to_telegram_tag("オリジナル"), "#オリジナル")
        self.assertEqual(
            to_telegram_tag("Re:ゼロから始める異世界生活"),
            "#Re_ゼロから始める異世界生活",
        )
        self.assertEqual(to_telegram_tag("＃東方Project"), "#東方Project")
        self.assertEqual(to_telegram_tag("10000users入り"), "#10000users入り")
        self.assertIsNone(to_telegram_tag("🎨🎨🎨"))
        self.assertIsNone(to_telegram_tag(""))
        self.assertIsNone(to_telegram_tag("   "))
        self.assertIsNone(to_telegram_tag("###"))

    def test_build_caption_with_info(self):
        info = PixivInfo("A < B", "artist & co", ["fantasy art"])
        caption = build_caption(
            "https://www.pixiv.net/en/artworks/123",
            "https://pixiv.cat/123.jpg",
            info,
            "via @haru",
        )
        self.assertIn("<b>A &lt; B</b>", caption)
        self.assertIn("artist &amp; co", caption)
        self.assertIn("#pixiv #fantasy_art", caption)
        self.assertIn("via @haru", caption)
        self.assertIn(
            '<a href="https://www.pixiv.net/en/artworks/123">🔗 Source (Pixiv)</a>',
            caption,
        )
        self.assertNotIn("pixiv.cat", caption)

    def test_build_caption_with_unicode_tags(self):
        info = PixivInfo("双子", "柊", ["女の子", "オリジナル", "Skeb", "白髪"])
        caption = build_caption(
            "https://www.pixiv.net/artworks/149706124",
            info=info,
            sender="via @xxx",
        )
        self.assertIn("<b>双子</b>", caption)
        self.assertIn("<b>Author:</b> 柊", caption)
        self.assertIn("#pixiv #女の子 #オリジナル #Skeb #白髪", caption)
        self.assertNotIn("#pixiv #pixiv", caption)

    def test_build_caption_deduplicates_tags(self):
        info = PixivInfo(
            "Art",
            "Author",
            ["pixiv", "Pixiv", "Skeb", "skeb", "🎨🎨", "#fantasy art"],
        )
        caption = build_caption("https://www.pixiv.net/artworks/123", info=info)
        self.assertIn("#pixiv #Skeb #fantasy_art", caption)
        self.assertNotIn("#pixiv #pixiv", caption)
        self.assertNotIn("#skeb", caption.lower().replace("#skeb", "", 1))

    def test_build_caption_without_info(self):
        caption = build_caption("https://www.pixiv.net/artworks/123")
        self.assertIn("#pixiv", caption)
        self.assertNotIn("#pixiv #pixiv", caption)

    async def test_metadata_comes_from_pixiv_ajax(self):
        pixiv_response = MagicMock(status_code=200)
        pixiv_response.json.return_value = {
            "body": {
                "title": "Artwork",
                "userName": "Artist",
                "tags": {"tags": [{"tag": "blue sky"}]},
                "urls": {
                    "regular": "https://i.pximg.net/regular.jpg",
                    "original": "https://i.pximg.net/original.jpg",
                },
            }
        }
        client = MagicMock()
        client.get = AsyncMock(side_effect=[pixiv_response])

        with patch("daphne.pixiv.httpx.AsyncClient", return_value=client):
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            info = await fetch_artwork_info("123")

        self.assertEqual(info.title, "Artwork")
        self.assertEqual(info.author_name, "Artist")
        self.assertEqual(info.tags, ["blue sky"])
        self.assertEqual(
            info.image_urls,
            ["https://i.pximg.net/original.jpg", "https://i.pximg.net/regular.jpg"],
        )

    async def test_image_fallback_skips_failed_proxy_candidate(self):
        failed = MagicMock(status_code=500)
        success = MagicMock(status_code=200, content=b"image")
        client = MagicMock()
        client.get = AsyncMock(side_effect=[failed, failed, success])

        with patch("daphne.pixiv.httpx.AsyncClient", return_value=client):
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            content, url = await fetch_pixiv_image(
                "123", ["https://i.pximg.net/original.jpg"]
            )

        self.assertEqual(content, b"image")
        self.assertEqual(url, "https://i.pximg.net/original.jpg")

    async def test_image_fallback_uses_pixiv_preview_when_original_is_unavailable(self):
        failed = MagicMock(status_code=404)
        success = MagicMock(status_code=200, content=b"preview")
        client = MagicMock()
        client.get = AsyncMock(side_effect=[failed, failed, failed, failed, success])

        with patch("daphne.pixiv.httpx.AsyncClient", return_value=client):
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            content, url = await fetch_pixiv_image(
                "123",
                [
                    "https://i.pximg.net/c/250x250_80_a2/custom-thumb/"
                    "img/2026/09/08/19/16/11/123_p0_custom1200.jpg"
                ],
                page=0,
            )

        self.assertEqual(content, b"preview")
        self.assertTrue(url.endswith("123_p0_custom1200.jpg"))

    async def test_image_fallback_uses_master_preview_for_missing_page_variant(self):
        failed = MagicMock(status_code=404)
        success = MagicMock(status_code=200, content=b"master")
        client = MagicMock()
        client.get = AsyncMock(side_effect=[failed, failed, failed, success])

        with patch("daphne.pixiv.httpx.AsyncClient", return_value=client):
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            content, url = await fetch_pixiv_image(
                "123",
                [
                    "https://i.pximg.net/c/250x250_80_a2/custom-thumb/"
                    "img/2026/09/08/19/16/11/123_p1_custom1200.jpg"
                ],
                page=1,
            )

        self.assertEqual(content, b"master")
        self.assertIn("540x540_70/img-master", url)
        self.assertTrue(url.endswith("123_p1_master1200.jpg"))

    async def test_metadata_expands_restricted_preview_to_all_pages(self):
        pixiv_response = MagicMock(status_code=200)
        pixiv_response.json.return_value = {
            "body": {
                "id": "149428101",
                "title": "Restricted artwork",
                "userName": "Artist",
                "pageCount": 2,
                "urls": {"original": None, "regular": None},
                "userIllusts": {
                    "149428101": {
                        "url": (
                            "https://i.pximg.net/c/250x250_80_a2/"
                            "custom-thumb/img/2026/09/08/19/16/11/"
                            "149428101-hash_p0_custom1200.jpg"
                        )
                    }
                },
            }
        }
        client = MagicMock()
        client.get = AsyncMock(side_effect=[pixiv_response])

        with patch("daphne.pixiv.httpx.AsyncClient", return_value=client):
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=None)
            info = await fetch_artwork_info("149428101")

        self.assertEqual(
            info.image_urls,
            [
                "https://i.pximg.net/c/250x250_80_a2/"
                "custom-thumb/img/2026/09/08/19/16/11/"
                "149428101-hash_p0_custom1200.jpg",
                "https://i.pximg.net/c/250x250_80_a2/"
                "custom-thumb/img/2026/09/08/19/16/11/"
                "149428101-hash_p1_custom1200.jpg",
            ],
        )
        self.assertEqual(info.page_count, 2)

    async def test_handler_limits_pixiv_pages_and_shows_remaining_count(self):
        update = MagicMock()
        update.message.text = "https://www.pixiv.net/artworks/123"
        update.message.chat_id = 42
        update.message.reply_to_message = None
        update.message.is_topic_message = False
        update.message.delete = AsyncMock()
        context = MagicMock()
        context.bot.send_chat_action = AsyncMock()
        context.bot.send_media_group = AsyncMock()

        info = PixivInfo(
            "Artwork",
            "Artist",
            [],
            [f"https://cdn/p{index}.jpg" for index in range(5)],
            page_count=5,
        )
        fetch_image = AsyncMock(
            side_effect=[
                (f"page {index}".encode(), f"https://cdn/p{index}.jpg")
                for index in range(3)
            ]
        )
        with (
            patch("daphne.pixiv.fetch_artwork_info", new=AsyncMock(return_value=info)),
            patch("daphne.pixiv.fetch_pixiv_image", new=fetch_image),
        ):
            await handle_pixiv_links(update, context)

        context.bot.send_media_group.assert_awaited_once()
        media = context.bot.send_media_group.await_args.kwargs["media"]
        self.assertEqual(len(media), 3)
        self.assertIn("+2 more images on Pixiv", media[0].caption)
        self.assertEqual(fetch_image.await_count, 3)
        context.bot.send_photo.assert_not_called()
        update.message.delete.assert_awaited_once()

    async def test_handler_failure_keeps_original_and_sets_failure_reaction(self):
        update = MagicMock()
        update.message.text = "https://www.pixiv.net/artworks/123"
        update.message.chat_id = 42
        update.message.reply_text = AsyncMock()
        update.message.reply_to_message = None
        update.message.is_topic_message = False
        update.message.delete = AsyncMock()
        context = MagicMock()
        context.bot.send_chat_action = AsyncMock()

        with (
            patch("daphne.pixiv.fetch_artwork_info", new=AsyncMock(return_value=None)),
            patch(
                "daphne.pixiv.fetch_pixiv_image",
                new=AsyncMock(side_effect=ValueError("unavailable")),
            ),
            patch("daphne.bot.set_reaction", new=AsyncMock()) as set_reaction,
        ):
            await handle_pixiv_links(update, context)

        set_reaction.assert_awaited_once_with(update.message, "😢")
        context.bot.send_message.assert_not_called()
        update.message.delete.assert_not_awaited()


class TestPixivFallbacks(unittest.IsolatedAsyncioTestCase):
    def _update(self):
        update = MagicMock()
        update.message.text = "https://www.pixiv.net/artworks/123"
        update.message.chat_id = 42
        update.message.reply_to_message = None
        update.message.is_topic_message = False
        update.message.delete = AsyncMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.bot.send_chat_action = AsyncMock()
        context.bot.send_photo = AsyncMock()
        return update, context

    async def test_unreadable_work_without_a_token_says_so(self):
        update, context = self._update()
        with (
            patch("daphne.pixiv.fetch_artwork_info", new=AsyncMock(return_value=None)),
            patch(
                "daphne.pixiv.fetch_pixiv_image",
                new=AsyncMock(side_effect=ValueError("x")),
            ),
            patch.dict("os.environ", {}, clear=True),
            patch("daphne.bot.set_reaction", new=AsyncMock()) as set_reaction,
        ):
            await handle_pixiv_links(update, context)
        set_reaction.assert_awaited_once_with(update.message, "😢")
        sent = update.message.reply_text.call_args[0][0]
        self.assertIn("Age-restricted works need a Pixiv login", sent)
        context.bot.send_photo.assert_not_called()

    async def test_gallery_fallback_is_used_when_the_cdn_route_fails(self):
        update, context = self._update()
        with (
            patch("daphne.pixiv.fetch_artwork_info", new=AsyncMock(return_value=None)),
            patch(
                "daphne.pixiv._fetch_primary",
                new=AsyncMock(side_effect=ValueError("x")),
            ),
            patch(
                "daphne.pixiv._gallery_fallback_sync",
                return_value=[(b"png-bytes", "123_p0.png")],
            ),
            patch("daphne.bot.set_reaction", new=AsyncMock()),
            patch.dict("os.environ", {"DAPHNE_PIXIV_REFRESH_TOKEN": "t"}),
        ):
            await handle_pixiv_links(update, context)
        context.bot.send_photo.assert_awaited_once()

    def test_gallery_fallback_does_nothing_without_a_token(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("daphne.pixiv.download_gallery") as download,
        ):
            self.assertEqual(pixiv._gallery_fallback_sync("123"), [])
        download.assert_not_called()

    def test_token_goes_into_a_private_config_file_not_the_arguments(self):
        seen = {}

        def fake_download(url, out_dir, extra_args=()):
            config = extra_args[extra_args.index("--config") + 1]
            seen["args"] = list(extra_args)
            seen["mode"] = stat.S_IMODE(os.stat(config).st_mode)
            seen["config"] = json.loads(Path(config).read_text())
            return []

        with (
            patch.dict("os.environ", {"DAPHNE_PIXIV_REFRESH_TOKEN": "secret-token"}),
            patch("daphne.pixiv.download_gallery", side_effect=fake_download),
        ):
            self.assertEqual(pixiv._gallery_fallback_sync("123"), [])
        self.assertEqual(seen["mode"], 0o600)
        self.assertEqual(
            seen["config"]["extractor"]["pixiv"]["refresh-token"], "secret-token"
        )
        self.assertFalse(any("secret-token" in arg for arg in seen["args"]))

    def test_gallery_pages_are_ordered_by_page_number(self):
        paths = ["/x/1_p10.jpg", "/x/1_p2.jpg", "/x/1_p0.jpg"]
        self.assertEqual(
            sorted(paths, key=pixiv._page_index),
            ["/x/1_p0.jpg", "/x/1_p2.jpg", "/x/1_p10.jpg"],
        )


class TestPixivRestrictedRouting(unittest.IsolatedAsyncioTestCase):
    def _update(self):
        update = MagicMock()
        update.message.text = "https://www.pixiv.net/artworks/123"
        update.message.chat_id = 42
        update.message.reply_to_message = None
        update.message.is_topic_message = False
        update.message.delete = AsyncMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.bot.send_chat_action = AsyncMock()
        context.bot.send_photo = AsyncMock()
        return update, context

    async def test_restricted_work_with_a_login_goes_to_the_login_route(self):
        update, context = self._update()
        restricted = PixivInfo(
            title="t", author_name="a", tags=[], image_urls=None, restricted=True
        )
        primary = AsyncMock()
        with (
            patch(
                "daphne.pixiv.fetch_artwork_info",
                new=AsyncMock(return_value=restricted),
            ),
            patch("daphne.pixiv._fetch_primary", new=primary),
            patch(
                "daphne.pixiv._gallery_fallback_sync",
                return_value=[(b"img", "123_p0.png")],
            ),
            patch("daphne.bot.download_slot", return_value=_NoSlot()),
            patch("daphne.bot.set_reaction", new=AsyncMock()),
            patch.dict("os.environ", {"DAPHNE_PIXIV_REFRESH_TOKEN": "t"}),
        ):
            await handle_pixiv_links(update, context)
        primary.assert_not_called()
        context.bot.send_photo.assert_awaited_once()
        update.message.reply_text.assert_not_called()

    async def test_restricted_work_without_a_login_says_so(self):
        update, context = self._update()
        restricted = PixivInfo(
            title="t", author_name="a", tags=[], image_urls=None, restricted=True
        )
        with (
            patch(
                "daphne.pixiv.fetch_artwork_info",
                new=AsyncMock(return_value=restricted),
            ),
            patch("daphne.pixiv._gallery_fallback_sync") as login,
            patch("daphne.bot.set_reaction", new=AsyncMock()),
            patch.dict("os.environ", {}, clear=True),
        ):
            await handle_pixiv_links(update, context)
        login.assert_not_called()
        sent = update.message.reply_text.call_args[0][0]
        self.assertIn("age-restricted", sent)

    def test_login_config_keeps_the_cache_in_memory_and_reads_few_pages(self):
        seen = {}

        def fake_download(url, out_dir, extra_args=()):
            config = extra_args[extra_args.index("--config") + 1]
            seen["config"] = json.loads(Path(config).read_text())
            seen["args"] = list(extra_args)
            return []

        with (
            patch.dict("os.environ", {"DAPHNE_PIXIV_REFRESH_TOKEN": "t"}),
            patch("daphne.pixiv.download_gallery", side_effect=fake_download),
        ):
            pixiv._gallery_fallback_sync("123")
        self.assertEqual(seen["config"]["cache"], {"file": ":memory:"})
        self.assertIn("--range", seen["args"])
        self.assertEqual(seen["args"][seen["args"].index("--range") + 1], "1-3")


class _NoSlot:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


if __name__ == "__main__":
    unittest.main()
