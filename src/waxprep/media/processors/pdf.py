"""PDF processing for WhatsApp attachments.

This module provides:
- PDF validation (file signature, page count, size)
- Text extraction from PDFs
- OCR for scanned PDFs
- Secure storage of original PDFs
- Creation of derived text representations

The processing pipeline:
1. Validate PDF file (signature, page count, size)
2. Store original securely
3. Extract text (with OCR if needed)
4. Save derived representation
5. Index for CI retrieval
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

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
class PDFExtraction:
    """Result of PDF text extraction."""

    text: str
    page_count: int
    has_ocr: bool
    model_used: str


class PDFProcessor:
    """Processes PDFs from WhatsApp attachments.

    Validates, stores, and extracts text from PDFs.
    """

    def __init__(
        self,
        storage: Storage,
        byte_store: ByteStore,
        limits: MediaLimits,
    ) -> None:
        self._storage = storage
        self._byte_store = byte_store
        self._limits = limits

    def validate_pdf(self, data: bytes) -> tuple[int, bool]:
        """Validate PDF file and return (page_count, needs_ocr).

        Raises MediaLimitError if validation fails.
        """
        try:
            # Check PDF signature
            if not data.startswith(b"%PDF"):
                raise MediaLimitError(
                    "invalid_pdf",
                    "File does not appear to be a valid PDF",
                )

            # Try to parse with PyPDF2 or similar
            # For now, we'll do basic validation
            # TODO: Implement proper PDF parsing

            # Check size
            if len(data) > self._limits.max_inbound_bytes:
                raise MediaLimitError(
                    "pdf_too_large",
                    f"PDF size {len(data)} exceeds {self._limits.max_inbound_bytes} bytes",
                )

            # Placeholder: assume 1 page, no OCR needed
            # In production, this would parse the PDF properly
            page_count = 1
            needs_ocr = False

            return page_count, needs_ocr

        except Exception as exc:
            raise MediaLimitError(
                "invalid_pdf",
                f"Invalid PDF file: {type(exc).__name__}: {exc}",
            ) from exc

    async def process_attachment(
        self,
        wax_id: WaxId,
        attachment: Attachment,
        history_entry: HistoryEntry,
        now: datetime,
    ) -> ArtifactContent | None:
        """Process a PDF attachment.

        Returns the derived ArtifactContent if processing succeeds, None otherwise.
        """
        try:
            # Get the stored bytes
            data = self._byte_store.get(attachment.storage_ref)
        except KeyError:
            logger.error(
                "PDF bytes not found",
                extra={"storage_ref": attachment.storage_ref},
            )
            return None

        # Validate
        try:
            page_count, needs_ocr = self.validate_pdf(data)
        except MediaLimitError as exc:
            logger.warning(
                "PDF validation failed",
                extra={
                    "attachment_id": str(attachment.id),
                    "error": str(exc),
                },
            )
            return None

        # Extract text
        try:
            extraction = await self._extract_text(data, page_count, needs_ocr)
        except Exception:
            logger.exception(
                "PDF extraction failed",
                extra={"attachment_id": str(attachment.id)},
            )
            return None

        # Create artifact content
        content = ArtifactContent(
            id=new_id(),
            wax_id=wax_id,
            artifact_version_id=attachment.id,  # Link to attachment
            representation="pdf_text",
            text=extraction.text,
            produced_by="pdf_processor",
            created_at=now,
        )

        return content

    async def _extract_text(
        self, data: bytes, page_count: int, needs_ocr: bool
    ) -> PDFExtraction:
        """Extract text from a PDF.

        This is a placeholder that should be replaced with actual PDF parsing.
        For now, it returns a basic extraction.
        """
        # TODO: Replace with actual PDF parsing (PyPDF2, pdfminer, etc.)
        # For OCR: use pytesseract or similar
        text = "Extracted text from PDF will appear here"

        return PDFExtraction(
            text=text,
            page_count=page_count,
            has_ocr=needs_ocr,
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
