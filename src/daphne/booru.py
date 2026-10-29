"""Single-post links from the booru family, converted with gallery-dl.

Covers Moebooru (yande.re, Konachan, lolibooru) and Danbooru. konachan.net is fetched directly,
because gallery-dl's Konachan extractor goes to konachan.com, which challenges it. Only single-post URLs are
accepted: a tag or search page would make gallery-dl download a whole result set.
"""

import asyncio
import logging
import re
import tempfile
from typing import Optional
from urllib.parse import unquote, urlsplit

import httpx

from daphne.gallery import MEDIA_GROUP_LIMIT, download_gallery
from daphne.messages import HtmlMessage, sender_attribution
from daphne.tg import (
    InputMediaPhoto,
    TelegramContext,
    TelegramUpdate,
    failure_reason,
    image_input,
    send_photo_or_document,
)
from daphne.twitter import try_delete_message

logger = logging.getLogger(__name__)

PARSE_MODE_HTML = "HTML"
# Telegram refuses photos over 10 MB inside a media group, so larger files go as documents.
PHOTO_LIMIT_BYTES = 10 * 1024 * 1024

BOORU_POST_RE = re.compile(
    r"https?://(?:www\.)?("
    r"yande\.re/post/show/\d+"
    r"|konachan\.(?:com|net)/post/show/\d+"
    r"|lolibooru\.moe/post/show/\d+"
    r"|danbooru\.donmai\.us/posts/\d+"
    r")(?![\w/])",
    re.IGNORECASE,
)


def extract_booru_url(text: str) -> Optional[str]:
    match = BOORU_POST_RE.search(text or "")
    return match.group(0) if match else None


def contains_booru_link(text: str) -> bool:
    return extract_booru_url(text) is not None


def _site_label(url: str) -> str:
    host = re.sub(r"^https?://(www\.)?", "", url, flags=re.IGNORECASE).split("/", 1)[0]
    return host.split(".")[0].capitalize()


# konachan.net serves its post JSON and images directly. gallery-dl's Konachan extractor
# always goes to konachan.com, which answers with a Cloudflare challenge, so the single
# post is fetched here instead. Images must come from the same site.
DIRECT_POST_RE = re.compile(
    r"https?://(konachan\.net)/post/show/(\d+)(?![\w/])", re.IGNORECASE
)
DIRECT_IMAGE_LIMIT_BYTES = 50 * 1024 * 1024
USER_AGENT = "daphne/0.5.0"


def _direct_moebooru_images(
    site: str, post_id: str, client: Optional[httpx.Client] = None
) -> list[tuple[bytes, str]]:
    owned = client is None
    client = client or httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=30.0, follow_redirects=False
    )
    try:
        resp = client.get(f"https://{site}/post.json", params={"tags": f"id:{post_id}"})
        resp.raise_for_status()
        posts = resp.json()
        if not posts:
            return []
        file_url = str(posts[0].get("file_url") or "")
        if file_url.startswith("//"):
            file_url = "https:" + file_url
        if not file_url.startswith(f"https://{site}/"):
            raise ValueError("Moebooru returned an image from another host")
        image = client.get(file_url, headers={"Referer": f"https://{site}/"})
        image.raise_for_status()
        if len(image.content) > DIRECT_IMAGE_LIMIT_BYTES:
            raise ValueError("Moebooru image is larger than the download limit")
        name = unquote(urlsplit(file_url).path.rsplit("/", 1)[-1])
        return [(image.content, name)]
    finally:
        if owned:
            client.close()


def _download_images(url: str) -> list[tuple[bytes, str]]:
    """Fetch every image of the post with gallery-dl, reading the bytes before the folder goes."""
    direct = DIRECT_POST_RE.fullmatch(url)
    if direct:
        return _direct_moebooru_images(direct.group(1), direct.group(2))
    if extract_booru_url(url) != url:
        raise ValueError("only single booru post links are downloaded")
    with tempfile.TemporaryDirectory(prefix="daphne-booru-") as work:
        images = []
        for path in download_gallery(url, work):
            with open(path, "rb") as handle:
                images.append((handle.read(), path.rsplit("/", 1)[-1]))
    return images


async def handle_booru_links(update: TelegramUpdate, context: TelegramContext) -> None:
    from daphne.bot import REACTION_FAILED, download_slot, set_reaction

    message = update.message
    if not message or not message.text:
        return
    url = extract_booru_url(message.text)
    if not url:
        return

    sender = sender_attribution(update.effective_user)
    user_id = update.effective_user.id if update.effective_user else 0
    loop = asyncio.get_running_loop()
    try:
        async with download_slot(user_id):
            images = await loop.run_in_executor(None, _download_images, url)
    except Exception as exc:
        logger.warning("Booru download failed: %s", failure_reason(exc))
        images = []

    if not images:
        await set_reaction(message, REACTION_FAILED)
        await message.reply_text(
            HtmlMessage(sender=sender)
            .text(
                "This booru post could not be downloaded. Your original link was kept."
            )
            .render(),
            parse_mode=PARSE_MODE_HTML,
        )
        return

    caption = (
        HtmlMessage(sender=sender).link(url, f"🔗 Source ({_site_label(url)})").render()
    )
    captioned = False
    chat_id = message.chat_id
    try:
        for start in range(0, len(images), MEDIA_GROUP_LIMIT):
            batch = images[start : start + MEDIA_GROUP_LIMIT]
            if len(batch) > 1 and all(len(d) <= PHOTO_LIMIT_BYTES for d, _ in batch):
                media = [
                    InputMediaPhoto(
                        media=image_input(data, name),
                        caption=caption if index == 0 and not captioned else None,
                        parse_mode=PARSE_MODE_HTML
                        if index == 0 and not captioned
                        else None,
                    )
                    for index, (data, name) in enumerate(batch)
                ]
                try:
                    await context.bot.send_media_group(chat_id=chat_id, media=media)
                    captioned = True
                    continue
                except Exception as exc:
                    logger.info(
                        "Media group refused, sending one by one: %s",
                        failure_reason(exc),
                    )
            for data, name in batch:
                text = None if captioned else caption
                mode = None if captioned else PARSE_MODE_HTML
                captioned = True
                if len(data) > PHOTO_LIMIT_BYTES:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=image_input(data, name),
                        caption=text,
                        parse_mode=mode,
                    )
                else:
                    await send_photo_or_document(
                        context.bot, chat_id, data, name, text, mode
                    )
    except Exception as exc:
        logger.warning("Booru upload failed: %s", failure_reason(exc))
        await set_reaction(message, REACTION_FAILED)
        return

    await try_delete_message(update)
