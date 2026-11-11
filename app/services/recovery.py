"""Startup recovery for interrupted publishing and scheduled runs."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.db.database import Database
from app.db.repositories import publishing as publishing_repo
from app.db.repositories import scheduler as scheduler_repo

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    uncertain_messages: int
    aborted_runs: int
    affected_destinations: int
    reset_scheduled_runs: int


class RecoveryService:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def recover(self) -> RecoveryReport:
        summary = await publishing_repo.recover_interrupted(self._db)
        reset = await scheduler_repo.reset_running_scheduled_runs(self._db)
        report = RecoveryReport(
            uncertain_messages=summary.uncertain_messages,
            aborted_runs=summary.aborted_runs,
            affected_destinations=summary.affected_destinations,
            reset_scheduled_runs=reset,
        )
        if any(
            (
                report.uncertain_messages,
                report.aborted_runs,
                report.reset_scheduled_runs,
            )
        ):
            logger.warning("Startup recovery: %s", report)
        else:
            logger.info("Startup recovery: no interrupted work found")
        return report
