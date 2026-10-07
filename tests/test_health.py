"""Tests for the foundation HTTP application."""

from fastapi.testclient import TestClient

from waxprep.app import app

client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")
    status_code: int = response.status_code
    body: object = response.json()

    assert status_code == 200
    assert body == {
        "status": "ok",
        "service": "waxprep",
        "version": "0.1.0",
    }
