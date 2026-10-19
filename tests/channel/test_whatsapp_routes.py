"""Tests for WhatsApp webhook routes — durable acceptance semantics."""

from __future__ import annotations

from waxprep.channel.whatsapp.handler import InboundResult
from waxprep.channel.whatsapp.routes import build_whatsapp_router
from waxprep.domain.identifiers import new_wax_id


def _router_with_accept(accept_result: InboundResult | None):
    def resolve_sender(sender: str):
        return new_wax_id()

    def accept_inbound(msg):
        return accept_result

    router = build_whatsapp_router(
        verify_token="test_verify_token",
        app_secret="test_app_secret",
        resolve_sender=resolve_sender,
        accept_inbound=accept_inbound,
    )
    return router


class TestDurableAcceptance:
    """The webhook must not return 200 unless the message was durably saved."""

    def test_invalid_signature_returns_403(self):
        router = _router_with_accept(
            InboundResult(
                accepted=True,
                wax_id=new_wax_id(),
                student_entry_id=None,
                outbound_delivery_id=None,
            )
        )
        from fastapi import FastAPI
        from starlette.testclient import TestClient

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        response = client.post(
            "/channels/whatsapp/webhook",
            json={"entry": []},
            headers={"X-Hub-Signature-256": "sha256=invalid"},
        )
        assert response.status_code == 403
