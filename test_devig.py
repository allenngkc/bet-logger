"""Unit tests for devig.py — pure de-vig math, no third-party deps.

Run either way:
    python test_devig.py
    pytest test_devig.py
"""

import math

import devig


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def test_margin_for_known_and_fallback():
    assert _close(devig.margin_for("soccer_1x2"), 0.06)
    assert _close(devig.margin_for("player_props"), 0.13)
    # Unknown / None fall back to the "other" bucket.
    assert _close(devig.margin_for("not_a_real_category"), devig.CATEGORY_MARGINS["other"])
    assert _close(devig.margin_for(None), devig.CATEGORY_MARGINS["other"])


def test_devig_prob_multiplicative_model():
    # Even-money leg priced at 2.0 in a 4% market -> 0.5 / 1.04.
    assert _close(devig.devig_prob(2.0, "totals_handicap"), 0.5 / 1.04)
    # De-vig always shaves the raw implied prob (book holds an edge).
    assert devig.devig_prob(2.0, "totals_handicap") < 0.5
    # Unknown category uses the fallback margin.
    assert _close(devig.devig_prob(2.0, "???"), 0.5 / (1 + devig.CATEGORY_MARGINS["other"]))


def test_devig_prob_rejects_bad_odds():
    for bad in (0.0, -1.0):
        try:
            devig.devig_prob(bad, "soccer_1x2")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for non-positive odds")


def test_parlay_fair_prob_is_product():
    legs = [
        {"odds_decimal": 1.5, "market_category": "soccer_1x2"},
        {"odds_decimal": 2.0, "market_category": "totals_handicap"},
    ]
    expected = devig.devig_prob(1.5, "soccer_1x2") * devig.devig_prob(2.0, "totals_handicap")
    assert _close(devig.parlay_fair_prob(legs), expected)


def test_parlay_fair_prob_skips_unusable_and_clamps():
    legs = [
        {"odds_decimal": 1.5, "market_category": "soccer_1x2"},
        {"odds_decimal": 0, "market_category": "soccer_1x2"},   # skipped
        {"selection": "no odds"},                                # skipped
        "not a dict",                                            # skipped
    ]
    assert _close(devig.parlay_fair_prob(legs), devig.devig_prob(1.5, "soccer_1x2"))
    # A very short leg priced near 1.0 must stay <= 1.0.
    assert devig.parlay_fair_prob([{"odds_decimal": 1.0001, "market_category": "other"}]) <= 1.0


def test_parlay_fair_prob_none_when_no_usable_legs():
    assert devig.parlay_fair_prob([]) is None
    assert devig.parlay_fair_prob(None) is None
    assert devig.parlay_fair_prob([{"selection": "x"}]) is None


def test_missing_category_uses_default():
    leg_no_cat = [{"odds_decimal": 2.0}]
    assert _close(
        devig.parlay_fair_prob(leg_no_cat),
        0.5 / (1 + devig.CATEGORY_MARGINS["other"]),
    )


def test_all_legs_priced():
    assert devig.all_legs_priced([{"odds_decimal": 1.5}, {"odds_decimal": 2.0}]) is True
    assert devig.all_legs_priced([{"odds_decimal": "1.8"}]) is True  # string-coercible
    assert devig.all_legs_priced([{"odds_decimal": 1.5}, {"selection": "x"}]) is False  # one missing
    assert devig.all_legs_priced([{"odds_decimal": 0}, {"odds_decimal": 2.0}]) is False  # non-positive
    assert devig.all_legs_priced([]) is False
    assert devig.all_legs_priced(None) is False


def test_same_game_detection():
    legs = [
        {"event": "Spain v France", "odds_decimal": 1.5},
        {"event": "spain v france", "odds_decimal": 1.8},  # same event, different case
    ]
    assert devig.same_game(legs) is True

    diff = [
        {"event": "Spain v France", "odds_decimal": 1.5},
        {"event": "Eng v Bra", "odds_decimal": 1.6},
    ]
    assert devig.same_game(diff) is False
    # Blank events never count as a match.
    assert devig.same_game([{"event": ""}, {"event": ""}]) is False


def test_market_categories_matches_margin_keys():
    assert set(devig.MARKET_CATEGORIES) == set(devig.CATEGORY_MARGINS)
    assert devig.DEFAULT_CATEGORY in devig.CATEGORY_MARGINS


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
