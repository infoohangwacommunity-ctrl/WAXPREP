from waxprep.channel.whatsapp.normalize import normalize_webhook_payload
from waxprep.domain.identifiers import new_wax_id


def test_normalize_text_and_image() -> None:
    wax = new_wax_id()
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "contacts": [{"wa_id": "2348012345678"}],
                            "messages": [
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
                            ],
                        },
                    }
                ]
            }
        ]
    }
    msgs = normalize_webhook_payload(payload, resolve_wax_id={"2348012345678": wax})
    assert len(msgs) == 2
    assert msgs[0].text == "hello"
    assert msgs[0].wax_id == wax
    assert msgs[1].attachments[0].provider_media_id == "media-1"
    assert msgs[1].text == "my work"


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
