import pytest

from waxprep.media.processors.diagram import (
    DiagramValidationError,
    render_diagram_svg,
    validate_diagram_spec,
)


def test_valid_diagram_renders_safe_svg() -> None:
    spec = validate_diagram_spec(
        {
            "width": 200,
            "height": 100,
            "nodes": [{"id": "a", "x": 40, "y": 50, "label": "A"}],
            "edges": [],
            "caption": "test",
        }
    )
    svg = render_diagram_svg(spec)
    assert svg.startswith("<svg")
    assert "<script" not in svg.lower()
    assert "javascript:" not in svg.lower()


def test_rejects_oversized() -> None:
    with pytest.raises(DiagramValidationError):
        validate_diagram_spec({"width": 99999, "height": 10, "nodes": [], "edges": []})
