"""Logging configuration with rotating files and UTF-8 console output."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from app.config import Settings


def setup_logging(settings: Settings) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    level = getattr(logging, settings.log_level, logging.INFO)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.setLevel(level)
    root.addHandler(console)

    if settings.log_file is not None:
        path = Path(settings.log_file)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                path,
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
        except OSError as exc:
            # Serverless runtimes such as Vercel have a read-only project
            # filesystem. Console logging must still work instead of aborting
            # application startup.
            root.warning("File logging disabled for %s: %s", path, exc)
        else:
            file_handler.setFormatter(formatter)
            file_handler.setLevel(level)
            root.addHandler(file_handler)

    # aiogram's per-update debug log is very noisy at INFO.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
