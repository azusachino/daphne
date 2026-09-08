"""Daphne's small Telegram boundary.

Handlers use these compatibility-shaped operations so the framework stays at the edge.
The implementation is aiogram; keeping this translation in one module makes a
future Telegram framework change a composition concern instead of a handler
rewrite.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any, Iterable

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineQuery,
    InputMediaPhoto,
    LinkPreviewOptions,
    Message,
    ReactionTypeEmoji,
    Update,
)

from daphne.config import telegram_api_url

LOCAL_BOT_API_TIMEOUT_SECONDS = 7200


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


def _media(value: Any) -> Any:
    if isinstance(value, InputMediaPhoto):
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


class TelegramInlineQuery:
    def __init__(self, query: InlineQuery):
        self._query = query

    def __getattr__(self, name: str) -> Any:
        return getattr(self._query, name)

    @property
    def from_user(self) -> Any:
        return self._query.from_user

    @property
    def query(self) -> str:
        return self._query.query

    async def answer(self, *args: Any, **kwargs: Any) -> Any:
        return await self._query.answer(*args, **kwargs)


class TelegramUpdate:
    def __init__(self, update: Update):
        self._update = update
        self.message = TelegramMessage(update.message) if update.message else None
        self.callback_query = (
            TelegramCallbackQuery(update.callback_query)
            if update.callback_query
            else None
        )
        self.inline_query = (
            TelegramInlineQuery(update.inline_query) if update.inline_query else None
        )
        self.update_id = update.update_id

    @property
    def effective_user(self) -> Any:
        if self.message:
            return self.message.from_user
        if self.callback_query:
            return self.callback_query.from_user
        if self.inline_query:
            return self.inline_query.from_user
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


def adapt_inline_query(query: InlineQuery) -> TelegramUpdate:
    return TelegramUpdate(Update(update_id=0, inline_query=query))


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
