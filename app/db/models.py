"""Typed records returned by the repository layer."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


def _value(row: Mapping[str, Any] | None, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


@dataclass(frozen=True, slots=True)
class Admin:
    id: int
    telegram_id: int
    role: str
    label: str | None
    is_active: bool
    created_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Admin:
        return cls(
            id=int(row["id"]),
            telegram_id=int(row["telegram_id"]),
            role=str(row["role"]),
            label=row["label"],
            is_active=bool(row["is_active"]),
            created_at=str(row["created_at"]),
        )


@dataclass(frozen=True, slots=True)
class SourceChat:
    id: int
    chat_id: int
    title: str | None
    enabled: bool
    auto_approve: bool
    created_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> SourceChat:
        return cls(
            id=int(row["id"]),
            chat_id=int(row["chat_id"]),
            title=row["title"],
            enabled=bool(row["enabled"]),
            auto_approve=bool(row["auto_approve"]),
            created_at=str(row["created_at"]),
        )


@dataclass(frozen=True, slots=True)
class SourceMessage:
    id: int
    source_chat_id: int
    chat_id: int
    message_id: int
    media_group_id: str | None
    content_type: str
    has_media: bool
    caption: str | None
    text_content: str | None
    message_date: str | None
    indexed_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> SourceMessage:
        return cls(
            id=int(row["id"]),
            source_chat_id=int(row["source_chat_id"]),
            chat_id=int(row["chat_id"]),
            message_id=int(row["message_id"]),
            media_group_id=row["media_group_id"],
            content_type=str(row["content_type"]),
            has_media=bool(row["has_media"]),
            caption=row["caption"],
            text_content=row["text_content"],
            message_date=row["message_date"],
            indexed_at=str(row["indexed_at"]),
        )


@dataclass(frozen=True, slots=True)
class Destination:
    id: int
    chat_id: int
    title: str | None
    batch_size: int
    auto_approve: bool
    enabled: bool
    paused: bool
    created_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Destination:
        return cls(
            id=int(row["id"]),
            chat_id=int(row["chat_id"]),
            title=row["title"],
            batch_size=int(row["batch_size"]),
            auto_approve=bool(row["auto_approve"]),
            enabled=bool(row["enabled"]),
            paused=bool(row["paused"]),
            created_at=str(row["created_at"]),
        )


@dataclass(frozen=True, slots=True)
class QueueState:
    destination_id: int
    next_sequence: int
    last_success_sequence: int | None
    updated_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> QueueState:
        return cls(
            destination_id=int(row["destination_id"]),
            next_sequence=int(row["next_sequence"]),
            last_success_sequence=(
                int(row["last_success_sequence"])
                if row["last_success_sequence"] is not None
                else None
            ),
            updated_at=str(row["updated_at"]),
        )


@dataclass(frozen=True, slots=True)
class Candidate:
    source: SourceMessage
    history_id: int | None
    history_status: str | None
    attempts: int


@dataclass(frozen=True, slots=True)
class PublishingRun:
    id: str
    destination_id: int
    trigger: str
    status: str
    planned_count: int
    success_count: int
    failed_count: int
    uncertain_count: int
    skipped_count: int
    started_at: str
    finished_at: str | None
    error: str | None
    destination_title: str | None = None
    destination_chat_id: int | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> PublishingRun:
        return cls(
            id=str(row["id"]),
            destination_id=int(row["destination_id"]),
            trigger=str(row["trigger"]),
            status=str(row["status"]),
            planned_count=int(row["planned_count"]),
            success_count=int(row["success_count"]),
            failed_count=int(row["failed_count"]),
            uncertain_count=int(row["uncertain_count"]),
            skipped_count=int(row["skipped_count"]),
            started_at=str(row["started_at"]),
            finished_at=row["finished_at"],
            error=row["error"],
            destination_title=_value(row, "destination_title"),
            destination_chat_id=(
                int(row["destination_chat_id"])
                if _value(row, "destination_chat_id") is not None
                else None
            ),
        )


@dataclass(slots=True)
class FailedMessage:
    source_message_id: int
    source_telegram_message_id: int
    source_chat_id: int
    error: str | None
    status: str


@dataclass(slots=True)
class RunResult:
    run_id: str
    destination_id: int
    destination_chat_id: int
    destination_title: str | None
    trigger: str
    status: str
    planned_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    uncertain_count: int = 0
    skipped_count: int = 0
    next_sequence: int | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    failures: list[FailedMessage] = field(default_factory=list)

    @property
    def processed_count(self) -> int:
        return self.success_count + self.failed_count + self.uncertain_count


@dataclass(frozen=True, slots=True)
class ModerationSettings:
    chat_id: int
    link_protection: bool
    updated_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ModerationSettings:
        return cls(
            chat_id=int(row["chat_id"]),
            link_protection=bool(row["link_protection"]),
            updated_at=str(row["updated_at"]),
        )


@dataclass(frozen=True, slots=True)
class JoinRequestLog:
    id: int
    chat_id: int
    user_id: int
    username: str | None
    first_name: str | None
    chat_title: str | None
    action: str
    error: str | None
    attempts: int
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> JoinRequestLog:
        return cls(
            id=int(row["id"]),
            chat_id=int(row["chat_id"]),
            user_id=int(row["user_id"]),
            username=row["username"],
            first_name=row["first_name"],
            chat_title=row["chat_title"],
            action=str(row["action"]),
            error=row["error"],
            attempts=int(row["attempts"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
