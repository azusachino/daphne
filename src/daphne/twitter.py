import re
import httpx
import logging
import io
from telegram import Update, InputMediaPhoto
from telegram.ext import ContextTypes

from daphne.messages import (
    HtmlMessage,
    PARSE_MODE_HTML,
    sender_attribution,
)

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

TWITTER_REGEX = re.compile(
    r"https?://(?:www\.)?(twitter\.com|x\.com|fxtwitter\.com|vxtwitter\.com|fixupx\.com)/([a-zA-Z0-9_]+)/status/(\d+)",
    re.IGNORECASE,
)


def contains_twitter_link(text: str) -> bool:
    """
    Returns True if the text contains a Twitter/X link.
    """
    return bool(TWITTER_REGEX.search(text))


def extract_twitter_link(text: str):
    """
    Extracts the domain, username, and tweet ID of the first Twitter/X link in the text.
    """
    match = TWITTER_REGEX.search(text)
    if match:
        domain = match.group(1)
        username = match.group(2)
        tweet_id = match.group(3)
        return domain, username, tweet_id
    return None


async def download_bytes(url: str) -> bytes:
    """
    Downloads media bytes from URL using a standard browser user agent.
    """
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers, timeout=15.0)
        resp.raise_for_status()
        return resp.content


async def send_photo_helper(
    bot, chat_id: int, url: str, caption: str, parse_mode: str
) -> None:
    try:
        await bot.send_photo(
            chat_id=chat_id, photo=url, caption=caption, parse_mode=parse_mode
        )
    except Exception as e:
        logger.warning(
            f"Failed to send photo by URL {url}: {e}. Retrying by downloading bytes..."
        )
        img_bytes = await download_bytes(url)
        bio = io.BytesIO(img_bytes)
        bio.name = "photo.jpg"
        await bot.send_photo(
            chat_id=chat_id, photo=bio, caption=caption, parse_mode=parse_mode
        )


async def send_video_helper(
    bot, chat_id: int, url: str, caption: str, parse_mode: str
) -> None:
    try:
        await bot.send_video(
            chat_id=chat_id, video=url, caption=caption, parse_mode=parse_mode
        )
    except Exception as e:
        logger.warning(
            f"Failed to send video by URL {url}: {e}. Retrying by downloading bytes..."
        )
        video_bytes = await download_bytes(url)
        bio = io.BytesIO(video_bytes)
        bio.name = "video.mp4"
        await bot.send_video(
            chat_id=chat_id, video=bio, caption=caption, parse_mode=parse_mode
        )


async def send_animation_helper(
    bot, chat_id: int, url: str, caption: str, parse_mode: str
) -> None:
    try:
        await bot.send_animation(
            chat_id=chat_id, animation=url, caption=caption, parse_mode=parse_mode
        )
    except Exception as e:
        logger.warning(
            f"Failed to send animation by URL {url}: {e}. Retrying by downloading bytes..."
        )
        gif_bytes = await download_bytes(url)
        bio = io.BytesIO(gif_bytes)
        bio.name = "animation.gif"
        await bot.send_animation(
            chat_id=chat_id, animation=bio, caption=caption, parse_mode=parse_mode
        )


async def send_media_group_helper(
    bot, chat_id: int, urls: list[str], caption: str, parse_mode: str
) -> None:
    try:
        media = []
        for i, url in enumerate(urls):
            if i == 0:
                media.append(
                    InputMediaPhoto(media=url, caption=caption, parse_mode=parse_mode)
                )
            else:
                media.append(InputMediaPhoto(media=url))
        await bot.send_media_group(chat_id=chat_id, media=media)
    except Exception as e:
        logger.warning(
            f"Failed to send media group by URL: {e}. Retrying by downloading bytes..."
        )
        media = []
        for i, url in enumerate(urls):
            img_bytes = await download_bytes(url)
            bio = io.BytesIO(img_bytes)
            bio.name = f"photo_{i}.jpg"
            if i == 0:
                media.append(
                    InputMediaPhoto(media=bio, caption=caption, parse_mode=parse_mode)
                )
            else:
                media.append(InputMediaPhoto(media=bio))
        await bot.send_media_group(chat_id=chat_id, media=media)


async def send_photos(
    bot, chat_id: int, urls: list[str], caption: str, parse_mode: str
) -> bool:
    if not urls:
        return False
    if len(urls) == 1:
        await send_photo_helper(bot, chat_id, urls[0], caption, parse_mode)
    else:
        await send_media_group_helper(bot, chat_id, urls, caption, parse_mode)
    return True


async def send_fallback(bot, chat_id: int, username: str, tweet_id: str) -> bool:
    fallback_url = f"https://fxtwitter.com/{username}/status/{tweet_id}"
    try:
        await bot.send_message(chat_id=chat_id, text=fallback_url)
        return True
    except Exception as e:
        logger.error(f"Failed to send fallback URL {fallback_url}: {e}")
        return False


def extract_hashtags(text: str) -> list[str]:
    tags = []
    seen = set()
    for raw_tag in re.findall(r"#(\w+)", text):
        tag = raw_tag.lower()
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


def build_caption(tweet_text: str, tweet_url: str, sender: str | None) -> str:
    tags = ["twitter"]
    for tag in extract_hashtags(tweet_text):
        if tag != "twitter":
            tags.append(tag)

    if len(tweet_text) > 900:
        tweet_text = tweet_text[:900] + "..."

    return (
        HtmlMessage(sender=sender)
        .text(tweet_text or None)
        .link(tweet_url)
        .tags(*tags)
        .render()
    )


async def try_delete_message(update: Update) -> None:
    message = update.message
    if not message:
        return
    is_reply = message.reply_to_message is not None
    is_topic = bool(message.is_topic_message)
    if not (is_reply or is_topic):
        try:
            await message.delete()
        except Exception as e:
            logger.warning(f"Failed to delete original message: {e}")


async def handle_twitter_links(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.message
    if not message or not message.text:
        return

    match_info = extract_twitter_link(message.text)
    if not match_info:
        return

    domain, username, tweet_id = match_info
    chat_id = message.chat_id

    try:
        await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")
    except Exception:
        pass

    # Try fetching from API
    api_url = f"https://api.fxtwitter.com/{username}/status/{tweet_id}"
    headers = {"User-Agent": USER_AGENT}

    success = False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(api_url, headers=headers, timeout=10.0)

        if resp.status_code == 200:
            data = resp.json()
            if data.get("code") == 200 and data.get("tweet"):
                tweet = data["tweet"]
                tweet_text = tweet.get("text", "")

                # Check for media
                media_info = tweet.get("media", {})
                media_all = media_info.get("all", [])
                if media_all:
                    photos = [
                        m
                        for m in media_all
                        if str(m.get("type", "")).lower() in {"photo", "image"}
                    ]
                    videos = [
                        m
                        for m in media_all
                        if str(m.get("type", "")).lower()
                        in {"video", "gif", "animated_gif"}
                    ]
                else:
                    photos = media_info.get("photos", [])
                    videos = media_info.get("videos", [])

                photo_urls = [p["url"] for p in photos if "url" in p]
                video_urls = [
                    v["url"]
                    for v in videos
                    if "url" in v
                    and str(v.get("type", "")).lower() not in {"gif", "animated_gif"}
                ]
                gif_urls = [
                    v["url"]
                    for v in videos
                    if "url" in v
                    and str(v.get("type", "")).lower() in {"gif", "animated_gif"}
                ]

                has_media = bool(photo_urls or video_urls or gif_urls)

                if has_media:
                    caption = build_caption(
                        tweet_text,
                        tweet.get("url")
                        or f"https://{domain}/{username}/status/{tweet_id}",
                        sender_attribution(update.effective_user),
                    )

                    caption_available = True
                    if photo_urls:
                        await send_photos(
                            context.bot,
                            chat_id,
                            photo_urls,
                            caption if caption_available else "",
                            parse_mode=PARSE_MODE_HTML,
                        )
                        caption_available = False
                        success = True
                    for video_url in video_urls:
                        try:
                            await context.bot.send_chat_action(
                                chat_id=chat_id, action="upload_video"
                            )
                        except Exception:
                            pass
                        await send_video_helper(
                            context.bot,
                            chat_id,
                            video_url,
                            caption if caption_available else "",
                            parse_mode=PARSE_MODE_HTML,
                        )
                        caption_available = False
                        success = True
                    for gif_url in gif_urls:
                        try:
                            await context.bot.send_chat_action(
                                chat_id=chat_id, action="upload_video"
                            )
                        except Exception:
                            pass
                        await send_animation_helper(
                            context.bot,
                            chat_id,
                            gif_url,
                            caption if caption_available else "",
                            parse_mode=PARSE_MODE_HTML,
                        )
                        caption_available = False
                        success = True
                else:
                    # Successfully fetched, but no media -> send fallback URL directly
                    success = await send_fallback(
                        context.bot, chat_id, username, tweet_id
                    )
            else:
                logger.warning(
                    f"FxTwitter API returned code {data.get('code')}: {data.get('message')}"
                )
        else:
            logger.warning(f"FxTwitter API returned HTTP status {resp.status_code}")
    except Exception as e:
        logger.exception(f"Error processing Twitter link {api_url}: {e}")

    if not success:
        # Fallback case on error
        success = await send_fallback(context.bot, chat_id, username, tweet_id)

    if success:
        await try_delete_message(update)
