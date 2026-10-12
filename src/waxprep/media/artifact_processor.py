"""Artifact processing service.

This module connects the artifact system to the model gateway.
It handles:
- Processing artifact versions through appropriate models
- Managing processing state and retries
- Connecting private storage references to model inputs
- Creating derived representations (ArtifactContent)

The key challenge: private storage references (file://wax_id/digest) are not
accessible to external model providers. This module bridges that gap.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import (
    ArtifactContent,
    ArtifactVersion,
    DerivationJob,
    JobStatus,
    ObjectType,
    ProvenanceLink,
    new_id,
)
from waxprep.media.byte_store import ByteStore
from waxprep.media.content_sniff import ContentCategory, sniff_content
from waxprep.model_gateway.gateway import ModelGateway
from waxprep.model_gateway.models import (
    ContentItem,
    ContentType,
    ModelRequest,
    ModelRole,
)
from waxprep.storage.protocol import Storage

logger = logging.getLogger(__name__)


class ArtifactProcessingStatus(StrEnum):
    """Processing state for an artifact version."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ArtifactProcessingConfig:
    """Configuration for artifact processing."""

    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 300.0
    backoff_multiplier: float = 2.0
    
    # Model names for different content types
    text_model: str = "solar-mini"
    image_model: str = "solar-mini"  # Placeholder - needs vision model
    document_model: str = "document-parse"
    
    # Processing timeout
    timeout_seconds: float = 120.0


@dataclass(frozen=True, slots=True)
class ProcessedArtifact:
    """Result of artifact processing."""

    artifact_version_id: UUID
    content: ArtifactContent | None
    error: str | None
    attempts: int


class ArtifactProcessor:
    """Processes artifact versions to create derived representations.

    The processor:
    1. Fetches the artifact bytes from private storage
    2. Determines the content type and appropriate processing
    3. Sends the content to the appropriate model
    4. Creates and saves the derived ArtifactContent
    5. Links the content to the artifact version with provenance

    Key design principle: The original artifact bytes remain private.
    Only derived text representations are sent to models or indexed.
    """

    def __init__(
        self,
        storage: Storage,
        byte_store: ByteStore,
        gateway: ModelGateway,
        clock: Any,  # Clock
        *,
        config: ArtifactProcessingConfig | None = None,
    ) -> None:
        self._storage = storage
        self._byte_store = byte_store
        self._gateway = gateway
        self._clock = clock
        self._config = config or ArtifactProcessingConfig()
        self._lock = asyncio.Lock()

    async def process_artifact_version(
        self,
        wax_id: WaxId,
        artifact_version: ArtifactVersion,
    ) -> ProcessedArtifact:
        """Process an artifact version and create derived content.

        Returns ProcessedArtifact with the result.
        """
        logger.info(
            "processing artifact version",
            extra={
                "wax_id": str(wax_id),
                "artifact_version_id": str(artifact_version.id),
            },
        )

        # Get the bytes from storage
        try:
            data = self._byte_store.get(artifact_version.storage_ref)
        except KeyError:
            logger.error(
                "artifact bytes not found",
                extra={"storage_ref": artifact_version.storage_ref},
            )
            return ProcessedArtifact(
                artifact_version_id=artifact_version.id,
                content=None,
                error="storage_ref_not_found",
                attempts=0,
            )

        # Determine content category
        sniff = sniff_content(data, artifact_version.mime_type)

        # Process based on content type
        try:
            if sniff.media_category == ContentCategory.IMAGE:
                content = await self._process_image(wax_id, artifact_version, data, sniff)
            elif sniff.media_category == ContentCategory.DOCUMENT:
                content = await self._process_document(wax_id, artifact_version, data, sniff)
            elif sniff.media_category == ContentCategory.TEXT:
                content = await self._process_text(wax_id, artifact_version, data, sniff)
            else:
                logger.warning(
                    "unsupported content category",
                    extra={
                        "category": sniff.media_category,
                        "mime_type": artifact_version.mime_type,
                    },
                )
                return ProcessedArtifact(
                    artifact_version_id=artifact_version.id,
                    content=None,
                    error=f"unsupported_content: {sniff.media_category}",
                    attempts=0,
                )

            return ProcessedArtifact(
                artifact_version_id=artifact_version.id,
                content=content,
                error=None,
                attempts=1,
            )

        except Exception as exc:
            logger.exception(
                "artifact processing failed",
                extra={
                    "artifact_version_id": str(artifact_version.id),
                    "category": sniff.media_category,
                },
            )
            return ProcessedArtifact(
                artifact_version_id=artifact_version.id,
                content=None,
                error=f"processing_error: {type(exc).__name__}: {exc}",
                attempts=1,
            )

    async def _process_image(
        self,
        wax_id: WaxId,
        artifact_version: ArtifactVersion,
        data: bytes,
        sniff: Any,
    ) -> ArtifactContent:
        """Process an image artifact.

        Creates a text description of the image using a vision model.
        """
        now = self._clock.now()

        # Build model request
        # The image data needs to be encoded for the model
        # For now, we'll create a placeholder description
        # In production, this would call a vision model
        
        # Create a data URI for the image
        import base64
        mime_type = artifact_version.mime_type or sniff.mime_type or "image/jpeg"
        encoded = base64.b64encode(data).decode("ascii")
        data_uri = f"data:{mime_type};base64,{encoded}"

        request = ModelRequest(
            wax_id=wax_id,
            role=ModelRole.ARTIFACT,
            model=self._config.image_model,
            instructions="Describe this image in detail. Include any text, diagrams, or mathematical content visible in the image.",
            input=(
                ContentItem(
                    type=ContentType.IMAGE,
                    uri=data_uri,
                    mime_type=mime_type,
                ),
            ),
            timeout_seconds=self._config.timeout_seconds,
        )

        try:
            response = await self._gateway.complete(request)
            description = response.text
        except Exception as exc:
            logger.warning(
                "image model failed",
                extra={"error": str(exc)},
            )
            description = f"[Image processing failed: {type(exc).__name__}]"

        # Create artifact content
        content = ArtifactContent(
            id=new_id(),
            wax_id=wax_id,
            artifact_version_id=artifact_version.id,
            representation="image_description",
            text=description,
            produced_by="artifact_processor",
            created_at=now,
        )

        # Save content
        self._storage.add_artifact_content(content)

        # Create provenance link
        provenance = ProvenanceLink(
            id=new_id(),
            wax_id=wax_id,
            derived_type=ObjectType.ARTIFACT_CONTENT,
            derived_id=content.id,
            source_type=ObjectType.ARTIFACT_VERSION,
            source_id=artifact_version.id,
            created_at=now,
            source_author_kind=None,
            quote=None,
        )
        self._storage.add_provenance_links((provenance,))

        return content

    async def _process_document(
        self,
        wax_id: WaxId,
        artifact_version: ArtifactVersion,
        data: bytes,
        sniff: Any,
    ) -> ArtifactContent:
        """Process a document artifact.

        Extracts text from the document using a document parsing model.
        """
        now = self._clock.now()

        # Build model request
        mime_type = artifact_version.mime_type or sniff.mime_type or "application/pdf"
        
        # Create a data URI for the document
        import base64
        encoded = base64.b64encode(data).decode("ascii")
        data_uri = f"data:{mime_type};base64,{encoded}"

        request = ModelRequest(
            wax_id=wax_id,
            role=ModelRole.ARTIFACT,
            model=self._config.document_model,
            instructions="Extract all text from this document. Preserve formatting, equations, and structure as much as possible.",
            input=(
                ContentItem(
                    type=ContentType.FILE,
                    uri=data_uri,
                    mime_type=mime_type,
                ),
            ),
            timeout_seconds=self._config.timeout_seconds,
        )

        try:
            response = await self._gateway.complete(request)
            text = response.text
        except Exception as exc:
            logger.warning(
                "document model failed",
                extra={"error": str(exc)},
            )
            text = f"[Document processing failed: {type(exc).__name__}]"

        # Create artifact content
        content = ArtifactContent(
            id=new_id(),
            wax_id=wax_id,
            artifact_version_id=artifact_version.id,
            representation="document_text",
            text=text,
            produced_by="artifact_processor",
            created_at=now,
        )

        # Save content
        self._storage.add_artifact_content(content)

        # Create provenance link
        provenance = ProvenanceLink(
            id=new_id(),
            wax_id=wax_id,
            derived_type=ObjectType.ARTIFACT_CONTENT,
            derived_id=content.id,
            source_type=ObjectType.ARTIFACT_VERSION,
            source_id=artifact_version.id,
            created_at=now,
            source_author_kind=None,
            quote=None,
        )
        self._storage.add_provenance_links((provenance,))

        return content

    async def _process_text(
        self,
        wax_id: WaxId,
        artifact_version: ArtifactVersion,
        data: bytes,
        sniff: Any,
    ) -> ArtifactContent:
        """Process a text artifact.

        For plain text files, just extract and save the text.
        """
        now = self._clock.now()

        # Decode text
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = data.decode("latin-1")
            except UnicodeDecodeError:
                text = f"[Binary data: {len(data)} bytes]"

        # Create artifact content
        content = ArtifactContent(
            id=new_id(),
            wax_id=wax_id,
            artifact_version_id=artifact_version.id,
            representation="text",
            text=text,
            produced_by="artifact_processor",
            created_at=now,
        )

        # Save content
        self._storage.add_artifact_content(content)

        # Create provenance link
        provenance = ProvenanceLink(
            id=new_id(),
            wax_id=wax_id,
            derived_type=ObjectType.ARTIFACT_CONTENT,
            derived_id=content.id,
            source_type=ObjectType.ARTIFACT_VERSION,
            source_id=artifact_version.id,
            created_at=now,
            source_author_kind=None,
            quote=None,
        )
        self._storage.add_provenance_links((provenance,))

        return content

    async def enqueue_processing(
        self,
        wax_id: WaxId,
        artifact_version_id: UUID,
        now: datetime,
    ) -> DerivationJob:
        """Enqueue a new artifact processing job."""
        job = DerivationJob(
            id=new_id(),
            wax_id=wax_id,
            kind="understand_artifact",
            object_type=ObjectType.ARTIFACT_VERSION,
            object_id=artifact_version_id,
            status=JobStatus.PENDING,
            attempts=0,
            run_after=now,
            created_at=now,
            updated_at=now,
            last_error=None,
            lease_until=None,
        )
        return self._storage.enqueue_job(job)

    async def process_job(self, job: DerivationJob) -> bool:
        """Process a queued artifact processing job.

        Returns True if processing succeeded, False otherwise.
        """
        now = self._clock.now()

        # Get the artifact version
        artifact_version = self._storage.get_artifact_version_by_id(
            job.wax_id, job.object_id
        )
        if artifact_version is None:
            logger.error(
                "artifact version not found for job",
                extra={"job_id": str(job.id)},
            )
            self._storage.mark_job_failed(
                job.wax_id,
                job.id,
                now,
                error="artifact_version_not_found",
                retry_at=None,
            )
            return False

        # Process the artifact
        result = await self.process_artifact_version(job.wax_id, artifact_version)

        if result.error:
            # Mark job as failed with retry
            if job.attempts < self._config.max_attempts:
                delay = self._calculate_backoff(job.attempts + 1)
                retry_at = now + timedelta(seconds=delay)
                self._storage.mark_job_failed(
                    job.wax_id,
                    job.id,
                    now,
                    error=result.error or "unknown_error",
                    retry_at=retry_at,
                )
                return False
            else:
                self._storage.mark_job_failed(
                    job.wax_id,
                    job.id,
                    now,
                    error=result.error or "max_attempts_exceeded",
                    retry_at=None,
                )
                return False

        # Mark job as done
        self._storage.mark_job_done(job.wax_id, job.id, now)
        return True

    def _calculate_backoff(self, attempt: int) -> float:
        """Calculate exponential backoff delay."""
        delay = self._config.base_delay_seconds * (self._config.backoff_multiplier ** (attempt - 1))
        return min(delay, self._config.max_delay_seconds)
