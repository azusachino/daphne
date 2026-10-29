import io
import unittest

import httpx

from daphne.media_http import MediaTooLarge, stream_media


class TestMediaStreaming(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_oversized_response(self):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"12345")
        )
        async with httpx.AsyncClient(transport=transport) as client:
            output = io.BytesIO()
            with self.assertRaises(MediaTooLarge):
                await stream_media(client, "https://cdn.example/file", output, 4)
            self.assertEqual(output.getvalue(), b"")

    async def test_downloads_within_limit(self):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"1234")
        )
        async with httpx.AsyncClient(transport=transport) as client:
            output = io.BytesIO()
            await stream_media(client, "https://cdn.example/file", output, 4)
            self.assertEqual(output.getvalue(), b"1234")
