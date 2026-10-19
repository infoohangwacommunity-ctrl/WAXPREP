"""WAX ID tests."""

from uuid import UUID

from waxprep.domain.identifiers import new_wax_id, parse_wax_id


def test_wax_ids_are_unique_uuids() -> None:
    a = new_wax_id()
    b = new_wax_id()
    assert a != b
    assert isinstance(a, UUID)


def test_wax_id_roundtrip() -> None:
    wax = new_wax_id()
    assert parse_wax_id(str(wax)) == wax


def test_wax_id_has_no_encoded_meaning() -> None:
    text = str(new_wax_id())
    assert not text.startswith("WAX-")
    assert "MATH" not in text.upper()
