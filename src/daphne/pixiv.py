import io
import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse, urlunparse

import httpx
from daphne.tg import InputMediaPhoto, TelegramContext, TelegramUpdate, as_input_file

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
MAX_PIXIV_IMAGES = 3


@dataclass
class PixivInfo:
    title: str
    author_name: str
    tags: list[str]
    image_urls: list[str] | None = None
    page_count: int = 1


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


def _expand_pixiv_pages(url: str, page_count: int) -> list[str]:
    parsed = urlparse(url)
    path = parsed.path
    page_match = re.search(r"_p\d+(?=_[^/.]+\.[^/.]+$|\.[^/.]+$)", path)
    if not page_match:
        return [url] if page_count == 1 else []

    base_path = path[: page_match.start()] + "_p0" + path[page_match.end() :]
    return [
        urlunparse(parsed._replace(path=base_path.replace("_p0", f"_p{page}", 1)))
        for page in range(page_count)
    ]


def _original_pixiv_url(url: str) -> str:
    parsed = urlparse(url)
    path = re.sub(r"^/c/[^/]+/(?:[^/]+/)?img/", "/img-original/img/", parsed.path)
    path = path.replace("/img-master/", "/img-original/", 1)
    path = re.sub(r"_p\d+(?:_[^/.]+)?(?=\.[^/.]+$)", "_p0", path)
    return urlunparse(parsed._replace(path=path))


def _master_pixiv_url(url: str, page: int) -> str:
    parsed = urlparse(url)
    if not parsed.path.startswith("/c/"):
        return ""
    path = re.sub(
        r"^/c/[^/]+/(?:[^/]+/)?img/",
        "/c/540x540_70/img-master/img/",
        parsed.path,
    )
    path = re.sub(
        r"_p\d+(?:_[^/.]+)?(?=\.[^/.]+$)",
        f"_p{page}_master1200",
        path,
    )
    return urlunparse(parsed._replace(path=path))


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
            page_count = max(1, int(body.get("pageCount") or 1))
            image_urls = [
                str(url) for url in (urls.get("original"), urls.get("regular")) if url
            ]
            if page_count > 1:
                preview = (body.get("userIllusts") or {}).get(artwork_id) or {}
                source_url = image_urls[0] if image_urls else preview.get("url")
                image_urls = (
                    _expand_pixiv_pages(str(source_url), page_count)
                    if source_url
                    else []
                )
            return PixivInfo(
                title=str(body.get("title") or ""),
                author_name=str(body.get("userName") or ""),
                tags=[str(tag.get("tag") or tag) for tag in tags],
                image_urls=image_urls,
                page_count=page_count,
            )
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            logger.warning("Failed to fetch Pixiv metadata for %s: %s", artwork_id, exc)
            return None


async def _fetch_pixiv_candidates(
    artwork_id: str, candidates: list[str]
) -> tuple[bytes, str]:
    headers = {"User-Agent": USER_AGENT, "Referer": "https://www.pixiv.net/"}
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for url in candidates:
            try:
                response = await client.get(url, headers=headers, timeout=30.0)
                if response.status_code == 200:
                    return response.content, url
            except httpx.HTTPError as exc:
                logger.info("Pixiv image candidate failed (%s): %s", url, exc)
    raise ValueError(f"No Pixiv image found for artwork {artwork_id}")


async def fetch_pixiv_image(
    artwork_id: str, fallback_urls: list[str] | None = None, page: int = 0
) -> tuple[bytes, str]:
    proxy_id = artwork_id if page == 0 else f"{artwork_id}-{page}"
    direct_candidates = []
    for url in fallback_urls or []:
        original_url = _original_pixiv_url(url).replace("_p0", f"_p{page}", 1)
        if original_url != url:
            direct_candidates.append(original_url)
        master_url = _master_pixiv_url(url, page)
        if master_url and master_url not in direct_candidates:
            direct_candidates.append(master_url)
        direct_candidates.append(url)
    candidates = [
        *(f"{PIXIV_CAT_BASE}/{proxy_id}.{ext}" for ext in ("jpg", "png")),
        *direct_candidates,
    ]
    return await _fetch_pixiv_candidates(artwork_id, candidates)


def _image_filename(url: str, index: int) -> str:
    suffix = urlparse(url).path.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
    return f"pixiv_{index}.{suffix}" if suffix else f"pixiv_{index}.jpg"


def _image_file(content: bytes, url: str, index: int):
    bio = io.BytesIO(content)
    bio.name = _image_filename(url, index)
    return as_input_file(bio)


def build_caption(
    original_url: str,
    pixiv_cat_url: str,
    info: Optional[PixivInfo],
    sender: Optional[str] = None,
    remaining_images: int = 0,
) -> str:
    message = HtmlMessage(sender=sender)
    if info:
        tags = [to_telegram_tag(tag) for tag in info.tags]
        message.title(info.title).fields(("Author:", info.author_name)).tags(
            "pixiv", *tags
        )
    message.links(original_url, pixiv_cat_url).tags("pixiv")
    if remaining_images:
        message.text(f"+{remaining_images} more images on Pixiv")
    return message.render()


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
        remaining_images = 0
        if info and info.page_count > 1:
            if not info.image_urls or len(info.image_urls) != info.page_count:
                raise ValueError(
                    f"Pixiv page URLs unavailable for artwork {artwork_id}"
                )
            remaining_images = max(0, info.page_count - MAX_PIXIV_IMAGES)
            images = [
                await fetch_pixiv_image(artwork_id, [url], page=index)
                for index, url in enumerate(info.image_urls[:MAX_PIXIV_IMAGES])
            ]
        else:
            images = [
                await fetch_pixiv_image(artwork_id, info.image_urls if info else None)
            ]
        caption = build_caption(
            original_url, images[0][1], info, sender, remaining_images
        )

        if len(images) == 1 and len(images[0][0]) <= 10 * 1024 * 1024:
            image_bytes, image_url = images[0]
            await context.bot.send_photo(
                chat_id=message.chat_id,
                photo=_image_file(image_bytes, image_url, 0),
                caption=caption,
                parse_mode=PARSE_MODE_HTML,
            )
        elif len(images) == 1 or any(
            len(content) > 10 * 1024 * 1024 for content, _ in images
        ):
            for index, (image_bytes, image_url) in enumerate(images):
                await context.bot.send_document(
                    chat_id=message.chat_id,
                    document=_image_file(image_bytes, image_url, index),
                    caption=caption if index == 0 else None,
                    parse_mode=PARSE_MODE_HTML if index == 0 else None,
                )
        else:
            media = [
                InputMediaPhoto(
                    media=_image_file(image_bytes, image_url, index),
                    caption=caption if index == 0 else None,
                    parse_mode=PARSE_MODE_HTML if index == 0 else None,
                )
                for index, (image_bytes, image_url) in enumerate(images)
            ]
            await context.bot.send_media_group(chat_id=message.chat_id, media=media)
    except Exception as exc:
        logger.warning("Pixiv image upload failed for %s: %s", artwork_id, exc)
        from daphne.bot import REACTION_FAILED, set_reaction

        await set_reaction(message, REACTION_FAILED)
        return

    await try_delete_message(update)
