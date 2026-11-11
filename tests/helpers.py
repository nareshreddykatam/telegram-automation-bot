"""Test helpers: fakes and source seeding."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from app.db.database import Database
from app.db.models import Destination, SourceChat
from app.db.repositories import destinations as destinations_repo
from app.db.repositories import messages as messages_repo
from app.db.repositories import sources as sources_repo
from app.services.sender import FloodWaitError, PermanentSendError, TransientSendError

SOURCE_CHAT_ID = -1001111111111
DEST_CHAT_ID = -1002222222222


async def create_source(
    db: Database,
    *,
    count: int,
    chat_id: int = SOURCE_CHAT_ID,
    album_every: int = 0,
    album_size: int = 3,
) -> SourceChat:
    source = await sources_repo.add_source(db, chat_id=chat_id, title="Test source", created_by=1)
    for index in range(1, count + 1):
        media_group_id = None
        if album_every and ((index - 1) % album_every) < album_size:
            media_group_id = f"album-{(index - 1) // album_every}"
        await messages_repo.index_message(
            db,
            source_chat_id=source.id,
            chat_id=chat_id,
            message_id=index,
            media_group_id=media_group_id,
            content_type="text",
            has_media=False,
            caption=None,
            text_content=f"post {index}",
            message_date=None,
        )
    return source


async def create_destination(
    db: Database,
    *,
    chat_id: int = DEST_CHAT_ID,
    batch_size: int = 100,
    start_sequence: int = 1,
) -> Destination:
    destination, _ = await destinations_repo.add_destination(
        db,
        chat_id=chat_id,
        title=f"Destination {chat_id}",
        batch_size=batch_size,
        start_sequence=start_sequence,
        created_by=1,
    )
    return destination


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[int, int, int]] = []
        self.albums: list[tuple[int, int, tuple[int, ...]]] = []
        self.permanent_failures: dict[int, str] = {}
        self.transient_failures: dict[int, int] = {}
        self.flood_failures: dict[int, int] = {}
        self._next_id = 10_000

    def _raise_if_needed(self, message_id: int) -> None:
        remaining = self.flood_failures.get(message_id, 0)
        if remaining > 0:
            self.flood_failures[message_id] = remaining - 1
            raise FloodWaitError(0, "simulated flood")
        remaining = self.transient_failures.get(message_id, 0)
        if remaining > 0:
            self.transient_failures[message_id] = remaining - 1
            raise TransientSendError("simulated transient failure")
        if message_id in self.permanent_failures:
            raise PermanentSendError(self.permanent_failures[message_id])

    async def copy_message(self, *, chat_id: int, from_chat_id: int, message_id: int) -> int:
        self._raise_if_needed(message_id)
        self.sent.append((chat_id, from_chat_id, message_id))
        self._next_id += 1
        return self._next_id

    async def copy_messages(
        self, *, chat_id: int, from_chat_id: int, message_ids: list[int]
    ) -> list[int]:
        for message_id in message_ids:
            self._raise_if_needed(message_id)
        self.albums.append((chat_id, from_chat_id, tuple(message_ids)))
        results = []
        for message_id in message_ids:
            self.sent.append((chat_id, from_chat_id, message_id))
            self._next_id += 1
            results.append(self._next_id)
        return results


class FakeBot:
    id = 999_000_999

    def __init__(self) -> None:
        self.sent_messages: list[tuple[int, str]] = []
        self.deleted: list[tuple[int, int]] = []
        self.approvals: list[tuple[int, int]] = []
        self.member_status: dict[tuple[int, int], str] = {}
        self.approve_failures = 0

    async def send_message(self, chat_id: int, text: str) -> Any:
        self.sent_messages.append((chat_id, text))
        return SimpleNamespace(message_id=1)

    async def get_chat_member(self, chat_id: int, user_id: int) -> Any:
        status = self.member_status.get((chat_id, user_id), "member")
        if callable(status):
            return status()
        return SimpleNamespace(status=status)

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        self.deleted.append((chat_id, message_id))
        return True

    async def approve_chat_join_request(self, chat_id: int, user_id: int) -> bool:
        if self.approve_failures:
            self.approve_failures -= 1
            raise RuntimeError("simulated approval failure")
        self.approvals.append((chat_id, user_id))
        return True

    async def get_chat(self, chat_id: int | str) -> Any:
        return SimpleNamespace(
            id=chat_id if isinstance(chat_id, int) else 123,
            type="supergroup",
            title="Fake Chat",
        )


@dataclass
class FakeUser:
    id: int
    is_bot: bool = False
    username: str | None = "user"
    first_name: str | None = "User"


@dataclass
class FakeChat:
    id: int
    type: str = "supergroup"
    title: str = "Test Group"


@dataclass
class FakeMessage:
    chat: FakeChat
    message_id: int
    from_user: FakeUser | None = None
    text: str | None = None
    caption: str | None = None
    content_type: str = "text"
    entities: list[Any] | None = None
    caption_entities: list[Any] | None = None
    media_group_id: str | None = None
    date: Any = None
    sender_chat: Any = None


@dataclass
class FakeEntity:
    type: str
    url: str | None = None
