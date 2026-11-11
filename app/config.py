"""Application configuration loaded exclusively from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


class ConfigError(ValueError):
    """Raised when configuration is missing or invalid."""


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or value.strip() == "" else value.strip()


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = _env(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = _env(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {value}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "true" if default else "false").lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean, got {raw!r}")


def _parse_owner_ids(raw: str) -> tuple[int, ...]:
    ids: list[int] = []
    for part in raw.replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            telegram_id = int(part)
        except ValueError as exc:
            raise ConfigError(f"OWNER_IDS contains a non-numeric id: {part!r}") from exc
        if telegram_id <= 0:
            raise ConfigError(f"OWNER_IDS values must be positive, got {telegram_id}")
        if telegram_id not in ids:
            ids.append(telegram_id)
    if not ids:
        raise ConfigError("OWNER_IDS must contain at least one Telegram user id")
    return tuple(ids)


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable runtime settings.

    Timestamps in the database are stored as UTC ISO-8601 strings. The timezone
    below is used for the publishing schedule and user-facing dates.
    """

    bot_token: str = field(repr=False)
    owner_ids: tuple[int, ...]
    database_path: Path = Path("data/bot.db")
    timezone: str = "Asia/Kolkata"
    publish_hour: int = 20
    publish_minute: int = 0
    default_batch_size: int = 100
    max_batch_size: int = 1000
    publish_chunk_size: int = 100
    send_delay_seconds: float = 3.0
    chunk_delay_seconds: float = 2.0
    max_send_retries: int = 3
    send_retry_backoff_seconds: float = 2.0
    log_level: str = "INFO"
    log_file: Path | None = Path("logs/bot.log")
    scheduler_enabled: bool = True
    catchup_on_startup: bool = True
    max_catchup_cycles: int = 7
    admin_reports: bool = True

    def __post_init__(self) -> None:
        if not self.bot_token:
            raise ConfigError("BOT_TOKEN is required")
        if not self.owner_ids:
            raise ConfigError("OWNER_IDS is required")
        if not 0 <= self.publish_hour <= 23:
            raise ConfigError("PUBLISH_HOUR must be between 0 and 23")
        if not 0 <= self.publish_minute <= 59:
            raise ConfigError("PUBLISH_MINUTE must be between 0 and 59")
        if not 1 <= self.publish_chunk_size <= self.max_batch_size:
            raise ConfigError("PUBLISH_CHUNK_SIZE must be between 1 and MAX_BATCH_SIZE")
        if not 1 <= self.default_batch_size <= self.max_batch_size:
            raise ConfigError("DEFAULT_BATCH_SIZE must be between 1 and MAX_BATCH_SIZE")
        if not 1 <= self.max_catchup_cycles <= 365:
            raise ConfigError("MAX_CATCHUP_CYCLES must be between 1 and 365")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"Unknown TIMEZONE: {self.timezone!r}") from exc

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> Settings:
        # override=False keeps real environment variables authoritative.
        load_dotenv(dotenv_path=env_file, override=False)
        token = _env("BOT_TOKEN", "")
        if not token:
            raise ConfigError("BOT_TOKEN is required")
        log_file_raw = os.getenv("LOG_FILE")
        if log_file_raw is None:
            log_file: Path | None = Path("logs/bot.log")
        elif log_file_raw.strip() == "":
            log_file = None
        else:
            log_file = Path(log_file_raw.strip()).expanduser()
        return cls(
            bot_token=_env("BOT_TOKEN", ""),
            owner_ids=_parse_owner_ids(_env("OWNER_IDS", "")),
            database_path=Path(_env("DATABASE_PATH", "data/bot.db")).expanduser(),
            timezone=_env("TIMEZONE", "Asia/Kolkata"),
            publish_hour=_env_int("PUBLISH_HOUR", 20, 0, 23),
            publish_minute=_env_int("PUBLISH_MINUTE", 0, 0, 59),
            default_batch_size=_env_int("DEFAULT_BATCH_SIZE", 100, 1, 100_000),
            max_batch_size=_env_int("MAX_BATCH_SIZE", 1000, 1, 100_000),
            publish_chunk_size=_env_int("PUBLISH_CHUNK_SIZE", 100, 1, 100_000),
            send_delay_seconds=_env_float("SEND_DELAY_SECONDS", 3.0, 0.0, 3600.0),
            chunk_delay_seconds=_env_float("CHUNK_DELAY_SECONDS", 2.0, 0.0, 3600.0),
            max_send_retries=_env_int("MAX_SEND_RETRIES", 3, 1, 20),
            send_retry_backoff_seconds=_env_float("SEND_RETRY_BACKOFF_SECONDS", 2.0, 0.0, 3600.0),
            log_level=_env("LOG_LEVEL", "INFO").upper(),
            log_file=log_file,
            scheduler_enabled=_env_bool("SCHEDULER_ENABLED", True),
            catchup_on_startup=_env_bool("CATCHUP_ON_STARTUP", True),
            max_catchup_cycles=_env_int("MAX_CATCHUP_CYCLES", 7, 1, 365),
            admin_reports=_env_bool("ADMIN_REPORTS", True),
        )
