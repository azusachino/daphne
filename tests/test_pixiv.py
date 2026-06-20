import unittest

from daphne.pixiv import PixivInfo, build_caption, extract_pixiv_id, to_telegram_tag


class TestPixiv(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
