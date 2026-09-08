import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from daphne.pixiv import (
    PixivInfo,
    build_caption,
    extract_pixiv_id,
    fetch_artwork_info,
    fetch_pixiv_image,
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


if __name__ == "__main__":
    unittest.main()
