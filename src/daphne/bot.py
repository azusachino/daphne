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


def extract_video_url(text: str) -> str | None:
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
        is_bilibili=is_bilibili_url(url),
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
        .tags("bilibili" if is_bilibili_url(url) else "youtube")
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


async def handle_video_link(
    update: Update, context: ContextTypes.DEFAULT_TYPE, url: str
) -> None:
    sender = sender_attribution(update.effective_user)
    status_msg = await update.message.reply_text(
        HtmlMessage(sender=sender).text("Fetching video metadata...").render(),
        parse_mode=PARSE_MODE_HTML,
    )

    loop = asyncio.get_running_loop()
    metadata = await loop.run_in_executor(None, fetch_video_metadata, url)
    max_upload_bytes = video_upload_limit_mb() * 1024 * 1024
    size = _metadata_size(metadata)
    if size is None:
        await status_msg.delete()
        await send_video_card(
            update,
            url,
            metadata,
            sender,
            "Video size is unknown",
        )
        return
    if size > max_upload_bytes:
        await status_msg.delete()
        await send_video_card(
            update,
            url,
            metadata,
            sender,
            f"Video is over {video_upload_limit_mb()} MB",
        )
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
            return

        await status_msg.edit_text(
            HtmlMessage(sender=sender).text("Uploading video...").render(),
            parse_mode=PARSE_MODE_HTML,
        )
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
    try:
        await update.message.delete()
    except Exception:
        pass


async def media_message_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.message
    if not message or not message.text:
        return

    from daphne.pixiv import contains_pixiv_link, handle_pixiv_links
    from daphne.twitter import contains_twitter_link, handle_twitter_links

    if contains_twitter_link(message.text):
        if await check_access_and_reply(update, "fix"):
            await handle_twitter_links(update, context)
    elif contains_pixiv_link(message.text):
        if await check_access_and_reply(update, "fix"):
            await handle_pixiv_links(update, context)
    elif video_url := extract_video_url(message.text):
        if await check_access_and_reply(update, "fix"):
            await handle_video_link(update, context, video_url)


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
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, media_message_handler)
    )
    return app
