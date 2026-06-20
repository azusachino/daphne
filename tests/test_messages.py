import unittest
from unittest.mock import MagicMock

from daphne.messages import HtmlMessage, append_footer, escape_html, sender_attribution


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


if __name__ == "__main__":
    unittest.main()
