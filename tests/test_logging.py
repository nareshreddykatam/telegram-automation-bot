"""Logging must not prevent startup on read-only or unwritable serverless filesystems."""

from __future__ import annotations

import logging

from app.config import Settings
from app.logging_config import setup_logging


def test_setup_logging_falls_back_to_console_when_file_is_unwritable(tmp_path):
    settings = Settings(
        bot_token="123:test",
        owner_ids=(1,),
        log_file=tmp_path,  # a directory cannot be opened as a log file
    )

    root = logging.getLogger()
    original_handlers = list(root.handlers)
    try:
        setup_logging(settings)
        assert root.handlers
        assert any(isinstance(handler, logging.StreamHandler) for handler in root.handlers)
        assert not any(isinstance(handler, logging.FileHandler) for handler in root.handlers)
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in original_handlers:
            root.addHandler(handler)
