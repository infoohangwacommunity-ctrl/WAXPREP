"""In-memory storage for unit tests (not a production backend)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import (
    Attachment,
    ChannelIdentity,
    Conversation,
    Event,
    Message,
    Notebook,
    NotebookEntry,
    Student,
    new_id,
)


class InMemoryStorage:
    """Deterministic in-process store implementing the Storage contract."""

    def __init__(self) -> None:
        self._students: dict[WaxId, Student] = {}
        self._channels: dict[tuple[str, str], ChannelIdentity] = {}
        self._conversations: dict[tuple[WaxId, UUID], Conversation] = {}
        self._messages: dict[tuple[WaxId, UUID, UUID], Message] = {}
        self._attachments: dict[tuple[WaxId, UUID], Attachment] = {}
        self._notebooks: dict[WaxId, Notebook] = {}
        self._entries: dict[tuple[WaxId, UUID], list[NotebookEntry]] = {}
        self._events: dict[WaxId, list[Event]] = {}

    def create_student(self, student: Student) -> Student:
        if student.wax_id in self._students:
            raise ValueError("student already exists")
        self._students[student.wax_id] = student
        return student

    def get_student(self, wax_id: WaxId) -> Student | None:
        return self._students.get(wax_id)

    def create_channel_identity(self, identity: ChannelIdentity) -> ChannelIdentity:
        key = (identity.channel, identity.external_id)
        if key in self._channels:
            raise ValueError("channel identity already exists")
        self._channels[key] = identity
        return identity

    def get_channel_identity(
        self, channel: str, external_id: str
    ) -> ChannelIdentity | None:
        return self._channels.get((channel, external_id))

    def create_conversation(self, conversation: Conversation) -> Conversation:
        if conversation.wax_id not in self._students:
            raise ValueError("unknown student")
        self._conversations[(conversation.wax_id, conversation.id)] = conversation
        return conversation

    def get_conversation(
        self, wax_id: WaxId, conversation_id: UUID
    ) -> Conversation | None:
        return self._conversations.get((wax_id, conversation_id))

    def create_message(self, message: Message) -> Message:
        if (message.wax_id, message.conversation_id) not in self._conversations:
            raise ValueError("unknown conversation for student")
        key = (message.wax_id, message.conversation_id, message.id)
        self._messages[key] = message
        return message

    def get_message(
        self, wax_id: WaxId, conversation_id: UUID, message_id: UUID
    ) -> Message | None:
        return self._messages.get((wax_id, conversation_id, message_id))

    def list_messages(
        self, wax_id: WaxId, conversation_id: UUID
    ) -> tuple[Message, ...]:
        rows = [
            m
            for (w, c, _), m in self._messages.items()
            if w == wax_id and c == conversation_id
        ]
        return tuple(sorted(rows, key=lambda m: m.created_at))

    def create_attachment(self, attachment: Attachment) -> Attachment:
        found = any(
            m.id == attachment.message_id and m.wax_id == attachment.wax_id
            for m in self._messages.values()
        )
        if not found:
            raise ValueError("unknown message for student")
        self._attachments[(attachment.wax_id, attachment.id)] = attachment
        return attachment

    def get_attachment(self, wax_id: WaxId, attachment_id: UUID) -> Attachment | None:
        return self._attachments.get((wax_id, attachment_id))

    def get_or_create_notebook(self, wax_id: WaxId, now: object) -> Notebook:
        existing = self._notebooks.get(wax_id)
        if existing is not None:
            return existing
        if not isinstance(now, datetime):
            raise TypeError("now must be a datetime")
        notebook = Notebook(
            id=new_id(),
            wax_id=wax_id,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._notebooks[wax_id] = notebook
        self._entries[(wax_id, notebook.id)] = []
        return notebook

    def get_notebook(self, wax_id: WaxId) -> Notebook | None:
        return self._notebooks.get(wax_id)

    def add_notebook_entry(self, entry: NotebookEntry) -> NotebookEntry:
        notebook = self._notebooks.get(entry.wax_id)
        if notebook is None or notebook.id != entry.notebook_id:
            raise ValueError("unknown notebook for student")
        self._entries.setdefault((entry.wax_id, entry.notebook_id), []).append(entry)
        return entry

    def list_notebook_entries(
        self, wax_id: WaxId, notebook_id: UUID
    ) -> tuple[NotebookEntry, ...]:
        return tuple(self._entries.get((wax_id, notebook_id), []))

    def append_event(self, event: Event) -> Event:
        if event.wax_id is None:
            return event
        self._events.setdefault(event.wax_id, []).append(event)
        return event

    def list_events(self, wax_id: WaxId) -> tuple[Event, ...]:
        return tuple(self._events.get(wax_id, []))


__all__ = ["InMemoryStorage"]
