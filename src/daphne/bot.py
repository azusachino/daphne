import asyncio
import logging
import os
import re
import tempfile

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from daphne.config import telegram_api_url, video_upload_limit_mb
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
from daphne.messages import HtmlMessage, PARSE_MODE_HTML, sender_attribution
from daphne.rbac import RbacService

logger = logging.getLogger(__name__)

ENV_BOT_TOKEN = "DAPHNE_BOT_TOKEN"
LOCAL_BOT_API_TIMEOUT_SECONDS = 7200
URL_REGEX = re.compile(r"https?://\S+")

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
            "Send a Twitter/X, Pixiv, Bilibili, b23, or YouTube link and I will convert it into Telegram-friendly media."
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
    for match in URL_REGEX.finditer(text):
        url = sanitize_video_url(match.group(0))
        if (
            is_bilibili_url(url)
            or "youtube.com" in url
            or "youtu.be" in url
        ):
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
    await update.message.reply_text(
        text,
        parse_mode=PARSE_MODE_HTML,
        disable_web_page_preview=False,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Open source", url=webpage_url)]]
        ),
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
    if size is None and not custom_metadata:
        await status_msg.delete()
        await send_video_card(
            update,
            url,
            metadata,
            sender,
            "Video size is unknown",
        )
        await delete_original_message(update)
        return
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
            await status_msg.edit_text(
                HtmlMessage(sender=sender).text("Downloading video...").render(),
                parse_mode=PARSE_MODE_HTML,
            )
            video_path = await loop.run_in_executor(None, download_video, url, out_dir)
        except Exception as exc:
            logger.exception("Failed to download video")
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

    from daphne.pixiv import contains_pixiv_link, handle_pixiv_links
    from daphne.twitter import contains_twitter_link, handle_twitter_links
    from daphne.bluesky import contains_bluesky_link, handle_bluesky_links
    from daphne.instagram import contains_instagram_link, handle_instagram_links
    from daphne.tiktok import contains_tiktok_link, handle_tiktok_links

    if contains_twitter_link(message.text):
        if await check_access_and_reply(update, "fix"):
            await handle_twitter_links(update, context)
    elif contains_pixiv_link(message.text):
        if await check_access_and_reply(update, "fix"):
            await handle_pixiv_links(update, context)
    elif contains_bluesky_link(message.text):
        if await check_access_and_reply(update, "fix"):
            await handle_bluesky_links(update, context)
    elif contains_instagram_link(message.text):
        if await check_access_and_reply(update, "fix"):
            await handle_instagram_links(update, context)
    elif contains_tiktok_link(message.text):
        if await check_access_and_reply(update, "fix"):
            await handle_tiktok_links(update, context)
    elif video_url := extract_video_url(message.text):
        if await check_access_and_reply(update, "fix"):
            await handle_video_link(update, context, video_url)


async def audio_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access_and_reply(update, "fix"):
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
        match = URL_REGEX.search(text)
        if match:
            url = sanitize_video_url(match.group(0))

    if not url:
        await message.reply_text(
            HtmlMessage().text("Please provide a link or reply to a message containing a link.").render(),
            parse_mode=PARSE_MODE_HTML
        )
        return

    sender = sender_attribution(update.effective_user)
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
            audio_path = await loop.run_in_executor(None, download_audio, url, out_dir)
        except Exception as exc:
            logger.exception("Failed to download audio")
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

        title = metadata.get("title") or os.path.splitext(os.path.basename(audio_path))[0]
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

    await status_msg.delete()
    await delete_original_message(update)


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
        )

    app = builder.build()
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("audio", audio_command))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, media_message_handler)
    )
    return app
