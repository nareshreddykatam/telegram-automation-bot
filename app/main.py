"""Long-polling application entrypoint.

The serverless webhook entrypoint lives in :mod:`api.telegram` and reuses the
same wiring from :mod:`app.bootstrap`. This module remains the SQLite/local and
rollback runtime.
"""

from __future__ import annotations

import asyncio
import logging

from app.bootstrap import build_application
from app.config import ConfigError, Settings
from app.db.factory import describe_database
from app.logging_config import setup_logging

logger = logging.getLogger(__name__)


async def build_and_run(settings: Settings) -> None:
    setup_logging(settings)
    application = await build_application(settings)
    try:
        me = await application.bot.get_me()
        logger.info("Starting bot @%s (id=%s)", me.username, me.id)
        logger.info(
            "Publishing time %02d:%02d %s; database=%s (%s); owners=%s",
            settings.publish_hour,
            settings.publish_minute,
            settings.timezone,
            settings.database_backend,
            describe_database(settings),
            len(settings.owner_ids),
        )

        recovery_report = await application.recovery.recover()
        if (
            recovery_report.uncertain_messages
            or recovery_report.aborted_runs
            or recovery_report.reset_scheduled_runs
        ):
            await application.reports.send_to_owners(
                "Startup recovery completed.\n"
                f"Uncertain messages (not resent to avoid duplicates): "
                f"{recovery_report.uncertain_messages}\n"
                f"Interrupted runs marked incomplete: "
                f"{recovery_report.aborted_runs}\n"
                f"Rescheduled incomplete daily claims: "
                f"{recovery_report.reset_scheduled_runs}\n"
                "Check /history for details."
            )

        if settings.scheduler_enabled:
            application.scheduler.start()
            application.background.spawn(
                application.scheduler.catch_up_if_needed("startup"),
                name="startup-catchup",
            )
        else:
            logger.warning(
                "SCHEDULER_ENABLED=false: daily publishing is disabled; /publishnow still works."
            )

        try:
            await application.bot.delete_webhook(drop_pending_updates=False)
        except Exception as exc:
            logger.warning("Could not delete webhook (continuing with polling): %s", exc)

        await application.dispatcher.start_polling(
            application.bot,
            allowed_updates=["message", "channel_post", "chat_join_request"],
        )
    finally:
        logger.info("Shutting down")
        await application.close()


async def main() -> None:
    settings = Settings.from_env()
    await build_and_run(settings)


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        print("Copy .env.example to .env and fill in BOT_TOKEN and OWNER_IDS.")
        raise SystemExit(2) from exc
