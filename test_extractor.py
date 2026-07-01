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


def test_leg_odds_optional_and_nullable():
    # Leg odds must be omittable/nullable so the model can report "not shown"
    # instead of guessing — which then drives the 0-EV path downstream.
    leg = extractor.BET_TOOL["input_schema"]["properties"]["legs"]["items"]
    assert "odds_decimal" not in leg["required"]
    assert "null" in leg["properties"]["odds_decimal"]["type"]
    assert "market_category" in leg["required"]  # still required


def test_schema_separates_pre_boost_and_boosted_odds():
    # combined_odds_decimal is the PRE-boost original (nullable, not required so
    # a slip that only prints a boosted price can leave it null); a dedicated
    # boosted_odds_decimal carries the already-boosted price.
    props = extractor.BET_TOOL["input_schema"]["properties"]
    assert "null" in props["combined_odds_decimal"]["type"]
    assert "combined_odds_decimal" not in extractor.BET_TOOL["input_schema"]["required"]
    assert "boosted_odds_decimal" in props
    assert "null" in props["boosted_odds_decimal"]["type"]
    assert extractor.BET_TOOL["input_schema"]["required"] == ["stake", "legs"]


def test_combined_odds_falls_back_to_leg_product():
    data = {
        "legs": [
            {"odds_decimal": 1.5},
            {"odds_decimal": 2.0},
        ]
    }
    assert abs(extractor.combined_odds_decimal(data) - 3.0) < 1e-9


# --- resolve_boost: feed compute_ev PRE-boost odds so a boost is never doubled ---

def test_resolve_boost_bet365_no_boosted_price():
    # bet365: slip shows the pre-boost combined odds; token comes from caption.
    # No boosted price on the slip -> pass odds through unchanged (no regression),
    # and no displayed boosted price to pay out on (compute the boost downstream).
    pre, pct, boosted, note = extractor.resolve_boost(
        {"combined_odds_decimal": 2.0, "token_pct": 50}
    )
    assert abs(pre - 2.0) < 1e-9 and pct == 50 and boosted is None and note is None


def test_resolve_boost_fanduel_both_prices_consistent():
    # FanDuel: struck-through +133 (2.33) recorded as the original, big +198
    # (2.98) recorded as boosted, 50% token. The original is genuine -> trust it;
    # the displayed boosted price (2.98) is returned as the payout odds so the
    # book's own rounding is preserved rather than recomputed (2.995).
    pre, pct, boosted, note = extractor.resolve_boost(
        {"combined_odds_decimal": 2.33, "boosted_odds_decimal": 2.98, "token_pct": 50}
    )
    assert abs(pre - 2.33) < 1e-9 and pct == 50 and note is None
    assert abs(boosted - 2.98) < 1e-9


def test_resolve_boost_fanduel_rounded_boost_preserved():
    # The reported bug: FanDuel Draw ML, +330 struck (4.30) and +465 big (5.65),
    # 40% token, $25. Boosting 4.30 by 40% gives 5.62 (140.50), but FanDuel
    # rounds its boosted odds to +465 (5.65) and pays 141.25. resolve_boost must
    # surface 5.65 so compute_ev pays on it; combined_odds stays the pre-boost 4.3.
    pre, pct, boosted, note = extractor.resolve_boost(
        {"combined_odds_decimal": 4.30, "boosted_odds_decimal": 5.65, "token_pct": 40}
    )
    assert abs(pre - 4.30) < 1e-9 and pct == 40 and note is None
    assert abs(boosted - 5.65) < 1e-9
    from ev import compute_ev
    ev = compute_ev(pre, pct, stake=25.0, boosted_decimal=boosted)
    assert abs(ev.boosted_return - 141.25) < 1e-9   # matches the slip, not 140.50
    assert abs(ev.combined_decimal - 4.30) < 1e-9   # record keeps the pre-boost odds


def test_resolve_boost_corrects_already_boosted_in_base_field():
    # The reported bug: the boosted price (+198 -> 2.98) lands in
    # combined_odds_decimal. With the boosted price also captured, detect the
    # inconsistency and back out the pre-boost odds so the boost isn't doubled;
    # the displayed 2.98 is returned as the payout odds.
    pre, pct, boosted, note = extractor.resolve_boost(
        {"combined_odds_decimal": 2.98, "boosted_odds_decimal": 2.98, "token_pct": 50}
    )
    assert abs(pre - 2.32) < 1e-9 and pct == 50 and note is not None
    assert abs(boosted - 2.98) < 1e-9
    # Sanity: compute_ev re-applying the token lands back on the slip's ~2.98.
    from ev import boosted_decimal_odds
    assert abs(boosted_decimal_odds(pre, pct) - 2.98) < 1e-9


def test_resolve_boost_only_boosted_price_shown():
    # Slip prints only the already-boosted price (no separate original).
    pre, pct, boosted, note = extractor.resolve_boost(
        {"boosted_odds_decimal": 2.98, "token_pct": 50}
    )
    assert abs(pre - 2.32) < 1e-9 and pct == 50 and note is not None
    assert abs(boosted - 2.98) < 1e-9


def test_resolve_boost_no_token_passes_through():
    pre, pct, boosted, note = extractor.resolve_boost({"combined_odds_decimal": 3.0})
    assert abs(pre - 3.0) < 1e-9 and pct == 0.0 and boosted is None and note is None


def test_resolve_boost_falls_back_to_leg_product():
    pre, pct, boosted, note = extractor.resolve_boost(
        {"legs": [{"odds_decimal": 1.5}, {"odds_decimal": 2.0}], "token_pct": 0}
    )
    assert abs(pre - 3.0) < 1e-9 and pct == 0.0 and boosted is None and note is None


def test_resolve_boost_raises_without_any_odds():
    try:
        extractor.resolve_boost({"legs": [], "token_pct": 50})
    except extractor.ExtractionError:
        pass
    else:
        raise AssertionError("expected ExtractionError when no odds are usable")


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
