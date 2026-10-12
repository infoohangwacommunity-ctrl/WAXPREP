"""Image processing for WhatsApp attachments.

This module provides:
- Image validation (size, dimensions, format)
- Image description using vision models
- Secure storage of original images
- Creation of derived text representations

The processing pipeline:
1. Validate image file (signature, dimensions, size)
2. Store original securely
3. Generate description using vision model
4. Save derived representation
5. Index for CI retrieval
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO

from PIL import Image  # type: ignore[import-not-found]

from waxprep.domain.identifiers import WaxId
from waxprep.domain.models import (
    Artifact,
    ArtifactContent,
    ArtifactStatus,
    ArtifactVersion,
    Attachment,
    HistoryEntry,
    ObjectType,
    ProvenanceLink,
    new_id,
)
from waxprep.media.byte_store import ByteStore
from waxprep.media.limits import MediaLimitError
from waxprep.settings import MediaLimits
from waxprep.storage.protocol import Storage

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ImageDescription:
    """Result of image description/understanding."""

    description: str
    detected_objects: tuple[str, ...]
    text_content: str | None
    model_used: str


class ImageProcessor:
    """Processes images from WhatsApp attachments.

    Validates, stores, and generates descriptions for images.
    """

    def __init__(
        self,
        storage: Storage,
        byte_store: ByteStore,
        limits: MediaLimits,
        capabilities: object | None = None,
    ) -> None:
        self._storage = storage
        self._byte_store = byte_store
        self._capabilities = capabilities
        self._limits = limits

    def validate_image(self, data: bytes) -> tuple[int, int, str]:
        """Validate image file and return (width, height, mime_type).

        Raises MediaLimitError if validation fails.
        """
        try:
            img = Image.open(BytesIO(data))
            width, height = img.size

            # Check dimensions
            max_pixels = self._limits.max_image_pixels
            if width * height > max_pixels:
                raise MediaLimitError(
                    "image_too_large",
                    f"Image dimensions {width}x{height} exceed {max_pixels} pixels",
                )

            # Check for decompression bombs
            if width > 100000 or height > 100000:
                raise MediaLimitError(
                    "image_dimensions_invalid",
                    f"Image dimensions {width}x{height} are invalid",
                )

            # Determine actual format
            format = img.format or "unknown"
            mime_type = f"image/{format.lower()}" if format != "unknown" else None

            return width, height, mime_type or "image/unknown"

        except Exception as exc:
            raise MediaLimitError(
                "invalid_image",
                f"Invalid image file: {type(exc).__name__}: {exc}",
            ) from exc

    async def process_attachment(
        self,
        wax_id: WaxId,
        attachment: Attachment,
        history_entry: HistoryEntry,
        now: datetime,
    ) -> ArtifactContent | None:
        """Process an image attachment.

        Returns the derived ArtifactContent if processing succeeds, None otherwise.
        """
        try:
            # Get the stored bytes
            data = self._byte_store.get(attachment.storage_ref)
        except KeyError:
            logger.error(
                "image bytes not found",
                extra={"storage_ref": attachment.storage_ref},
            )
            return None

        # Validate
        try:
            width, height, mime_type = self.validate_image(data)
        except MediaLimitError as exc:
            logger.warning(
                "image validation failed",
                extra={
                    "attachment_id": str(attachment.id),
                    "error": str(exc),
                },
            )
            return None

        # Generate description
        try:
            description = await self._describe_image(data, width, height)
        except Exception:
            logger.exception(
                "image description failed",
                extra={"attachment_id": str(attachment.id)},
            )
            return None

        # Create artifact content
        content = ArtifactContent(
            id=new_id(),
            wax_id=wax_id,
            artifact_version_id=attachment.id,  # Link to attachment
            representation="image_description",
            text=description.description,
            produced_by="image_processor",
            created_at=now,
        )

        return content

    async def _describe_image(
        self, data: bytes, width: int, height: int
    ) -> ImageDescription:
        """Generate a description of the image.

        This is a placeholder that should be replaced with actual vision model calls.
        For now, it returns a basic description.
        """
        # TODO: Replace with actual vision model integration
        # For now, return a basic description based on what we can detect
        return ImageDescription(
            description=f"Image of size {width}x{height} pixels",
            detected_objects=(),
            text_content=None,
            model_used="placeholder",
        )

    def create_artifact_from_attachment(
        self,
        wax_id: WaxId,
        attachment: Attachment,
        history_entry: HistoryEntry,
        now: datetime,
    ) -> tuple[Artifact, ArtifactVersion]:
        """Create artifact and version from an attachment."""
        # Get workspace
        workspace = self._storage.get_or_create_workspace(wax_id, now)

        # Create artifact
        artifact = Artifact(
            id=new_id(),
            wax_id=wax_id,
            workspace_id=workspace.id,
            status=ArtifactStatus.ACTIVE,
            current_version_id=None,
            created_at=now,
            updated_at=now,
        )
        created_artifact = self._storage.create_artifact(artifact)

        # Create version
        version = ArtifactVersion(
            id=new_id(),
            wax_id=wax_id,
            artifact_id=created_artifact.id,
            version_number=1,
            storage_ref=attachment.storage_ref,
            original_filename=attachment.media_category,
            mime_type=attachment.mime_type,
            size_bytes=attachment.size_bytes,
            checksum_sha256=None,  # TODO: Calculate from data
            created_at=now,
        )
        created_version = self._storage.create_artifact_version(version)

        # Update artifact current version
        updated_artifact = self._storage.set_current_artifact_version(
            wax_id, created_artifact.id, created_version.id, now
        )

        # Create provenance link
        provenance = ProvenanceLink(
            id=new_id(),
            wax_id=wax_id,
            derived_type=ObjectType.ARTIFACT_VERSION,
            derived_id=created_version.id,
            source_type=ObjectType.HISTORY,
            source_id=history_entry.id,
            created_at=now,
            source_author_kind=history_entry.author_kind,
            quote=None,
        )
        self._storage.add_provenance_links((provenance,))

        return updated_artifact, created_version
