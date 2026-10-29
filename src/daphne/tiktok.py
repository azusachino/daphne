import re
import os
import httpx
import tempfile
import logging
from daphne.tg import TelegramContext, TelegramUpdate, failure_reason

from daphne.messages import PARSE_MODE_HTML, sender_attribution
from daphne.twitter import try_delete_message
from daphne.downloader import probe_video_dimensions, format_video_caption
from daphne.config import video_upload_limit_mb
from daphne.media_http import MediaTooLarge, stream_media

logger = logging.getLogger(__name__)

TIKTOK_REGEX = re.compile(
    r"https?://(?:www\.|vm\.|vt\.)?(?:tiktok\.com/@[^/]+/video/\d+|tiktok\.com/\w+|douyin\.com/video/\d+)",
    re.IGNORECASE,
)


def contains_tiktok_link(text: str) -> bool:
    """
    Returns True if the text contains a TikTok or Douyin link.
    """
    return bool(TIKTOK_REGEX.search(text))


def extract_tiktok_link(text: str) -> str | None:
    """
    Extracts the first TikTok or Douyin link in the text.
    """
    match = TIKTOK_REGEX.search(text)
    if match:
        return match.group(0)
    return None


async def handle_tiktok_links(update: TelegramUpdate, context: TelegramContext) -> None:
    """
    Downloads and sends video from a TikTok link using TikWM API, with fallback to yt-dlp.
    """
    message = update.message
    if not message or not message.text:
        return

    url = extract_tiktok_link(message.text)
    if not url:
        return

    chat_id = message.chat_id
    sender = sender_attribution(update.effective_user)
    logger.info("TikTok handler: start processing link %s for chat_id=%s", url, chat_id)

    # 1. Trigger visual feedback action
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="upload_video")
    except Exception as exc:
        logger.warning("TikTok upload action failed: %s", failure_reason(exc))

    # 2. Query TikWM API
    api_url = "https://tikwm.com/api/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    success = False
    try:
        logger.info("TikTok handler: querying TikWM API for URL: %s", url)
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                api_url, params={"url": url}, headers=headers, timeout=15.0
            )
            if resp.status_code == 200:
                res_data = resp.json()
                if res_data.get("code") == 0 and "data" in res_data:
                    data = res_data["data"]
                    video_url = data.get("play")
                    title = data.get("title", "")
                    uploader = data.get("author", {}).get("unique_id", "unknown")
                    duration_secs = data.get("duration")
                    logger.info(
                        "TikTok handler: TikWM API succeeded. Video ID: %s, title: %s, uploader: %s",
                        data.get("id"),
                        title[:100],
                        uploader,
                    )

                    # Construct caption
                    caption = format_video_caption(
                        title=title,
                        uploader=uploader,
                        duration=str(duration_secs) if duration_secs else "Unknown",
                        url=url,
                        platform="tiktok",
                        sender=sender,
                    )

                    # Download video file
                    if video_url:
                        with tempfile.TemporaryDirectory() as out_dir:
                            video_path = os.path.join(
                                out_dir, f"{data.get('id', 'tiktok_video')}.mp4"
                            )
                            logger.info(
                                "TikTok handler: downloading video bytes from direct URL %s",
                                video_url,
                            )
                            from daphne.bot import download_slot, send_video_card

                            max_upload_bytes = video_upload_limit_mb() * 1024 * 1024
                            user_id = (
                                update.effective_user.id if update.effective_user else 0
                            )
                            try:
                                async with download_slot(user_id):
                                    with open(video_path, "wb") as f:
                                        await stream_media(
                                            client,
                                            video_url,
                                            f,
                                            max_upload_bytes,
                                            headers=headers,
                                        )
                            except MediaTooLarge:
                                await send_video_card(
                                    update,
                                    url,
                                    {
                                        "title": title,
                                        "uploader": uploader,
                                        "duration": duration_secs,
                                        "webpage_url": url,
                                    },
                                    sender,
                                    f"Video is over {video_upload_limit_mb()} MB",
                                )
                                return

                            width, height, duration = probe_video_dimensions(video_path)
                            logger.info(
                                "TikTok handler: sending video to Telegram (width=%s, height=%s, duration=%s)",
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
                else:
                    logger.warning(
                        "TikWM API returned error code/msg: %s - %s",
                        res_data.get("code"),
                        res_data.get("msg"),
                    )
            else:
                logger.warning("TikWM API HTTP error status: %s", resp.status_code)
    except Exception as e:
        logger.exception("Failed to fetch TikTok video from TikWM: %s", e)

    # Fallback to standard yt-dlp downloader if TikWM fails
    if not success:
        logger.info(
            "TikTok handler: falling back to standard yt-dlp downloader for URL: %s",
            url,
        )
        from daphne.bot import handle_video_link

        await handle_video_link(update, context, url)
        return

    if success:
        logger.info(
            "TikTok handler: processing finished successfully. Deleting original message."
        )
        await try_delete_message(update)
    else:
        logger.warning("TikTok handler: processing failed to complete successfully.")
