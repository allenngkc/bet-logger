"""Offline tests for extractor.py pure helpers — no anthropic SDK needed.

The SDK import is deferred, so the media-type helpers and combined-odds logic
stay importable/testable without `pip install anthropic`.

Run either way:
    python test_extractor.py
    pytest test_extractor.py
"""

import extractor

# Minimal valid magic-byte headers for each format.
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
_GIF = b"GIF89a" + b"\x00" * 16
_WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 8


def test_sniff_each_format():
    assert extractor._sniff_media_type(_PNG) == "image/png"
    assert extractor._sniff_media_type(_JPEG) == "image/jpeg"
    assert extractor._sniff_media_type(_GIF) == "image/gif"
    assert extractor._sniff_media_type(_WEBP) == "image/webp"


def test_sniff_unknown_returns_none():
    assert extractor._sniff_media_type(b"not an image at all") is None
    assert extractor._sniff_media_type(b"") is None


def test_resolve_prefers_bytes_over_mislabel():
    # The reported bug: PNG bytes that Discord labeled image/webp.
    assert extractor._resolve_media_type(_PNG, "image/webp") == "image/png"
    assert extractor._resolve_media_type(_JPEG, "image/png") == "image/jpeg"


def test_resolve_falls_back_to_label_when_unknown_bytes():
    # Unrecognized bytes -> trust a valid declared type...
    assert extractor._resolve_media_type(b"????", "image/jpeg") == "image/jpeg"
    # ...and coerce an invalid/missing declared type to PNG.
    assert extractor._resolve_media_type(b"????", "image/tiff") == "image/png"
    assert extractor._resolve_media_type(b"????", None) == "image/png"


def test_combined_odds_falls_back_to_leg_product():
    data = {
        "legs": [
            {"odds_decimal": 1.5},
            {"odds_decimal": 2.0},
        ]
    }
    assert abs(extractor.combined_odds_decimal(data) - 3.0) < 1e-9


def _run_all() -> None:
    tests = [
        v for k, v in sorted(globals().items())
        if k.startswith("test_") and callable(v)
    ]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} tests passed.")


if __name__ == "__main__":
    _run_all()
