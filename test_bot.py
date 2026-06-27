"""Offline tests for bot.py pure helpers — no discord/dotenv needed.

Run either way:
    python test_bot.py
    pytest test_bot.py
"""

import bot
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
    # No fair prob -> EV fields blank, flag unknown, but breakeven still present.
    assert row["ev_flag"] == "unknown"
    assert row["ev_per_unit"] == "" and row["ev_pct"] == "" and row["ev_profit"] == ""
    assert row["fair_prob"] == ""
    assert isinstance(row["breakeven_prob"], float)


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
    assert s["flags"] == {"+EV": 2, "-EV": 1, "unknown": 1}
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
