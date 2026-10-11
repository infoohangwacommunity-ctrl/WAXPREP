from waxprep.media.content_sniff import sniff_content


def test_sniff_jpeg() -> None:
    data = b"\xff\xd8\xff\xe0" + b"\x00" * 20
    r = sniff_content(data, claimed_mime="application/octet-stream")
    assert r.mime_type == "image/jpeg"
    assert r.media_category == "image"
    assert r.supported_for_processing


def test_sniff_pdf() -> None:
    r = sniff_content(b"%PDF-1.4\n%", None)
    assert r.mime_type == "application/pdf"
    assert r.supported_for_processing


def test_sniff_unknown() -> None:
    r = sniff_content(b"\x00\x01\x02\x03random", "application/x-evil")
    assert r.media_category == "unknown"
    assert not r.supported_for_processing
