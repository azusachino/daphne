import asyncio
import functools
import logging
import os
import re
import tempfile
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Optional

from aiogram import BaseMiddleware, Bot, F
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReactionTypeEmoji,
    TelegramObject,
)
import uuid

from matcha import Capabilities, Delivery, Reason, Upload

from daphne import bilibili
from daphne.config import (
    max_concurrent_downloads,
    max_user_concurrent_downloads,
    video_upload_limit_mb,
)
from daphne.downloader import (
    download_video,
    format_duration,
    format_video_caption,
    is_bilibili_url,
    is_youtube_url,
    probe_video_dimensions,
)
from daphne.messages import (
    HtmlMessage,
    PARSE_MODE_HTML,
    reason_text,
    sender_attribution,
)
from daphne.rbac import RbacService
from daphne.redact import redact
from daphne.video import (
    Unreadable,
    Video,
    canonical_link,
    resolve as resolve_video,
    resolve_or_blank,
)
from daphne.tg import (
    TelegramBot,
    TelegramContext,
    TelegramUpdate,
    adapt_callback_query,
    adapt_message,
    adapt_update,
    failure_reason,
    context_for,
    new_dispatcher,
    new_router,
)

TG_HARD_LIMIT_BYTES = 2 * 1024 * 1024 * 1024
# A download button keeps its card's resolved video. The cache is bounded, so a busy bot
# cannot grow it without limit; an evicted button reports that the link expired.
CALLBACK_CACHE_LIMIT = 500
CALLBACK_URL_CACHE: dict[str, Video] = {}


def _remember_callback(short_id: str, video: Video) -> None:
    CALLBACK_URL_CACHE[short_id] = video
    while len(CALLBACK_URL_CACHE) > CALLBACK_CACHE_LIMIT:
        CALLBACK_URL_CACHE.pop(next(iter(CALLBACK_URL_CACHE)))


logger = logging.getLogger(__name__)

# In-process concurrency guard for heavy downloads (yt-dlp, you-get, lux, yutto).
# Semaphores are created lazily inside the running loop. A user must clear both
# their own per-user slot and a shared global slot before a download proceeds,
# so one big transfer cannot starve the executor for everyone else.
_global_download_semaphore: Optional[asyncio.Semaphore] = None
_user_download_semaphores: dict[int, asyncio.Semaphore] = {}
_active_card_downloads: set[str] = set()
MAX_USER_SEMAPHORES = 1024


def _get_global_download_semaphore() -> asyncio.Semaphore:
    global _global_download_semaphore
    if _global_download_semaphore is None:
        _global_download_semaphore = asyncio.Semaphore(max_concurrent_downloads())
    return _global_download_semaphore


def _get_user_download_semaphore(user_id: int) -> asyncio.Semaphore:
    sem = _user_download_semaphores.get(user_id)
    if sem is None:
        if len(_user_download_semaphores) >= MAX_USER_SEMAPHORES:
            for uid in [
                k for k, v in _user_download_semaphores.items() if not v.locked()
            ]:
                _user_download_semaphores.pop(uid, None)
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
        except Exception as exc:
            logger.warning("Best-effort Telegram call failed: %s", failure_reason(exc))
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
# 😢 rather than 👎 — a failed conversion is a system apology, not a downvote
# of the user's content.
REACTION_FAILED = "😢"


async def set_reaction(message, emoji: Optional[str]) -> None:
    """
    Best-effort reaction on the user's message. Passing ``None`` clears it.
    Swallows all errors (private chats, unsupported emoji, missing rights) so
    progress cues never break the actual conversion flow.
    """
    if message is None:
        return
    try:
        reaction = [ReactionTypeEmoji(emoji=emoji)] if emoji else []
        await message.set_reaction(reaction=reaction)
    except Exception as exc:
        logger.debug("Failed to set reaction %s: %s", emoji, exc)


LINK_RE = re.compile(
    r"\b(?<!@)(?:https?://)?(?:www\.|vm\.|vt\.)?(?:"
    r"bilibili\.com|b23\.tv|"
    r"youtube\.com|youtu\.be|"
    r"yande\.re|konachan\.(?:com|net)|lolibooru\.moe|danbooru\.donmai\.us|"
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


async def check_access_and_reply(update: TelegramUpdate, command: str) -> bool:
    user_id = update.effective_user.id if update.effective_user else 0
    chat_id = update.effective_chat.id if update.effective_chat else 0

    access = rbac_service.check_access(user_id, chat_id, command)
    if access.is_allowed():
        return True

    if chat_id not in rbac_service.chats and not rbac_service.is_admin(user_id):
        return False

    if access.is_rate_limited():
        text = HtmlMessage().text("Rate limit exceeded. Please wait.").render()
    else:
        text = HtmlMessage().text(f"Permission denied: {access.reason}").render()
    await update.message.reply_text(text, parse_mode=PARSE_MODE_HTML)
    return False


def _daphne_overview(sender: Optional[str], greeting: bool = False) -> str:
    body = (
        "Send a Twitter/X, Pixiv, Bluesky, Instagram, TikTok, Bilibili, or "
        "YouTube link and I will convert it into Telegram-friendly media."
    )
    return (
        HtmlMessage(sender=sender)
        .title("👋 Welcome to daphne" if greeting else "daphne")
        .text(body)
        .tags("daphne", "media")
        .render()
    )


async def start_command(update: TelegramUpdate, context: TelegramContext) -> None:
    if not await check_access_and_reply(update, "help"):
        return
    text = _daphne_overview(sender_attribution(update.effective_user), greeting=True)
    await update.message.reply_text(text, parse_mode=PARSE_MODE_HTML)


async def help_command(update: TelegramUpdate, context: TelegramContext) -> None:
    if not await check_access_and_reply(update, "help"):
        return
    text = _daphne_overview(sender_attribution(update.effective_user))
    await update.message.reply_text(text, parse_mode=PARSE_MODE_HTML)


def extract_video_url(text: str) -> str | None:
    """The first video link in a message, as the one URL daphne may print or download.

    Identity comes from matcha, which owns the canonical form. A b23.tv share link is
    opaque, so it keeps its tracking-free form here and is expanded by `resolve` in a
    worker; a link matcha has no grammar for keeps its tracking-free form too, because
    dropping it would silently lose a video the bot already handled.
    """
    text = preprocess_text_links(text)
    for match in URL_REGEX.finditer(text):
        raw = match.group(0)
        if is_bilibili_url(raw) or is_youtube_url(raw):
            return canonical_link(raw)
    return None


def _size_reason(size: int) -> str:
    """Why a known-size video is not uploaded: the site limit, or Telegram's own."""
    if size > TG_HARD_LIMIT_BYTES:
        return "Video is over Telegram's 2GiB limit"
    return f"Video is over {video_upload_limit_mb()} MB"


def _card_reason(reason: Reason, video: Video, preview_access) -> str:
    """The card's heading, from the shared reason vocabulary rather than from prose.

    A size and a quota are daphne's own limits, so they are named as such; everything
    else is the platform's answer, phrased once for every platform.
    """
    size = video.estimated_bytes
    if reason is Reason.TOO_LARGE:
        return _size_reason(size or 0)
    if reason is Reason.NO_ACCESS:
        if preview_access.is_rate_limited():
            return "Video Preview Quota Exceeded (direct download only)"
        return "Video Details"
    if reason is Reason.NO_PLAN:
        return "Video Details"
    return reason_text(reason)


def _duration_text(seconds: float | int | None) -> str:
    if seconds is None:
        return "Unknown"
    try:
        return format_duration(int(float(seconds)))
    except TypeError, ValueError:
        return "Unknown"


def _video_caption(
    video: Video,
    fallback_path: str | None,
    duration: int | None,
    sender: str | None,
) -> str:
    """The uploaded video's caption. Absent fields are left out, never faked."""
    title = video.info.title or (
        os.path.splitext(os.path.basename(fallback_path))[0]
        if fallback_path
        else "Video"
    )
    dur_secs = video.info.duration_s if video.info.duration_s is not None else duration
    return format_video_caption(
        title=title,
        uploader=video.info.creator or "Unknown",
        duration=_duration_text(dur_secs),
        url=video.canonical_url,
        platform=video.platform,
        sender=sender,
    )


async def send_video_card(
    update: TelegramUpdate,
    video: Video,
    sender: str | None,
    reason: str,
) -> None:
    webpage_url = video.canonical_url
    platform_name = video.platform
    text = (
        HtmlMessage(sender=sender)
        .title(reason)
        .fields(
            ("Title:", video.info.title or "Video"),
            ("Uploader:", video.info.creator or "Unknown"),
            ("Duration:", _duration_text(video.info.duration_s)),
        )
        .link(webpage_url, f"🔗 Source ({platform_name.capitalize()})")
        .tags(platform_name)
        .render()
    )
    buttons = []
    short_id = str(uuid.uuid4())[:8]
    _remember_callback(short_id, video)

    size = video.estimated_bytes
    if size is None or size <= TG_HARD_LIMIT_BYTES:
        buttons.append(
            InlineKeyboardButton(text="Download video", callback_data=f"dl:{short_id}")
        )
    buttons.append(InlineKeyboardButton(text="Open source", url=webpage_url))

    await update.message.reply_text(
        text,
        parse_mode=PARSE_MODE_HTML,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons]),
    )


async def delete_original_message(update: TelegramUpdate) -> None:
    try:
        await update.message.delete()
    except Exception as exc:
        logger.warning("Best-effort Telegram call failed: %s", failure_reason(exc))


async def _upload_resolved_video(
    update: TelegramUpdate,
    context: TelegramContext,
    video: Video,
) -> None:
    """Download an already-decided video and upload it to the chat.

    Module-private on purpose: ``decide_delivery`` is the only road to an upload,
    so a new handler cannot wire itself around the gate by importing this.
    """
    sender = sender_attribution(update.effective_user)
    status_msg = await update.message.reply_text(
        HtmlMessage(sender=sender).text("Preparing download...").render(),
        parse_mode=PARSE_MODE_HTML,
    )

    loop = asyncio.get_running_loop()
    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action="upload_video"
        )
    except Exception as exc:
        logger.warning("Best-effort Telegram call failed: %s", failure_reason(exc))

    # The gate already decided upload; the real bytes are still checked after the
    # download, where they are actually known.
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
                    HtmlMessage(sender=sender).text("Downloading video...").render(),
                    parse_mode=PARSE_MODE_HTML,
                )
                video_path = await loop.run_in_executor(
                    None,
                    download_video,
                    video.fetch_url,
                    out_dir,
                    video.expected_duration,
                )
        except Exception as exc:
            logger.exception("Failed to download video")
            await set_reaction(update.message, REACTION_FAILED)
            await status_msg.edit_text(
                HtmlMessage(sender=sender)
                .text(f"Video download failed: {redact(str(exc))}")
                .render(),
                parse_mode=PARSE_MODE_HTML,
            )
            return

        file_size = os.path.getsize(video_path)
        if file_size > max_upload_bytes:
            await status_msg.delete()
            await send_video_card(update, video, sender, _size_reason(file_size))
            return

        await status_msg.edit_text(
            HtmlMessage(sender=sender).text("Uploading video...").render(),
            parse_mode=PARSE_MODE_HTML,
        )
        try:
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, action="upload_video"
            )
        except Exception as exc:
            logger.warning("Best-effort Telegram call failed: %s", failure_reason(exc))
        width, height, duration = await loop.run_in_executor(
            None, probe_video_dimensions, video_path
        )
        caption = _video_caption(video, video_path, duration, sender)

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

        try:
            with open(video_path, "rb") as video_file:
                await context.bot.send_video(video=video_file, **kwargs)
        except Exception as exc:
            logger.exception("Failed to send video")
            await set_reaction(update.message, REACTION_FAILED)
            await status_msg.edit_text(
                HtmlMessage(sender=sender)
                .text(f"Failed to upload video: {redact(str(exc))}")
                .render(),
                parse_mode=PARSE_MODE_HTML,
            )
            return

    await status_msg.delete()
    await delete_original_message(update)


async def decide_delivery(
    update: TelegramUpdate, video: Video, sender: Optional[str]
) -> Delivery:
    """The one delivery gate: matcha decides upload-or-card, RBAC charges on upload.

    Every handler that resolved media passes through here before any byte is fetched
    or sent. An ``Upload`` answer charges the ``preview_video`` quota; a ``Card`` is
    rendered here and charges nothing -- its Download button charges
    ``download_video`` when pressed. Handlers that fetched their own bytes upload
    them themselves once this gate answers ``Upload``.
    """
    user_id = update.effective_user.id if update.effective_user else 0
    chat_id = update.effective_chat.id if update.effective_chat else 0
    preview_access = rbac_service.check_access(
        user_id, chat_id, "preview_video", dry_run=True
    )
    # Policy says daphne previews; capability says how large a file may be and whether
    # this chat may preview at all. matcha owns the resulting upload-or-card rule.
    caps = Capabilities(
        max_upload_bytes=video_upload_limit_mb() * 1024 * 1024,
        preview_allowed=preview_access.is_allowed(),
    )
    decision = video.delivery(caps)
    if isinstance(decision, Upload):
        # Charge preview_video quota
        rbac_service.check_access(user_id, chat_id, "preview_video")
        return decision
    await send_video_card(
        update, video, sender, _card_reason(decision.reason, video, preview_access)
    )
    return decision


async def deliver_video(
    update: TelegramUpdate,
    context: TelegramContext,
    video: Video,
    sender: Optional[str],
) -> None:
    """Gate, then deliver: the generic path for a link the bot resolved itself."""
    decision = await decide_delivery(update, video, sender)
    if isinstance(decision, Upload):
        await _upload_resolved_video(update, context, video)


async def handle_video_url(
    update: TelegramUpdate,
    context: TelegramContext,
    url: str,
    platform: str,
) -> None:
    """Resolve a page URL in a worker, then take the normal upload path.

    A platform handler reaches this after its own extractor gave up. The metadata chain
    still gets its chance to describe the link, and a link no engine can describe still
    downloads with a blank description rather than failing outright.
    """
    loop = asyncio.get_running_loop()
    video = await loop.run_in_executor(
        None, functools.partial(resolve_or_blank, url, platform=platform)
    )
    await deliver_video(
        update, context, video, sender_attribution(update.effective_user)
    )


async def media_message_handler(
    update: TelegramUpdate, context: TelegramContext
) -> None:
    message = update.message
    if not message or not message.text:
        return

    user_id = update.effective_user.id if update.effective_user else 0
    chat_id = update.effective_chat.id if update.effective_chat else 0
    logger.info(
        "Received message from user_id=%s in chat_id=%s (%d chars)",
        user_id,
        chat_id,
        len(message.text),
    )

    text = preprocess_text_links(message.text)
    if text != message.text:
        object.__setattr__(message, "text", text)

    from daphne.pixiv import contains_pixiv_link, handle_pixiv_links
    from daphne.twitter import contains_twitter_link, handle_twitter_links
    from daphne.bluesky import contains_bluesky_link, handle_bluesky_links
    from daphne.instagram import contains_instagram_link, handle_instagram_links
    from daphne.tiktok import contains_tiktok_link, handle_tiktok_links
    from daphne.booru import contains_booru_link, handle_booru_links

    is_twitter = contains_twitter_link(message.text)
    is_pixiv = contains_pixiv_link(message.text)
    is_bluesky = contains_bluesky_link(message.text)
    is_instagram = contains_instagram_link(message.text)
    is_tiktok = contains_tiktok_link(message.text)
    is_booru = contains_booru_link(message.text)
    video_url = extract_video_url(message.text)

    if is_twitter:
        logger.info("Routing to Twitter handler")
        if await check_access_and_reply(update, "convert_link"):
            await set_reaction(message, REACTION_WORKING)
            await handle_twitter_links(update, context)
    elif is_pixiv:
        logger.info("Routing to Pixiv handler")
        if await check_access_and_reply(update, "convert_link"):
            await set_reaction(message, REACTION_WORKING)
            await handle_pixiv_links(update, context)
    elif is_bluesky:
        logger.info("Routing to Bluesky handler")
        if await check_access_and_reply(update, "convert_link"):
            await set_reaction(message, REACTION_WORKING)
            await handle_bluesky_links(update, context)
    elif is_instagram:
        logger.info("Routing to Instagram handler")
        if await check_access_and_reply(update, "convert_link"):
            await set_reaction(message, REACTION_WORKING)
            await handle_instagram_links(update, context)
    elif is_booru:
        logger.info("Routing to booru handler")
        if await check_access_and_reply(update, "convert_link"):
            await set_reaction(message, REACTION_WORKING)
            await handle_booru_links(update, context)
    elif is_tiktok:
        logger.info("Routing to TikTok handler")
        if await check_access_and_reply(update, "convert_link"):
            await set_reaction(message, REACTION_WORKING)
            await handle_tiktok_links(update, context)
    elif video_url:
        logger.info("Routing to generic video handler for URL: %s", video_url)
        # Check fetch_metadata permission & quota
        access = rbac_service.check_access(user_id, chat_id, "fetch_metadata")
        if not access.is_allowed():
            if chat_id not in rbac_service.chats and not rbac_service.is_admin(user_id):
                return
            if access.is_rate_limited():
                text = HtmlMessage().text(f"Quota exceeded: {access.reason}").render()
            else:
                text = (
                    HtmlMessage().text(f"Permission denied: {access.reason}").render()
                )
            await update.message.reply_text(text, parse_mode=PARSE_MODE_HTML)
            return

        await set_reaction(message, REACTION_WORKING)

        sender = sender_attribution(update.effective_user)
        status_msg = await update.message.reply_text(
            HtmlMessage(sender=sender).text("Fetching video metadata...").render(),
            parse_mode=PARSE_MODE_HTML,
        )

        loop = asyncio.get_running_loop()
        try:
            video = await loop.run_in_executor(None, resolve_video, video_url)
        except bilibili.RateLimitedError:
            logger.warning("Bilibili risk control refused every metadata attempt")
            await set_reaction(update.message, REACTION_FAILED)
            await status_msg.edit_text(
                HtmlMessage(sender=sender)
                .text(
                    f"{reason_text(Reason.RATE_LIMITED)} Your original link was kept."
                )
                .render(),
                parse_mode=PARSE_MODE_HTML,
            )
            return
        except Unreadable as exc:
            # No engine could read the link; the reason decides what the user is told.
            logger.info("Video metadata unreadable: %s", exc.reason.value)
            await set_reaction(update.message, REACTION_FAILED)
            await status_msg.edit_text(
                HtmlMessage(sender=sender)
                .text(f"{reason_text(exc.reason)} Your original link was kept.")
                .render(),
                parse_mode=PARSE_MODE_HTML,
            )
            return
        except Exception:
            logger.exception("Failed to fetch video metadata")
            await set_reaction(update.message, REACTION_FAILED)
            await status_msg.edit_text(
                HtmlMessage(sender=sender)
                .text("Video metadata fetch failed.")
                .render(),
                parse_mode=PARSE_MODE_HTML,
            )
            return

        await status_msg.delete()
        await deliver_video(update, context, video, sender)
        # A card is not an upload; keep the source message.


async def download_button_callback(
    update: TelegramUpdate, context: TelegramContext
) -> None:
    query = update.callback_query
    if not query:
        return

    data = query.data
    if not data or not data.startswith("dl:"):
        await query.answer()
        return

    short_id = data[3:]
    if short_id in _active_card_downloads:
        await query.answer("Download is already in progress.", show_alert=True)
        return
    entry = CALLBACK_URL_CACHE.get(short_id)
    if not entry:
        await query.answer("Error: Video link has expired.", show_alert=True)
        return
    video = entry

    user_id = query.from_user.id if query.from_user else 0
    chat_id = query.message.chat.id if query.message else 0
    sender = sender_attribution(query.from_user)

    # Check download_video permission & quota (actual non-dry-run check)
    access = rbac_service.check_access(user_id, chat_id, "download_video")
    if not access.is_allowed():
        # Do not download, alert the user
        await query.answer(access.reason or "Permission denied", show_alert=True)
        return

    _active_card_downloads.add(short_id)
    try:
        await _run_card_download(
            query, context, short_id, video, user_id, chat_id, sender
        )
    finally:
        _active_card_downloads.discard(short_id)


async def _run_card_download(
    query,
    context: TelegramContext,
    short_id: str,
    video: Video,
    user_id: int,
    chat_id: int,
    sender: Optional[str],
) -> None:
    # Acknowledge the callback query immediately to avoid loading animation on button
    await query.answer()

    # Edit the card message to indicate progress and remove the buttons. The resolved
    # video came with the card, so the link is not looked up again.
    try:
        await query.edit_message_text(
            HtmlMessage(sender=sender).text("Preparing download...").render(),
            parse_mode=PARSE_MODE_HTML,
            reply_markup=None,
        )
    except Exception as exc:
        logger.warning("Best-effort Telegram call failed: %s", failure_reason(exc))

    loop = asyncio.get_running_loop()
    size = video.estimated_bytes
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
        except Exception as exc:
            logger.warning("Best-effort Telegram call failed: %s", failure_reason(exc))
        return

    try:
        await query.edit_message_text(
            HtmlMessage(sender=sender).text("Downloading video...").render(),
            parse_mode=PARSE_MODE_HTML,
        )
    except Exception as exc:
        logger.warning("Best-effort Telegram call failed: %s", failure_reason(exc))

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
                except Exception as exc:
                    logger.warning(
                        "Best-effort Telegram call failed: %s", failure_reason(exc)
                    )

            async with download_slot(user_id, on_wait=_notify_queued):
                video_path = await loop.run_in_executor(
                    None,
                    download_video,
                    video.fetch_url,
                    out_dir,
                    video.expected_duration,
                )
        except Exception as exc:
            logger.exception("Failed to download video")
            failure = (
                HtmlMessage(sender=sender)
                .text(f"Video download failed: {redact(str(exc))}")
                .render()
            )
            try:
                await query.edit_message_text(failure, parse_mode=PARSE_MODE_HTML)
            except Exception:
                logger.exception("Could not show the download failure on the card")
                if query.message:
                    await query.message.reply_text(failure, parse_mode=PARSE_MODE_HTML)
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
            except Exception as exc:
                logger.warning(
                    "Best-effort Telegram call failed: %s", failure_reason(exc)
                )
            return

        try:
            await query.edit_message_text(
                HtmlMessage(sender=sender).text("Uploading video...").render(),
                parse_mode=PARSE_MODE_HTML,
            )
        except Exception as exc:
            logger.warning("Best-effort Telegram call failed: %s", failure_reason(exc))

        try:
            await context.bot.send_chat_action(chat_id=chat_id, action="upload_video")
        except Exception as exc:
            logger.warning("Best-effort Telegram call failed: %s", failure_reason(exc))

        width, height, duration = await loop.run_in_executor(
            None, probe_video_dimensions, video_path
        )
        caption = _video_caption(video, video_path, duration, sender)

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
                    .text(f"Failed to upload video: {redact(str(exc))}")
                    .render(),
                    parse_mode=PARSE_MODE_HTML,
                )
            except Exception as exc:
                logger.warning(
                    "Best-effort Telegram call failed: %s", failure_reason(exc)
                )
            return

    # Delete the status card message on success
    try:
        await query.message.delete()
    except Exception as exc:
        logger.warning("Best-effort Telegram call failed: %s", failure_reason(exc))

    # Attempt to delete the original message containing the link if it was replied to
    try:
        if query.message and query.message.reply_to_message:
            await query.message.reply_to_message.delete()
    except Exception as exc:
        logger.warning("Best-effort Telegram call failed: %s", failure_reason(exc))


async def log_update(update: TelegramUpdate, context: TelegramContext) -> None:
    """Logs every update Telegram delivers, before RBAC or routing runs. This
    is the single source of truth for "did the bot even receive this" — every
    other handler only logs once it has already decided to act."""
    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None
    if update.message is not None:
        text = update.message.text or ""
        kind = (
            f"message: {text!r}"
            if text.startswith("/")
            else f"message: <{len(text)} chars>"
        )
    elif update.callback_query is not None:
        kind = f"callback_query: {update.callback_query.data!r}"
    else:
        kind = type(update).__name__
    logger.info(
        "Update received: update_id=%s user_id=%s chat_id=%s %s",
        update.update_id,
        user_id,
        chat_id,
        kind,
    )


def log_handler(name: str):
    """Wraps a handler to log its start and end (result + elapsed time).

    log_update (group=-1) already proves an update was received; this proves
    which specific handler ran with it and how it concluded, so "received but
    did nothing" is never a silent, undiagnosable state again.
    """

    def decorator(func: Callable[..., Awaitable[None]]):
        @functools.wraps(func)
        async def wrapper(update: TelegramUpdate, context: TelegramContext):
            user_id = update.effective_user.id if update.effective_user else None
            chat_id = update.effective_chat.id if update.effective_chat else None
            start = time.monotonic()
            logger.info(
                "Handler start: name=%s user_id=%s chat_id=%s", name, user_id, chat_id
            )
            try:
                result = await func(update, context)
            except Exception:
                elapsed = time.monotonic() - start
                logger.exception(
                    "Handler failed: name=%s user_id=%s chat_id=%s elapsed=%.3fs",
                    name,
                    user_id,
                    chat_id,
                    elapsed,
                )
                raise
            else:
                elapsed = time.monotonic() - start
                logger.info(
                    "Handler end: name=%s user_id=%s chat_id=%s elapsed=%.3fs",
                    name,
                    user_id,
                    chat_id,
                    elapsed,
                )
                return result

        return wrapper

    return decorator


class UpdateLoggingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, TelegramUpdate):
            update = event
        else:
            update = adapt_update(event)
        await log_update(update, context_for(TelegramBot(data["bot"])))
        return await handler(event, data)


async def _dispatch_handler(
    name: str,
    handler: Callable[[TelegramUpdate, TelegramContext], Awaitable[None]],
    update: TelegramUpdate,
    bot: Bot,
    args: list[str] | None = None,
) -> None:
    context = context_for(TelegramBot(bot), args)
    await log_handler(name)(handler)(update, context)


def build_dispatcher():
    router = new_router()

    @router.error()
    async def unhandled_error(event: ErrorEvent) -> None:
        exception = event.exception
        logger.error(
            "Unhandled update error: update_id=%s error=%s",
            getattr(event.update, "update_id", None),
            exception,
            exc_info=(type(exception), exception, exception.__traceback__),
        )

    @router.message(Command("start"))
    async def _start(message, bot: Bot):
        await _dispatch_handler("start", start_command, adapt_message(message), bot)

    @router.message(Command("help"))
    async def _help(message, bot: Bot):
        await _dispatch_handler("help", help_command, adapt_message(message), bot)

    @router.callback_query(F.data.startswith("dl:"))
    async def _download_callback(callback_query, bot: Bot):
        await _dispatch_handler(
            "download_button_callback",
            download_button_callback,
            adapt_callback_query(callback_query),
            bot,
        )

    @router.message(F.text, ~F.text.startswith("/"))
    async def _media_message(message, bot: Bot):
        await _dispatch_handler(
            "media_message", media_message_handler, adapt_message(message), bot
        )

    dispatcher = new_dispatcher(router)
    dispatcher.update.outer_middleware(UpdateLoggingMiddleware())
    return dispatcher


BOT_COMMANDS = [
    BotCommand(command="start", description="Welcome & quick overview"),
    BotCommand(command="help", description="Show what daphne can do"),
]


async def register_bot_commands(bot: TelegramBot) -> None:
    await bot.set_my_commands(BOT_COMMANDS)
    logger.info("Registered Telegram bot commands; count=%d.", len(BOT_COMMANDS))
