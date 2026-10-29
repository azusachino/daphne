import re
import logging
import httpx
from typing import Optional, Tuple, List
from daphne.tg import TelegramContext, TelegramUpdate, failure_reason

from daphne.messages import HtmlMessage, PARSE_MODE_HTML, sender_attribution
from daphne.twitter import extract_hashtags, send_photos, try_delete_message

logger = logging.getLogger(__name__)

BLUESKY_NO_MEDIA_TEXT = (
    "This post has no downloadable media; your original link was kept."
)
BLUESKY_FAILED_TEXT = (
    "Could not convert this Bluesky post right now; your original link was kept."
)


async def _report_failure(message, context, chat_id, sender, stage: str) -> None:
    """Marks a Bluesky request as failed: failure reaction, then a footered reply."""
    logger.warning("Bluesky conversion stopped at %s; original link kept", stage)
    # Imported here: bot.py imports this module, so a top-level import would be circular.
    from daphne.bot import REACTION_FAILED, set_reaction

    await set_reaction(message, REACTION_FAILED)
    text = BLUESKY_NO_MEDIA_TEXT if stage == "no-media" else BLUESKY_FAILED_TEXT
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=HtmlMessage(sender=sender).text(text).render(),
            parse_mode=PARSE_MODE_HTML,
        )
    except Exception as exc:
        logger.warning(
            "Bluesky failure reply could not be sent: %s", failure_reason(exc)
        )


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
        .link(url, "🔗 Source (Bluesky)")
        .tags(*tags)
        .render()
    )


async def handle_bluesky_links(
    update: TelegramUpdate, context: TelegramContext
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
    except Exception as exc:
        logger.warning("Bluesky upload action failed: %s", failure_reason(exc))

    sender = sender_attribution(update.effective_user)

    # 2. Resolve handle to DID
    did = await resolve_handle(handle)
    if not did:
        await _report_failure(message, context, chat_id, sender, "resolve")
        return

    # 3. Fetch thread content
    thread_data = await fetch_post_thread(did, post_id)
    if not thread_data or "thread" not in thread_data:
        await _report_failure(message, context, chat_id, sender, "thread")
        return

    thread_post = thread_data["thread"].get("post")
    if not thread_post:
        await _report_failure(message, context, chat_id, sender, "thread")
        return

    try:
        author = thread_post.get("author", {})
        author_name = author.get("displayName", author.get("handle", handle))
        author_handle = author.get("handle", handle)
        record = thread_post.get("record", {})
        post_text = record.get("text", "")
        embed = thread_post.get("embed", {})
        original_url = f"https://bsky.app/profile/{author_handle}/post/{post_id}"
        photo_urls, video_url = extract_media_from_embed(embed)
    except (AttributeError, TypeError, KeyError, ValueError) as exc:
        logger.warning(
            "Bluesky thread had an unexpected shape: %s", failure_reason(exc)
        )
        await _report_failure(message, context, chat_id, sender, "thread")
        return

    success = False

    if photo_urls:
        caption = build_bluesky_caption(
            post_text, original_url, author_name, author_handle, sender
        )
        try:
            from daphne.bot import download_slot

            user_id = update.effective_user.id if update.effective_user else 0
            async with download_slot(user_id):
                success = await send_photos(
                    context.bot,
                    chat_id,
                    photo_urls,
                    caption,
                    parse_mode=PARSE_MODE_HTML,
                )
        except Exception as exc:
            logger.warning("Bluesky photo send failed: %s", failure_reason(exc))
            await _report_failure(message, context, chat_id, sender, "send")
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
        return
    else:
        # No media to send: say so and keep the original message, without a link fallback.
        await _report_failure(message, context, chat_id, sender, "no-media")

    if success:
        await try_delete_message(update)
