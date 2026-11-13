"""Vercel Python entrypoint for the Telegram webhook.

Vercel routes this file to ``/api/telegram``. The module deliberately does not
import ``app.main``: that entrypoint starts long polling and APScheduler, which
must never run inside a serverless function.
"""

from __future__ import annotations

import logging

from app.config import Settings
from app.logging_config import setup_logging
from app.webhook import create_webhook_app

logging.basicConfig(level=logging.INFO)
settings = Settings.from_env()
setup_logging(settings)
app = create_webhook_app(settings)
