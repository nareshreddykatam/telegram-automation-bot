"""Admin-facing publishing reports."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from app.config import Settings
from app.constants import (
    RUN_COMPLETED,
    RUN_COMPLETED_WITH_ERRORS,
    RUN_NO_MESSAGES,
)
from app.db.database import Database
from app.db.models import PublishingRun, RunResult
from app.db.repositories import admins as admins_repo
from app.utils.time import format_local

logger = logging.getLogger(__name__)

_MAX_MESSAGE_LENGTH = 3900


class ReportService:
    def __init__(self, db: Database, bot: Any, settings: Settings) -> None:
        self._db = db
        self._bot = bot
        self._settings = settings

    async def send_to_owners(self, text: str) -> None:
        if not self._settings.admin_reports:
            return
        owners = await admins_repo.owner_ids(self._db)
        for owner_id in owners:
            await self.send_to_chat(owner_id, text)

    async def send_to_chat(self, chat_id: int, text: str) -> bool:
        try:
            await self._bot.send_message(chat_id=chat_id, text=_truncate(text))
            return True
        except Exception as exc:
            logger.warning("Could not send report to %s: %s", chat_id, exc)
            return False

    def format_cycle_report(
        self, schedule_date: str, results: Iterable[RunResult], trigger: str
    ) -> str:
        results = list(results)
        lines = [
            f"Publishing report - {schedule_date} ({trigger})",
            f"Destinations processed: {len(results)}",
            "",
        ]
        if not results:
            lines.append("No enabled destinations.")
        total_ok = total_failed = total_uncertain = 0
        for result in results:
            total_ok += result.success_count
            total_failed += result.failed_count
            total_uncertain += result.uncertain_count
            lines.append(
                f"- {_destination_label(result)}: {result.status}; "
                f"ok={result.success_count}, failed={result.failed_count}, "
                f"uncertain={result.uncertain_count}, next={result.next_sequence}"
            )
            if result.error:
                lines.append(f"  note: {result.error}")
            for failure in result.failures[:5]:
                lines.append(
                    f"  ! source #{failure.source_message_id} "
                    f"(msg {failure.source_telegram_message_id}): "
                    f"{failure.status} - {failure.error or 'unknown error'}"
                )
            if len(result.failures) > 5:
                lines.append(f"  ... and {len(result.failures) - 5} more failures")
        lines.extend(
            [
                "",
                f"Totals: ok={total_ok}, failed={total_failed}, uncertain={total_uncertain}",
            ]
        )
        if total_failed or total_uncertain:
            lines.append("Use /history for details. Failed messages are never silently skipped.")
        return "\n".join(lines)

    def format_run_report(self, result: RunResult) -> str:
        lines = [
            f"Publishing finished - {_destination_label(result)}",
            f"Trigger: {result.trigger}",
            f"Status: {result.status}",
            f"Success: {result.success_count}",
            f"Failed: {result.failed_count}",
            f"Uncertain: {result.uncertain_count}",
            f"Next source sequence: {result.next_sequence}",
        ]
        if result.error:
            lines.append(f"Note: {result.error}")
        for failure in result.failures[:10]:
            lines.append(
                f"! source #{failure.source_message_id} "
                f"(msg {failure.source_telegram_message_id}): "
                f"{failure.status} - {failure.error or 'unknown error'}"
            )
        if len(result.failures) > 10:
            lines.append(f"... and {len(result.failures) - 10} more failures")
        return "\n".join(lines)

    def format_history(self, runs: list[PublishingRun]) -> str:
        if not runs:
            return "No publishing history yet."
        lines = ["Recent publishing runs:", ""]
        for run in runs:
            title = run.destination_title or f"chat {run.destination_chat_id}"
            lines.append(
                f"{format_local(run.started_at, self._settings.tzinfo)} | "
                f"{title} ({run.destination_chat_id}) | {run.trigger} | {run.status} | "
                f"ok={run.success_count} failed={run.failed_count} "
                f"uncertain={run.uncertain_count}"
            )
            if run.error:
                lines.append(f"  note: {run.error}")
        return "\n".join(lines)

    @staticmethod
    def is_cycle_success(result: RunResult) -> bool:
        return result.status in {
            RUN_COMPLETED,
            RUN_COMPLETED_WITH_ERRORS,
            RUN_NO_MESSAGES,
        }


def _destination_label(result: RunResult) -> str:
    title = result.destination_title or "destination"
    return f"{title} ({result.destination_chat_id})"


def _truncate(text: str) -> str:
    if len(text) <= _MAX_MESSAGE_LENGTH:
        return text
    return text[: _MAX_MESSAGE_LENGTH - 20] + "\n... (truncated)"
