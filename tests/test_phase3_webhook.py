"""Phase 3 webhook runtime tests. No Telegram or PostgreSQL connection is used."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.config import ConfigError, Settings
from app.services.background import BackgroundTasks
from app.webhook import MAX_BODY_BYTES, TelegramWebhook, create_webhook_app

SECRET = "test-secret_123"


def _settings(**overrides) -> Settings:
    values = {
        "bot_token": "123456:test-token",
        "owner_ids": (1,),
        "database_backend": "postgres",
        "database_url": "postgresql://user:pass@example.test:6543/postgres",
        "telegram_webhook_secret": SECRET,
        "log_file": None,
    }
    values.update(overrides)
    return Settings(**values)


class FakeBot:
    pass


class FakeBackground:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def wait(self, timeout: float = 10.0) -> None:
        self.waits.append(timeout)


class FakeDispatcher:
    def __init__(self, *, fail: bool = False) -> None:
        self.updates: list[object] = []
        self.fail = fail

    async def feed_update(self, bot: object, update: object) -> None:
        if self.fail:
            raise RuntimeError("simulated dispatcher failure")
        self.updates.append(update)


class FakeApplication:
    def __init__(self, *, fail: bool = False) -> None:
        self.bot = FakeBot()
        self.dispatcher = FakeDispatcher(fail=fail)
        self.background = FakeBackground()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class Factory:
    def __init__(self, *, fail: bool = False) -> None:
        self.created: list[FakeApplication] = []
        self.fail = fail

    async def __call__(self, settings: Settings) -> FakeApplication:
        application = FakeApplication(fail=self.fail)
        self.created.append(application)
        return application


def _app(factory: Factory, **overrides) -> TelegramWebhook:
    return create_webhook_app(
        _settings(**overrides),
        application_factory=factory,  # type: ignore[arg-type]
        background_wait_seconds=5.0,
    )


async def _call(
    app: TelegramWebhook,
    *,
    method: str = "POST",
    path: str = "/api/telegram",
    secret: str | None = SECRET,
    content_type: str | None = "application/json",
    body: bytes = b'{"update_id": 1}',
    chunks: list[bytes] | None = None,
) -> tuple[int, dict]:
    headers: list[tuple[bytes, bytes]] = []
    if secret is not None:
        headers.append((b"x-telegram-bot-api-secret-token", secret.encode()))
    if content_type is not None:
        headers.append((b"content-type", content_type.encode()))

    if chunks is None:
        messages = [{"type": "http.request", "body": body, "more_body": False}]
    else:
        messages = [
            {
                "type": "http.request",
                "body": chunk,
                "more_body": index < len(chunks) - 1,
            }
            for index, chunk in enumerate(chunks)
        ]

    sent: list[dict] = []

    async def receive() -> dict:
        return messages.pop(0)

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers,
    }
    await app(scope, receive, send)

    start = next(item for item in sent if item["type"] == "http.response.start")
    response_body = b"".join(
        item.get("body", b"") for item in sent if item["type"] == "http.response.body"
    )
    return int(start["status"]), json.loads(response_body) if response_body else {}


async def test_webhook_accepts_valid_update_and_flushes_background_tasks():
    factory = Factory()
    app = _app(factory)

    status, payload = await _call(app)
    assert status == 200
    assert payload == {"ok": True}
    assert len(factory.created) == 1
    assert len(factory.created[0].dispatcher.updates) == 1
    assert factory.created[0].background.waits == [5.0]

    # A warm instance reuses the same database/bot/dispatcher wiring.
    status, _ = await _call(app)
    assert status == 200
    assert len(factory.created) == 1
    assert len(factory.created[0].dispatcher.updates) == 2


async def test_webhook_rejects_missing_or_wrong_secret():
    factory = Factory()
    app = _app(factory)

    status, payload = await _call(app, secret=None)
    assert status == 403
    assert payload["error"] == "invalid_webhook_secret"

    status, payload = await _call(app, secret="wrong-secret")
    assert status == 403
    assert payload["error"] == "invalid_webhook_secret"
    assert factory.created == []


async def test_webhook_rejects_unsupported_method_and_content_type():
    factory = Factory()
    app = _app(factory)

    status, payload = await _call(app, method="PUT")
    assert status == 405
    assert payload["error"] == "method_not_allowed"

    status, payload = await _call(app, content_type="text/plain")
    assert status == 415
    assert payload["error"] == "content_type_must_be_application_json"
    assert factory.created == []


async def test_webhook_rejects_invalid_json_and_invalid_update_shape():
    factory = Factory()
    app = _app(factory)

    status, payload = await _call(app, body=b"not-json")
    assert status == 400
    assert payload["error"] == "invalid_update"

    status, payload = await _call(app, body=b'{"update_id": "not-an-integer"}')
    assert status == 400
    assert payload["error"] == "invalid_update"
    assert factory.created == []


async def test_webhook_rejects_oversized_body_across_chunks():
    factory = Factory()
    app = _app(factory)
    half = b"x" * (MAX_BODY_BYTES // 2 + 1)

    status, payload = await _call(app, chunks=[half, half])
    assert status == 413
    assert payload["error"] == "request_too_large"
    assert factory.created == []


async def test_webhook_returns_500_when_dispatcher_fails_and_still_flushes():
    factory = Factory(fail=True)
    app = _app(factory)

    status, payload = await _call(app)
    assert status == 500
    assert payload["error"] == "update_processing_failed"
    assert len(factory.created) == 1
    assert factory.created[0].background.waits == [5.0]


async def test_webhook_health_is_fast_and_does_not_connect():
    factory = Factory()
    app = _app(factory)

    status, payload = await _call(app, method="GET", body=b"")
    assert status == 200
    assert payload == {"status": "ok", "mode": "webhook"}
    assert factory.created == []


async def test_webhook_lifespan_shutdown_closes_application():
    factory = Factory()
    app = _app(factory)
    await _call(app)
    assert factory.created[0].closed is False

    messages = [
        {"type": "lifespan.startup"},
        {"type": "lifespan.shutdown"},
    ]
    sent: list[dict] = []

    async def receive() -> dict:
        return messages.pop(0)

    async def send(message: dict) -> None:
        sent.append(message)

    await app({"type": "lifespan"}, receive, send)
    assert factory.created[0].closed is True
    assert [item["type"] for item in sent] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]


def test_webhook_refuses_sqlite_and_missing_secret():
    with pytest.raises(ConfigError, match="requires DATABASE_BACKEND=postgres"):
        create_webhook_app(_settings(database_backend="sqlite"))

    with pytest.raises(ConfigError, match="TELEGRAM_WEBHOOK_SECRET"):
        create_webhook_app(_settings(telegram_webhook_secret=None))


async def test_background_wait_completes_tasks_and_cancels_timeout():
    background = BackgroundTasks()
    completed = asyncio.Event()

    async def finish_quickly() -> None:
        await asyncio.sleep(0)
        completed.set()

    background.spawn(finish_quickly(), name="quick")
    await background.wait(timeout=1.0)
    assert completed.is_set()

    cancelled = asyncio.Event()

    async def never_finishes() -> None:
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    background.spawn(never_finishes(), name="slow")
    await background.wait(timeout=0.01)
    assert cancelled.is_set()
