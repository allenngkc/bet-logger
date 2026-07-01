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


def test_odds_of_one_is_not_a_usable_price():
    # Decimal 1.0 is a non-price (zero profit). It commonly appears when a SGP
    # slip hides per-leg odds and the model emits a 1.0 placeholder; it must NOT
    # de-vig to a ~100% implied prob and fabricate EV. (Regression: a 2-leg SGP
    # with 1.0 placeholders produced a 90.7% fair prob and a bogus +EV.)
    legs = [
        {"odds_decimal": 1.0, "market_category": "totals_handicap"},
        {"odds_decimal": 1.0, "market_category": "soccer_1x2"},
    ]
    assert devig.all_legs_priced(legs) is False
    assert devig.parlay_fair_prob(legs) is None
    # A genuine near-certain favourite just above 1.0 is still usable.
    assert devig.all_legs_priced([{"odds_decimal": 1.01}]) is True


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
    assert devig.all_legs_priced([{"odds_decimal": 1.0}, {"odds_decimal": 2.0}]) is False  # 1.0 is a non-price
    assert devig.all_legs_priced([]) is False
    assert devig.all_legs_priced(None) is False


def test_with_combined_fallback_single_unpriced_leg():
    # The reported edge case: a single-leg straight bet whose lone leg the model
    # left unpriced (only the combined 4.3 was read). The combined price IS the
    # leg's price, so fill it in and let EV be computed instead of the 0-EV path.
    legs = [{"event": "Netherlands v Morocco", "selection": "Draw",
             "market_category": "soccer_1x2"}]
    filled = devig.with_combined_fallback(legs, 4.3)
    assert devig.all_legs_priced(filled) is True
    assert _close(filled[0]["odds_decimal"], 4.3)
    # Input is not mutated — the fallback returns a new list.
    assert "odds_decimal" not in legs[0]
    assert _close(devig.parlay_fair_prob(filled), devig.devig_prob(4.3, "soccer_1x2"))


def test_with_combined_fallback_prefers_leg_own_odds():
    # If the lone leg is already priced, its own odds win — combined is ignored.
    legs = [{"selection": "Draw", "market_category": "soccer_1x2", "odds_decimal": 4.5}]
    filled = devig.with_combined_fallback(legs, 4.3)
    assert filled is legs and _close(filled[0]["odds_decimal"], 4.5)


def test_with_combined_fallback_ignores_parlays():
    # 2+ legs: never split/duplicate the combined price across legs. An unpriced
    # parlay stays unpriced -> 0 EV.
    legs = [
        {"selection": "A", "market_category": "soccer_1x2"},
        {"selection": "B", "market_category": "totals_handicap"},
    ]
    assert devig.with_combined_fallback(legs, 6.0) is legs
    assert devig.all_legs_priced(devig.with_combined_fallback(legs, 6.0)) is False


def test_with_combined_fallback_no_usable_combined():
    # No usable combined price (<= 1.0 or None) -> nothing to fall back to.
    legs = [{"selection": "Draw", "market_category": "soccer_1x2"}]
    assert devig.with_combined_fallback(legs, 1.0) is legs
    assert devig.with_combined_fallback(legs, None) is legs
    # Non-list / empty inputs pass through unchanged.
    assert devig.with_combined_fallback([], 4.3) == []
    assert devig.with_combined_fallback(None, 4.3) is None


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
