from datetime import UTC, datetime

from support import TEST_CIPHER
from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import (
    AuthorKind,
    HistoryEntry,
    Student,
    StudentStatus,
    new_id,
)
from waxprep.media.byte_store import InMemoryByteStore
from waxprep.media.ingest import ingest_bytes
from waxprep.settings import MediaLimits
from waxprep.storage.memory import InMemoryStorage


def test_ingest_persists_attachment_and_bytes() -> None:
    storage = InMemoryStorage()
    store = InMemoryByteStore(TEST_CIPHER)
    wax = new_wax_id()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    storage.create_student(
        Student(wax_id=wax, created_at=now, updated_at=now, status=StudentStatus.ACTIVE)
    )
    entry = HistoryEntry(
        id=new_id(),
        wax_id=wax,
        author_kind=AuthorKind.STUDENT,
        content_kind="image/jpeg",
        text=None,
        occurred_at=now,
        recorded_at=now,
        channel="whatsapp",
        channel_ref="wamid.1",
    )
    storage.append_history_entry(entry)
    data = b"\xff\xd8\xff\xe0" + b"\x00" * 64
    result = ingest_bytes(
        storage,
        store,
        wax_id=wax,
        history_entry_id=entry.id,
        data=data,
        claimed_mime="image/jpeg",
        original_filename="photo.jpg",
        now=now,
        limits=MediaLimits(),
    )
    assert result.supported_for_processing
    assert storage.get_attachment(wax, result.attachment.id) is not None
    assert store.get(result.attachment.storage_ref) == data
