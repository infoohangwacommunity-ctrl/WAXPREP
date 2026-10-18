"""One-shot interactive offer registry."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from waxprep.domain.identifiers import new_wax_id
from waxprep.domain.models import (
    InteractiveOffer,
    InteractiveOfferStatus,
    Student,
    StudentStatus,
    new_id,
)
from waxprep.storage.memory import InMemoryStorage


def test_create_find_consume_offer() -> None:
    storage = InMemoryStorage()
    wax = new_wax_id()
    now = datetime(2024, 1, 1, tzinfo=UTC)
    storage.create_student(
        Student(
            wax_id=wax,
            created_at=now,
            updated_at=now,
            status=StudentStatus.ACTIVE,
        )
    )
    button_id = "opaque-btn-1"
    offer = InteractiveOffer(
        id=new_id(),
        wax_id=wax,
        tutor_entry_id=None,
        provider_outbound_message_id="wamid.OUT1",
        buttons=({"id": button_id, "title": "Quiz me"},),
        status=InteractiveOfferStatus.OPEN,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=24),
    )
    storage.create_interactive_offer(offer)

    found = storage.find_interactive_offer_by_button_id(wax, button_id)
    assert found is not None
    assert found.id == offer.id
    assert found.status == InteractiveOfferStatus.OPEN

    consumed = storage.consume_interactive_offer(wax, offer.id, button_id, now)
    assert consumed is not None
    assert consumed.status == InteractiveOfferStatus.CONSUMED
    assert consumed.consumed_button_id == button_id

    # Second consume fails (already consumed).
    again = storage.consume_interactive_offer(wax, offer.id, button_id, now)
    assert again is None


def test_expired_offer_not_consumable() -> None:
    storage = InMemoryStorage()
    wax = new_wax_id()
    now = datetime(2024, 1, 1, tzinfo=UTC)
    storage.create_student(
        Student(
            wax_id=wax,
            created_at=now,
            updated_at=now,
            status=StudentStatus.ACTIVE,
        )
    )
    button_id = "opaque-btn-2"
    offer = InteractiveOffer(
        id=new_id(),
        wax_id=wax,
        tutor_entry_id=None,
        provider_outbound_message_id=None,
        buttons=({"id": button_id, "title": "Explain"},),
        status=InteractiveOfferStatus.OPEN,
        created_at=now,
        updated_at=now,
        expires_at=now - timedelta(seconds=1),
    )
    storage.create_interactive_offer(offer)
    result = storage.consume_interactive_offer(
        wax, offer.id, button_id, now + timedelta(minutes=1)
    )
    assert result is None
