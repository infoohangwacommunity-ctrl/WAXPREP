"""Tests for the foundation HTTP application."""

from fastapi.testclient import TestClient

from waxprep.app import app

client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")
    status_code: int = response.status_code
    body = response.json()
    assert isinstance(body, dict)

    assert status_code == 200
    assert body["status"] == "ok"
    assert body["service"] == "waxprep"
    assert body["version"] == "0.1.0"
