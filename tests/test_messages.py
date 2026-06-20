import unittest
from unittest.mock import MagicMock

from daphne.messages import append_footer, escape_html, sender_attribution


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


if __name__ == "__main__":
    unittest.main()
