import io
import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import httpx
from daphne.tg import TelegramContext, TelegramUpdate

from daphne.messages import (
    HtmlMessage,
    PARSE_MODE_HTML,
    sender_attribution,
)
from daphne.twitter import try_delete_message

logger = logging.getLogger(__name__)

PIXIV_CAT_BASE = "https://pixiv.cat"
PHIXIV_API = "https://phixiv.net/api/info"
USER_AGENT = "daphne/0.1.0"


@dataclass
class PixivInfo:
    title: str
    author_name: str
    tags: list[str]
    image_urls: list[str] | None = None


def contains_pixiv_link(text: str) -> bool:
    return extract_pixiv_id(text) is not None


def _clean_url_token(value: str) -> str:
    return value.strip("`\"',(").rstrip(".,!?;:)]}")


def _is_pixiv_host(hostname: Optional[str]) -> bool:
    if not hostname:
        return False
    return hostname == "pixiv.net" or hostname.endswith(".pixiv.net")


def extract_pixiv_id(text: str) -> Optional[str]:
    for token in text.split():
        clean = _clean_url_token(token)
        parsed = urlparse(clean)
        if parsed.scheme not in {"http", "https"} or not _is_pixiv_host(
            parsed.hostname
        ):
            continue
        segments = [segment for segment in parsed.path.split("/") if segment]
        try:
            index = segments.index("artworks")
        except ValueError:
            continue
        if index + 1 >= len(segments):
            continue
        artwork_id = segments[index + 1]
        if artwork_id.isascii() and artwork_id.isdigit():
            return artwork_id
    return None


def extract_pixiv_url(text: str) -> Optional[str]:
    for token in text.split():
        clean = _clean_url_token(token)
        parsed = urlparse(clean)
        if parsed.scheme in {"http", "https"} and _is_pixiv_host(parsed.hostname):
            return clean.split("?", 1)[0]
    return None


def to_telegram_tag(tag: str) -> str:
    raw = tag.strip().lstrip("#")
    sanitized = re.sub(r"[^0-9A-Za-z_]+", "_", raw).strip("_")
    sanitized = re.sub(r"_+", "_", sanitized)
    return f"#{sanitized}" if sanitized else "#pixiv"


async def fetch_artwork_info(artwork_id: str) -> Optional[PixivInfo]:
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                PHIXIV_API,
                params={"id": artwork_id, "language": "en"},
                headers=headers,
                timeout=10.0,
            )
            if response.status_code == 200:
                data = response.json()
                if data.get("title") or data.get("author_name"):
                    return PixivInfo(
                        title=str(data.get("title") or ""),
                        author_name=str(data.get("author_name") or ""),
                        tags=[str(tag) for tag in data.get("tags", [])],
                    )
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            logger.info("Phixiv metadata unavailable for %s: %s", artwork_id, exc)

        try:
            response = await client.get(
                f"https://www.pixiv.net/ajax/illust/{artwork_id}",
                params={"lang": "en"},
                headers={**headers, "Referer": "https://www.pixiv.net/"},
                timeout=10.0,
            )
            if response.status_code != 200:
                return None
            body = response.json().get("body") or {}
            if not body:
                return None
            tags = (body.get("tags") or {}).get("tags") or []
            urls = body.get("urls") or {}
            return PixivInfo(
                title=str(body.get("title") or ""),
                author_name=str(body.get("userName") or ""),
                tags=[str(tag.get("tag") or tag) for tag in tags],
                image_urls=[
                    str(url)
                    for url in (urls.get("original"), urls.get("regular"))
                    if url
                ],
            )
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            logger.warning("Failed to fetch Pixiv metadata for %s: %s", artwork_id, exc)
            return None


async def fetch_pixiv_image(
    artwork_id: str, fallback_urls: list[str] | None = None
) -> tuple[bytes, str]:
    headers = {"User-Agent": USER_AGENT, "Referer": "https://www.pixiv.net/"}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        candidates = [
            *(f"{PIXIV_CAT_BASE}/{artwork_id}.{ext}" for ext in ("jpg", "png")),
            *(fallback_urls or []),
        ]
        for url in candidates:
            try:
                response = await client.get(url, headers=headers, timeout=30.0)
                if response.status_code == 200:
                    return response.content, url
            except httpx.HTTPError as exc:
                logger.info("Pixiv image candidate failed (%s): %s", url, exc)
    raise ValueError(f"No Pixiv image found for artwork {artwork_id}")


def build_caption(
    original_url: str,
    pixiv_cat_url: str,
    info: Optional[PixivInfo],
    sender: Optional[str] = None,
) -> str:
    if info:
        tags = [to_telegram_tag(tag) for tag in info.tags]
        return (
            HtmlMessage(sender=sender)
            .title(info.title)
            .fields(("Author:", info.author_name))
            .links(original_url, pixiv_cat_url)
            .tags("pixiv", *tags)
            .render()
        )
    return (
        HtmlMessage(sender=sender)
        .links(original_url, pixiv_cat_url)
        .tags("pixiv")
        .render()
    )


async def handle_pixiv_links(update: TelegramUpdate, context: TelegramContext) -> None:
    message = update.message
    if not message or not message.text:
        return

    artwork_id = extract_pixiv_id(message.text)
    if not artwork_id:
        return

    try:
        await context.bot.send_chat_action(
            chat_id=message.chat_id, action="upload_photo"
        )
    except Exception:
        pass

    original_url = (
        extract_pixiv_url(message.text) or f"{PIXIV_CAT_BASE}/{artwork_id}.jpg"
    )
    info = await fetch_artwork_info(artwork_id)
    sender = sender_attribution(update.effective_user)

    try:
        image_bytes, image_url = await fetch_pixiv_image(
            artwork_id, info.image_urls if info else None
        )
        caption = build_caption(original_url, image_url, info, sender)
        bio = io.BytesIO(image_bytes)
        bio.name = f"pixiv.{image_url.rsplit('.', 1)[-1].split('?', 1)[0]}"
        if len(image_bytes) <= 10 * 1024 * 1024:
            await context.bot.send_photo(
                chat_id=message.chat_id,
                photo=bio,
                caption=caption,
                parse_mode=PARSE_MODE_HTML,
            )
        else:
            await context.bot.send_document(
                chat_id=message.chat_id,
                document=bio,
                caption=caption,
                parse_mode=PARSE_MODE_HTML,
            )
    except Exception as exc:
        logger.warning("Pixiv image upload failed for %s: %s", artwork_id, exc)
        await context.bot.send_message(chat_id=message.chat_id, text=original_url)

    await try_delete_message(update)
