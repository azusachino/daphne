import unittest
from unittest.mock import patch

import httpx

from daphne.doh import check_public_url
from daphne.downloader import run_tool
from daphne.gallery import download_gallery


class TestDoHPreflight(unittest.TestCase):
    def _client(self, a, aaaa, seen):
        def response(request):
            seen.append(request)
            values = a if request.url.params["type"] == "A" else aaaa
            return httpx.Response(200, json={"Status": 0, "Answer": values})

        return httpx.Client(transport=httpx.MockTransport(response))

    def test_public_answers_send_only_hostname(self):
        seen = []
        client = self._client(
            [{"type": 1, "data": "8.8.8.8"}],
            [{"type": 28, "data": "2606:4700:4700::1111"}],
            seen,
        )
        with patch("daphne.doh.httpx.Client", return_value=client):
            check_public_url("https://example.com/private-path?secret=hidden")
        self.assertEqual(len(seen), 2)
        self.assertTrue(
            all(request.url.params["name"] == "example.com" for request in seen)
        )
        self.assertTrue(all("secret" not in str(request.url) for request in seen))

    def test_mixed_public_private_answers_are_rejected(self):
        seen = []
        client = self._client(
            [{"type": 1, "data": "8.8.8.8"}], [{"type": 28, "data": "::1"}], seen
        )
        with patch("daphne.doh.httpx.Client", return_value=client):
            with self.assertRaises(ValueError):
                check_public_url("https://example.com/post")

    def test_empty_or_failed_dns_is_rejected(self):
        for a, aaaa in (([], []), ([{"type": 1, "data": "not-an-ip"}], [])):
            client = self._client(a, aaaa, [])
            with patch("daphne.doh.httpx.Client", return_value=client):
                with self.assertRaises(ValueError):
                    check_public_url("https://example.com/post")
        with patch(
            "daphne.doh.httpx.Client", side_effect=httpx.ConnectError("offline")
        ):
            with self.assertRaises(ValueError):
                check_public_url("https://example.com/post")

    def test_private_addresses_and_credentials_are_rejected(self):
        for url in (
            "http://127.0.0.1/a",
            "http://[::ffff:127.0.0.1]/",
            "https://a@8.8.8.8/",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                check_public_url(url)

    def test_tool_does_not_start_when_check_fails(self):
        with (
            patch(
                "daphne.downloader.check_public_url", side_effect=ValueError("rejected")
            ),
            patch("daphne.downloader.subprocess.Popen") as popen,
        ):
            with self.assertRaises(ValueError):
                run_tool(["yt-dlp", "--", "https://example.com/video"], 1)
        popen.assert_not_called()

    def test_gallery_does_not_start_when_check_fails(self):
        import tempfile

        with (
            tempfile.TemporaryDirectory() as folder,
            patch(
                "daphne.gallery.check_public_url", side_effect=ValueError("rejected")
            ),
            patch("daphne.gallery.subprocess.run") as run,
        ):
            self.assertEqual(download_gallery("https://example.com/post", folder), [])
        run.assert_not_called()
