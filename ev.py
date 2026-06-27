"""Token-boosted EV math for the bet logger.

Pure functions — no I/O, no Discord, no network. Everything is in DECIMAL odds.

A bet365 "profit-boost token" multiplies the PROFIT portion of a winning bet
(the stake is never boosted), and using a token requires a 3-leg parlay. See
PROJECT_PLAN.md §6.

Most fields on ``EVResult`` are per 1 unit staked. The stake-/percent-scaled
exceptions are ``boosted_return`` (= boosted_decimal × stake), ``ev_pct``
(EV as a percent of stake), and ``ev_profit`` (expected profit for ``stake``).
"""

from __future__ import annotations

from dataclasses import dataclass


def american_to_decimal(odds: float) -> float:
    """Convert American odds to decimal. +150 -> 2.5, -200 -> 1.5."""
    if odds > 0:
        return 1 + odds / 100
    if odds < 0:
        return 1 + 100 / abs(odds)
    raise ValueError("American odds cannot be 0")


def implied_prob(decimal: float) -> float:
    """Implied win probability from decimal odds (single side, no de-vig)."""
    if decimal <= 0:
        raise ValueError("decimal odds must be > 0")
    return 1 / decimal


def boosted_decimal_odds(combined_decimal: float, boost_pct: float) -> float:
    """Apply a profit-boost token to combined decimal odds.

    ``boost_pct`` is the token percentage (e.g. 50 for a 50% boost). The profit
    portion ``(D - 1)`` is multiplied by ``(1 + boost_pct / 100)``::

        0   -> unchanged
        50  -> profit x1.5
        100 -> profit x2
    """
    return 1 + (combined_decimal - 1) * (1 + boost_pct / 100)


# A bet is labelled "+EV" only when EV clears floating-point noise. The plan
# specifies `ev_per_unit > 0`; this tolerance keeps the label deterministic at
# the exact breakeven point, where FP rounding can otherwise render a true 0.0
# EV as a tiny positive and mislabel a break-even bet as +EV.
_EV_EPS = 1e-9


@dataclass
class EVResult:
    combined_decimal: float          # combined parlay odds, decimal
    boost_pct: float                 # token %, e.g. 50 (0 if no token)
    boosted_decimal: float           # odds after the profit boost
    breakeven_prob: float            # win prob needed to break even (post-boost)
    boosted_return_per_unit: float   # total return per 1 unit staked = boosted_decimal
    boosted_return: float            # total return for the given stake = boosted_decimal * stake
    fair_prob: float | None          # user-supplied fair win prob, or None
    ev_per_unit: float | None        # EV per 1 unit staked, or None if no fair_prob
    ev_pct: float | None             # EV as % of stake = ev_per_unit * 100, or None
    ev_profit: float | None          # expected profit for `stake` = ev_per_unit * stake, or None
    flag: str                        # "+EV" | "-EV" | "unknown"


def compute_ev(
    combined_decimal: float,
    boost_pct: float,
    stake: float = 1.0,
    fair_prob: float | None = None,
) -> EVResult:
    """Compute token-boosted EV for a parlay.

    Args:
        combined_decimal: Combined parlay odds in decimal (>= 1).
        boost_pct: Profit-boost token percentage (e.g. 50). Use 0 for no token.
        stake: Wager amount. Used only to scale ``boosted_return``; per-unit
            fields are unaffected.
        fair_prob: User-supplied fair win probability in [0, 1]. When ``None``,
            EV cannot be computed: ``flag`` is "unknown" and ``ev_per_unit`` is
            ``None`` (``breakeven_prob`` is still returned).

    Returns:
        EVResult. All fields are per 1 unit staked except ``boosted_return``.
    """
    if combined_decimal < 1.0:
        raise ValueError("combined_decimal must be >= 1.0 (decimal odds)")
    if boost_pct < 0:
        raise ValueError("boost_pct must be >= 0")
    if stake <= 0:
        raise ValueError("stake must be > 0")
    if fair_prob is not None and not (0.0 <= fair_prob <= 1.0):
        raise ValueError("fair_prob must be in [0, 1]")

    boosted = boosted_decimal_odds(combined_decimal, boost_pct)
    breakeven = 1 / boosted

    if fair_prob is None:
        ev_per_unit: float | None = None
        ev_pct: float | None = None
        ev_profit: float | None = None
        flag = "unknown"
    else:
        ev_per_unit = fair_prob * (boosted - 1) - (1 - fair_prob)
        ev_pct = ev_per_unit * 100
        ev_profit = ev_per_unit * stake
        flag = "+EV" if ev_per_unit > _EV_EPS else "-EV"

    return EVResult(
        combined_decimal=combined_decimal,
        boost_pct=boost_pct,
        boosted_decimal=boosted,
        breakeven_prob=breakeven,
        boosted_return_per_unit=boosted,
        boosted_return=boosted * stake,
        fair_prob=fair_prob,
        ev_per_unit=ev_per_unit,
        ev_pct=ev_pct,
        ev_profit=ev_profit,
        flag=flag,
    )
