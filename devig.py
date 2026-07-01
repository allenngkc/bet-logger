"""Single-sided de-vig and parlay fair-probability estimation.

A bet slip shows only the side the user backed, never the opposite side, so a
true two-sided de-vig (normalising both implied probs) is impossible. Instead we
assume a typical bet365 market margin (overround) per market category and back
out an estimated fair probability with the multiplicative model::

    p_fair = (1 / odds_decimal) / (1 + margin[category])

These are ESTIMATES. Their accuracy depends entirely on how well the per-category
margins below match bet365's real holds — tune them against observed two-sided
prices. The parlay fair prob is the product of the leg fair probs, which assumes
the legs are INDEPENDENT; same-game legs are correlated, so detect those
(``same_game``) and treat their EV as approximate.

Pure module — no I/O, no third-party deps — so it stays importable/testable and
``extractor`` can reuse ``MARKET_CATEGORIES`` for its tool schema.
"""

from __future__ import annotations

# Category -> assumed total market overround (sum of implied probs minus 1), as
# a fraction. These are starter estimates of bet365's typical hold per market —
# tune them against real two-sided prices. "other" is the fallback bucket for
# legs the model can't confidently classify.
CATEGORY_MARGINS: dict[str, float] = {
    "soccer_1x2": 0.06,            # match result / 1X2 (3-way)
    "totals_handicap": 0.04,       # over/under goals, Asian handicap, spreads
    "both_teams_to_score": 0.07,   # BTTS yes/no
    "corners_cards": 0.09,         # corner & card totals/handicaps
    "player_props": 0.13,          # shots, tackles, passes, assists, etc.
    "goalscorer": 0.18,            # anytime / first / last goalscorer
    "other": 0.07,                 # fallback for anything uncategorised
}

DEFAULT_CATEGORY = "other"

# The controlled vocabulary the extractor offers the model (and we validate
# against). Order mirrors CATEGORY_MARGINS.
MARKET_CATEGORIES = tuple(CATEGORY_MARGINS)


def _to_float(value: object) -> float | None:
    """Best-effort float coercion; returns None on failure (defensive)."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# A real selection price in decimal odds is strictly greater than 1.0: 1.0 means
# zero profit and anything below is impossible. A bet slip that hides per-leg
# odds (e.g. a same-game parlay showing only the combined price) often yields a
# 1.0 — or the extractor emitting a placeholder — which de-vigs to a bogus ~100%
# implied prob. Treat anything <= 1.0 as "no usable odds" so EV isn't fabricated
# from a non-price; callers then fall back to the 0-EV path.
_MIN_USABLE_ODDS = 1.0


def _usable_odds(value: object) -> float | None:
    """The decimal odds as a float if they're a real price (> 1.0), else None."""
    odds = _to_float(value)
    return odds if (odds is not None and odds > _MIN_USABLE_ODDS) else None


def margin_for(category: str | None) -> float:
    """Assumed overround for a category, falling back to ``DEFAULT_CATEGORY``."""
    if category is None:
        return CATEGORY_MARGINS[DEFAULT_CATEGORY]
    return CATEGORY_MARGINS.get(category, CATEGORY_MARGINS[DEFAULT_CATEGORY])


def devig_prob(odds_decimal: float, category: str | None) -> float:
    """Estimated fair win prob for ONE leg via the multiplicative model.

    ``p_fair = (1 / odds_decimal) / (1 + margin[category])``. Because we see only
    one side, the result is an estimate that leans entirely on the category
    margin (see module docstring).

    Raises:
        ValueError: if ``odds_decimal <= 0``.
    """
    if odds_decimal <= 0:
        raise ValueError("odds_decimal must be > 0")
    raw_implied = 1.0 / odds_decimal
    return raw_implied / (1.0 + margin_for(category))


def parlay_fair_prob(legs: object) -> float | None:
    """Product of per-leg de-vigged fair probs, or None if no usable leg odds.

    Each leg is a dict with ``odds_decimal`` and an optional ``market_category``
    (missing/unknown categories use ``DEFAULT_CATEGORY``). Legs without usable
    odds are skipped. Assumes leg independence — flag same-game parlays via
    ``same_game``. The result is clamped to ``(0, 1]``.
    """
    product = 1.0
    found = False
    for leg in legs or []:
        if not isinstance(leg, dict):
            continue
        odds = _usable_odds(leg.get("odds_decimal"))
        if odds is None:
            continue
        product *= devig_prob(odds, leg.get("market_category"))
        found = True
    if not found:
        return None
    return min(product, 1.0)


def all_legs_priced(legs: object) -> bool:
    """True only if there's at least one leg and every leg has usable odds (> 1.0).

    EV needs each leg's odds to de-vig; if any leg is missing them the parlay
    fair prob would be computed from a subset (silently wrong), so callers treat
    this as "EV not countable" and report 0 EV instead. Odds <= 1.0 are not a
    real price (see ``_usable_odds``) — a slip that hides per-leg odds (SGP) or a
    1.0 placeholder counts as unpriced, not as a near-certain leg.
    """
    found = False
    for leg in legs or []:
        if not isinstance(leg, dict):
            return False
        if _usable_odds(leg.get("odds_decimal")) is None:
            return False
        found = True
    return found


def with_combined_fallback(legs: object, combined_decimal: object) -> object:
    """Fill a lone unpriced leg's odds from the combined price (single bets only).

    For a SINGLE-leg straight bet the combined odds ARE that leg's odds, so if the
    model didn't price the lone leg we can still de-vig it — using
    ``combined_decimal`` as the leg's price — instead of reporting 0 EV (a common
    case: a straight bet with a profit token). Returns a NEW legs list with the
    filled odds in that one case; otherwise returns ``legs`` unchanged (never
    mutates the input).

    Deliberately a no-op for 2+ legs: a parlay's combined price must not be split
    or duplicated across legs (that fabricates a fair prob), so a multi-leg parlay
    whose per-leg odds are missing still fails ``all_legs_priced`` and reports 0
    EV. Also a no-op when the lone leg is already priced (its own odds win) or when
    ``combined_decimal`` isn't a usable price (> 1.0).
    """
    if not isinstance(legs, list) or len(legs) != 1:
        return legs
    leg = legs[0]
    if not isinstance(leg, dict) or _usable_odds(leg.get("odds_decimal")) is not None:
        return legs
    combined = _usable_odds(combined_decimal)
    if combined is None:
        return legs
    return [{**leg, "odds_decimal": combined}]


def same_game(legs: object) -> bool:
    """True if two or more legs share the same (normalised) event.

    A same-game parlay's legs are correlated, so the independence assumption in
    ``parlay_fair_prob`` — and thus the EV estimate — is unreliable.
    """
    seen: set[str] = set()
    for leg in legs or []:
        if not isinstance(leg, dict):
            continue
        event = str(leg.get("event", "")).strip().lower()
        if not event:
            continue
        if event in seen:
            return True
        seen.add(event)
    return False
