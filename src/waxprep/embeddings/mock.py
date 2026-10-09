"""Deterministic fake embedding provider for offline tests."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass


@dataclass
class DeterministicEmbeddingProvider:
    """Hash-based vectors for contract tests only — not semantic search."""

    dimensions: int = 16
    model: str = "mock-deterministic"

    async def embed(self, text: str) -> tuple[float, ...]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        values: list[float] = []
        while len(values) < self.dimensions:
            for byte in digest:
                values.append((byte / 255.0) * 2.0 - 1.0)
                if len(values) >= self.dimensions:
                    break
            digest = hashlib.sha256(digest).digest()
        # L2 normalize
        norm = math.sqrt(sum(v * v for v in values)) or 1.0
        return tuple(v / norm for v in values)
