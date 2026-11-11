"""Owner-only Telegram administration panel."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.config import Settings
from app.constants import HISTORY_PAGE_SIZE_DEFAULT, HISTORY_PAGE_SIZE_MAX
from app.db.database import Database
from app.db.repositories import destinations as destinations_repo
from app.db.repositories import moderation as moderation_repo
from app.db.repositories import publishing as publishing_repo
from app.db.repositories import scheduler as scheduler_repo
from app.db.repositories import sources as sources_repo
from app.scheduler.scheduler import PublishingScheduler
from app.services.background import BackgroundTasks
from app.services.join_requests import JoinRequestService
from app.services.publishing import PublishingService
from app.services.reports import ReportService
from app.utils.errors import CommandError
from app.utils.time import format_local, local_now

logger = logging.getLogger(__name__)

router = Router(name="admin")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _resolve_chat(bot: Bot, raw: str) -> Any:
    raw = raw.strip()
    if not raw:
        raise CommandError("A chat ID or @username is required.")
    try:
        target: int | str = int(raw) if raw.lstrip("-").isdigit() else raw
        return await bot.get_chat(target)
    except Exception as exc:
        raise CommandError(f"Could not resolve chat {raw}: {exc}") from exc


async def _require_bot_admin(bot: Bot, chat_id: int) -> Any:
    try:
        member = await bot.get_chat_member(chat_id, bot.id)
    except Exception as exc:
        raise CommandError(
            "I could not verify my administrator status. Add the bot to the "
            f"chat as an administrator first. Details: {exc}"
        ) from exc
    status = getattr(member, "status", None)
    value = getattr(status, "value", status)
    if value not in {"creator", "administrator"}:
        raise CommandError(
            "The bot must be an administrator in this chat before it can be configured."
        )
    return member


async def _find_destination(db: Database, bot: Bot, raw: str) -> Any:
    raw = raw.strip()
    if not raw:
        raise CommandError("A destination chat ID is required.")
    destination = None
    if raw.lstrip("-").isdigit():
        destination = await destinations_repo.get_by_chat_id(db, int(raw))
    if destination is None:
        chat = await _resolve_chat(bot, raw)
        destination = await destinations_repo.get_by_chat_id(db, int(chat.id))
    if destination is None:
        raise CommandError(f"Destination not configured: {raw}")
    return destination


def _parse_positive_int(raw: str, label: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise CommandError(f"{label} must be an integer.") from exc
    if value < 1:
        raise CommandError(f"{label} must be at least 1.")
    return value


def _chat_label(chat_id: int, title: str | None) -> str:
    return f"{title} ({chat_id})" if title else str(chat_id)


# ---------------------------------------------------------------------------
# Sources and destinations
# ---------------------------------------------------------------------------


@router.message(Command("addsource"))
async def cmd_addsource(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    if not command.args:
        raise CommandError("Usage: /addsource <chat_id|@username> [title]")
    parts = command.args.strip().split(maxsplit=1)
    chat = await _resolve_chat(bot, parts[0])
    if chat.type not in {ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP}:
        raise CommandError("A source must be a channel, group, or supergroup.")
    await _require_bot_admin(bot, int(chat.id))
    title = parts[1].strip() if len(parts) > 1 else getattr(chat, "title", None)
    source = await sources_repo.add_source(
        db,
        chat_id=int(chat.id),
        title=title,
        created_by=int(message.from_user.id) if message.from_user else 0,
    )
    await message.answer(
        "Source configured:\n"
        f"{_chat_label(source.chat_id, source.title)}\n\n"
        "Only messages posted from now on are indexed. Telegram does not allow "
        "a bot to fetch a private channel's historical posts. Upload content "
        "after adding the source; the bot never downloads media."
    )


@router.message(Command("adddest"))
async def cmd_adddest(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    settings: Settings,
) -> None:
    if not command.args:
        raise CommandError(
            "Usage: /adddest <chat_id|@username> [batch_size] [start_sequence]\n"
            "Example: /adddest -1001111111111 100 201"
        )
    parts = command.args.strip().split()
    chat = await _resolve_chat(bot, parts[0])
    if chat.type not in {ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP}:
        raise CommandError("A destination must be a channel, group, or supergroup.")

    batch_size = settings.default_batch_size
    if len(parts) >= 2:
        batch_size = _parse_positive_int(parts[1], "Batch size")
    if batch_size > settings.max_batch_size:
        raise CommandError(
            f"Batch size cannot exceed {settings.max_batch_size}. "
            "Change MAX_BATCH_SIZE in the environment if needed."
        )
    start_sequence = 1
    if len(parts) >= 3:
        start_sequence = _parse_positive_int(parts[2], "Start sequence")

    member = await _require_bot_admin(bot, int(chat.id))
    if chat.type == ChatType.CHANNEL and getattr(member, "can_post_messages", None) is False:
        raise CommandError(
            "The bot administrator account lacks the 'Post Messages' permission in this channel."
        )

    destination, created = await destinations_repo.add_destination(
        db,
        chat_id=int(chat.id),
        title=getattr(chat, "title", None),
        batch_size=batch_size,
        start_sequence=start_sequence,
        created_by=int(message.from_user.id) if message.from_user else 0,
    )
    queue = await destinations_repo.get_queue(db, destination.id)
    cursor = queue.next_sequence if queue else start_sequence
    if created:
        text = (
            "Destination added:\n"
            f"{_chat_label(destination.chat_id, destination.title)}\n"
            f"Batch size: {destination.batch_size}\n"
            f"Independent queue starts at source sequence: {cursor}"
        )
    else:
        text = (
            "Destination already existed and was re-enabled. Its queue cursor "
            f"was preserved at {cursor}. Batch size updated to "
            f"{destination.batch_size}."
        )
    await message.answer(text)


@router.message(Command("removedest"))
async def cmd_removedest(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    if not command.args:
        raise CommandError("Usage: /removedest <chat_id>")
    destination = await _find_destination(db, bot, command.args.strip().split()[0])
    await destinations_repo.set_enabled(db, destination.chat_id, False)
    await message.answer(
        "Destination disabled (history and queue cursor retained):\n"
        f"{_chat_label(destination.chat_id, destination.title)}\n"
        "Use /adddest with the same chat ID to re-enable it."
    )


@router.message(Command("setbatch"))
async def cmd_setbatch(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    settings: Settings,
) -> None:
    if not command.args or len(command.args.split()) < 2:
        raise CommandError("Usage: /setbatch <chat_id> <size>")
    parts = command.args.split()
    destination = await _find_destination(db, bot, parts[0])
    size = _parse_positive_int(parts[1], "Batch size")
    if size > settings.max_batch_size:
        raise CommandError(f"Batch size cannot exceed {settings.max_batch_size}.")
    await destinations_repo.set_batch_size(db, destination.chat_id, size)
    await message.answer(
        f"Batch size for {_chat_label(destination.chat_id, destination.title)} "
        f"is now {size}. The queue cursor was not changed."
    )


# ---------------------------------------------------------------------------
# Pause / resume
# ---------------------------------------------------------------------------


@router.message(Command("pause"))
async def cmd_pause(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
) -> None:
    if command.args:
        destination = await _find_destination(db, bot, command.args.split()[0])
        await destinations_repo.set_paused(db, destination.chat_id, True)
        await message.answer(
            f"Paused {_chat_label(destination.chat_id, destination.title)}. "
            "An in-flight batch stops before its next message."
        )
        return
    await scheduler_repo.set_globally_paused(
        db, True, int(message.from_user.id) if message.from_user else None
    )
    await message.answer(
        "All publishing is paused. Scheduled and manual batches will not start. "
        "An in-flight batch stops before its next message. Use /resume."
    )


@router.message(Command("resume"))
async def cmd_resume(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    publishing: PublishingService,
    scheduler: PublishingScheduler,
    reports: ReportService,
    background: BackgroundTasks,
) -> None:
    if command.args:
        destination = await _find_destination(db, bot, command.args.split()[0])
        await destinations_repo.set_paused(db, destination.chat_id, False)

        async def _resume_destination() -> None:
            result = await publishing.publish_destination(destination.id, trigger="resume")
            await reports.send_to_owners(reports.format_run_report(result))

        background.spawn(_resume_destination(), name="resume-destination")
        await message.answer(
            f"Resumed {_chat_label(destination.chat_id, destination.title)}. "
            "A catch-up batch is running in the background."
        )
        return

    await scheduler_repo.set_globally_paused(
        db, False, int(message.from_user.id) if message.from_user else None
    )
    background.spawn(scheduler.catch_up_if_needed("resume"), name="resume-catchup")
    await message.answer(
        "Publishing resumed. The bot is checking for a missed daily batch now. "
        "A report will be sent when it finishes."
    )


# ---------------------------------------------------------------------------
# Status / history
# ---------------------------------------------------------------------------


@router.message(Command("status"))
async def cmd_status(
    message: Message,
    db: Database,
    settings: Settings,
    scheduler: PublishingScheduler,
    background: BackgroundTasks,
    started_at: datetime,
) -> None:
    globally_paused = await scheduler_repo.is_globally_paused(db)
    last_cycle = await scheduler_repo.get_last_cycle_date(db)
    next_run = scheduler.next_run_time()
    sources = await sources_repo.list_sources(db, enabled_only=False)
    destination_rows = await destinations_repo.list_with_queue(db)
    now = datetime.now(UTC)

    lines = [
        "Bot status",
        f"Timezone: {settings.timezone}",
        f"Daily publish: {settings.publish_hour:02d}:{settings.publish_minute:02d}",
        f"Next scheduled run: {format_local(next_run, settings.tzinfo)}",
        f"Last completed cycle: {last_cycle or 'none'}",
        f"Global pause: {'YES' if globally_paused else 'no'}",
        f"Active background jobs: {background.active_count}",
        f"Uptime: {_format_uptime(now - started_at)}",
        "",
        "Sources:",
    ]
    if not sources:
        lines.append("- none")
    for source in sources[:20]:
        count = await sources_repo.message_count(db, source.id)
        state = "enabled" if source.enabled else "disabled"
        lines.append(
            f"- {_chat_label(source.chat_id, source.title)} | {state} | "
            f"indexed={count} | autoapprove={'yes' if source.auto_approve else 'no'}"
        )
    if len(sources) > 20:
        lines.append(f"- ... and {len(sources) - 20} more")

    lines.append("")
    lines.append("Destinations:")
    if not destination_rows:
        lines.append("- none")
    for destination, queue in destination_rows[:20]:
        cursor = queue.next_sequence if queue else 1
        pending = await publishing_repo.pending_count(
            db, destination_id=destination.id, next_sequence=cursor
        )
        states = []
        if not destination.enabled:
            states.append("disabled")
        if destination.paused:
            states.append("paused")
        state = ",".join(states) if states else "active"
        lines.append(
            f"- {_chat_label(destination.chat_id, destination.title)} | {state} | "
            f"batch={destination.batch_size} | cursor={cursor} | "
            f"pending={pending} | autoapprove="
            f"{'yes' if destination.auto_approve else 'no'}"
        )
    if len(destination_rows) > 20:
        lines.append(f"- ... and {len(destination_rows) - 20} more")

    await message.answer(_truncate_status("\n".join(lines)))


@router.message(Command("history"))
async def cmd_history(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    reports: ReportService,
) -> None:
    destination = None
    limit = HISTORY_PAGE_SIZE_DEFAULT
    tokens = command.args.split() if command.args else []
    if len(tokens) >= 2:
        destination = await _find_destination(db, bot, tokens[0])
        limit = _parse_positive_int(tokens[1], "Limit")
    elif len(tokens) == 1:
        token = tokens[0]
        if token.lstrip("-").isdigit() and int(token) <= HISTORY_PAGE_SIZE_MAX:
            limit = _parse_positive_int(token, "Limit")
        else:
            destination = await _find_destination(db, bot, token)
    limit = min(limit, HISTORY_PAGE_SIZE_MAX)

    runs = await publishing_repo.list_runs(
        db, destination_id=destination.id if destination else None, limit=limit
    )
    text = reports.format_history(runs)
    failures = await publishing_repo.recent_failures(
        db, destination_id=destination.id if destination else None, limit=10
    )
    if failures:
        text += "\n\nRecent failed/uncertain messages:\n"
        for failure in failures:
            text += (
                f"- source #{failure.source_message_id} "
                f"(msg {failure.source_telegram_message_id}) "
                f"[{failure.status}]: {failure.error or 'unknown error'}\n"
            )
    await message.answer(_truncate_status(text))


@router.message(Command("publishnow"))
async def cmd_publishnow(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    settings: Settings,
    publishing: PublishingService,
    reports: ReportService,
    background: BackgroundTasks,
) -> None:
    if await scheduler_repo.is_globally_paused(db):
        raise CommandError("Publishing is paused. Use /resume first.")
    if command.args:
        destination = await _find_destination(db, bot, command.args.split()[0])
        destination_ids = [destination.id]
        if destination.paused:
            raise CommandError("That destination is paused. Use /resume first.")
    else:
        destinations = await destinations_repo.list_destinations(db, enabled_only=True)
        destination_ids = [destination.id for destination in destinations if not destination.paused]
    if not destination_ids:
        raise CommandError("No active destinations are available.")

    schedule_date = local_now(settings.tzinfo).date().isoformat()

    async def _run() -> None:
        results = []
        try:
            for destination_id in destination_ids:
                results.append(
                    await publishing.publish_destination(destination_id, trigger="manual")
                )
            report = reports.format_cycle_report(schedule_date, results, "manual")
            await reports.send_to_owners(report)
        except Exception as exc:
            logger.exception("Manual publishing failed")
            await reports.send_to_owners(f"Manual publishing failed: {exc}")

    background.spawn(_run(), name="publish-now")
    await message.answer(
        f"Manual publishing started for {len(destination_ids)} destination(s). "
        "This runs in the background; a report will be sent when it finishes."
    )


# ---------------------------------------------------------------------------
# Moderation and join requests
# ---------------------------------------------------------------------------


@router.message(Command("linkprotection"))
async def cmd_linkprotection(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
) -> None:
    tokens = command.args.split() if command.args else []
    action = tokens[0].lower() if tokens else "status"
    target_raw = tokens[1] if len(tokens) > 1 else None

    if target_raw:
        chat = await _resolve_chat(bot, target_raw)
    elif message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        chat = message.chat
    else:
        raise CommandError(
            "Usage: /linkprotection <on|off|status> <chat_id>\n"
            "Inside a group you can omit the chat ID."
        )

    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        raise CommandError("Link protection only applies to groups/supergroups.")
    if action not in {"on", "off", "status"}:
        raise CommandError("Usage: /linkprotection <on|off|status> [chat_id]")

    if action == "status":
        current = await moderation_repo.get_settings(db, int(chat.id))
        state = "ON" if current and current.link_protection else "OFF"
        await message.answer(
            f"Link protection for {_chat_label(int(chat.id), getattr(chat, 'title', None))}: "
            f"{state}"
        )
        return

    member = await _require_bot_admin(bot, int(chat.id))
    if getattr(member, "can_delete_messages", None) is False:
        raise CommandError(
            "The bot lacks the 'Delete Messages' administrator permission in this chat."
        )
    enabled = action == "on"
    await moderation_repo.upsert_settings(db, int(chat.id), enabled)
    await message.answer(
        f"Link protection is now {'ON' if enabled else 'OFF'} for "
        f"{_chat_label(int(chat.id), getattr(chat, 'title', None))}.\n"
        "Administrator messages are never deleted by this rule. Every action is logged."
    )


@router.message(Command("autoapprove"))
async def cmd_autoapprove(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
) -> None:
    if not command.args:
        raise CommandError("Usage: /autoapprove <on|off> <chat_id|@username>")
    tokens = command.args.split()
    if len(tokens) < 2:
        raise CommandError("Usage: /autoapprove <on|off> <chat_id|@username>")
    action = tokens[0].lower()
    if action not in {"on", "off"}:
        raise CommandError("Usage: /autoapprove <on|off> <chat_id|@username>")
    chat = await _resolve_chat(bot, tokens[1])
    enabled = action == "on"
    destination = await destinations_repo.get_by_chat_id(db, int(chat.id))
    source = await sources_repo.get_by_chat_id(db, int(chat.id))
    if destination is None and source is None:
        raise CommandError("That chat is neither a configured source nor destination.")
    if destination:
        await destinations_repo.set_auto_approve(db, int(chat.id), enabled)
    if source:
        await sources_repo.set_auto_approve(db, int(chat.id), enabled)
    await message.answer(
        f"Auto-approval for new join requests is now "
        f"{'ON' if enabled else 'OFF'} for "
        f"{_chat_label(int(chat.id), getattr(chat, 'title', None))}."
    )


@router.message(Command("approvepending"))
async def cmd_approvepending(
    message: Message,
    command: CommandObject,
    bot: Bot,
    db: Database,
    join_service: JoinRequestService,
) -> None:
    chat_id: int | None = None
    if command.args:
        target = command.args.split()[0]
        try:
            destination = await _find_destination(db, bot, target)
            chat_id = destination.chat_id
        except CommandError:
            chat = await _resolve_chat(bot, target)
            source = await sources_repo.get_by_chat_id(db, int(chat.id))
            if source is None:
                raise CommandError("That chat is not a configured source or destination.") from None
            chat_id = source.chat_id
    summary = await join_service.approve_pending(chat_id=chat_id)
    await message.answer(
        "Join request retry finished:\n"
        f"Attempted: {summary.attempted}\n"
        f"Approved: {summary.approved}\n"
        f"Failed: {summary.failed}\n\n"
        "Telegram does not provide a method to list pending join requests. This "
        "command retries requests the bot previously observed and logged."
    )


# ---------------------------------------------------------------------------
# Local formatting helpers
# ---------------------------------------------------------------------------


def _format_uptime(delta) -> str:
    total_seconds = max(0, int(delta.total_seconds()))
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    return f"{minutes}m {seconds}s"


def _truncate_status(text: str, limit: int = 3900) -> str:
    return text if len(text) <= limit else text[: limit - 15] + "\n... (truncated)"
