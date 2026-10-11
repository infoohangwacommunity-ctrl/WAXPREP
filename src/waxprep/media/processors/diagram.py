"""Safe educational diagram rendering from validated JSON specs.

No scripts, foreign objects, or external network references in SVG.
Free-form photorealistic image generation is not supported.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any


class DiagramValidationError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class DiagramSpec:
    width: int
    height: int
    nodes: tuple[dict[str, Any], ...]
    edges: tuple[dict[str, Any], ...]
    caption: str = ""


_MAX_W = 2000
_MAX_H = 2000
_MAX_NODES = 80
_MAX_EDGES = 120
_MAX_LABEL = 120


def validate_diagram_spec(raw: dict[str, Any]) -> DiagramSpec:
    try:
        width = int(raw.get("width", 400))
        height = int(raw.get("height", 300))
    except (TypeError, ValueError) as exc:
        raise DiagramValidationError("width/height must be integers") from exc
    if not (1 <= width <= _MAX_W and 1 <= height <= _MAX_H):
        raise DiagramValidationError("dimensions out of bounds")
    nodes_raw = raw.get("nodes") or []
    edges_raw = raw.get("edges") or []
    if not isinstance(nodes_raw, list) or not isinstance(edges_raw, list):
        raise DiagramValidationError("nodes and edges must be lists")
    if len(nodes_raw) > _MAX_NODES or len(edges_raw) > _MAX_EDGES:
        raise DiagramValidationError("too many nodes or edges")
    nodes: list[dict[str, Any]] = []
    for n in nodes_raw:
        if not isinstance(n, dict):
            raise DiagramValidationError("node must be object")
        label = str(n.get("label", ""))[:_MAX_LABEL]
        try:
            x, y = float(n["x"]), float(n["y"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DiagramValidationError("node requires numeric x,y") from exc
        nodes.append(
            {"id": str(n.get("id", len(nodes))), "x": x, "y": y, "label": label}
        )
    edges: list[dict[str, Any]] = []
    for e in edges_raw:
        if not isinstance(e, dict):
            raise DiagramValidationError("edge must be object")
        edges.append(
            {
                "from": str(e.get("from", "")),
                "to": str(e.get("to", "")),
                "label": str(e.get("label", ""))[:_MAX_LABEL],
            }
        )
    caption = str(raw.get("caption", ""))[:_MAX_LABEL]
    return DiagramSpec(width, height, tuple(nodes), tuple(edges), caption)


def render_diagram_svg(spec: DiagramSpec) -> str:
    """Render a minimal safe SVG (no script, no external hrefs)."""
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{spec.width}" '
        f'height="{spec.height}" viewBox="0 0 {spec.width} {spec.height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
    ]
    by_id = {n["id"]: n for n in spec.nodes}
    for e in spec.edges:
        a, b = by_id.get(e["from"]), by_id.get(e["to"])
        if a and b:
            parts.append(
                f'<line x1="{a["x"]}" y1="{a["y"]}" x2="{b["x"]}" y2="{b["y"]}" '
                f'stroke="#333" stroke-width="2"/>'
            )
    for n in spec.nodes:
        label = html.escape(n["label"])
        parts.append(
            f'<circle cx="{n["x"]}" cy="{n["y"]}" r="18" fill="#e8f0fe" stroke="#1a73e8"/>'
        )
        parts.append(
            f'<text x="{n["x"]}" y="{n["y"] + 4}" text-anchor="middle" '
            f'font-size="12" fill="#202124">{label}</text>'
        )
    if spec.caption:
        parts.append(
            f'<text x="8" y="{spec.height - 8}" font-size="12" fill="#5f6368">'
            f"{html.escape(spec.caption)}</text>"
        )
    parts.append("</svg>")
    svg = "".join(parts)
    # Hard safety: reject if anything sneaks in
    lowered = svg.lower()
    for banned in ("<script", "javascript:", "xlink:href", "<foreignobject", "data:"):
        if banned in lowered:
            raise DiagramValidationError("unsafe SVG content rejected")
    return svg
