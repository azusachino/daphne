import asyncio
import logging
import os
import re
import tempfile
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InlineQueryResultMpeg4Gif,
    InlineQueryResultPhoto,
    InlineQueryResultVideo,
    InputMediaPhoto,
    InputTextMessageContent,
    ReactionTypeEmoji,
    Update,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
)
import uuid

from daphne.config import (
    max_concurrent_downloads,
    max_user_concurrent_downloads,
    telegram_api_url,
    video_upload_limit_mb,
)
from daphne.downloader import (
    download_audio,
    download_video,
    fetch_video_metadata,
    format_duration,
    format_video_caption,
    is_bilibili_url,
    probe_video_dimensions,
    sanitize_video_url,
)
from daphne.gallery import chunk_images, download_gallery
from daphne.messages import HtmlMessage, PARSE_MODE_HTML, sender_attribution
from daphne.rbac import RbacService

CALLBACK_URL_CACHE: dict[str, str] = {}

logger = logging.getLogger(__name__)

# In-process concurrency guard for heavy downloads (yt-dlp / gallery-dl).
# Semaphores are created lazily inside the running loop. A user must clear both
# their own per-user slot and a shared global slot before a download proceeds,
# so one big transfer cannot starve the executor for everyone else.
_global_download_semaphore: Optional[asyncio.Semaphore] = None
_user_download_semaphores: dict[int, asyncio.Semaphore] = {}


def _get_global_download_semaphore() -> asyncio.Semaphore:
    global _global_download_semaphore
    if _global_download_semaphore is None:
        _global_download_semaphore = asyncio.Semaphore(max_concurrent_downloads())
    return _global_download_semaphore


def _get_user_download_semaphore(user_id: int) -> asyncio.Semaphore:
    sem = _user_download_semaphores.get(user_id)
    if sem is None:
        sem = asyncio.Semaphore(max_user_concurrent_downloads())
        _user_download_semaphores[user_id] = sem
    return sem


@asynccontextmanager
async def download_slot(
    user_id: int, on_wait: Optional[Callable[[], Awaitable[None]]] = None
):
    """
    Acquire a per-user then global download slot. If a slot is not immediately
    free, ``on_wait`` (when given) is awaited once to notify the user before we
    block. FIFO ordering is provided by the underlying semaphores.
    """
    user_sem = _get_user_download_semaphore(user_id)
    global_sem = _get_global_download_semaphore()
    if on_wait is not None and (user_sem.locked() or global_sem.locked()):
        try:
            await on_wait()
        except Exception:
            pass
    async with user_sem:
        async with global_sem:
            yield


ENV_BOT_TOKEN = "DAPHNE_BOT_TOKEN"
LOCAL_BOT_API_TIMEOUT_SECONDS = 7200
URL_REGEX = re.compile(r"https?://\S+")

# Lightweight progress feedback via message reactions. These are members of
# Telegram's default allowed-reaction set, so setMessageReaction accepts them
# without the chat needing custom reactions enabled.
REACTION_WORKING = "👀"
REACTION_DONE = "👍"
REACTION_FAILED = "👎"


async def set_reaction(message, emoji: Optional[str]) -> None:
    """
    Best-effort reaction on the user's message. Passing ``None`` clears it.
    Swallows all errors (private chats, unsupported emoji, missing rights) so
    progress cues never break the actual conversion flow.
    """
    if message is None:
        return
    try:
        reaction = [ReactionTypeEmoji(emoji)] if emoji else []
        await message.set_reaction(reaction=reaction)
    except Exception as exc:
        logger.debug("Failed to set reaction %s: %s", emoji, exc)


LINK_RE = re.compile(
    r"\b(?<!@)(?:https?://)?(?:www\.|vm\.|vt\.)?(?:"
    r"bilibili\.com|b23\.tv|"
    r"youtube\.com|youtu\.be|"
    r"twitter\.com|x\.com|fxtwitter\.com|vxtwitter\.com|fixupx\.com|"
    r"pixiv\.net|"
    r"bsky\.app|"
    r"instagram\.com|"
    r"tiktok\.com|douyin\.com"
    r")(/\S*)?",
    re.IGNORECASE,
)


def preprocess_text_links(text: str) -> str:
    def replace(match):
        matched = match.group(0)
        if matched.lower().startswith(("http://", "https://")):
            return matched
        return "https://" + matched

    return LINK_RE.sub(replace, text)


rbac_service = RbacService.load()


async def check_access_and_reply(update: Update, command: str) -> bool:
    user_id = update.effective_user.id if update.effective_user else 0
    chat_id = update.effective_chat.id if update.effective_chat else 0

    access = rbac_service.check_access(user_id, chat_id, command)
    if access.is_allowed():
        return True

    if access.is_rate_limited():
        text = HtmlMessage().text("Rate limit exceeded. Please wait.").render()
    else:
        text = HtmlMessage().text(f"Permission denied: {access.reason}").render()
    await update.message.reply_text(text, parse_mode=PARSE_MODE_HTML)
    return False


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access_and_reply(update, "help"):
        return

    text = (
        HtmlMessage(sender=sender_attribution(update.effective_user))
        .title("daphne")
        .text(
            "Send a Twitter/X, Pixiv, Bilibili, b23, or YouTube link and I will "
            "convert it into Telegram-friendly media.\n\n"
            "/audio <link> — extract audio as MP3\n"
            "/gallery <link> — download an image gallery"
        )
        .tags("daphne", "media")
        .render()
    )
    await update.message.reply_text(text, parse_mode=PARSE_MODE_HTML)


def detect_platform(url: str) -> str:
    url_lower = url.lower()
    if is_bilibili_url(url_lower):
        return "bilibili"
    elif "youtube.com" in url_lower or "youtu.be" in url_lower:
        return "youtube"
    elif "tiktok.com" in url_lower or "douyin.com" in url_lower:
        return "tiktok"
    elif "instagram.com" in url_lower:
        return "instagram"
    elif "bsky.app" in url_lower or "bluesky" in url_lower:
        return "bluesky"
    return "video"


def extract_video_url(text: str) -> str | None:
    text = preprocess_text_links(text)
    for match in URL_REGEX.finditer(text):
        url = sanitize_video_url(match.group(0))
        if is_bilibili_url(url) or "youtube.com" in url or "youtu.be" in url:
            return url
    return None


def _metadata_size(metadata: dict) -> int | None:
    for key in ("filesize", "filesize_approx"):
        value = metadata.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _expected_duration(metadata: dict) -> float | None:
    value = metadata.get("duration")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _video_caption_from_metadata(
    metadata: dict,
    url: str,
    fallback_path: str | None,
    duration: int | None,
    sender: str | None,
) -> str:
    title = metadata.get("title") or (
        os.path.splitext(os.path.basename(fallback_path))[0]
        if fallback_path
        else "Video"
    )
    uploader = metadata.get("uploader") or "Unknown"
    webpage_url = metadata.get("webpage_url") or url

    dur_secs = metadata.get("duration")
    if dur_secs is None:
        dur_secs = duration
    if dur_secs is not None:
        try:
            dur_str = format_duration(int(float(dur_secs)))
        except (ValueError, TypeError):
            dur_str = "Unknown"
    else:
        dur_str = "Unknown"

    return format_video_caption(
        title=title,
        uploader=uploader,
        duration=dur_str,
        url=webpage_url,
        platform=detect_platform(url),
        sender=sender,
    )


async def send_video_card(
    update: Update,
    url: str,
    metadata: dict,
    sender: str | None,
    reason: str,
) -> None:
    title = metadata.get("title") or "Video"
    uploader = metadata.get("uploader") or "Unknown"
    webpage_url = metadata.get("webpage_url") or url
    duration = metadata.get("duration")
    duration_text = "Unknown"
    if duration is not None:
        try:
            duration_text = format_duration(int(float(duration)))
        except (TypeError, ValueError):
            pass
    text = (
        HtmlMessage(sender=sender)
        .title(reason)
        .fields(
            ("Title:", title),
            ("Uploader:", uploader),
            ("Duration:", duration_text),
        )
        .link(webpage_url)
        .tags(detect_platform(url))
        .render()
    )
    buttons = []
    short_id = str(uuid.uuid4())[:8]
    CALLBACK_URL_CACHE[short_id] = webpage_url

    TG_HARD_LIMIT_BYTES = 2 * 1024 * 1024 * 1024
    size = _metadata_size(metadata)
    if size is None or size <= TG_HARD_LIMIT_BYTES:
        buttons.append(
            InlineKeyboardButton("Download Video", callback_data=f"dl:{short_id}")
        )
    buttons.append(InlineKeyboardButton("Open source", url=webpage_url))

    await update.message.reply_text(
        text,
        parse_mode=PARSE_MODE_HTML,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([buttons]),
    )


async def delete_original_message(update: Update) -> None:
    try:
        await update.message.delete()
    except Exception:
        pass


async def handle_video_link(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    custom_metadata: dict | None = None,
) -> None:
    sender = sender_attribution(update.effective_user)
    status_msg = await update.message.reply_text(
        HtmlMessage(sender=sender).text("Fetching video metadata...").render(),
        parse_mode=PARSE_MODE_HTML,
    )

    loop = asyncio.get_running_loop()
    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action="upload_video"
        )
    except Exception:
        pass

    if custom_metadata:
        metadata = custom_metadata
    else:
        metadata = await loop.run_in_executor(None, fetch_video_metadata, url)

    max_upload_bytes = video_upload_limit_mb() * 1024 * 1024
    size = _metadata_size(metadata)
    if size is not None and size > max_upload_bytes:
        await status_msg.delete()
        await send_video_card(
            update,
            url,
            metadata,
            sender,
            f"Video is over {video_upload_limit_mb()} MB",
        )
        await delete_original_message(update)
        return

    with tempfile.TemporaryDirectory() as out_dir:
        try:
            user_id = update.effective_user.id if update.effective_user else 0

            async def _notify_queued() -> None:
                await status_msg.edit_text(
                    HtmlMessage(sender=sender)
                    .text("Queued, waiting for a free download slot...")
                    .render(),
                    parse_mode=PARSE_MODE_HTML,
                )

            async with download_slot(user_id, on_wait=_notify_queued):
                await status_msg.edit_text(
                    HtmlMessage(sender=sender).text("Downloading video...").render(),
                    parse_mode=PARSE_MODE_HTML,
                )
                video_path = await loop.run_in_executor(
                    None, download_video, url, out_dir, _expected_duration(metadata)
                )
        except Exception as exc:
            logger.exception("Failed to download video")
            await set_reaction(update.message, REACTION_FAILED)
            await status_msg.edit_text(
                HtmlMessage(sender=sender)
                .text(f"Video download failed: {exc}")
                .render(),
                parse_mode=PARSE_MODE_HTML,
            )
            return

        file_size = os.path.getsize(video_path)
        if file_size > max_upload_bytes:
            await status_msg.delete()
            await send_video_card(
                update,
                url,
                metadata,
                sender,
                f"Video is over {video_upload_limit_mb()} MB",
            )
            await delete_original_message(update)
            return

        await status_msg.edit_text(
            HtmlMessage(sender=sender).text("Uploading video...").render(),
            parse_mode=PARSE_MODE_HTML,
        )
        try:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, action="upload_video"
            )
        except Exception:
            pass
        width, height, duration = await loop.run_in_executor(
            None, probe_video_dimensions, video_path
        )
        caption = _video_caption_from_metadata(
            metadata, url, video_path, duration, sender
        )

        kwargs = {
            "chat_id": update.effective_chat.id,
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

    await status_msg.delete()
    await delete_original_message(update)


async def media_message_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.message
    if not message or not message.text:
        return

    user_id = update.effective_user.id if update.effective_user else 0
    chat_id = update.effective_chat.id if update.effective_chat else 0
    logger.info(
        "Received message from user_id=%s in chat_id=%s: %r",
        user_id,
        chat_id,
        message.text,
    )

    text = preprocess_text_links(message.text)
    if text != message.text:
        object.__setattr__(message, "text", text)

    from daphne.pixiv import contains_pixiv_link, handle_pixiv_links
    from daphne.twitter import contains_twitter_link, handle_twitter_links
    from daphne.bluesky import contains_bluesky_link, handle_bluesky_links
    from daphne.instagram import contains_instagram_link, handle_instagram_links
    from daphne.tiktok import contains_tiktok_link, handle_tiktok_links

    is_twitter = contains_twitter_link(message.text)
    is_pixiv = contains_pixiv_link(message.text)
    is_bluesky = contains_bluesky_link(message.text)
    is_instagram = contains_instagram_link(message.text)
    is_tiktok = contains_tiktok_link(message.text)
    video_url = extract_video_url(message.text)

    # Acknowledge a recognised link with a lightweight "working" reaction. On
    # success the original message is deleted (taking the reaction with it); on
    # failure the handlers switch it to a failure reaction.
    if any([is_twitter, is_pixiv, is_bluesky, is_instagram, is_tiktok, video_url]):
        await set_reaction(message, REACTION_WORKING)

    if is_twitter:
        logger.info("Routing to Twitter handler")
        if await check_access_and_reply(update, "convert_link"):
            await handle_twitter_links(update, context)
    elif is_pixiv:
        logger.info("Routing to Pixiv handler")
        if await check_access_and_reply(update, "convert_link"):
            await handle_pixiv_links(update, context)
    elif is_bluesky:
        logger.info("Routing to Bluesky handler")
        if await check_access_and_reply(update, "convert_link"):
            await handle_bluesky_links(update, context)
    elif is_instagram:
        logger.info("Routing to Instagram handler")
        if await check_access_and_reply(update, "convert_link"):
            await handle_instagram_links(update, context)
    elif is_tiktok:
        logger.info("Routing to TikTok handler")
        if await check_access_and_reply(update, "convert_link"):
            await handle_tiktok_links(update, context)
    elif video_url:
        logger.info("Routing to generic video handler for URL: %s", video_url)
        # Check fetch_metadata permission & quota
        access = rbac_service.check_access(user_id, chat_id, "fetch_metadata")
        if not access.is_allowed():
            if access.is_rate_limited():
                text = HtmlMessage().text(f"Quota exceeded: {access.reason}").render()
            else:
                text = (
                    HtmlMessage().text(f"Permission denied: {access.reason}").render()
                )
            await update.message.reply_text(text, parse_mode=PARSE_MODE_HTML)
            return

        sender = sender_attribution(update.effective_user)
        status_msg = await update.message.reply_text(
            HtmlMessage(sender=sender).text("Fetching video metadata...").render(),
            parse_mode=PARSE_MODE_HTML,
        )

        try:
            loop = asyncio.get_running_loop()
            metadata = await loop.run_in_executor(None, fetch_video_metadata, video_url)
        except Exception as exc:
            logger.exception("Failed to fetch video metadata")
            await set_reaction(update.message, REACTION_FAILED)
            await status_msg.edit_text(
                HtmlMessage(sender=sender)
                .text(f"Video metadata fetch failed: {exc}")
                .render(),
                parse_mode=PARSE_MODE_HTML,
            )
            return

        preview_access = rbac_service.check_access(
            user_id, chat_id, "preview_video", dry_run=True
        )
        max_upload_bytes = video_upload_limit_mb() * 1024 * 1024
        size = _metadata_size(metadata)

        if (
            size is not None
            and size <= max_upload_bytes
            and preview_access.is_allowed()
        ):
            # Charge preview_video quota
            rbac_service.check_access(user_id, chat_id, "preview_video")
            await status_msg.delete()
            await handle_video_link(
                update, context, video_url, custom_metadata=metadata
            )
        else:
            # Fallback to sending the video card
            await status_msg.delete()
            TG_HARD_LIMIT_BYTES = 2 * 1024 * 1024 * 1024
            if size is not None and size > TG_HARD_LIMIT_BYTES:
                reason = "Video is over Telegram's 2GiB limit"
            elif size is not None and size > max_upload_bytes:
                reason = f"Video is over {video_upload_limit_mb()} MB"
            elif preview_access.is_rate_limited():
                reason = "Video Preview Quota Exceeded (direct download only)"
            else:
                reason = "Video Details"

            await send_video_card(
                update,
                video_url,
                metadata,
                sender,
                reason,
            )
            await delete_original_message(update)


async def audio_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access_and_reply(update, "extract_audio"):
        return

    message = update.message
    if not message:
        return

    # Extract text/link
    text = ""
    if len(context.args) > 0:
        text = " ".join(context.args)
    elif message.reply_to_message and message.reply_to_message.text:
        text = message.reply_to_message.text

    url = None
    if text:
        text = preprocess_text_links(text)
        match = URL_REGEX.search(text)
        if match:
            url = sanitize_video_url(match.group(0))

    if not url:
        await message.reply_text(
            HtmlMessage()
            .text("Please provide a link or reply to a message containing a link.")
            .render(),
            parse_mode=PARSE_MODE_HTML,
        )
        return

    sender = sender_attribution(update.effective_user)
    await set_reaction(message, REACTION_WORKING)
    status_msg = await message.reply_text(
        HtmlMessage(sender=sender).text("Fetching audio metadata...").render(),
        parse_mode=PARSE_MODE_HTML,
    )

    loop = asyncio.get_running_loop()
    try:
        await context.bot.send_chat_action(
            chat_id=message.chat_id, action="upload_audio"
        )
    except Exception:
        pass

    metadata = await loop.run_in_executor(None, fetch_video_metadata, url)
    max_upload_bytes = video_upload_limit_mb() * 1024 * 1024

    with tempfile.TemporaryDirectory() as out_dir:
        try:
            user_id = update.effective_user.id if update.effective_user else 0

            async def _notify_queued() -> None:
                await status_msg.edit_text(
                    HtmlMessage(sender=sender)
                    .text("Queued, waiting for a free download slot...")
                    .render(),
                    parse_mode=PARSE_MODE_HTML,
                )

            async with download_slot(user_id, on_wait=_notify_queued):
                await status_msg.edit_text(
                    HtmlMessage(sender=sender).text("Downloading audio...").render(),
                    parse_mode=PARSE_MODE_HTML,
                )
                try:
                    await context.bot.send_chat_action(
                        chat_id=message.chat_id, action="upload_audio"
                    )
                except Exception:
                    pass
                audio_path = await loop.run_in_executor(
                    None, download_audio, url, out_dir
                )
        except Exception as exc:
            logger.exception("Failed to download audio")
            await set_reaction(message, REACTION_FAILED)
            await status_msg.edit_text(
                HtmlMessage(sender=sender)
                .text(f"Audio download failed: {exc}")
                .render(),
                parse_mode=PARSE_MODE_HTML,
            )
            return

        file_size = os.path.getsize(audio_path)
        if file_size > max_upload_bytes:
            await status_msg.delete()
            await message.reply_text(
                HtmlMessage(sender=sender)
                .title("Audio is too large")
                .text(f"Audio file is over {video_upload_limit_mb()} MB limit.")
                .render(),
                parse_mode=PARSE_MODE_HTML,
            )
            await delete_original_message(update)
            return

        await status_msg.edit_text(
            HtmlMessage(sender=sender).text("Uploading audio...").render(),
            parse_mode=PARSE_MODE_HTML,
        )
        try:
            await context.bot.send_chat_action(
                chat_id=message.chat_id, action="upload_audio"
            )
        except Exception:
            pass

        title = (
            metadata.get("title") or os.path.splitext(os.path.basename(audio_path))[0]
        )
        performer = metadata.get("uploader") or "Unknown"
        duration_secs = metadata.get("duration")
        dur_val = None
        if duration_secs is not None:
            try:
                dur_val = int(float(duration_secs))
            except (ValueError, TypeError):
                pass

        # Construct simple caption
        platform = detect_platform(url)
        caption = (
            HtmlMessage(sender=sender)
            .title(title)
            .fields(("Uploader:", performer))
            .link(metadata.get("webpage_url") or url)
            .tags(platform, "audio")
            .render()
        )

        kwargs = {
            "chat_id": message.chat_id,
            "title": title,
            "performer": performer,
            "caption": caption,
            "parse_mode": PARSE_MODE_HTML,
        }
        if dur_val is not None:
            kwargs["duration"] = dur_val

        with open(audio_path, "rb") as audio_file:
            await context.bot.send_audio(audio=audio_file, **kwargs)

    await set_reaction(message, REACTION_DONE)
    await status_msg.delete()
    await delete_original_message(update)


async def gallery_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access_and_reply(update, "download_gallery"):
        return

    message = update.message
    if not message:
        return

    text = ""
    if context.args:
        text = " ".join(context.args)
    elif message.reply_to_message and message.reply_to_message.text:
        text = message.reply_to_message.text

    url = None
    if text:
        text = preprocess_text_links(text)
        match = URL_REGEX.search(text)
        if match:
            url = match.group(0)

    if not url:
        await message.reply_text(
            HtmlMessage()
            .text("Please provide a link or reply to a message containing a link.")
            .render(),
            parse_mode=PARSE_MODE_HTML,
        )
        return

    sender = sender_attribution(update.effective_user)
    user_id = update.effective_user.id if update.effective_user else 0
    await set_reaction(message, REACTION_WORKING)
    status_msg = await message.reply_text(
        HtmlMessage(sender=sender).text("Downloading gallery...").render(),
        parse_mode=PARSE_MODE_HTML,
    )

    loop = asyncio.get_running_loop()
    with tempfile.TemporaryDirectory() as out_dir:
        try:

            async def _notify_queued() -> None:
                await status_msg.edit_text(
                    HtmlMessage(sender=sender)
                    .text("Queued, waiting for a free download slot...")
                    .render(),
                    parse_mode=PARSE_MODE_HTML,
                )

            async with download_slot(user_id, on_wait=_notify_queued):
                try:
                    await context.bot.send_chat_action(
                        chat_id=message.chat_id, action="upload_photo"
                    )
                except Exception:
                    pass
                images = await loop.run_in_executor(
                    None, download_gallery, url, out_dir
                )
        except Exception as exc:
            logger.exception("Failed to download gallery")
            await set_reaction(message, REACTION_FAILED)
            await status_msg.edit_text(
                HtmlMessage(sender=sender)
                .text(f"Gallery download failed: {exc}")
                .render(),
                parse_mode=PARSE_MODE_HTML,
            )
            return

        if not images:
            await set_reaction(message, REACTION_FAILED)
            await status_msg.edit_text(
                HtmlMessage(sender=sender)
                .text("No images found for that link.")
                .render(),
                parse_mode=PARSE_MODE_HTML,
            )
            return

        caption = (
            HtmlMessage(sender=sender)
            .text(f"{len(images)} image(s)")
            .link(url)
            .tags("gallery")
            .render()
        )
        await status_msg.edit_text(
            HtmlMessage(sender=sender)
            .text(f"Uploading {len(images)} image(s)...")
            .render(),
            parse_mode=PARSE_MODE_HTML,
        )

        first_group = True
        for chunk in chunk_images(images):
            open_files = []
            try:
                media = []
                for index, path in enumerate(chunk):
                    handle = open(path, "rb")
                    open_files.append(handle)
                    if first_group and index == 0:
                        media.append(
                            InputMediaPhoto(
                                media=handle,
                                caption=caption,
                                parse_mode=PARSE_MODE_HTML,
                            )
                        )
                    else:
                        media.append(InputMediaPhoto(media=handle))
                await context.bot.send_media_group(chat_id=message.chat_id, media=media)
            finally:
                for handle in open_files:
                    handle.close()
            first_group = False

    await set_reaction(message, REACTION_DONE)
    await status_msg.delete()
    await delete_original_message(update)


INLINE_RESULT_LIMIT = 10


def _twitter_inline_results(media: dict, caption: str) -> list:
    """Build inline photo/video/gif results from a resolved tweet."""
    results: list = []
    for photo_url in media.get("photos", []):
        results.append(
            InlineQueryResultPhoto(
                id=uuid.uuid4().hex,
                photo_url=photo_url,
                thumbnail_url=photo_url,
                caption=caption,
                parse_mode=PARSE_MODE_HTML,
            )
        )
    for video in media.get("videos", []):
        thumb = video.get("thumbnail")
        if not thumb:
            continue
        results.append(
            InlineQueryResultVideo(
                id=uuid.uuid4().hex,
                video_url=video["url"],
                mime_type="video/mp4",
                thumbnail_url=thumb,
                title="Video",
                caption=caption,
                parse_mode=PARSE_MODE_HTML,
            )
        )
    for gif in media.get("gifs", []):
        thumb = gif.get("thumbnail")
        if not thumb:
            continue
        results.append(
            InlineQueryResultMpeg4Gif(
                id=uuid.uuid4().hex,
                mpeg4_url=gif["url"],
                thumbnail_url=thumb,
                caption=caption,
                parse_mode=PARSE_MODE_HTML,
            )
        )
    return results[:INLINE_RESULT_LIMIT]


def _instagram_inline_results(media: dict, caption: str) -> list:
    """Build inline photo/video results from a resolved Instagram post."""
    results: list = []
    for photo_url in media.get("photos", []):
        results.append(
            InlineQueryResultPhoto(
                id=uuid.uuid4().hex,
                photo_url=photo_url,
                thumbnail_url=photo_url,
                caption=caption,
                parse_mode=PARSE_MODE_HTML,
            )
        )
    if media.get("video_url") and media.get("thumbnail"):
        results.append(
            InlineQueryResultVideo(
                id=uuid.uuid4().hex,
                video_url=media["video_url"],
                mime_type="video/mp4",
                thumbnail_url=media["thumbnail"],
                title=media.get("title") or "Video",
                caption=caption,
                parse_mode=PARSE_MODE_HTML,
            )
        )
    return results[:INLINE_RESULT_LIMIT]


async def inline_query_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Inline conversion: ``@daphne <url>`` — resolves a link to native media
    (photos/video/gif) the user can send straight into any chat. Telegram
    provides no chat context for inline queries, so authorization is user-level
    only (``chat_id=0`` means the chat tier of RBAC can never match) via the
    ``inline_convert`` permission.
    """
    inline_query = update.inline_query
    if inline_query is None:
        return

    user_id = inline_query.from_user.id if inline_query.from_user else 0
    query = (inline_query.query or "").strip()

    access = rbac_service.check_access(user_id, 0, "inline_convert")
    if not access.is_allowed():
        await inline_query.answer(
            results=[],
            cache_time=5,
            is_personal=True,
            button=InlineQueryResultsButton(
                text="Not authorized — open a chat with me",
                start_parameter="start",
            ),
        )
        return

    if not query:
        await inline_query.answer(
            results=[],
            cache_time=5,
            is_personal=True,
            button=InlineQueryResultsButton(
                text="Paste a link after the bot name",
                start_parameter="start",
            ),
        )
        return

    from daphne.twitter import (
        contains_twitter_link,
        extract_twitter_link,
        resolve_twitter_media,
        build_caption,
    )
    from daphne.instagram import (
        contains_instagram_link,
        extract_instagram_link,
        resolve_instagram_media,
    )

    processed = preprocess_text_links(query)
    sender = sender_attribution(inline_query.from_user)
    loop = asyncio.get_running_loop()
    results: list = []
    source_url: str | None = None
    source_title = "Media"

    if contains_twitter_link(processed):
        info = extract_twitter_link(processed)
        if info:
            domain, username, tweet_id = info
            source_url = f"https://{domain}/{username}/status/{tweet_id}"
            source_title = "Tweet"
            media = await resolve_twitter_media(username, tweet_id)
            if media:
                source_url = media["url"]
                caption = build_caption(media["text"], media["url"], sender)
                results = _twitter_inline_results(media, caption)
    elif contains_instagram_link(processed):
        ig_url = extract_instagram_link(processed)
        if ig_url:
            source_url = ig_url
            source_title = "Instagram post"
            media = await loop.run_in_executor(None, resolve_instagram_media, ig_url)
            if media:
                source_url = media["original_url"]
                source_title = media.get("title") or (
                    f"Instagram post by @{media['uploader']}"
                )
                caption = (
                    HtmlMessage(sender=sender)
                    .title(source_title)
                    .fields(("Uploader:", f"@{media['uploader']}"))
                    .link(media["original_url"])
                    .tags("instagram")
                    .render()
                )
                results = _instagram_inline_results(media, caption)
    else:
        match = URL_REGEX.search(processed)
        if match:
            url = sanitize_video_url(match.group(0))
            source_url = url
            try:
                metadata = await loop.run_in_executor(None, fetch_video_metadata, url)
            except Exception:
                logger.exception("Inline metadata fetch failed")
                metadata = {}
            source_title = metadata.get("title") or "Media"
            source_url = metadata.get("webpage_url") or url
            direct_url = metadata.get("url")
            thumbnail = metadata.get("thumbnail")
            if direct_url and thumbnail:
                caption = (
                    HtmlMessage(sender=sender)
                    .title(source_title)
                    .link(source_url)
                    .tags(detect_platform(url))
                    .render()
                )
                results.append(
                    InlineQueryResultVideo(
                        id=uuid.uuid4().hex,
                        video_url=direct_url,
                        mime_type="video/mp4",
                        thumbnail_url=thumbnail,
                        title=source_title,
                        caption=caption,
                        parse_mode=PARSE_MODE_HTML,
                    )
                )

    # Always offer a link-sharing article so an authorized user gets a usable
    # result even when direct media can't be resolved.
    if source_url:
        results.append(
            InlineQueryResultArticle(
                id=uuid.uuid4().hex,
                title=f"Share link: {source_title}",
                description=source_url,
                input_message_content=InputTextMessageContent(
                    message_text=(
                        HtmlMessage(sender=sender)
                        .title(source_title)
                        .link(source_url)
                        .render()
                    ),
                    parse_mode=PARSE_MODE_HTML,
                ),
            )
        )

    if not results:
        await inline_query.answer(results=[], cache_time=5, is_personal=True)
        return

    await inline_query.answer(results=results, cache_time=30, is_personal=True)


async def download_button_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query:
        return

    data = query.data
    if not data or not data.startswith("dl:"):
        await query.answer()
        return

    short_id = data[3:]
    url = CALLBACK_URL_CACHE.get(short_id)
    if not url:
        await query.answer("Error: Video link has expired.", show_alert=True)
        return

    user_id = query.from_user.id if query.from_user else 0
    chat_id = query.message.chat.id if query.message else 0
    sender = sender_attribution(query.from_user)

    # Check download_video permission & quota (actual non-dry-run check)
    access = rbac_service.check_access(user_id, chat_id, "download_video")
    if not access.is_allowed():
        # Do not download, alert the user
        await query.answer(access.reason or "Permission denied", show_alert=True)
        return

    # Acknowledge the callback query immediately to avoid loading animation on button
    await query.answer()

    # Edit the card message to indicate progress and remove the buttons
    try:
        await query.edit_message_text(
            HtmlMessage(sender=sender).text("Fetching video metadata...").render(),
            parse_mode=PARSE_MODE_HTML,
            reply_markup=None,
        )
    except Exception:
        pass

    loop = asyncio.get_running_loop()
    try:
        metadata = await loop.run_in_executor(None, fetch_video_metadata, url)
    except Exception as exc:
        logger.exception("Failed to fetch video metadata")
        try:
            await query.edit_message_text(
                HtmlMessage(sender=sender)
                .text(f"Video metadata fetch failed: {exc}")
                .render(),
                parse_mode=PARSE_MODE_HTML,
            )
        except Exception:
            pass
        return

    TG_HARD_LIMIT_BYTES = 2 * 1024 * 1024 * 1024
    size = _metadata_size(metadata)
    if size is not None and size > TG_HARD_LIMIT_BYTES:
        try:
            await query.edit_message_text(
                HtmlMessage(sender=sender)
                .text(
                    f"Video exceeds Telegram's 2GiB upload limit (size: {size / (1024 * 1024 * 1024):.2f} GiB)."
                )
                .render(),
                parse_mode=PARSE_MODE_HTML,
            )
        except Exception:
            pass
        return

    try:
        await query.edit_message_text(
            HtmlMessage(sender=sender).text("Downloading video...").render(),
            parse_mode=PARSE_MODE_HTML,
        )
    except Exception:
        pass

    with tempfile.TemporaryDirectory() as out_dir:
        try:

            async def _notify_queued() -> None:
                try:
                    await query.edit_message_text(
                        HtmlMessage(sender=sender)
                        .text("Queued, waiting for a free download slot...")
                        .render(),
                        parse_mode=PARSE_MODE_HTML,
                    )
                except Exception:
                    pass

            async with download_slot(user_id, on_wait=_notify_queued):
                video_path = await loop.run_in_executor(
                    None, download_video, url, out_dir, _expected_duration(metadata)
                )
        except Exception as exc:
            logger.exception("Failed to download video")
            try:
                await query.edit_message_text(
                    HtmlMessage(sender=sender)
                    .text(f"Video download failed: {exc}")
                    .render(),
                    parse_mode=PARSE_MODE_HTML,
                )
            except Exception:
                pass
            return

        file_size = os.path.getsize(video_path)
        if file_size > TG_HARD_LIMIT_BYTES:
            try:
                await query.edit_message_text(
                    HtmlMessage(sender=sender)
                    .text(
                        f"Downloaded video file size ({file_size / (1024 * 1024 * 1024):.2f} GiB) exceeds Telegram's 2GiB limit."
                    )
                    .render(),
                    parse_mode=PARSE_MODE_HTML,
                )
            except Exception:
                pass
            return

        try:
            await query.edit_message_text(
                HtmlMessage(sender=sender).text("Uploading video...").render(),
                parse_mode=PARSE_MODE_HTML,
            )
        except Exception:
            pass

        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="upload_video")
        except Exception:
            pass

        width, height, duration = await loop.run_in_executor(
            None, probe_video_dimensions, video_path
        )
        caption = _video_caption_from_metadata(
            metadata, url, video_path, duration, sender
        )

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

        try:
            with open(video_path, "rb") as video_file:
                await context.bot.send_video(video=video_file, **kwargs)
        except Exception as exc:
            logger.exception("Failed to send video")
            try:
                await query.edit_message_text(
                    HtmlMessage(sender=sender)
                    .text(f"Failed to upload video: {exc}")
                    .render(),
                    parse_mode=PARSE_MODE_HTML,
                )
            except Exception:
                pass
            return

    # Delete the status card message on success
    try:
        await query.message.delete()
    except Exception:
        pass

    # Attempt to delete the original message containing the link if it was replied to
    try:
        if query.message and query.message.reply_to_message:
            await query.message.reply_to_message.delete()
    except Exception:
        pass


def build_application() -> Application:
    token = os.environ.get(ENV_BOT_TOKEN)
    if not token:
        raise ValueError(f"{ENV_BOT_TOKEN} environment variable not set")

    builder = Application.builder().token(token).job_queue(None)
    local_api_url = telegram_api_url()
    if local_api_url:
        local_api_url = local_api_url.rstrip("/")
        logger.info("Using local Telegram Bot API server: %s", local_api_url)
        builder = (
            builder.base_url(f"{local_api_url}/bot")
            .base_file_url(f"{local_api_url}/file/bot")
            .local_mode(True)
            .media_write_timeout(LOCAL_BOT_API_TIMEOUT_SECONDS)
            .read_timeout(LOCAL_BOT_API_TIMEOUT_SECONDS)
            .connect_timeout(30.0)
        )

    app = builder.build()
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("audio", audio_command))
    app.add_handler(CommandHandler("gallery", gallery_command))
    app.add_handler(InlineQueryHandler(inline_query_handler))
    app.add_handler(CallbackQueryHandler(download_button_callback, pattern=r"^dl:"))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, media_message_handler)
    )
    return app
