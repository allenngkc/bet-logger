"""Offline tests for bot.py pure helpers — no discord/dotenv needed.

Run either way:
    python test_bot.py
    pytest test_bot.py
"""

import bot
import devig
import sheets
from ev import compute_ev


def test_format_leg_basic():
    s = bot.format_leg(
        {"event": "Spain v France", "selection": "Spain", "odds_decimal": 1.5}
    )
    assert s == "Spain v France — Spain @ 1.5"


def test_format_leg_with_market_and_str_odds():
    s = bot.format_leg(
        {"event": "A v B", "selection": "Over 2.5", "market": "Goals", "odds_decimal": "1.8"}
    )
    assert s == "A v B — Over 2.5 (Goals) @ 1.8"


def test_format_leg_with_market_category():
    s = bot.format_leg(
        {
            "event": "Spain v France",
            "selection": "Over 2.5",
            "market": "Total Goals",
            "market_category": "totals_handicap",
            "odds_decimal": 1.8,
        }
    )
    assert s == "Spain v France — Over 2.5 (Total Goals) [totals_handicap] @ 1.8"


def test_build_row_maps_all_columns():
    data = {
        "bookmaker": "bet365",
        "currency": "£",
        "stake": 10,
        "combined_odds_decimal": 2.0,
        "token_pct": 50,
        "category": "WC",
        "placed_by": None,
        "fair_probability": 0.55,
        "legs": [
            {"event": "Spain v France", "selection": "Spain", "odds_decimal": 1.25},
            {"event": "Eng v Bra", "selection": "BTTS", "odds_decimal": 1.6},
            {"event": "Arg v Ger", "selection": "Over 1.5", "odds_decimal": 1.0},
        ],
        "notes": "feeling good",
    }
    ev = compute_ev(2.0, 50, stake=10.0, fair_prob=0.55)
    row = bot.build_row(
        data, ev,
        placed_by="Alex", logged_at="",
        screenshot_url="http://cdn/x.png", channel_id=111, message_id=222,
    )

    # Produces exactly the COLUMNS keys — no more, no less.
    assert set(row.keys()) == set(sheets.COLUMNS)

    assert row["bookmaker"] == "bet365"
    assert row["stake"] == 10.0
    assert row["currency"] == "£"
    assert row["token_pct"] == 50
    assert row["combined_odds"] == 2.0
    assert row["num_legs"] == 3
    assert row["leg1"].startswith("Spain v France — Spain @")
    assert row["fair_prob"] == 0.55
    assert row["ev_flag"] == "+EV"
    assert abs(row["ev_pct"] - 37.5) < 1e-9
    assert abs(row["ev_profit"] - 3.75) < 1e-9
    assert row["boosted_return"] == 25.0
    assert row["result"] == "pending"
    assert row["actual_return"] == "" and row["profit"] == ""
    assert row["channel_id"] == 111 and row["message_id"] == 222
    assert row["notes"] == "feeling good"


def test_build_row_overflow_and_no_fair_prob():
    data = {
        "stake": 5,
        "combined_odds_decimal": 3.0,
        "legs": [
            {"event": "E1", "selection": "S1", "odds_decimal": 1.2},
            {"event": "E2", "selection": "S2", "odds_decimal": 1.2},
            {"event": "E3", "selection": "S3", "odds_decimal": 1.2},
            {"event": "E4", "selection": "S4", "odds_decimal": 1.2},
        ],
    }
    ev = compute_ev(3.0, 0, stake=5.0)  # no fair_prob
    row = bot.build_row(
        data, ev,
        placed_by="x", logged_at="",
        screenshot_url="", channel_id=1, message_id=2,
    )
    assert row["num_legs"] == 4
    assert row["leg3"].startswith("E3")
    assert "extra legs" in row["notes"] and "E4" in row["notes"]
    # No fair prob -> EV reported as 0 ("0 EV"); breakeven still present.
    assert row["ev_flag"] == "0 EV"
    assert row["ev_per_unit"] == 0.0 and row["ev_pct"] == 0.0 and row["ev_profit"] == 0.0
    assert row["fair_prob"] == ""
    assert isinstance(row["breakeven_prob"], float)


def test_build_row_same_game_note():
    data = {
        "stake": 5,
        "combined_odds_decimal": 3.0,
        "legs": [
            {"event": "Spain v France", "selection": "Spain", "odds_decimal": 1.5},
            {"event": "Spain v France", "selection": "Over 2.5", "odds_decimal": 2.0},
        ],
        "notes": "same match",
    }
    ev = compute_ev(3.0, 0, stake=5.0)
    row = bot.build_row(
        data, ev,
        placed_by="x", logged_at="",
        screenshot_url="", channel_id=1, message_id=2,
        same_game=True,
    )
    assert "same match" in row["notes"]
    assert "SGP" in row["notes"] and "approximate" in row["notes"]


def test_straight_bet_with_token_is_positive_ev():
    # A single-leg "straight" bet with a profit token: the leg's odds ARE shown
    # (it's the only selection), so we de-vig them and the boost pushes it +EV.
    # Confirms the pipeline handles num_legs == 1 (no same-game, leg priced).
    legs = [
        {
            "event": "Panama v Croatia",
            "selection": "Croatia ML",
            "market_category": "soccer_1x2",
            "odds_decimal": 2.0,
        }
    ]
    assert devig.all_legs_priced(legs) is True
    assert devig.same_game(legs) is False
    fair = devig.parlay_fair_prob(legs)
    assert fair is not None and 0.0 < fair <= 1.0
    ev = compute_ev(2.0, 40, stake=25.0, fair_prob=fair)
    assert ev.flag == "+EV"

    row = bot.build_row(
        {"stake": 25, "combined_odds_decimal": 2.0, "token_pct": 40, "legs": legs},
        ev,
        placed_by="Momo", logged_at="",
        screenshot_url="", channel_id=1, message_id=2,
        same_game=False,
    )
    assert row["num_legs"] == 1
    assert row["ev_flag"] == "+EV"


def test_sgp_with_visible_leg_odds_is_leveraged():
    # When the slip DOES print per-leg odds (even if the caption omits them), the
    # pipeline reads and uses them: all_legs_priced True -> fair prob -> EV is
    # actually computed (not the 0-EV "leg odds missing" path). Counterpart to
    # test_unpriced_sgp_legs_report_zero_ev.
    legs = [
        {"event": "Panama v Croatia", "selection": "Over 2.5 Goals",
         "market_category": "totals_handicap", "odds_decimal": 1.8},
        {"event": "Panama v Croatia", "selection": "Croatia ML",
         "market_category": "soccer_1x2", "odds_decimal": 2.1},
    ]
    assert devig.all_legs_priced(legs) is True
    fair = devig.parlay_fair_prob(legs)
    assert fair is not None and 0.0 < fair <= 1.0
    ev = compute_ev(1.8 * 2.1, 40, stake=25.0, fair_prob=fair)
    assert ev.flag != "0 EV" and ev.ev_per_unit != 0.0
    # The leg odds the model read off the slip surface in the row's leg strings.
    row = bot.build_row(
        {"stake": 25, "token_pct": 40, "legs": legs}, ev,
        placed_by="Momo", logged_at="",
        screenshot_url="", channel_id=1, message_id=2, same_game=True,
    )
    assert "@ 1.8" in row["leg1"] and "@ 2.1" in row["leg2"]


def test_unpriced_sgp_legs_report_zero_ev():
    # Regression for the reported bug: a 2-leg SGP whose per-leg odds aren't shown
    # (model emitted 1.0 placeholders, "@ 1" in the embed) must NOT fabricate a
    # fair prob. With no usable leg odds, EV is reported as 0, not a bogus +EV.
    legs = [
        {"event": "Panama v Croatia", "selection": "Over 2.5 Goals",
         "market_category": "totals_handicap", "odds_decimal": 1.0},
        {"event": "Panama v Croatia", "selection": "Croatia ML",
         "market_category": "soccer_1x2", "odds_decimal": 1.0},
    ]
    fair = devig.parlay_fair_prob(legs) if devig.all_legs_priced(legs) else None
    assert fair is None
    ev = compute_ev(2.07, 40, stake=25.0, fair_prob=fair)
    assert ev.flag == "0 EV"
    assert ev.ev_per_unit == 0.0


def test_build_row_boost_note_appended():
    # When the profit boost had to be reconciled (e.g. FanDuel's already-boosted
    # price), the note is recorded so the boosted return is auditable.
    data = {
        "stake": 50,
        "combined_odds_decimal": 2.33,
        "token_pct": 50,
        "legs": [{"event": "Col v DRC", "selection": "Over 8.5 Corners"}],
        "notes": "wc",
    }
    ev = compute_ev(2.33, 50, stake=50.0)
    row = bot.build_row(
        data, ev,
        placed_by="Momo", logged_at="",
        screenshot_url="", channel_id=1, message_id=2,
        boost_note="used the slip's boosted price (the recorded odds were already boosted)",
    )
    assert "wc" in row["notes"]
    assert "[boost:" in row["notes"]
    # Boosted return is the single-boost figure (~149.75 on 2.33 @ 50%), not 198.5.
    assert abs(row["boosted_return"] - 149.75) < 0.5


def test_build_row_defaults_bookmaker_and_blanks():
    data = {"stake": "2.50", "combined_odds_decimal": 4.0, "legs": []}
    ev = compute_ev(4.0, 0, stake=2.5)
    row = bot.build_row(
        data, ev,
        placed_by="y", logged_at="",
        screenshot_url="", channel_id=1, message_id=2,
    )
    assert row["bookmaker"] == "bet365"   # defaulted
    assert row["stake"] == 2.5            # coerced from "2.50"
    assert row["token_pct"] == ""         # absent -> blank
    assert row["leg1"] == "" and row["num_legs"] == 0


def test_summarize_counts_and_settled():
    records = [
        {"stake": 10, "ev_flag": "+EV", "result": "pending", "category": "WC"},
        {"stake": 5, "ev_flag": "-EV", "result": "win", "profit": "7", "category": "WC"},
        {"stake": 8, "ev_flag": "+EV", "result": "loss", "profit": "-8", "category": "EPL"},
        {"stake": "2", "ev_flag": "unknown", "result": "void", "profit": 0, "category": ""},
    ]
    s = bot.summarize(records)
    assert s["total"] == 4
    assert abs(s["staked_all"] - 25.0) < 1e-9
    assert s["flags"] == {"+EV": 2, "-EV": 1, "0 EV": 0, "unknown": 1}
    assert s["pending"] == 1
    assert s["settled_count"] == 3
    assert abs(s["settled_staked"] - 15.0) < 1e-9     # 5 + 8 + 2
    assert abs(s["settled_profit"] - (-1.0)) < 1e-9   # 7 - 8 + 0
    assert abs(s["roi"] - (-1.0 / 15.0)) < 1e-9
    assert s["by_category"]["WC"]["count"] == 1
    assert abs(s["by_category"]["WC"]["profit"] - 7.0) < 1e-9
    assert "(uncategorized)" in s["by_category"]       # void row had empty category


def test_summarize_empty():
    s = bot.summarize([])
    assert s["total"] == 0 and s["settled_count"] == 0 and s["roi"] is None


def test_cumulative_pnl_orders_and_accumulates():
    records = [
        {"result": "win", "profit": "5", "logged_at": "2026-06-02T00:00:00Z"},
        {"result": "loss", "profit": "-3", "logged_at": "2026-06-01T00:00:00Z"},
        {"result": "pending", "profit": "", "logged_at": "2026-06-03T00:00:00Z"},
        {"result": "void", "profit": 0, "logged_at": "2026-06-04T00:00:00Z"},
    ]
    series = bot.cumulative_pnl(records)
    # ordered by logged_at: -3 (Jun 1), +5 (Jun 2), 0 (Jun 4); pending excluded
    assert [round(v, 2) for _, v in series] == [-3.0, 2.0, 2.0]


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
