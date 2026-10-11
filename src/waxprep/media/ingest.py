"""Inbound attachment ingest: validate, encrypt, persist, enqueue processing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import Attachment, new_id
from waxprep.media.byte_store import ByteStore, content_digest
from waxprep.media.content_sniff import sniff_content
from waxprep.media.limits import MediaLimitError, enforce_inbound_size
from waxprep.settings import MediaLimits
from waxprep.storage.protocol import Storage


@dataclass(frozen=True, slots=True)
class IngestedAttachment:
    attachment: Attachment
    digest: str
    media_category: str
    mime_type: str | None
    supported_for_processing: bool


def ingest_bytes(
    storage: Storage,
    byte_store: ByteStore,
    *,
    wax_id: WaxId,
    history_entry_id,
    data: bytes,
    claimed_mime: str | None,
    original_filename: str | None,
    now: datetime,
    limits: MediaLimits,
) -> IngestedAttachment:
    enforce_inbound_size(len(data), limits)
    sniff = sniff_content(data, claimed_mime)
    digest = content_digest(data)
    ref = byte_store.put(wax_id, digest, data)
    # media_category from sniff; store digest in storage_ref namespace already
    att = Attachment(
        id=new_id(),
        wax_id=wax_id,
        history_entry_id=history_entry_id,
        media_category=sniff.media_category,
        mime_type=sniff.mime_type or claimed_mime,
        storage_ref=ref,
        size_bytes=len(data),
        created_at=now,
    )
    storage.create_attachment(att)
    return IngestedAttachment(
        attachment=att,
        digest=digest,
        media_category=sniff.media_category,
        mime_type=att.mime_type,
        supported_for_processing=sniff.supported_for_processing,
    )


def delete_attachment_bytes(byte_store: ByteStore, storage_ref: str) -> None:
    """Idempotent byte deletion."""
    byte_store.delete(storage_ref)
    if byte_store.exists(storage_ref):
        raise MediaLimitError(
            "delete_incomplete",
            "ciphertext still accessible after delete",
        )
