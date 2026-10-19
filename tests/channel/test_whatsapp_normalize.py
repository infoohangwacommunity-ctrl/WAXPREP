"""Normalization of all WhatsApp inbound types into NormalizedInbound."""

from __future__ import annotations

from waxprep.channel.contracts import (
    CONTENT_KIND_IMAGE,
    CONTENT_KIND_INTERACTIVE_BUTTON,
    CONTENT_KIND_INTERACTIVE_LIST,
    CONTENT_KIND_REACTION,
    CONTENT_KIND_TEXT,
    CONTENT_KIND_UNKNOWN,
)
from waxprep.channel.whatsapp.normalize import normalize_webhook_payload
from waxprep.domain.identifiers import new_wax_id


def _payload(messages: list[dict]) -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "contacts": [{"wa_id": "2348012345678"}],
                            "messages": messages,
                        },
                    }
                ]
            }
        ]
    }


def test_normalize_text_and_image() -> None:
    wax = new_wax_id()
    payload = _payload(
        [
            {
                "from": "2348012345678",
                "id": "wamid.TEXT1",
                "timestamp": "1700000000",
                "type": "text",
                "text": {"body": "hello"},
            },
            {
                "from": "2348012345678",
                "id": "wamid.IMG1",
                "timestamp": "1700000001",
                "type": "image",
                "image": {
                    "id": "media-1",
                    "mime_type": "image/jpeg",
                    "caption": "my work",
                },
            },
        ]
    )
    msgs = normalize_webhook_payload(payload, resolve_wax_id={"2348012345678": wax})
    assert len(msgs) == 2
    assert msgs[0].text == "hello"
    assert msgs[0].content_kind == CONTENT_KIND_TEXT
    assert msgs[0].wax_id == wax
    assert msgs[1].attachments[0].provider_media_id == "media-1"
    assert msgs[1].text == "my work"
    assert msgs[1].content_kind == CONTENT_KIND_IMAGE


def test_ignores_status_events() -> None:
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "statuses": [{"id": "x", "status": "delivered"}],
                            "messages": [],
                        },
                    }
                ]
            }
        ]
    }
    assert normalize_webhook_payload(payload) == ()


def test_normalize_reaction() -> None:
    msgs = normalize_webhook_payload(
        _payload(
            [
                {
                    "from": "2348012345678",
                    "id": "wamid.REACT1",
                    "timestamp": "1700000002",
                    "type": "reaction",
                    "reaction": {"emoji": "👍", "message_id": "wamid.TARGET"},
                }
            ]
        )
    )
    assert len(msgs) == 1
    assert msgs[0].content_kind == CONTENT_KIND_REACTION
    assert "👍" in (msgs[0].text or "")
    assert msgs[0].structured["emoji"] == "👍"
    assert msgs[0].structured["target_message_id"] == "wamid.TARGET"
    assert msgs[0].reply_to_provider_id == "wamid.TARGET"


def test_normalize_button_reply() -> None:
    msgs = normalize_webhook_payload(
        _payload(
            [
                {
                    "from": "2348012345678",
                    "id": "wamid.BTN1",
                    "timestamp": "1700000003",
                    "type": "interactive",
                    "interactive": {
                        "type": "button_reply",
                        "button_reply": {
                            "id": "opaque-uuid-1",
                            "title": "Try a problem",
                        },
                    },
                    "context": {"id": "wamid.OUTBOUND1"},
                }
            ]
        )
    )
    assert len(msgs) == 1
    assert msgs[0].content_kind == CONTENT_KIND_INTERACTIVE_BUTTON
    assert msgs[0].text == "Try a problem"
    assert msgs[0].structured["button_id"] == "opaque-uuid-1"
    assert msgs[0].reply_to_provider_id == "wamid.OUTBOUND1"


def test_normalize_list_reply() -> None:
    msgs = normalize_webhook_payload(
        _payload(
            [
                {
                    "from": "2348012345678",
                    "id": "wamid.LIST1",
                    "timestamp": "1700000004",
                    "type": "interactive",
                    "interactive": {
                        "type": "list_reply",
                        "list_reply": {
                            "id": "row-a",
                            "title": "Photosynthesis",
                            "description": "Chapter 3",
                        },
                    },
                }
            ]
        )
    )
    assert len(msgs) == 1
    assert msgs[0].content_kind == CONTENT_KIND_INTERACTIVE_LIST
    assert "Photosynthesis" in (msgs[0].text or "")
    assert msgs[0].structured["row_id"] == "row-a"


def test_normalize_context_reply() -> None:
    msgs = normalize_webhook_payload(
        _payload(
            [
                {
                    "from": "2348012345678",
                    "id": "wamid.QUOTE1",
                    "timestamp": "1700000005",
                    "type": "text",
                    "text": {"body": "what does this mean?"},
                    "context": {"id": "wamid.TUTOR_PREV"},
                }
            ]
        )
    )
    assert len(msgs) == 1
    assert msgs[0].reply_to_provider_id == "wamid.TUTOR_PREV"
    assert msgs[0].text == "what does this mean?"


def test_normalize_unknown_never_dropped() -> None:
    msgs = normalize_webhook_payload(
        _payload(
            [
                {
                    "from": "2348012345678",
                    "id": "wamid.UNK1",
                    "timestamp": "1700000006",
                    "type": "order",
                    "order": {"catalog_id": "x"},
                }
            ]
        )
    )
    assert len(msgs) == 1
    assert msgs[0].content_kind == CONTENT_KIND_UNKNOWN
    assert msgs[0].text is not None
    assert "unsupported" in msgs[0].text.lower()
