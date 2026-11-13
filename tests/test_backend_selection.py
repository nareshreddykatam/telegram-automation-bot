"""Unit tests for DSN helpers and backend selection (no database needed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import ConfigError, Settings
from app.db.database import Database
from app.db.dsn import describe_dsn, dsn_has_sslmode, normalize_dsn
from app.db.factory import create_database, describe_database


def _settings(**overrides) -> Settings:
    values = {"bot_token": "123:test", "owner_ids": (1,), "log_file": None}
    values.update(overrides)
    return Settings(**values)


def test_sqlite_is_the_default_backend():
    settings = _settings(database_path=Path("data/test.db"))
    assert settings.database_backend == "sqlite"
    database = create_database(settings)
    assert isinstance(database, Database)
    assert database.backend == "sqlite"
    assert describe_database(settings) == str(Path("data/test.db"))


def test_postgres_requires_a_url():
    with pytest.raises(ConfigError, match="DATABASE_URL is required"):
        _settings(database_backend="postgres")


def test_unknown_backend_is_rejected():
    with pytest.raises(ConfigError, match="DATABASE_BACKEND"):
        _settings(database_backend="mysql")


def test_invalid_pool_bounds_are_rejected():
    with pytest.raises(ConfigError, match="DB_POOL_MIN"):
        _settings(database_backend="postgres", database_url="postgresql://h/db", db_pool_max=0)


def test_negative_statement_cache_is_rejected():
    with pytest.raises(ConfigError, match="DB_STATEMENT_CACHE_SIZE"):
        _settings(db_statement_cache_size=-1)


def test_database_url_is_not_in_repr():
    settings = _settings(database_backend="postgres", database_url="postgresql://u:secret@h/db")
    assert "secret" not in repr(settings)


def test_postgres_factory_returns_postgres_backend():
    pytest.importorskip("asyncpg")
    from app.db.postgres import PostgresDatabase

    settings = _settings(
        database_backend="postgres",
        database_url="postgresql://user:secret@example.test:6543/postgres?sslmode=require",
        db_pool_min=1,
        db_pool_max=2,
    )
    database = create_database(settings)
    assert isinstance(database, PostgresDatabase)
    assert database.backend == "postgres"
    label = describe_database(settings)
    assert "example.test" in label
    assert "secret" not in label


def test_settings_read_backend_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("BOT_TOKEN", "123:test")
    monkeypatch.setenv("OWNER_IDS", "1,2")
    monkeypatch.setenv("DATABASE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@host:6543/postgres")
    monkeypatch.setenv("DB_POOL_MAX", "3")
    settings = Settings.from_env(env_file=tmp_path / "missing.env")
    assert settings.database_backend == "postgres"
    assert settings.database_url == "postgresql://user:pass@host:6543/postgres"
    assert settings.db_pool_max == 3


def test_empty_database_url_is_treated_as_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("BOT_TOKEN", "123:test")
    monkeypatch.setenv("OWNER_IDS", "1")
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("DATABASE_URL", "   ")
    settings = Settings.from_env(env_file=tmp_path / "missing.env")
    assert settings.database_url is None


def test_normalize_dsn_drops_pooler_only_options():
    dsn = "postgresql://u:p@h:6543/postgres?sslmode=require&pgbouncer=true&connection_limit=1"
    normalized = normalize_dsn(dsn)
    assert "pgbouncer" not in normalized
    assert "connection_limit" not in normalized
    assert "sslmode=require" in normalized
    assert dsn_has_sslmode(normalized) is True


def test_normalize_dsn_leaves_clean_dsn_unchanged():
    dsn = "postgresql://u:p@h:5432/postgres"
    assert normalize_dsn(dsn) == dsn
    assert dsn_has_sslmode(dsn) is False


def test_describe_dsn_hides_credentials():
    label = describe_dsn("postgresql://user:secret@db.example.test:6543/postgres")
    assert label == "postgresql://db.example.test:6543/postgres"
    assert "secret" not in label


def test_pool_defaults_are_serverless_friendly():
    settings = _settings(
        database_backend="postgres",
        database_url="postgresql://user@db.example.test:6543/postgres",
    )
    assert settings.db_pool_min == 0
    assert settings.db_pool_max == 2

    pytest.importorskip("asyncpg")
    database = create_database(settings)
    assert "schema=" not in database.label


def test_db_schema_is_not_a_runtime_setting(monkeypatch, tmp_path):
    monkeypatch.setenv("BOT_TOKEN", "123:test")
    monkeypatch.setenv("OWNER_IDS", "1")
    monkeypatch.setenv("DATABASE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host:6543/postgres")
    monkeypatch.setenv("DB_SCHEMA", "custom")

    settings = Settings.from_env(env_file=tmp_path / "missing.env")
    assert not hasattr(settings, "db_schema")
