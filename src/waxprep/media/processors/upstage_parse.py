"""Upstage Document Parse / OCR behind ArtifactProcessor.

Uses only documented endpoints:
  POST https://api.upstage.ai/v1/document-digitization
Never sends image/file content to Solar Responses.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from waxprep.derive.artifacts import ExtractedRepresentation
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import ArtifactVersion
from waxprep.media.byte_store import ByteStore
from waxprep.media.limits import MediaLimitError
from waxprep.settings import MediaLimits

logger = logging.getLogger(__name__)


class UpstageDocumentProcessor:
    """Document Parse/OCR for supported documents and images."""

    def __init__(
        self,
        api_key: str,
        byte_store: ByteStore,
        *,
        base_url: str = "https://api.upstage.ai/v1",
        model: str = "document-parse",
        limits: MediaLimits | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._byte_store = byte_store
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._limits = limits or MediaLimits()
        self._client = client

    @property
    def name(self) -> str:
        return f"upstage:{self._model}"

    async def extract(
        self, wax_id: WaxId, version: ArtifactVersion
    ) -> tuple[ExtractedRepresentation, ...]:
        try:
            data = self._byte_store.get(version.storage_ref)
        except KeyError:
            logger.warning("artifact bytes missing for %s", version.id)
            return ()
        if len(data) > self._limits.max_inbound_bytes:
            raise MediaLimitError(
                "inbound_too_large",
                "document exceeds configured byte limit",
            )
        filename = version.original_filename or "document.bin"
        mime = version.mime_type or "application/octet-stream"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        files = {"document": (filename, data, mime)}
        form = {"model": self._model, "output_formats": '["markdown","text"]'}
        close = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self._limits.request_timeout_seconds)
            close = True
        try:
            response = await client.post(
                f"{self._base_url}/document-digitization",
                headers=headers,
                files=files,
                data=form,
            )
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
        finally:
            if close:
                await client.aclose()
        text = _extract_text(payload)
        if not text.strip():
            return ()
        return (
            ExtractedRepresentation(
                "document_parse",
                text,
            ),
        )


def _extract_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, dict):
        for key in ("markdown", "text", "html"):
            val = content.get(key)
            if isinstance(val, str) and val.strip():
                return val
    if isinstance(content, str):
        return content
    text = payload.get("text")
    if isinstance(text, str):
        return text
    return ""


class MockDocumentProcessor:
    """Deterministic processor for offline tests."""

    def __init__(self, text: str = "extracted text") -> None:
        self._text = text

    @property
    def name(self) -> str:
        return "mock-document"

    async def extract(
        self, wax_id: WaxId, version: ArtifactVersion
    ) -> tuple[ExtractedRepresentation, ...]:
        return (ExtractedRepresentation("document_parse", self._text),)
