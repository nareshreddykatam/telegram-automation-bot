"""Bounded PostgreSQL publishing worker entrypoint.

Run one budgeted pass per invocation (for example from a GitHub Actions job)::

    DATABASE_BACKEND=postgres DATABASE_URL=... python -m app.worker

The worker never runs on SQLite: that backend keeps its Phase 1 inline engine.
Expired leases are recovered before the pass starts, so a previous crashed
invocation cannot leave a destination permanently blocked.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot

from app.config import Settings
from app.db.factory import create_database, describe_database
from app.db.repositories import queue as queue_repo
from app.logging_config import setup_logging
from app.services import billing
from app.services.sender import AiogramSender
from app.services.worker import PublishingWorker

logger = logging.getLogger(__name__)


async def recover_expired_leases(db) -> int:
    """Recover every currently expired job lease, newest expiry first."""

    recovered = 0
    while True:
        outcome = await queue_repo.recover_expired_job(db)
        if outcome is None:
            return recovered
        recovered += 1
        logger.warning(
            "Recovered expired job %s for destination %s: %s",
            outcome.job_id,
            outcome.destination_id,
            outcome.action,
        )


async def run_worker(settings: Settings) -> int:
    if settings.database_backend != "postgres":
        logger.error(
            "Phase 2 worker requires DATABASE_BACKEND=postgres; SQLite uses the "
            "legacy inline engine."
        )
        return 2

    setup_logging(settings)
    db = create_database(settings)
    await db.connect()
    bot = Bot(token=settings.bot_token)
    try:
        logger.info("Worker database: %s", describe_database(settings))
        reaped = await billing.reap_abandoned_runs(db)
        if reaped:
            logger.warning("Charged %s abandoned worker run(s) at the timeout cap", reaped)
        recovered = await recover_expired_leases(db)
        if recovered:
            logger.warning("Recovered %s expired job lease(s)", recovered)

        report = await PublishingWorker(db, AiogramSender(bot), settings).run()
        logger.info(
            "Worker finished: admitted=%s jobs=%s attempts=%s completed=%s "
            "retry=%s uncertain=%s stopped=%s charged=%ss errors=%s",
            report.admitted,
            report.claimed_jobs,
            report.send_attempts,
            report.completed_jobs,
            report.retry_jobs,
            report.uncertain_messages,
            report.stopped_reason,
            report.charged_seconds,
            report.errors,
        )
        if not report.admitted:
            logger.warning("Worker was not admitted: monthly accounting cap reached")
            return 3
        return 0
    finally:
        await bot.session.close()
        await db.close()


def main() -> int:
    settings = Settings.from_env()
    return asyncio.run(run_worker(settings))


if __name__ == "__main__":
    sys.exit(main())
