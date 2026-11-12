import asyncio
import re
import os
import httpx
import tempfile
import logging
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InputMediaVideo

from daphne.tg import TelegramContext, TelegramUpdate

from daphne.messages import HtmlMessage, PARSE_MODE_HTML, author_tag, sender_attribution
from daphne.twitter import send_photos, try_delete_message
from daphne.downloader import probe_video_dimensions
from daphne.media_http import MediaTooLarge, stream_media

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


class CarouselItemTooLarge(Exception):
    """A carousel video's real bytes crossed the cap the gate approved."""

    def __init__(self, video):
        super().__init__("carousel video exceeded the upload cap")
        self.video = video


async def send_carousel_media(bot, chat_id, videos, caption, update, photos=()):
    """Deliver a gated carousel: fetch every video, then photos, then groups.

    The gate already approved each item; this only fetches bytes and sends.
    All video bytes land on disk before anything is sent, so a mid-download
    failure (:class:`CarouselItemTooLarge`) leaves nothing half-delivered.
    The preview quota charges after each successful send: once per album,
    once per single video when the album is refused.
    """
    # Local import: bot.py imports this module's handler, resolved at call time.
    from daphne.bot import _charge_preview
    from daphne.config import upload_cap_bytes

    with tempfile.TemporaryDirectory() as directory:
        paths = []
        async with httpx.AsyncClient() as client:
            for index, video in enumerate(videos):
                path = os.path.join(directory, f"{index}.mp4")
                try:
                    with open(path, "wb") as output:
                        # Already verified public by the pricing HEAD.
                        await stream_media(
                            client,
                            video.fetch_url,
                            output,
                            upload_cap_bytes(),
                            verify_public=False,
                        )
                except MediaTooLarge:
                    raise CarouselItemTooLarge(video) from None
                paths.append(path)
        video_caption = "" if photos else caption
        for start in range(0, len(paths), 10):
            batch = paths[start : start + 10]
            batch_caption = video_caption if start == 0 else ""
            if len(batch) >= 2:
                media = [
                    InputMediaVideo(
                        media=FSInputFile(path),
                        caption=batch_caption if i == 0 else None,
                        parse_mode=PARSE_MODE_HTML if i == 0 else None,
                        supports_streaming=True,
                    )
                    for i, path in enumerate(batch)
                ]
                try:
                    await bot.send_media_group(chat_id=chat_id, media=media)
                    await _charge_preview(update)
                    continue
                except TelegramBadRequest as exc:
                    logger.warning("Instagram video album refused: %s", exc.message)
            for i, path in enumerate(batch):
                await bot.send_video(
                    chat_id=chat_id,
                    video=FSInputFile(path),
                    caption=batch_caption if i == 0 else None,
                    parse_mode=PARSE_MODE_HTML,
                    supports_streaming=True,
                )
                await _charge_preview(update)
        # Photos go out last: a transport failure before this point leaves
        # them unsent, which is what the failure note promises.
        if photos:
            await send_photos(bot, chat_id, photos, caption, PARSE_MODE_HTML)


async def handle_instagram_links(
    update: TelegramUpdate, context: TelegramContext
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
    clean_url = url.split("?")[0]
    try:
        from parth_dl.extractors import MediaExtractor

        extractor = MediaExtractor()
        logger.info(
            "Instagram handler: extracting metadata using parth-dl for clean URL %s",
            clean_url,
        )
        data = await asyncio.get_running_loop().run_in_executor(
            None, extractor.extract, clean_url
        )
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
        # Do NOT fall back to handle_video_url here: yt-dlp's own Instagram
        # extractor hits the same public post/API endpoints parth-dl already
        # tried (and we just tried it above), so the full yt-dlp
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
            parse_mode=None,
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
    slug = author_tag(uploader)
    if slug and slug not in tags:
        tags.append(slug)
    caption = (
        HtmlMessage(sender=sender)
        .title(title or f"Instagram Post by @{uploader}")
        .fields(("Uploader:", f"@{uploader}"))
        .link(original_url, "🔗 Source (Instagram)")
        .tags(*tags)
        .render()
    )

    success = False

    # A carousel's entries represent different items; formats within an entry
    # are alternatives for that item, not additional videos.
    entries = data.get("entries") or []
    if media_type == "carousel" and entries:
        photos = []
        videos = []
        # Local imports: bot.py imports this module's handler, resolved at call
        # time to avoid the cycle.
        from daphne.bot import (
            REACTION_FAILED,
            decide_delivery,
            download_slot,
            set_reaction,
        )
        from daphne.media_http import content_length
        from daphne.video import from_metadata
        from matcha import Upload

        try:
            for entry in entries:
                formats = [fmt for fmt in entry.get("formats", []) if fmt.get("url")]
                if not formats or entry.get("kind") not in {"image", "video"}:
                    raise ValueError("Instagram carousel contains an unavailable item")
                target = photos if entry["kind"] == "image" else videos
                target.append(formats[0]["url"])
            # Price and gate every video item before any byte is fetched. One
            # card-able item cards the whole video set -- a half-delivered group
            # is worse than a card -- and the card already says why. Photos are
            # not preview-gated by design.
            async with httpx.AsyncClient() as client:
                priced = [
                    from_metadata(
                        item_url,
                        {
                            "title": title,
                            "uploader": uploader,
                            "webpage_url": url,
                            "size": await content_length(
                                client,
                                item_url,
                                headers={"Referer": "https://www.instagram.com/"},
                            ),
                        },
                        platform="instagram",
                    )
                    for item_url in videos
                ]
            for video in priced:
                if not isinstance(await decide_delivery(update, video, sender), Upload):
                    # The gate rendered the card for this item. Deliver the
                    # photos, keep the original message, and stop.
                    if photos:
                        await send_photos(
                            context.bot, chat_id, photos, caption, PARSE_MODE_HTML
                        )
                    return
            user_id = update.effective_user.id if update.effective_user else 0
            async with download_slot(user_id):
                try:
                    await send_carousel_media(
                        context.bot,
                        chat_id,
                        priced,
                        caption,
                        update,
                        photos,
                    )
                except CarouselItemTooLarge as too_large:
                    # The real bytes crossed the cap after the gate approved
                    # the price. Nothing was sent yet, so the photos still go
                    # out — and every video in the set gets a card, because a
                    # half-delivered group is worse than cards. The refused
                    # item says why; the rest just offer the button.
                    from daphne.bot import _size_reason, send_video_card

                    if photos:
                        await send_photos(
                            context.bot, chat_id, photos, caption, PARSE_MODE_HTML
                        )
                    await send_video_card(
                        update,
                        too_large.video,
                        sender,
                        _size_reason(too_large.video.estimated_bytes or 0),
                    )
                    for video in priced:
                        if video is too_large.video:
                            continue
                        await send_video_card(update, video, sender, "Video Details")
                    return
        except Exception:
            logger.exception("Instagram carousel delivery failed")
            await set_reaction(message, REACTION_FAILED)
            await context.bot.send_message(
                chat_id=chat_id,
                text="Could not deliver every carousel item; your original link was kept.",
            )
            return
        await try_delete_message(update)
        return

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
    else:
        # Video/Reel post
        formats = data.get("formats", [])
        if formats:
            video_url = formats[0]["url"]

            # Local imports: bot.py imports this module's handler, resolved at
            # call time to avoid the cycle.
            from daphne.bot import (
                _charge_preview,
                _size_reason,
                decide_delivery,
                download_slot,
                send_video_card,
            )
            from daphne.config import upload_cap_bytes
            from daphne.video import from_metadata
            from matcha import Upload
            from daphne.media_http import content_length

            # Price the direct file before downloading it: a HEAD gives the byte
            # size when the CDN states one, and the delivery gate decides
            # upload-or-card without a single media byte fetched.
            async with httpx.AsyncClient() as client:
                size = await content_length(
                    client,
                    video_url,
                    headers={"Referer": "https://www.instagram.com/"},
                )
            video = from_metadata(
                url,
                {
                    "title": title,
                    "uploader": uploader,
                    "duration": data.get("duration"),
                    "webpage_url": url,
                    "size": size,
                },
                platform="instagram",
            )
            decision = await decide_delivery(update, video, sender)
            if not isinstance(decision, Upload):
                # The gate rendered the card. That is a delivered outcome, not a
                # failure the fallback chain should retry.
                return

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
                    from daphne.bot import download_slot

                    user_id = update.effective_user.id if update.effective_user else 0
                    async with download_slot(user_id):
                        async with httpx.AsyncClient() as client:
                            with open(video_path, "wb") as f:
                                # Already verified public by the pricing HEAD.
                                await stream_media(
                                    client,
                                    video_url,
                                    f,
                                    upload_cap_bytes(),
                                    verify_public=False,
                                )
                    logger.info(
                        "Instagram handler: video download completed. File size: %d bytes",
                        os.path.getsize(video_path),
                    )
                except MediaTooLarge:
                    from daphne.bot import _size_reason, send_video_card

                    # The CDN's price was wrong: the real bytes passed the cap the
                    # gate already approved. Card the priced video.
                    await send_video_card(
                        update,
                        video,
                        sender,
                        _size_reason(video.estimated_bytes or 0),
                    )
                    return
                except Exception as exc:
                    logger.exception(
                        "Failed to download Instagram video from direct URL: %s", exc
                    )
                    # The gate already approved this priced video; the direct bytes
                    # failed. Card it -- the Download button runs the full pipeline
                    # on demand -- instead of a second silent resolve+download cycle.
                    from daphne.bot import send_video_card

                    await send_video_card(
                        update,
                        video,
                        sender,
                        "Direct download failed — use Download for the full pipeline",
                    )
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
                await _charge_preview(update)
                success = True

    if success:
        logger.info(
            "Instagram handler: processing finished successfully. Deleting original message."
        )
        await try_delete_message(update)
    else:
        logger.warning("Instagram handler: processing failed to complete successfully.")
