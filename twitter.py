import re
import html
import httpx
import logging
import io
from telegram import Update, InputMediaPhoto
from telegram.ext import ContextTypes

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


async def send_fallback(bot, chat_id: int, username: str, tweet_id: str) -> bool:
    fallback_url = f"https://fxtwitter.com/{username}/status/{tweet_id}"
    try:
        await bot.send_message(chat_id=chat_id, text=fallback_url)
        return True
    except Exception as e:
        logger.error(f"Failed to send fallback URL {fallback_url}: {e}")
        return False


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
                photos = media_info.get("photos", [])
                videos = media_info.get("videos", [])

                photo_urls = [p["url"] for p in photos if "url" in p]
                video_urls = [
                    v["url"] for v in videos if "url" in v and v.get("type") != "gif"
                ]
                gif_urls = [
                    v["url"] for v in videos if "url" in v and v.get("type") == "gif"
                ]

                has_media = bool(photo_urls or video_urls or gif_urls)

                if has_media:
                    # Format caption
                    # Extract hashtags
                    tags = []
                    raw_tags = re.findall(r"#(\w+)", tweet_text)
                    seen = set()
                    for t in raw_tags:
                        t_lower = t.lower()
                        if t_lower not in seen:
                            seen.add(t_lower)
                            tags.append(t_lower)
                    if "twitter" not in tags:
                        tags.append("twitter")

                    hashtag_line = " ".join(f"#{t}" for t in tags)

                    # Original URL without query parameters
                    original_url = f"https://{domain}/{username}/status/{tweet_id}"

                    # Sender attribution
                    user = update.effective_user
                    sender_attr = ""
                    if user:
                        if user.username:
                            sender_attr = f"via @{user.username}"
                        else:
                            sender_attr = f"via {user.full_name}"

                    # Truncate tweet text to protect caption length limits in Telegram
                    if len(tweet_text) > 700:
                        tweet_text = tweet_text[:700] + "..."

                    # Escape HTML for text elements
                    escaped_text = html.escape(tweet_text)
                    escaped_sender = html.escape(sender_attr) if sender_attr else ""
                    escaped_url = html.escape(original_url)

                    parts = [escaped_text]
                    parts.append(f'🔗 <a href="{escaped_url}">{escaped_url}</a>')
                    parts.append(hashtag_line)
                    if escaped_sender:
                        parts.append(escaped_sender)

                    caption = "\n\n".join(parts)

                    # Send media
                    if video_urls:
                        await send_video_helper(
                            context.bot,
                            chat_id,
                            video_urls[0],
                            caption,
                            parse_mode="HTML",
                        )
                        success = True
                    elif gif_urls:
                        await send_animation_helper(
                            context.bot,
                            chat_id,
                            gif_urls[0],
                            caption,
                            parse_mode="HTML",
                        )
                        success = True
                    elif photo_urls:
                        if len(photo_urls) == 1:
                            await send_photo_helper(
                                context.bot,
                                chat_id,
                                photo_urls[0],
                                caption,
                                parse_mode="HTML",
                            )
                            success = True
                        else:
                            await send_media_group_helper(
                                context.bot,
                                chat_id,
                                photo_urls,
                                caption,
                                parse_mode="HTML",
                            )
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
