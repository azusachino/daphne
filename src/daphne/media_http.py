"""Bounded streaming for direct social-media downloads."""

import asyncio

import httpx

from daphne.doh import check_public_url


class MediaTooLarge(ValueError):
    pass


async def content_length(
    client: httpx.AsyncClient, url: str, *, headers=None, timeout=10.0
) -> int | None:
    """A file's byte size from a HEAD request, when the source states it.

    Sources that stream or chunk dynamically return nothing usable; the caller
    treats ``None`` as unpriced and the delivery gate cards rather than guesses.
    A URL that cannot be verified public is equally unpriced, not an error:
    pricing must never crash a delivery path.
    """
    try:
        await asyncio.to_thread(check_public_url, url)
    except ValueError:
        return None
    try:
        resp = await client.head(
            url, headers=headers, follow_redirects=True, timeout=timeout
        )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    raw = resp.headers.get("Content-Length")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


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
