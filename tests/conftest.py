from __future__ import annotations

import pytest
import pytest_asyncio

from app.config import Settings
from app.db.database import Database


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        bot_token="123456:test-token",
        owner_ids=(1,),
        database_path=tmp_path / "bot.db",
        timezone="Asia/Kolkata",
        send_delay_seconds=0.0,
        chunk_delay_seconds=0.0,
        send_retry_backoff_seconds=0.0,
        log_file=None,
        admin_reports=False,
    )


@pytest_asyncio.fixture
async def db(settings: Settings):
    database = Database(settings.database_path)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


@pytest_asyncio.fixture
async def pg_db():
    """PostgreSQL backend in a disposable schema; skipped without TEST_DATABASE_URL."""

    from tests.pg_utils import pg_database

    async with pg_database() as (database, _schema):
        yield database
