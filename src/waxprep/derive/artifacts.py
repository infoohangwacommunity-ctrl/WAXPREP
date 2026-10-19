"""Making artifacts understandable without hard-coding any file type.

The processing capability (a model that can read the file) is injected.
This service never branches on what kind of file it is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from waxprep.clock import Clock
from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import (
    ArtifactContent,
    ArtifactVersion,
    ObjectType,
    new_id,
)
from waxprep.index.indexer import JOB_UNDERSTAND_ARTIFACT, Indexer
from waxprep.model_gateway.gateway import ModelGateway
from waxprep.model_gateway.models import (
    ContentItem,
    ContentType,
    ModelRequest,
    ModelRole,
)
from waxprep.storage.protocol import Storage

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExtractedRepresentation:
    """Any readable form of an artifact: text, transcript, description..."""

    representation: str
    text: str


class ArtifactProcessor(Protocol):
    @property
    def name(self) -> str: ...

    async def extract(
        self, wax_id: WaxId, version: ArtifactVersion
    ) -> tuple[ExtractedRepresentation, ...]: ...


class ArtifactUnderstandingService:
    """Stores what can be read from an artifact and makes it searchable."""

    def __init__(
        self,
        storage: Storage,
        indexer: Indexer,
        processor: ArtifactProcessor,
        clock: Clock,
    ) -> None:
        self._storage = storage
        self._indexer = indexer
        self._processor = processor
        self._clock = clock

    def on_version_created(self, version: ArtifactVersion) -> None:
        """Make the name findable now and queue the deeper reading."""
        self._indexer.index(
            version.wax_id,
            ObjectType.ARTIFACT_VERSION,
            version.id,
            " ".join(p for p in (version.original_filename, version.mime_type) if p),
            version.created_at,
        )
        self._indexer.enqueue(
            version.wax_id,
            JOB_UNDERSTAND_ARTIFACT,
            ObjectType.ARTIFACT_VERSION,
            version.id,
        )

    async def understand(
        self, wax_id: WaxId, version_id: UUID
    ) -> tuple[ArtifactContent, ...]:
        version = self._storage.get_artifact_version_by_id(wax_id, version_id)
        if version is None:
            return ()
        extracted = await self._processor.extract(wax_id, version)
        saved: list[ArtifactContent] = []
        for item in extracted:
            if not item.text.strip():
                continue
            content = ArtifactContent(
                id=new_id(),
                wax_id=wax_id,
                artifact_version_id=version.id,
                representation=item.representation,
                text=item.text,
                produced_by=self._processor.name,
                created_at=self._clock.now(),
            )
            self._storage.add_artifact_content(content)
            self._indexer.index(
                wax_id,
                ObjectType.ARTIFACT_CONTENT,
                content.id,
                content.text,
                content.created_at,
            )
            saved.append(content)
        return tuple(saved)


ARTIFACT_INSTRUCTIONS = """
Read the provided item and write down everything in it that a tutor would need
later, as plain text: all text, numbered questions, the student's answers and
marks if present, what an image shows, or a faithful transcript of speech.
Do not summarise away details and do not invent anything.
""".strip()


class GatewayArtifactProcessor:
    """Lets a capable model read any artifact through the Model Gateway.

    The only mapping here is the standard top-level media type to the
    gateway's four neutral content kinds. Whether the model can actually read
    it is decided by the gateway's capability check, not by this code.
    """

    def __init__(self, gateway: ModelGateway, *, model_name: str = "artifact") -> None:
        self._gateway = gateway
        self._model_name = model_name

    @property
    def name(self) -> str:
        return self._model_name

    async def extract(
        self, wax_id: WaxId, version: ArtifactVersion
    ) -> tuple[ExtractedRepresentation, ...]:
        top = (version.mime_type or "").split("/", 1)[0]
        kind = {"image": ContentType.IMAGE, "audio": ContentType.AUDIO}.get(
            top, ContentType.FILE
        )
        response = await self._gateway.complete(
            ModelRequest(
                wax_id=wax_id,
                role=ModelRole.ARTIFACT,
                model=self._model_name,
                instructions=ARTIFACT_INSTRUCTIONS,
                input=(
                    ContentItem(
                        type=kind,
                        uri=version.storage_ref,
                        mime_type=version.mime_type,
                    ),
                ),
                max_output_tokens=4000,
            )
        )
        return (ExtractedRepresentation("readable_text", response.text),)
