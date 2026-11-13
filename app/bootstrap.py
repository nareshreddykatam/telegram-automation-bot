"""Shared bot wiring for polling and serverless webhook runtimes.

The dispatcher, handlers, services and middleware must be identical in both
deployment modes. Keeping the construction here prevents the Vercel webhook from
drifting away from the long-polling entrypoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from app.config import Settings
from app.db.base import DatabaseBackend
from app.db.factory import create_database
from app.db.repositories import admins as admins_repo
from app.filters.database import LinkProtectionFilter, SourceChatFilter
from app.handlers import admin as admin_handlers
from app.handlers import errors as error_handlers
from app.handlers import join_requests as join_handlers
from app.handlers import moderation as moderation_handlers
from app.handlers import public as public_handlers
from app.handlers import source as source_handlers
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


@dataclass(slots=True)
class BotApplication:
    """Fully wired application resources with an explicit shutdown path."""

    settings: Settings
    db: DatabaseBackend
    bot: Bot
    dispatcher: Dispatcher
    scheduler: PublishingScheduler
    background: BackgroundTasks
    reports: ReportService
    recovery: RecoveryService
    started_at: datetime

    async def close(self) -> None:
        await self.background.shutdown()
        await self.scheduler.shutdown()
        try:
            await self.bot.session.close()
        finally:
            await self.db.close()


async def build_application(settings: Settings) -> BotApplication:
    """Connect the database and wire handlers without starting any transport."""

    db = create_database(settings)
    await db.connect()

    bot: Bot | None = None
    try:
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

        dp = Dispatcher()
        dp.update.middleware(
            DependencyMiddleware(
                db=db,
                settings=settings,
                bot=bot,
                started_at=started_at,
                publishing=publishing,
                moderation=moderation,
                ingest=ingest,
                join_service=join_service,
                reports=reports,
                scheduler=scheduler,
                background=background,
            )
        )

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

        return BotApplication(
            settings=settings,
            db=db,
            bot=bot,
            dispatcher=dp,
            scheduler=scheduler,
            background=background,
            reports=reports,
            recovery=recovery,
            started_at=started_at,
        )
    except BaseException:
        if bot is not None:
            await bot.session.close()
        await db.close()
        raise


__all__ = ["BotApplication", "build_application"]
