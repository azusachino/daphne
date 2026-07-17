import html
import logging
import re
from typing import Optional

import httpx
from telegram import Update
from telegram.ext import ContextTypes

from daphne.messages import HtmlMessage, PARSE_MODE_HTML, sender_attribution
from daphne.twitter import (
    USER_AGENT as REDDIT_USER_AGENT,
    send_photos,
    try_delete_message,
)

logger = logging.getLogger(__name__)

REDDIT_REGEX = re.compile(
    r"https?://(?:www\.|old\.|new\.|np\.|m\.)?reddit\.com/r/[^/\s]+/comments/[a-zA-Z0-9]+(?:/[^\s]*)?"
    r"|https?://(?:www\.|old\.|new\.|np\.|m\.)?reddit\.com/r/[^/\s]+/s/[a-zA-Z0-9]+"
    r"|https?://redd\.it/[a-zA-Z0-9]+",
    re.IGNORECASE,
)


def contains_reddit_link(text: str) -> bool:
    return bool(REDDIT_REGEX.search(text))


def extract_reddit_link(text: str) -> Optional[str]:
    match = REDDIT_REGEX.search(text)
    return match.group(0) if match else None


async def resolve_share_link(url: str) -> str:
    """Resolves a mobile-app share link (/r/<sub>/s/<shortcode>) to its
    canonical /comments/... permalink. Appending .json directly to a share
    link doesn't reliably follow Reddit's redirect, so the plain URL is
    resolved first and .json is appended to the result."""
    headers = {"User-Agent": REDDIT_USER_AGENT}
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(
                url.split("?")[0], headers=headers, timeout=15.0
            )
        return str(response.url).split("?")[0].rstrip("/")
    except Exception as exc:
        logger.warning("Failed to resolve Reddit share link %s: %s", url, exc)
        return url


class RedditBlocked(Exception):
    """Reddit's own API refused the request (403/429). yt-dlp's Reddit
    extractor hits this same .json API internally, so falling back to the
    full multi-engine video pipeline would almost certainly hit the same
    wall again — callers should fail fast instead of retrying elsewhere."""


async def fetch_post(url: str) -> Optional[dict]:
    """Fetches Reddit's own public .json listing for a post — no auth needed
    for public subreddits, so no login-wall workaround is required here."""
    clean_url = url.split("?")[0].rstrip("/")
    if "/s/" in clean_url:
        clean_url = await resolve_share_link(clean_url)
    json_url = f"{clean_url}.json"
    headers = {"User-Agent": REDDIT_USER_AGENT}
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(json_url, headers=headers, timeout=15.0)
        if response.status_code in (403, 429):
            raise RedditBlocked(f"status={response.status_code}")
        if response.status_code != 200:
            logger.warning(
                "Reddit JSON fetch failed: url=%s status=%s",
                json_url,
                response.status_code,
            )
            return None
        data = response.json()
        return data[0]["data"]["children"][0]["data"]
    except RedditBlocked:
        raise
    except Exception as exc:
        logger.warning("Failed to fetch Reddit post JSON for %s: %s", url, exc)
        return None


def is_video_post(post: dict) -> bool:
    if post.get("is_video"):
        return True
    return bool((post.get("secure_media") or {}).get("reddit_video"))


def gallery_image_urls(post: dict) -> list[str]:
    media_metadata = post.get("media_metadata") or {}
    gallery_items = (post.get("gallery_data") or {}).get("items") or []
    urls = []
    for item in gallery_items:
        meta = media_metadata.get(item.get("media_id")) or {}
        source = meta.get("s") or {}
        image_url = source.get("u") or source.get("gif")
        if image_url:
            urls.append(html.unescape(image_url))
    return urls


def single_image_url(post: dict) -> Optional[str]:
    direct_url = post.get("url_overridden_by_dest") or post.get("url") or ""
    if direct_url.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".gif")):
        return direct_url
    images = (post.get("preview") or {}).get("images") or []
    if images:
        source_url = (images[0].get("source") or {}).get("url")
        if source_url:
            return html.unescape(source_url)
    return None


def build_caption(post: dict, original_url: str, sender: Optional[str]) -> str:
    subreddit = post.get("subreddit_name_prefixed") or ""
    author = post.get("author") or ""
    return (
        HtmlMessage(sender=sender)
        .title(post.get("title") or "")
        .fields(
            ("Subreddit", subreddit),
            ("Author", f"u/{author}" if author else None),
        )
        .link(original_url)
        .tags("reddit", subreddit.lstrip("r/") if subreddit else None)
        .render()
    )


async def handle_reddit_links(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.message
    if not message or not message.text:
        return

    url = extract_reddit_link(message.text)
    if not url:
        return

    chat_id = message.chat_id
    sender = sender_attribution(update.effective_user)

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")
    except Exception:
        pass

    try:
        post = await fetch_post(url)
    except RedditBlocked:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "Reddit is rate-limiting or blocking this request right now "
                f"— try again shortly.\n{url}"
            ),
        )
        return

    if not post:
        # Not a reachable/public post (deleted, quarantined, private
        # subreddit) — fall back to the generic video card so the link
        # doesn't just vanish silently.
        from daphne.bot import handle_video_link

        await handle_video_link(update, context, url)
        return

    permalink = post.get("permalink")
    original_url = f"https://www.reddit.com{permalink}" if permalink else url

    if is_video_post(post):
        # v.redd.it serves video and audio as separate DASH streams; yt-dlp
        # already knows how to mux them, so reuse the generic video pipeline
        # instead of reimplementing that merge here.
        from daphne.bot import handle_video_link

        await handle_video_link(update, context, original_url)
        return

    caption = build_caption(post, original_url, sender)
    image_urls = gallery_image_urls(post)
    if not image_urls:
        single = single_image_url(post)
        if single:
            image_urls = [single]

    if image_urls:
        await send_photos(
            context.bot, chat_id, image_urls, caption, parse_mode=PARSE_MODE_HTML
        )
        await try_delete_message(update)
        return

    # Text post or a media shape we don't render (e.g. a poll) — pass the
    # clean link through rather than dropping the message.
    await context.bot.send_message(chat_id=chat_id, text=original_url)
    await try_delete_message(update)
