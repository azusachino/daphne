import os
import io
import datetime
import logging
import asyncio
import random
from typing import Tuple, Optional, Dict, Any
import httpx
from daphne.messages import append_footer, escape_html

logger = logging.getLogger("daphne.image_fetch")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def get_yesterday_date(
    today_date: Optional[datetime.date] = None,
) -> Tuple[int, int, int]:
    """
    Get the (day, month, year) for yesterday.
    """
    if today_date is None:
        today_date = datetime.date.today()
    yesterday = today_date - datetime.timedelta(days=1)
    return yesterday.day, yesterday.month, yesterday.year


def parse_popular_api(posts: Any, source: str) -> Optional[Dict[str, Any]]:
    """
    Parse the popular posts API response and extract the top post with a valid image URL.
    """
    if not posts or not isinstance(posts, list):
        return None

    for post in posts:
        if not isinstance(post, dict):
            continue
        post_id = post.get("id")
        if not post_id:
            continue

        if source == "yandere":
            file_url = post.get("file_url")
            tags = post.get("tags", "")
        elif source == "danbooru":
            file_url = post.get("file_url") or post.get("large_file_url")
            tags = post.get("tag_string", "")
        else:
            continue

        if file_url:
            return {
                "id": post_id,
                "file_url": file_url,
                "tags": tags,
            }
    return None


async def download_image(url: str, referer: str) -> bytes:
    """
    Download image from the given URL with custom User-Agent and Referer headers.
    """
    headers = {"User-Agent": USER_AGENT, "Referer": referer}
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers, timeout=60.0)
        response.raise_for_status()
        return response.content


async def send_photo_with_retry(
    bot,
    chat_id: str,
    photo_bytes: bytes,
    caption: str,
    filename: str,
    retries: int = 5,
    initial_delay: float = 2.0,
) -> None:
    """
    Send photo with exponential backoff on Telegram failures.
    """
    delay = initial_delay
    for attempt in range(1, retries + 1):
        try:
            bio = io.BytesIO(photo_bytes)
            bio.name = filename
            await bot.send_photo(
                chat_id=chat_id, photo=bio, caption=caption, parse_mode="HTML"
            )
            logger.info("Successfully sent photo to Telegram")
            return
        except Exception as e:
            logger.warning(f"Attempt {attempt}/{retries} to send photo failed: {e}")
            if attempt == retries:
                raise e
            # Exponential backoff with jitter
            jitter = random.uniform(0, 0.5) * delay
            sleep_time = delay + jitter
            logger.info(f"Sleeping for {sleep_time:.2f} seconds before retrying...")
            await asyncio.sleep(sleep_time)
            delay *= 2.0


def format_caption(site: str, post_id: int, date_str: str, tags: str) -> str:
    """
    Format caption for the image post.
    """
    if site == "yandere":
        post_url = f"https://yande.re/post/show/{post_id}"
        site_label = "yande.re"
    else:
        post_url = f"https://danbooru.donmai.us/posts/{post_id}"
        site_label = "danbooru"

    tags_formatted = escape_html(tags)
    if len(tags_formatted) > 500:
        tags_formatted = tags_formatted[:497] + "..."

    body = (
        f"🌟 <b>{site_label} Daily Popular</b> ({date_str})\n"
        f'🔗 <a href="{post_url}">Post #{post_id}</a>\n\n'
        f"🏷️ <code>{tags_formatted}</code>"
    )
    return append_footer(body)


async def fetch_popular_image(
    bot, site: str, channel: str, today_date: Optional[datetime.date] = None
) -> None:
    """
    Fetch the popular image for the site and send it to the specified channel.
    """
    day, month, year = get_yesterday_date(today_date)
    date_str = f"{year:04d}-{month:02d}-{day:02d}"

    if site == "yandere":
        api_url = f"https://yande.re/post/popular_by_day.json?day={day}&month={month}&year={year}"
        referer = "https://yande.re/"
    elif site == "danbooru":
        api_url = f"https://danbooru.donmai.us/explore/posts/popular.json?day={day}&month={month}&year={year}"
        referer = "https://danbooru.donmai.us/"
    else:
        raise ValueError(f"Unknown site: {site}")

    logger.info(f"Fetching popular posts for {site} on {date_str} from {api_url}")

    headers = {"User-Agent": USER_AGENT, "Referer": referer}

    async with httpx.AsyncClient() as client:
        response = await client.get(api_url, headers=headers, timeout=30.0)
        response.raise_for_status()
        posts = response.json()

    post_data = parse_popular_api(posts, site)
    if not post_data:
        logger.warning(f"No popular image found for {site} on {date_str}")
        return

    logger.info(f"Top post found: ID {post_data['id']}, URL {post_data['file_url']}")

    # Download image
    photo_bytes = await download_image(post_data["file_url"], referer)

    # Determine extension
    url_path = post_data["file_url"].split("?")[0]
    _, ext = os.path.splitext(url_path)
    if not ext or len(ext) > 5:
        ext = ".jpg"
    filename = f"photo{ext}"

    # Send to Telegram
    caption = format_caption(site, post_data["id"], date_str, post_data["tags"])
    await send_photo_with_retry(bot, channel, photo_bytes, caption, filename)
