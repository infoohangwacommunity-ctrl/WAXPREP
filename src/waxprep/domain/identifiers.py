"""Opaque student identity (WAX ID)."""

from __future__ import annotations

from typing import NewType
from uuid import UUID, uuid4

# Opaque internal identity — random UUID, no PII or educational meaning.
WaxId = NewType("WaxId", UUID)


def new_wax_id() -> WaxId:
    """Create a cryptographically random opaque WAX ID."""
    return WaxId(uuid4())


def parse_wax_id(value: str | UUID) -> WaxId:
    """Parse a WAX ID from a string or UUID."""
    if isinstance(value, UUID):
        return WaxId(value)
    return WaxId(UUID(str(value)))


__all__ = ["WaxId", "new_wax_id", "parse_wax_id"]
