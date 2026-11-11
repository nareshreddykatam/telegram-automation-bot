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


def test_scheduled_target_date_before_and_after_publish_time():
    tz = ZoneInfo("Asia/Kolkata")
    before = datetime(2026, 1, 2, 19, 59, tzinfo=tz)
    after = datetime(2026, 1, 2, 20, 0, tzinfo=tz)
    assert scheduled_target_date(tz, 20, 0, before).isoformat() == "2026-01-01"
    assert scheduled_target_date(tz, 20, 0, after).isoformat() == "2026-01-02"
