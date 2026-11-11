"""Daily scheduler, catch-up and pause/resume orchestration."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import Settings
from app.constants import RUN_DISABLED
from app.db.database import Database
from app.db.models import RunResult
from app.db.repositories import destinations as destinations_repo
from app.db.repositories import scheduler as scheduler_repo
from app.services.publishing import PublishingService
from app.services.reports import ReportService
from app.utils.time import format_local, local_now, scheduled_target_date

logger = logging.getLogger(__name__)


class PublishingScheduler:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        publishing: PublishingService,
        reports: ReportService,
    ) -> None:
        self._db = db
        self._settings = settings
        self._publishing = publishing
        self._reports = reports
        self._cycle_lock = asyncio.Lock()
        self._scheduler = AsyncIOScheduler(timezone=settings.tzinfo)

    def start(self) -> None:
        if not self._settings.scheduler_enabled:
            logger.warning("Scheduler disabled by configuration")
            return
        self._scheduler.add_job(
            self._scheduled_job,
            CronTrigger(
                hour=self._settings.publish_hour,
                minute=self._settings.publish_minute,
                timezone=self._settings.tzinfo,
            ),
            id="daily_publishing",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
        self._scheduler.start()
        job = self._scheduler.get_job("daily_publishing")
        logger.info(
            "Scheduler started; next daily run: %s",
            format_local(job.next_run_time, self._settings.tzinfo),
        )

    async def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def next_run_time(self) -> datetime | None:
        if not self._scheduler.running:
            return None
        job = self._scheduler.get_job("daily_publishing")
        return job.next_run_time if job else None

    async def _scheduled_job(self) -> None:
        today = local_now(self._settings.tzinfo).date().isoformat()
        try:
            await self.run_cycle(today, "schedule")
        except Exception:
            logger.exception("Scheduled publishing cycle failed")

    async def run_cycle(self, schedule_date: str, trigger: str) -> list[RunResult]:
        if await scheduler_repo.is_globally_paused(self._db):
            logger.info("Publishing cycle %s skipped: globally paused", schedule_date)
            return []
        if self._cycle_lock.locked():
            logger.warning(
                "Publishing cycle %s skipped: another cycle is already running",
                schedule_date,
            )
            return []

        async with self._cycle_lock:
            logger.info("Starting publishing cycle date=%s trigger=%s", schedule_date, trigger)
            destinations = await destinations_repo.list_destinations(self._db, enabled_only=True)
            results: list[RunResult] = []
            all_success = True

            for destination in destinations:
                claimed = await scheduler_repo.claim_scheduled_run(
                    self._db,
                    schedule_date=schedule_date,
                    destination_id=destination.id,
                    trigger=trigger,
                )
                if not claimed:
                    logger.info(
                        "Destination %s already completed cycle %s",
                        destination.chat_id,
                        schedule_date,
                    )
                    continue

                result = await self._publishing.publish_destination(destination.id, trigger=trigger)
                results.append(result)
                success = self._reports.is_cycle_success(result) or (result.status == RUN_DISABLED)
                if not success:
                    all_success = False
                await scheduler_repo.finish_scheduled_run(
                    self._db,
                    schedule_date=schedule_date,
                    destination_id=destination.id,
                    run_id=result.run_id,
                    success=success,
                    error=result.error,
                )

            if results and all_success:
                await scheduler_repo.set_last_cycle_date(self._db, schedule_date)
            elif not results:
                # Nothing was due; record the date so startup catch-up does not
                # loop on an empty cycle.
                await scheduler_repo.set_last_cycle_date(self._db, schedule_date)

            report = self._reports.format_cycle_report(schedule_date, results, trigger)
            await self._reports.send_to_owners(report)
            logger.info(
                "Finished publishing cycle date=%s results=%s all_success=%s",
                schedule_date,
                len(results),
                all_success,
            )
            return results

    async def catch_up_if_needed(self, trigger: str = "catchup") -> list[RunResult]:
        if not self._settings.catchup_on_startup and trigger == "startup":
            logger.info("Startup catch-up disabled")
            return []
        if await scheduler_repo.is_globally_paused(self._db):
            logger.info("Catch-up skipped: publishing is paused")
            return []

        target = scheduled_target_date(
            self._settings.tzinfo,
            self._settings.publish_hour,
            self._settings.publish_minute,
        )
        last_raw = await scheduler_repo.get_last_cycle_date(self._db)
        last_date: date | None = None
        if last_raw:
            try:
                last_date = date.fromisoformat(last_raw)
            except ValueError:
                logger.warning("Invalid last_cycle_date in database: %r", last_raw)

        if last_date is not None and last_date >= target:
            logger.info(
                "No catch-up needed; last completed cycle %s >= target %s",
                last_date,
                target,
            )
            return []

        if last_date is None and trigger == "startup":
            logger.info(
                "No completed cycle is recorded yet; skipping automatic startup "
                "publishing. The normal schedule or /publishnow will publish."
            )
            return []

        if last_date is None:
            missed_dates = [target]
        else:
            missed_dates = []
            cursor = last_date + timedelta(days=1)
            while cursor <= target:
                missed_dates.append(cursor)
                cursor += timedelta(days=1)

        if len(missed_dates) > self._settings.max_catchup_cycles:
            logger.warning(
                "Catch-up capped at %s cycles; %s older missed dates will remain as queue backlog",
                self._settings.max_catchup_cycles,
                len(missed_dates) - self._settings.max_catchup_cycles,
            )
            missed_dates = missed_dates[: self._settings.max_catchup_cycles]

        logger.info(
            "Catching up missed cycles: %s to %s (%s cycle(s)); last_completed=%s trigger=%s",
            missed_dates[0],
            missed_dates[-1],
            len(missed_dates),
            last_date,
            trigger,
        )
        all_results: list[RunResult] = []
        for missed_date in missed_dates:
            results = await self.run_cycle(missed_date.isoformat(), trigger)
            all_results.extend(results)
            if not results and await scheduler_repo.is_globally_paused(self._db):
                break
        return all_results
