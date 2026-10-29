import io
import unittest
from unittest.mock import patch

import httpx

from daphne.media_http import MediaTooLarge, stream_media


class TestMediaStreaming(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_oversized_response(self):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"12345")
        )
        with patch("daphne.media_http.check_public_url"):
            async with httpx.AsyncClient(transport=transport) as client:
                output = io.BytesIO()
                with self.assertRaises(MediaTooLarge):
                    await stream_media(client, "https://cdn.example/file", output, 4)
                self.assertEqual(output.getvalue(), b"")

    async def test_rejects_private_before_fetch(self):
        transport = httpx.MockTransport(lambda request: self.fail("unexpected request"))
        with patch(
            "daphne.media_http.check_public_url", side_effect=ValueError("private")
        ):
            async with httpx.AsyncClient(transport=transport) as client:
                with self.assertRaises(ValueError):
                    await stream_media(client, "http://127.0.0.1", io.BytesIO(), 4)

    async def test_downloads_within_limit(self):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"1234")
        )
        with patch("daphne.media_http.check_public_url"):
            async with httpx.AsyncClient(transport=transport) as client:
                output = io.BytesIO()
                await stream_media(client, "https://cdn.example/file", output, 4)
                self.assertEqual(output.getvalue(), b"1234")
