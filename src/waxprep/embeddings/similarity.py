"""Vector similarity helpers for standard PostgreSQL / in-memory ranking."""

from __future__ import annotations

import math


def cosine_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    if len(a) != len(b):
        raise ValueError("embedding dimensions must match")
    if not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def validate_embedding(
    values: list[float], *, expected_dims: int | None = None
) -> tuple[float, ...]:
    if not values:
        raise ValueError("embedding is empty")
    if expected_dims is not None and len(values) != expected_dims:
        raise ValueError(
            f"embedding dimensions mismatch: got {len(values)}, "
            f"expected {expected_dims}"
        )
    out: list[float] = []
    for v in values:
        if not math.isfinite(v):
            raise ValueError("embedding contains non-finite values")
        out.append(float(v))
    return tuple(out)
