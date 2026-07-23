import asyncio
import re
import os
import httpx
import tempfile
import logging
from telegram import Update
from telegram.ext import ContextTypes

from daphne.messages import HtmlMessage, PARSE_MODE_HTML, sender_attribution
from daphne.twitter import send_photos, try_delete_message
from daphne.downloader import probe_video_dimensions
from daphne.config import video_upload_limit_mb

logger = logging.getLogger(__name__)

INSTAGRAM_REGEX = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:[^/]+/)?(?:p|reel|tv)/([a-zA-Z0-9_\-]+)",
    re.IGNORECASE,
)


def contains_instagram_link(text: str) -> bool:
    """
    Returns True if the text contains an Instagram post/reel link.
    """
    return bool(INSTAGRAM_REGEX.search(text))


def extract_instagram_link(text: str) -> str | None:
    """
    Extracts the first Instagram post/reel link in the text.
    """
    match = INSTAGRAM_REGEX.search(text)
    if match:
        return match.group(0)
    return None


def resolve_instagram_media(url: str) -> dict | None:
    """
    Resolve an Instagram post/reel to direct media URLs via parth-dl, without
    sending anything. Used by inline mode. Returns None on any failure.
    Synchronous (parth-dl is blocking) — call via an executor.
    """
    try:
        from parth_dl.extractors import MediaExtractor

        extractor = MediaExtractor()
        data = extractor.extract(url.split("?")[0])
    except Exception as e:
        logger.warning("resolve_instagram_media failed: %s", e)
        return None

    if not data:
        return None

    uploader = data.get("uploader", "unknown")
    title = data.get("title", "")
    shortcode = data.get("id", "")
    media_type = data.get("type", "image")

    photo_urls = [img["url"] for img in data.get("images", []) if img.get("url")]
    if not photo_urls and data.get("thumbnail"):
        photo_urls = [data["thumbnail"]]

    formats = data.get("formats", [])
    video_url = formats[0]["url"] if (media_type != "image" and formats) else None

    return {
        "uploader": uploader,
        "title": title,
        "original_url": f"https://www.instagram.com/p/{shortcode}/",
        "photos": photo_urls,
        "video_url": video_url,
        "thumbnail": data.get("thumbnail"),
    }


async def handle_instagram_links(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Extracts, downloads, and sends media from an Instagram link using parth-dl.
    """
    message = update.message
    if not message or not message.text:
        return

    url = extract_instagram_link(message.text)
    if not url:
        return

    chat_id = message.chat_id
    sender = sender_attribution(update.effective_user)
    logger.info(
        "Instagram handler: start processing link %s for chat_id=%s", url, chat_id
    )

    # 1. Trigger visual feedback action
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")
    except Exception:
        pass

    # 2. Extract media metadata via parth-dl
    try:
        from parth_dl.extractors import MediaExtractor

        extractor = MediaExtractor()
        # Clean URL parameter for more reliable API parsing
        clean_url = url.split("?")[0]
        logger.info(
            "Instagram handler: extracting metadata using parth-dl for clean URL %s",
            clean_url,
        )
        data = extractor.extract(clean_url)
    except Exception as e:
        logger.exception("Failed to extract Instagram media via parth-dl: %s", e)
        data = None

    if not data:
        # parth-dl failed outright (its 4 scraping methods are fragile to
        # Instagram markup/API changes). Before giving up, try yt-dlp's own
        # Instagram extractor: it reaches the same public post but, unlike
        # parth-dl, is actively maintained and can still succeed anonymously
        # even when the post has no video (it just needs
        # --ignore-no-formats-error to hand back the metadata it already
        # extracted instead of raising).
        logger.info("parth-dl found nothing for %s; trying yt-dlp fallback", clean_url)
        from daphne.downloader import fetch_instagram_fallback_media

        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(
            None, fetch_instagram_fallback_media, clean_url
        )

    if not data:
        logger.warning(
            "No data extracted for Instagram URL: %s. Content may be private, "
            "deleted, or Instagram is blocking the request.",
            url,
        )
        # Do NOT fall back to handle_video_link here: yt-dlp's own Instagram
        # extractor hits the same public post/API endpoints parth-dl already
        # tried (and we just tried it above), so the full yt-dlp/you-get/lux
        # chain would just retry into the same wall at real cost (multiple
        # engines, ~15s) for no benefit.
        from daphne.bot import REACTION_FAILED, set_reaction

        await set_reaction(message, REACTION_FAILED)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "Couldn't fetch this Instagram post — it may be private, deleted, "
                f"or Instagram is blocking the request right now.\n{url}"
            ),
        )
        return

    uploader = data.get("uploader", "unknown")
    title = data.get("title", "")
    media_type = data.get("type", "image")
    shortcode = data.get("id", "")
    original_url = f"https://www.instagram.com/p/{shortcode}/"
    logger.info(
        "Instagram handler: metadata retrieved. Shortcode: %s, type: %s, uploader: %s",
        shortcode,
        media_type,
        uploader,
    )

    # Construct caption
    tags = ["instagram"]
    caption = (
        HtmlMessage(sender=sender)
        .title(title or f"Instagram Post by @{uploader}")
        .fields(("Uploader:", f"@{uploader}"))
        .link(original_url)
        .tags(*tags)
        .render()
    )

    success = False

    # Check media type
    if media_type == "image" or not data.get("formats"):
        # Image post or carousel of images
        photo_urls = [img["url"] for img in data.get("images", []) if img.get("url")]
        if not photo_urls and data.get("thumbnail"):
            photo_urls = [data["thumbnail"]]

        if photo_urls:
            logger.info(
                "Instagram handler: sending %d image(s) to Telegram", len(photo_urls)
            )
            success = await send_photos(
                context.bot,
                chat_id,
                photo_urls,
                caption,
                parse_mode=PARSE_MODE_HTML,
            )
    else:
        # Video/Reel post
        formats = data.get("formats", [])
        if formats:
            video_url = formats[0]["url"]
            try:
                await context.bot.send_chat_action(
                    chat_id=chat_id, action="upload_video"
                )
            except Exception:
                pass

            # Download video bytes to a temporary directory
            with tempfile.TemporaryDirectory() as out_dir:
                video_path = os.path.join(out_dir, f"{shortcode}.mp4")
                logger.info(
                    "Instagram handler: downloading video bytes from %s", video_url
                )
                try:
                    async with httpx.AsyncClient() as client:
                        resp = await client.get(video_url, timeout=30.0)
                        resp.raise_for_status()
                        with open(video_path, "wb") as f:
                            f.write(resp.content)
                    logger.info(
                        "Instagram handler: video download completed. File size: %d bytes",
                        os.path.getsize(video_path),
                    )
                except Exception as exc:
                    logger.exception(
                        "Failed to download Instagram video from direct URL: %s", exc
                    )
                    # Try fallback to standard video link downloader
                    from daphne.bot import handle_video_link

                    await handle_video_link(update, context, url)
                    return

                # Check video upload limit
                max_upload_bytes = video_upload_limit_mb() * 1024 * 1024
                file_size = os.path.getsize(video_path)
                if file_size > max_upload_bytes:
                    logger.warning(
                        "Instagram video size %d exceeds limit %d",
                        file_size,
                        max_upload_bytes,
                    )
                    from daphne.bot import handle_video_link

                    await handle_video_link(update, context, url)
                    return

                width, height, duration = probe_video_dimensions(video_path)
                logger.info(
                    "Instagram handler: sending video to Telegram (width=%s, height=%s, duration=%s)",
                    width,
                    height,
                    duration,
                )

                # Send video
                kwargs = {
                    "chat_id": chat_id,
                    "supports_streaming": True,
                    "caption": caption,
                    "parse_mode": PARSE_MODE_HTML,
                }
                if width is not None:
                    kwargs["width"] = width
                if height is not None:
                    kwargs["height"] = height
                if duration is not None:
                    kwargs["duration"] = int(float(duration))

                with open(video_path, "rb") as video_file:
                    await context.bot.send_video(video=video_file, **kwargs)
                success = True

    if success:
        logger.info(
            "Instagram handler: processing finished successfully. Deleting original message."
        )
        await try_delete_message(update)
    else:
        logger.warning("Instagram handler: processing failed to complete successfully.")
