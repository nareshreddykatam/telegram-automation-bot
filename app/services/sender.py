"""Telegram sender abstraction.

The publishing service depends on this protocol rather than aiogram directly,
which keeps queue logic unit-testable and makes failures explicit.
"""

from __future__ import annotations

import logging
from typing import Protocol

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import MessageId

logger = logging.getLogger(__name__)


class SendError(Exception):
    """Base class for normalized publishing failures."""


class TransientSendError(SendError):
    """Retryable failure: network/server/unknown; cursor must not advance."""


class AmbiguousSendError(TransientSendError):
    """The request may have reached Telegram.

    Phase 2 treats this as ``uncertain`` and never blindly resends. It subclasses
    ``TransientSendError`` so the legacy SQLite publishing engine keeps its
    historical retry behaviour.
    """


class PermanentSendError(SendError):
    """Content/chat cannot be copied; record failure and advance cursor."""


class FloodWaitError(TransientSendError):
    def __init__(self, retry_after: int, message: str | None = None) -> None:
        super().__init__(message or f"Telegram flood wait: {retry_after}s")
        self.retry_after = max(0, int(retry_after))


class MessageSender(Protocol):
    async def copy_message(
        self, *, chat_id: int, from_chat_id: int, message_id: int
    ) -> int | None: ...


class AlbumMessageSender(Protocol):
    async def copy_messages(
        self, *, chat_id: int, from_chat_id: int, message_ids: list[int]
    ) -> list[int | None]: ...


class AiogramSender:
    """copies messages (not forwards) so destination posts have no attribution."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def copy_message(self, *, chat_id: int, from_chat_id: int, message_id: int) -> int | None:
        try:
            result: MessageId = await self._bot.copy_message(
                chat_id=chat_id,
                from_chat_id=from_chat_id,
                message_id=message_id,
            )
            return result.message_id
        except TelegramRetryAfter as exc:
            raise FloodWaitError(exc.retry_after, str(exc)) from exc
        except TelegramForbiddenError as exc:
            raise PermanentSendError(
                f"Bot is not allowed to post/copy in destination: {exc.message}"
            ) from exc
        except TelegramBadRequest as exc:
            raise PermanentSendError(f"Telegram rejected the message: {exc.message}") from exc
        except (TelegramNetworkError, TimeoutError) as exc:
            # The update may have reached Telegram before the connection died or
            # timed out. Phase 2 records this as uncertain instead of resending.
            raise AmbiguousSendError(f"Telegram network/timeout error: {exc}") from exc
        except TelegramServerError as exc:
            raise TransientSendError(f"Telegram server error: {exc}") from exc
        except Exception as exc:  # Defensive: never silently drop a message.
            logger.exception("Unexpected Telegram copy error")
            raise TransientSendError(f"Unexpected copy error: {exc}") from exc

    async def copy_messages(
        self, *, chat_id: int, from_chat_id: int, message_ids: list[int]
    ) -> list[int | None]:
        try:
            results = await self._bot.copy_messages(
                chat_id=chat_id,
                from_chat_id=from_chat_id,
                message_ids=message_ids,
            )
            return [item.message_id for item in results]
        except TelegramRetryAfter as exc:
            raise FloodWaitError(exc.retry_after, str(exc)) from exc
        except TelegramForbiddenError as exc:
            raise PermanentSendError(
                f"Bot is not allowed to post/copy in destination: {exc.message}"
            ) from exc
        except TelegramBadRequest as exc:
            raise PermanentSendError(f"Telegram rejected the album: {exc.message}") from exc
        except (TelegramNetworkError, TimeoutError) as exc:
            raise AmbiguousSendError(f"Telegram network/timeout error: {exc}") from exc
        except TelegramServerError as exc:
            raise TransientSendError(f"Telegram server error: {exc}") from exc
        except Exception as exc:
            logger.exception("Unexpected Telegram album copy error")
            raise TransientSendError(f"Unexpected album copy error: {exc}") from exc
