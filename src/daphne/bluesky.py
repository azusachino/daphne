import re
import logging
import httpx
from typing import Optional, Tuple, List
from telegram import Update
from telegram.ext import ContextTypes

from daphne.messages import HtmlMessage, PARSE_MODE_HTML, sender_attribution
from daphne.twitter import extract_hashtags, send_photos, try_delete_message

logger = logging.getLogger(__name__)

BSKY_REGEX = re.compile(
    r"https?://(?:www\.)?bsky\.app/profile/([^/]+)/post/([^/]+)",
    re.IGNORECASE,
)
PUBLIC_API_BASE = "https://public.api.bsky.app/xrpc"


def contains_bluesky_link(text: str) -> bool:
    return bool(BSKY_REGEX.search(text))


def extract_bluesky_link(text: str) -> Optional[Tuple[str, str]]:
    match = BSKY_REGEX.search(text)
    if match:
        return match.group(1), match.group(2)
    return None


async def resolve_handle(handle: str) -> Optional[str]:
    url = f"{PUBLIC_API_BASE}/com.atproto.identity.resolveHandle"
    params = {"handle": handle}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, timeout=10.0)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("did")
            else:
                logger.warning(
                    "Bluesky handle resolution failed for %s: %s",
                    handle,
                    resp.status_code,
                )
    except Exception as e:
        logger.exception("Error resolving Bluesky handle %s: %s", handle, e)
    return None


async def fetch_post_thread(did: str, post_id: str) -> Optional[dict]:
    uri = f"at://{did}/app.bsky.feed.post/{post_id}"
    url = f"{PUBLIC_API_BASE}/app.bsky.feed.getPostThread"
    params = {"uri": uri}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, timeout=10.0)
            if resp.status_code == 200:
                return resp.json()
            else:
                logger.warning(
                    "Bluesky getPostThread failed for %s: %s", uri, resp.status_code
                )
    except Exception as e:
        logger.exception("Error fetching Bluesky thread %s: %s", uri, e)
    return None


def extract_media_from_embed(embed: dict) -> Tuple[List[str], Optional[str]]:
    if not embed:
        return [], None

    t = embed.get("$type", "")
    if t == "app.bsky.embed.recordWithMedia#view":
        embed = embed.get("media", {})
        t = embed.get("$type", "")

    if t == "app.bsky.embed.images#view":
        images = embed.get("images", [])
        photo_urls = [img.get("fullsize") for img in images if img.get("fullsize")]
        return photo_urls, None
    elif t == "app.bsky.embed.video#view":
        return [], embed.get("playlist")

    return [], None


def build_bluesky_caption(
    text: str,
    url: str,
    author_name: str,
    author_handle: str,
    sender: Optional[str],
) -> str:
    tags = ["bluesky"]
    for tag in extract_hashtags(text):
        if tag != "bluesky":
            tags.append(tag)

    if len(text) > 900:
        text = text[:900] + "..."

    title = f"{author_name} (@{author_handle})"
    return (
        HtmlMessage(sender=sender)
        .title(title)
        .text(text or None)
        .link(url)
        .tags(*tags)
        .render()
    )


async def handle_bluesky_links(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.message
    if not message or not message.text:
        return

    match_info = extract_bluesky_link(message.text)
    if not match_info:
        return

    handle, post_id = match_info
    chat_id = message.chat_id

    # 1. Trigger photo upload indicator
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")
    except Exception:
        pass

    # 2. Resolve handle to DID
    did = await resolve_handle(handle)
    if not did:
        logger.warning("Could not resolve Bluesky handle: %s", handle)
        return

    # 3. Fetch thread content
    thread_data = await fetch_post_thread(did, post_id)
    if not thread_data or "thread" not in thread_data:
        logger.warning(
            "Could not fetch Bluesky thread data for %s/post/%s", handle, post_id
        )
        return

    thread_post = thread_data["thread"].get("post")
    if not thread_post:
        logger.warning(
            "No post field inside thread response for %s/post/%s", handle, post_id
        )
        return

    author = thread_post.get("author", {})
    author_name = author.get("displayName", author.get("handle", handle))
    author_handle = author.get("handle", handle)
    record = thread_post.get("record", {})
    post_text = record.get("text", "")
    embed = thread_post.get("embed", {})

    original_url = f"https://bsky.app/profile/{author_handle}/post/{post_id}"
    photo_urls, video_url = extract_media_from_embed(embed)
    sender = sender_attribution(update.effective_user)

    success = False

    if photo_urls:
        caption = build_bluesky_caption(
            post_text, original_url, author_name, author_handle, sender
        )
        success = await send_photos(
            context.bot,
            chat_id,
            photo_urls,
            caption,
            parse_mode=PARSE_MODE_HTML,
        )
    elif video_url:
        # Import handle_video_link here to avoid circular imports
        from daphne.bot import handle_video_link

        custom_metadata = {
            "title": post_text[:100] if post_text else "Bluesky Video",
            "uploader": f"{author_name} (@{author_handle})",
            "duration": None,
            "webpage_url": original_url,
        }
        await handle_video_link(
            update, context, video_url, custom_metadata=custom_metadata
        )
        success = True
    else:
        # Post has no media. Send a fallback message
        fallback_msg = f"https://bsky.app/profile/{author_handle}/post/{post_id}"
        try:
            await context.bot.send_message(chat_id=chat_id, text=fallback_msg)
            success = True
        except Exception as e:
            logger.error("Failed to send Bluesky fallback message: %s", e)

    if success:
        await try_delete_message(update)
