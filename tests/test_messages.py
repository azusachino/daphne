import unittest
from unittest.mock import MagicMock

from daphne.messages import (
    HtmlMessage,
    append_footer,
    author_tag,
    escape_html,
    sender_attribution,
    slugify_tag,
)


class TestMessages(unittest.TestCase):
    def test_escape_html(self):
        self.assertEqual(escape_html('<a x="1">&'), "&lt;a x=&quot;1&quot;&gt;&amp;")

    def test_sender_attribution_username(self):
        user = MagicMock()
        user.username = "haru"
        user.full_name = "Haru"
        self.assertEqual(sender_attribution(user), "via @haru")

    def test_append_footer(self):
        text = append_footer("<b>Hello</b>", "via @haru")
        self.assertIn("<b>Hello</b>", text)
        self.assertIn("daphne", text)
        self.assertIn("via @haru", text)

    def test_html_message_builder(self):
        text = (
            HtmlMessage(sender="via @haru")
            .title("A < B")
            .fields(("Uploader", "me & you"))
            .link("https://example.com/?a=1&b=2")
            .tags("twitter", "#art")
            .render()
        )
        self.assertIn("<b>A &lt; B</b>", text)
        self.assertIn("<b>Uploader:</b> me &amp; you", text)
        self.assertIn(
            '<a href="https://example.com/?a=1&amp;b=2">https://example.com/?a=1&amp;b=2</a>',
            text,
        )
        self.assertIn("#twitter #art", text)
        self.assertIn("via @haru", text)

    def test_slugify_tag_multi_word_name(self):
        # Multiple words/punctuation collapse into one underscore-joined slug,
        # not a tag per word.
        self.assertEqual(slugify_tag("The New York Times"), "the_new_york_times")
        self.assertEqual(slugify_tag("Uploader #1"), "uploader_1")

    def test_slugify_tag_non_ascii_name(self):
        # Non-Latin scripts are preserved, not stripped to nothing.
        self.assertEqual(slugify_tag("山田太郎"), "山田太郎")
        # Full-width and half-width forms of the "same" name normalize (NFKC)
        # to the same slug.
        self.assertEqual(slugify_tag("ＨＡＲＵ"), slugify_tag("HARU"))

    def test_slugify_tag_empty_or_symbols_only(self):
        self.assertEqual(slugify_tag(""), "")
        self.assertEqual(slugify_tag("🎨🎨🎨"), "")

    def test_author_tag_skips_missing_or_unknown(self):
        self.assertIsNone(author_tag(None))
        self.assertIsNone(author_tag(""))
        self.assertIsNone(author_tag("unknown"))
        self.assertIsNone(author_tag("Unknown"))
        self.assertIsNone(author_tag("🎨🎨🎨"))

    def test_author_tag_returns_slug_for_real_name(self):
        self.assertEqual(author_tag("waterloo_intern"), "waterloo_intern")
        self.assertEqual(author_tag("The New York Times"), "the_new_york_times")


if __name__ == "__main__":
    unittest.main()
