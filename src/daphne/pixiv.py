import asyncio
import json
import os
import tempfile
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse, urlunparse

import httpx
from daphne.tg import (
    TelegramContext,
    TelegramUpdate,
    send_image_groups,
    failure_reason,
)

from daphne.messages import (
    HtmlMessage,
    PARSE_MODE_HTML,
    sender_attribution,
)
from daphne.gallery import download_gallery
from daphne.twitter import try_delete_message

logger = logging.getLogger(__name__)

PIXIV_CAT_BASE = "https://pixiv.cat"
PIXIV_RESTRICTED_TEXT = (
    "This Pixiv work is age-restricted, so the bot can't convert it anonymously. "
    "Open it in Pixiv while signed in. Your original link was kept."
)
PIXIV_UNAVAILABLE_TEXT = (
    "This Pixiv work could not be read. Age-restricted works need a Pixiv login, which "
    "this bot has only when its owner sets one. Your original link was kept."
)
PIXIV_REFRESH_TOKEN_ENV = "DAPHNE_PIXIV_REFRESH_TOKEN"
USER_AGENT = "daphne/0.1.0"
MAX_PIXIV_IMAGES = 3


@dataclass
class PixivInfo:
    title: str
    author_name: str
    tags: list[str]
    image_urls: list[str] | None = None
    page_count: int = 1
    restricted: bool = False


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


def to_telegram_tag(tag: str) -> Optional[str]:
    raw = unicodedata.normalize("NFKC", tag.strip()).lstrip("#")
    sanitized = re.sub(r"[^\w]+", "_", raw).strip("_")
    sanitized = re.sub(r"_+", "_", sanitized)
    return f"#{sanitized}" if sanitized else None


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
                restricted=bool(body.get("xRestrict")),
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


def build_caption(
    original_url: str,
    pixiv_cat_url: str = "",
    info: Optional[PixivInfo] = None,
    sender: Optional[str] = None,
    remaining_images: int = 0,
) -> str:
    message = HtmlMessage(sender=sender)
    tags = ["pixiv"]
    seen = {"pixiv"}
    if info:
        message.title(info.title).fields(("Author:", info.author_name))
        for raw_tag in info.tags:
            tag = to_telegram_tag(raw_tag)
            if not tag:
                continue
            key = tag.lstrip("#").lower()
            if key not in seen:
                seen.add(key)
                tags.append(tag)
    if remaining_images:
        message.text(f"+{remaining_images} more images on Pixiv")
    message.link(original_url, "🔗 Source (Pixiv)").tags(*tags)
    return message.render()


async def _fetch_primary(
    artwork_id: str, info: Optional[PixivInfo]
) -> tuple[list[tuple[bytes, str]], int]:
    """The CDN route: pixiv.cat, then Pixiv's own files. Raises ValueError when nothing works."""
    if info and info.page_count > 1:
        if not info.image_urls or len(info.image_urls) != info.page_count:
            raise ValueError(f"Pixiv page URLs unavailable for artwork {artwork_id}")
        remaining = max(0, info.page_count - MAX_PIXIV_IMAGES)
        images = [
            await fetch_pixiv_image(artwork_id, [url], page=index)
            for index, url in enumerate(info.image_urls[:MAX_PIXIV_IMAGES])
        ]
        return images, remaining
    image = await fetch_pixiv_image(artwork_id, info.image_urls if info else None)
    return [image], 0


def pixiv_refresh_token() -> str:
    """A Pixiv login token, set by the owner. It is only read here and never logged."""
    return os.environ.get(PIXIV_REFRESH_TOKEN_ENV, "").strip()


def _page_index(path: str) -> int:
    match = re.search(r"_p(\d+)", os.path.basename(path))
    return int(match.group(1)) if match else 0


def _gallery_fallback_sync(artwork_id: str) -> list[tuple[bytes, str]]:
    """gallery-dl with the owner's login, for works the CDN route cannot read or refuses.

    The token goes into a private config file that is removed afterwards. gallery-dl's login
    cache is kept in memory, so nothing it stores reaches the disk. The token is not put on
    the command line, in the tool's environment, or in a log line. It reads only the first
    pages. Without a token this is a no-op.
    """
    token = pixiv_refresh_token()
    if not token:
        return []
    with tempfile.TemporaryDirectory(prefix="daphne-pixiv-") as work:
        config = os.path.join(work, "gallery-dl.json")
        settings = {
            "extractor": {"pixiv": {"refresh-token": token}},
            "cache": {"file": ":memory:"},
        }
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(settings, handle)
        paths = download_gallery(
            f"https://www.pixiv.net/artworks/{artwork_id}",
            os.path.join(work, "out"),
            ["--config", config, "--range", f"1-{MAX_PIXIV_IMAGES}"],
        )
        images = []
        for path in sorted(paths, key=_page_index):
            with open(path, "rb") as handle:
                images.append((handle.read(), os.path.basename(path)))
    return images


async def _gallery_images(
    artwork_id: str, info: Optional[PixivInfo], user_id: int
) -> tuple[list[tuple[bytes, str]], int]:
    """The login fallback, inside the download slots. Returns the images and how many are left."""
    from daphne.bot import download_slot

    loop = asyncio.get_running_loop()
    try:
        async with download_slot(user_id):
            images = await loop.run_in_executor(
                None, _gallery_fallback_sync, artwork_id
            )
    except Exception as exc:
        logger.warning(
            "Pixiv login fallback failed for %s: %s", artwork_id, failure_reason(exc)
        )
        return [], 0
    shown = images[:MAX_PIXIV_IMAGES]
    remaining = max(0, info.page_count - len(shown)) if info else 0
    return shown, remaining


async def handle_pixiv_links(update: TelegramUpdate, context: TelegramContext) -> None:
    from daphne.bot import REACTION_FAILED, set_reaction

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
    except Exception as exc:
        logger.warning("Pixiv upload action failed: %s", failure_reason(exc))

    original_url = (
        extract_pixiv_url(message.text) or f"{PIXIV_CAT_BASE}/{artwork_id}.jpg"
    )
    info = await fetch_artwork_info(artwork_id)
    sender = sender_attribution(update.effective_user)
    user_id = update.effective_user.id if update.effective_user else 0
    restricted = bool(info and info.restricted)
    has_login = bool(pixiv_refresh_token())
    if restricted:
        logger.warning("Pixiv artwork %s is age-restricted", artwork_id)

    images: list[tuple[bytes, str]] = []
    remaining = 0
    if restricted:
        # The CDN route cannot read age-restricted works, so only the login route is tried.
        if has_login:
            images, remaining = await _gallery_images(artwork_id, info, user_id)
    else:
        try:
            images, remaining = await _fetch_primary(artwork_id, info)
        except ValueError as exc:
            logger.info("Pixiv CDN route failed for %s: %s", artwork_id, exc)
            if has_login:
                images, remaining = await _gallery_images(artwork_id, info, user_id)

    if not images:
        await set_reaction(message, REACTION_FAILED)
        text = (
            PIXIV_RESTRICTED_TEXT
            if restricted and not has_login
            else PIXIV_UNAVAILABLE_TEXT
        )
        await message.reply_text(
            HtmlMessage(sender=sender).text(text).render(),
            parse_mode=PARSE_MODE_HTML,
        )
        return

    try:
        caption = build_caption(original_url, images[0][1], info, sender, remaining)
        chat_id = message.chat_id
        await send_image_groups(context.bot, chat_id, images, caption, PARSE_MODE_HTML)
    except Exception as exc:
        logger.warning("Pixiv image upload failed for %s: %s", artwork_id, exc)
        await set_reaction(message, REACTION_FAILED)
        return

    await try_delete_message(update)
