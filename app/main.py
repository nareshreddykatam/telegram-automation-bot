"""Application entrypoint: wires configuration, services, handlers and scheduler."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from app.config import ConfigError, Settings
from app.db.database import Database
from app.db.repositories import admins as admins_repo
from app.filters.database import LinkProtectionFilter, SourceChatFilter
from app.handlers import admin as admin_handlers
from app.handlers import errors as error_handlers
from app.handlers import join_requests as join_handlers
from app.handlers import moderation as moderation_handlers
from app.handlers import public as public_handlers
from app.handlers import source as source_handlers
from app.logging_config import setup_logging
from app.middlewares.admin_guard import AdminGuardMiddleware
from app.middlewares.dependencies import DependencyMiddleware
from app.scheduler.scheduler import PublishingScheduler
from app.services.background import BackgroundTasks
from app.services.ingest import SourceIngestService
from app.services.join_requests import JoinRequestService
from app.services.moderation import ModerationService
from app.services.publishing import PublishingService
from app.services.recovery import RecoveryService
from app.services.reports import ReportService
from app.services.sender import AiogramSender

logger = logging.getLogger(__name__)


async def build_and_run(settings: Settings) -> None:
    setup_logging(settings)

    db = Database(settings.database_path)
    await db.connect()
    await admins_repo.seed_owners(db, settings.owner_ids)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=None),
    )
    started_at = datetime.now(UTC)

    sender = AiogramSender(bot)
    publishing = PublishingService(db, sender, settings)
    moderation = ModerationService(db, bot)
    ingest = SourceIngestService(db)
    join_service = JoinRequestService(db, bot)
    reports = ReportService(db, bot, settings)
    scheduler = PublishingScheduler(db, settings, publishing, reports)
    recovery = RecoveryService(db)
    background = BackgroundTasks()

    dependencies = {
        "db": db,
        "settings": settings,
        "bot": bot,
        "started_at": started_at,
        "publishing": publishing,
        "moderation": moderation,
        "ingest": ingest,
        "join_service": join_service,
        "reports": reports,
        "scheduler": scheduler,
        "background": background,
    }

    dp = Dispatcher()
    dp.update.middleware(DependencyMiddleware(**dependencies))

    source_handlers.router.message.filter(SourceChatFilter(db))
    source_handlers.router.channel_post.filter(SourceChatFilter(db))
    moderation_handlers.router.message.filter(LinkProtectionFilter(db))
    admin_handlers.router.message.middleware(AdminGuardMiddleware(db))

    dp.include_router(public_handlers.router)
    dp.include_router(admin_handlers.router)
    dp.include_router(source_handlers.router)
    dp.include_router(moderation_handlers.router)
    dp.include_router(join_handlers.router)
    dp.include_router(error_handlers.router)

    try:
        me = await bot.get_me()
        logger.info("Starting bot @%s (id=%s)", me.username, me.id)
        logger.info(
            "Publishing time %02d:%02d %s; database=%s; owners=%s",
            settings.publish_hour,
            settings.publish_minute,
            settings.timezone,
            settings.database_path,
            len(settings.owner_ids),
        )

        recovery_report = await recovery.recover()
        if (
            recovery_report.uncertain_messages
            or recovery_report.aborted_runs
            or recovery_report.reset_scheduled_runs
        ):
            await reports.send_to_owners(
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
            scheduler.start()
            background.spawn(scheduler.catch_up_if_needed("startup"), name="startup-catchup")
        else:
            logger.warning(
                "SCHEDULER_ENABLED=false: daily publishing is disabled; /publishnow still works."
            )

        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except Exception as exc:
            logger.warning("Could not delete webhook (continuing with polling): %s", exc)

        await dp.start_polling(
            bot,
            allowed_updates=["message", "channel_post", "chat_join_request"],
        )
    finally:
        logger.info("Shutting down")
        await background.shutdown()
        await scheduler.shutdown()
        try:
            await bot.session.close()
        finally:
            await db.close()


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
