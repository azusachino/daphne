import unittest
from unittest.mock import AsyncMock, MagicMock, patch

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

    async def test_metadata_falls_back_to_pixiv_ajax(self):
        phixiv_response = MagicMock(status_code=200)
        phixiv_response.json.return_value = {
            "message": "The phixiv API is no longer available"
        }
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
        client.get = AsyncMock(side_effect=[phixiv_response, pixiv_response])

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
        phixiv_response = MagicMock(status_code=200)
        phixiv_response.json.return_value = {
            "message": "The phixiv API is no longer available"
        }
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
        client.get = AsyncMock(side_effect=[phixiv_response, pixiv_response])

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


if __name__ == "__main__":
    unittest.main()
