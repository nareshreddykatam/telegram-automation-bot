"""Vercel-compatible Telegram webhook ASGI application.

Telegram delivers one update per HTTPS request. The handler path must only
perform short database work on PostgreSQL and must never run the inline SQLite
publishing engine. Long publishing is owned by ``python -m app.worker``.

Security
--------
Every POST must carry Telegram's
``X-Telegram-Bot-Api-Secret-Token`` header. The value comes from
``TELEGRAM_WEBHOOK_SECRET`` and is compared in constant time. The endpoint is
also restricted to PostgreSQL so a misconfigured deployment cannot silently
write a SQLite file into an ephemeral serverless filesystem.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Any

from aiogram.types import Update

from app.bootstrap import BotApplication, build_application
from app.config import ConfigError, Settings

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 1_048_576
WEBHOOK_SECRET_HEADER = "x-telegram-bot-api-secret-token"
JSON_CONTENT_TYPE = "application/json"

ApplicationFactory = Callable[[Settings], Awaitable[BotApplication]]


class TelegramWebhook:
    """ASGI callable that validates and dispatches Telegram updates."""

    def __init__(
        self,
        settings: Settings,
        *,
        application_factory: ApplicationFactory = build_application,
        background_wait_seconds: float = 10.0,
    ) -> None:
        if settings.database_backend != "postgres":
            raise ConfigError(
                "The webhook runtime requires DATABASE_BACKEND=postgres; "
                "SQLite is only supported by the local polling runtime"
            )
        if not settings.telegram_webhook_secret:
            raise ConfigError(
                "TELEGRAM_WEBHOOK_SECRET is required for the webhook runtime"
            )
        if background_wait_seconds <= 0:
            raise ValueError("background_wait_seconds must be positive")

        self._settings = settings
        self._application_factory = application_factory
        self._background_wait_seconds = background_wait_seconds
        self._application: BotApplication | None = None
        self._application_lock = asyncio.Lock()

    async def get_application(self) -> BotApplication:
        """Build the shared application once per warm function instance."""

        if self._application is None:
            async with self._application_lock:
                if self._application is None:
                    logger.info(
                        "Building webhook application (database=%s)",
                        "postgres",
                    )
                    self._application = await self._application_factory(self._settings)
        return self._application

    async def close(self) -> None:
        application, self._application = self._application, None
        if application is not None:
            await application.close()

    async def handle_update(self, update: Update) -> None:
        application = await self.get_application()
        try:
            await application.dispatcher.feed_update(application.bot, update)
        finally:
            # Handlers normally finish immediately. `/resume` without arguments
            # spawns a short PostgreSQL catch-up enqueue; let it finish before
            # Vercel can freeze the instance.
            await application.background.wait(self._background_wait_seconds)

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            await self._handle_lifespan(receive, send)
            return
        if scope_type != "http":
            return

        method = str(scope.get("method", "")).upper()
        if method == "GET":
            await _send_json(send, HTTPStatus.OK, {"status": "ok", "mode": "webhook"})
            return
        if method != "POST":
            await _send_json(
                send,
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"ok": False, "error": "method_not_allowed"},
                extra_headers=[(b"allow", b"GET, POST")],
            )
            return

        headers = _headers(scope)
        provided = headers.get(WEBHOOK_SECRET_HEADER, "")
        expected = self._settings.telegram_webhook_secret or ""
        if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
            await _send_json(
                send,
                HTTPStatus.FORBIDDEN,
                {"ok": False, "error": "invalid_webhook_secret"},
            )
            return

        content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != JSON_CONTENT_TYPE:
            await _send_json(
                send,
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"ok": False, "error": "content_type_must_be_application_json"},
            )
            return

        body, too_large = await _read_body(receive, MAX_BODY_BYTES)
        if too_large:
            await _send_json(
                send,
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"ok": False, "error": "request_too_large"},
            )
            return

        try:
            payload = json.loads(body)
            update = Update.model_validate(payload)
        except Exception:
            await _send_json(
                send,
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "invalid_update"},
            )
            return

        try:
            await self.handle_update(update)
        except Exception:
            # Return 5xx so Telegram retries a genuinely failed update. Durable
            # database constraints keep duplicate deliveries from duplicating
            # source messages, requests or jobs.
            logger.exception("Webhook update processing failed")
            await _send_json(
                send,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": "update_processing_failed"},
            )
            return

        await _send_json(send, HTTPStatus.OK, {"ok": True})

    async def _handle_lifespan(
        self,
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        while True:
            message = await receive()
            if message.get("type") == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message.get("type") == "lifespan.shutdown":
                await self.close()
                await send({"type": "lifespan.shutdown.complete"})
                return


def create_webhook_app(
    settings: Settings,
    *,
    application_factory: ApplicationFactory = build_application,
    background_wait_seconds: float = 10.0,
) -> TelegramWebhook:
    """Construct the ASGI application for ``api/telegram.py`` and tests."""

    return TelegramWebhook(
        settings,
        application_factory=application_factory,
        background_wait_seconds=background_wait_seconds,
    )


def _headers(scope: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers", []):
        name = raw_name.decode("latin-1").lower()
        value = raw_value.decode("latin-1")
        result[name] = value
    return result


async def _read_body(
    receive: Callable[[], Awaitable[dict[str, Any]]], limit: int
) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            break
        chunk = message.get("body", b"")
        total += len(chunk)
        if total > limit:
            return b"", True
        chunks.append(chunk)
        if not message.get("more_body", False):
            break
    return b"".join(chunks), False


async def _send_json(
    send: Callable[[dict[str, Any]], Awaitable[None]],
    status: int,
    payload: dict[str, Any],
    *,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"cache-control", b"no-store"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": int(status), "headers": headers})
    await send({"type": "http.response.body", "body": body})


__all__ = [
    "MAX_BODY_BYTES",
    "WEBHOOK_SECRET_HEADER",
    "TelegramWebhook",
    "create_webhook_app",
]
