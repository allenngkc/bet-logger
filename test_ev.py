"""Unit tests for ev.py — pure math, no third-party deps.

Run either way:
    python test_ev.py     # plain runner, no pytest needed
    pytest test_ev.py     # if pytest is installed
"""

import math

from ev import (
    EVResult,
    american_to_decimal,
    boosted_decimal_odds,
    compute_ev,
    implied_prob,
)


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def test_american_to_decimal():
    assert _close(american_to_decimal(150), 2.5)
    assert _close(american_to_decimal(-200), 1.5)
    assert _close(american_to_decimal(100), 2.0)
    assert _close(american_to_decimal(-100), 2.0)
    try:
        american_to_decimal(0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for 0 American odds")


def test_implied_prob():
    assert _close(implied_prob(2.0), 0.5)
    assert _close(implied_prob(4.0), 0.25)


def test_boosted_decimal_odds():
    assert _close(boosted_decimal_odds(2.0, 0), 2.0)     # no token
    assert _close(boosted_decimal_odds(2.0, 50), 2.5)    # profit x1.5
    assert _close(boosted_decimal_odds(2.0, 100), 3.0)   # profit x2
    assert _close(boosted_decimal_odds(3.0, 50), 4.0)    # 1 + 2*1.5


def test_compute_ev_no_fair_prob():
    r = compute_ev(2.0, 50, stake=10.0)
    assert isinstance(r, EVResult)
    assert _close(r.boosted_decimal, 2.5)
    assert _close(r.breakeven_prob, 0.4)
    assert _close(r.boosted_return_per_unit, 2.5)
    assert _close(r.boosted_return, 25.0)   # 2.5 * 10
    assert r.fair_prob is None
    # No fair prob -> EV reported as 0 ("0 EV"), not "unknown".
    assert r.ev_per_unit == 0.0
    assert r.ev_pct == 0.0
    assert r.ev_profit == 0.0
    assert r.flag == "0 EV"


def test_compute_ev_positive():
    # boosted = 2.5; ev = 0.5*1.5 - 0.5 = 0.25
    r = compute_ev(2.0, 50, fair_prob=0.5)
    assert _close(r.ev_per_unit, 0.25)
    assert _close(r.ev_pct, 25.0)
    assert _close(r.ev_profit, 0.25)   # stake defaults to 1.0
    assert r.flag == "+EV"


def test_compute_ev_both_representations():
    # The preview example: combined 2.0, 50% token, £10 stake, fair 0.55.
    # boosted = 2.5; ev/unit = 0.55*1.5 - 0.45 = 0.375
    r = compute_ev(2.0, 50, stake=10.0, fair_prob=0.55)
    assert _close(r.boosted_decimal, 2.5)
    assert _close(r.breakeven_prob, 0.4)
    assert _close(r.ev_per_unit, 0.375)
    assert _close(r.ev_pct, 37.5)        # % edge
    assert _close(r.ev_profit, 3.75)     # 0.375 * 10
    assert _close(r.boosted_return, 25.0)
    assert r.flag == "+EV"


def test_compute_ev_negative():
    # ev = 0.3*1.5 - 0.7 = -0.25
    r = compute_ev(2.0, 50, fair_prob=0.3)
    assert _close(r.ev_per_unit, -0.25)
    assert r.flag == "-EV"


def test_compute_ev_breakeven_flagged_negative():
    # At fair_prob == breakeven_prob (0.4), ev == 0 -> plan flags "-EV".
    r = compute_ev(2.0, 50, fair_prob=0.4)
    assert _close(r.ev_per_unit, 0.0)
    assert r.flag == "-EV"


def test_zero_token_unchanged():
    # No token: boosted == combined; ev = 0.4*2.5 - 0.6 = 0.4
    r = compute_ev(3.5, 0, fair_prob=0.4)
    assert _close(r.boosted_decimal, 3.5)
    assert _close(r.ev_per_unit, 0.4)
    assert r.flag == "+EV"


def test_validation():
    bad_calls = [
        lambda: compute_ev(0.9, 50),               # decimal < 1
        lambda: compute_ev(2.0, -1),               # negative boost
        lambda: compute_ev(2.0, 50, stake=0),      # non-positive stake
        lambda: compute_ev(2.0, 50, fair_prob=1.5),
        lambda: compute_ev(2.0, 50, fair_prob=-0.1),
    ]
    for call in bad_calls:
        try:
            call()
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


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
