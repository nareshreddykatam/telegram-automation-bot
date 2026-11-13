from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.config import ConfigError, Settings
from app.utils.time import scheduled_target_date


def test_settings_required_secrets():
    with pytest.raises(ConfigError):
        Settings(bot_token="", owner_ids=(1,))
    with pytest.raises(ConfigError):
        Settings(bot_token="token", owner_ids=())


def test_settings_rejects_invalid_timezone():
    with pytest.raises(ConfigError):
        Settings(bot_token="token", owner_ids=(1,), timezone="Mars/Olympus")


def test_settings_rejects_invalid_webhook_secret():
    with pytest.raises(ConfigError, match="TELEGRAM_WEBHOOK_SECRET"):
        Settings(bot_token="token", owner_ids=(1,), telegram_webhook_secret="bad secret!")

    secret = "A-z_09" * 50
    with pytest.raises(ConfigError, match="TELEGRAM_WEBHOOK_SECRET"):
        Settings(bot_token="token", owner_ids=(1,), telegram_webhook_secret=secret)


def test_settings_accepts_telegram_bot_token_alias(monkeypatch, tmp_path):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:alias-token")
    monkeypatch.setenv("OWNER_IDS", "1")

    settings = Settings.from_env(tmp_path / "missing.env")
    assert settings.bot_token == "123:alias-token"


def test_scheduled_target_date_before_and_after_publish_time():
    tz = ZoneInfo("Asia/Kolkata")
    before = datetime(2026, 1, 2, 19, 59, tzinfo=tz)
    after = datetime(2026, 1, 2, 20, 0, tzinfo=tz)
    assert scheduled_target_date(tz, 20, 0, before).isoformat() == "2026-01-01"
    assert scheduled_target_date(tz, 20, 0, after).isoformat() == "2026-01-02"
