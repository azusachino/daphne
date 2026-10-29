"""Daphne's small Telegram boundary.

Handlers use these compatibility-shaped operations so the framework stays at the edge.
The implementation is aiogram; keeping this translation in one module makes a
future Telegram framework change a composition concern instead of a handler
rewrite.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import httpx
from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    LinkPreviewOptions,
    Message,
    ReactionTypeEmoji,
    Update,
)

from daphne.config import telegram_api_url

logger = logging.getLogger(__name__)

LOCAL_BOT_API_TIMEOUT_SECONDS = 7200


def failure_reason(exc: BaseException) -> str:
    """Name a failure for the logs without the request URL, which can carry a bot token."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, TelegramAPIError):
        return f"Telegram {exc.message}"
    if isinstance(exc, ValueError | KeyError | TypeError):
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__


def _input_file(value: Any) -> Any:
    if isinstance(value, (str, FSInputFile, BufferedInputFile)):
        return value
    if isinstance(value, io.BytesIO):
        return BufferedInputFile(
            value.getvalue(), filename=getattr(value, "name", "file")
        )
    if hasattr(value, "read"):
        filename = (
            value.name if isinstance(getattr(value, "name", None), str) else "file"
        )
        return FSInputFile(filename)
    return value


as_input_file = _input_file


def image_input(data: bytes, name: str) -> Any:
    """A fresh upload for one image. Each send needs its own, because a send consumes it."""
    bio = io.BytesIO(data)
    bio.name = name
    return as_input_file(bio)


async def send_photo_or_document(
    bot: Any,
    chat_id: int,
    data: bytes,
    name: str,
    caption: Optional[str] = None,
    parse_mode: Optional[str] = None,
) -> None:
    """Send one image as a photo; if Telegram refuses it as one, send the file as a document.

    Telegram rejects some photos by size, dimensions or aspect ratio, whatever the file size.
    A document carries the same image without that limit.
    """
    try:
        await bot.send_photo(
            chat_id=chat_id,
            photo=image_input(data, name),
            caption=caption,
            parse_mode=parse_mode,
        )
    except Exception as exc:
        logger.info("Photo refused, sending as a document: %s", failure_reason(exc))
        await bot.send_document(
            chat_id=chat_id,
            document=image_input(data, name),
            caption=caption,
            parse_mode=parse_mode,
        )


async def send_image_groups(
    bot: Any,
    chat_id: int,
    images: list[tuple[bytes, str]],
    caption: str,
    parse_mode: str,
) -> None:
    """Send eligible photos and oversized files in separate albums of at most ten."""
    caption_available = True
    photos = [(data, name) for data, name in images if len(data) <= 10_000_000]
    documents = [(data, name) for data, name in images if len(data) > 10_000_000]
    for kind, entries in (("photo", photos), ("document", documents)):
        for start in range(0, len(entries), 10):
            batch = entries[start : start + 10]
            if len(batch) >= 2:
                media_type = InputMediaPhoto if kind == "photo" else InputMediaDocument
                media = [
                    media_type(
                        media=image_input(data, name),
                        caption=caption if index == 0 and caption_available else None,
                        parse_mode=parse_mode
                        if index == 0 and caption_available
                        else None,
                    )
                    for index, (data, name) in enumerate(batch)
                ]
                try:
                    await bot.send_media_group(chat_id=chat_id, media=media)
                    caption_available = False
                    continue
                except Exception as exc:
                    logger.info("Image album refused: %s", failure_reason(exc))
            for data, name in batch:
                text = caption if caption_available else None
                if kind == "photo":
                    await send_photo_or_document(
                        bot, chat_id, data, name, text, parse_mode
                    )
                else:
                    await bot.send_document(
                        chat_id=chat_id,
                        document=image_input(data, name),
                        caption=text,
                        parse_mode=parse_mode,
                    )
                caption_available = False


def _media(value: Any) -> Any:
    if isinstance(value, InputMediaPhoto | InputMediaDocument | InputMediaVideo):
        return value
    media = getattr(value, "media", None)
    if media is None:
        return value
    return InputMediaPhoto(
        media=_input_file(media),
        caption=getattr(value, "caption", None),
        parse_mode=getattr(value, "parse_mode", None),
    )


class TelegramBot:
    """The narrow bot surface used by Daphne handlers."""

    def __init__(self, bot: Bot):
        self._bot = bot

    @property
    def raw(self) -> Bot:
        return self._bot

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bot, name)

    async def send_video(self, **kwargs: Any) -> Any:
        kwargs["video"] = _input_file(kwargs["video"])
        return await self._bot.send_video(**kwargs)

    async def send_audio(self, **kwargs: Any) -> Any:
        kwargs["audio"] = _input_file(kwargs["audio"])
        return await self._bot.send_audio(**kwargs)

    async def send_photo(self, **kwargs: Any) -> Any:
        kwargs["photo"] = _input_file(kwargs["photo"])
        return await self._bot.send_photo(**kwargs)

    async def send_document(self, **kwargs: Any) -> Any:
        kwargs["document"] = _input_file(kwargs["document"])
        return await self._bot.send_document(**kwargs)

    async def send_animation(self, **kwargs: Any) -> Any:
        kwargs["animation"] = _input_file(kwargs["animation"])
        return await self._bot.send_animation(**kwargs)

    async def send_media_group(
        self, *, chat_id: int, media: Iterable[Any], **kwargs: Any
    ) -> Any:
        return await self._bot.send_media_group(
            chat_id=chat_id, media=[_media(item) for item in media], **kwargs
        )


class TelegramMessage:
    def __init__(self, message: Message):
        self._message = message
        self._text_override: str | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._message, name)

    @property
    def chat(self) -> Any:
        return self._message.chat

    @property
    def chat_id(self) -> int:
        return self._message.chat.id

    @property
    def from_user(self) -> Any:
        return self._message.from_user

    @property
    def text(self) -> str | None:
        return (
            self._text_override
            if self._text_override is not None
            else self._message.text
        )

    @text.setter
    def text(self, value: str | None) -> None:
        self._text_override = value

    @property
    def reply_to_message(self) -> TelegramMessage | None:
        message = self._message.reply_to_message
        return TelegramMessage(message) if message else None

    @property
    def is_topic_message(self) -> bool:
        return bool(self._message.is_topic_message)

    async def reply_text(self, text: str, **kwargs: Any) -> TelegramMessage:
        if kwargs.pop("disable_web_page_preview", False):
            kwargs["link_preview_options"] = LinkPreviewOptions(is_disabled=True)
        return TelegramMessage(await self._message.answer(text, **kwargs))

    async def edit_text(self, text: str, **kwargs: Any) -> TelegramMessage:
        return TelegramMessage(await self._message.edit_text(text, **kwargs))

    async def delete(self, **kwargs: Any) -> Any:
        return await self._message.delete(**kwargs)

    async def set_reaction(self, *, reaction: list[ReactionTypeEmoji]) -> Any:
        return await self._message.react(reaction)


class TelegramCallbackQuery:
    def __init__(self, query: CallbackQuery):
        self._query = query

    def __getattr__(self, name: str) -> Any:
        return getattr(self._query, name)

    @property
    def message(self) -> TelegramMessage | None:
        message = self._query.message
        return TelegramMessage(message) if message else None

    @property
    def from_user(self) -> Any:
        return self._query.from_user

    @property
    def data(self) -> str | None:
        return self._query.data

    async def answer(self, *args: Any, **kwargs: Any) -> Any:
        return await self._query.answer(*args, **kwargs)

    async def edit_message_text(self, text: str, **kwargs: Any) -> Any:
        if self._query.message is None:
            return None
        return await self._query.message.edit_text(text, **kwargs)


class TelegramUpdate:
    def __init__(self, update: Update):
        self._update = update
        self.message = TelegramMessage(update.message) if update.message else None
        self.callback_query = (
            TelegramCallbackQuery(update.callback_query)
            if update.callback_query
            else None
        )
        self.update_id = update.update_id

    @property
    def effective_user(self) -> Any:
        if self.message:
            return self.message.from_user
        if self.callback_query:
            return self.callback_query.from_user
        return None

    @property
    def effective_chat(self) -> Any:
        if self.message:
            return self.message.chat
        if self.callback_query and self.callback_query.message:
            return self.callback_query.message.chat
        return None


@dataclass(slots=True)
class TelegramContext:
    bot: TelegramBot
    args: list[str]


def adapt_update(update: Update) -> TelegramUpdate:
    return TelegramUpdate(update)


def adapt_message(message: Message) -> TelegramUpdate:
    return TelegramUpdate(Update(update_id=0, message=message))


def adapt_callback_query(query: CallbackQuery) -> TelegramUpdate:
    return TelegramUpdate(Update(update_id=0, callback_query=query))


def context_for(bot: TelegramBot, args: list[str] | None = None) -> TelegramContext:
    return TelegramContext(bot=bot, args=args or [])


def build_bot(token: str) -> TelegramBot:
    api_url = telegram_api_url()
    session_kwargs: dict[str, Any] = {"timeout": LOCAL_BOT_API_TIMEOUT_SECONDS}
    if api_url:
        api_url = api_url.rstrip("/")
        session_kwargs["api"] = TelegramAPIServer.from_base(api_url, is_local=True)
    session = AiohttpSession(**session_kwargs)
    return TelegramBot(
        Bot(
            token=token,
            session=session,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
    )


def new_router() -> Router:
    return Router(name="daphne")


def new_dispatcher(router: Router) -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher
