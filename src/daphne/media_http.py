"""Bounded streaming for direct social-media downloads."""

import asyncio

from daphne.doh import check_public_url


class MediaTooLarge(ValueError):
    pass


async def stream_media(
    client, url, sink, limit: int, *, headers=None, timeout=30.0
) -> None:
    await asyncio.to_thread(check_public_url, url)
    size = 0
    async with client.stream("GET", url, headers=headers, timeout=timeout) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > limit:
                raise MediaTooLarge("Media exceeds the download limit")
            sink.write(chunk)
