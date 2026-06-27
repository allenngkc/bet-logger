"""Offline tests for sheets.py — exercises the COLUMNS contract and append
ordering with a fake worksheet, so no gspread or live Sheet is needed.

Run either way:
    python test_sheets.py
    pytest test_sheets.py
"""

import sheets


class FakeWorksheet:
    """Stand-in for a gspread Worksheet that records what would be written."""

    def __init__(self, records=None, header=None):
        self.appended = []
        self._records = records if records is not None else []
        self._header = header if header is not None else list(sheets.COLUMNS)

    def append_row(self, values, value_input_option=None):
        self.appended.append((values, value_input_option))

    def get_all_records(self):
        return self._records

    def row_values(self, n):
        return self._header


def _use_fake(ws) -> None:
    sheets._worksheet = ws  # bypass auth/network for the cached handle


def test_columns_contract():
    assert len(sheets.COLUMNS) == len(set(sheets.COLUMNS)), "duplicate column names"
    expected = [
        "logged_at", "stake", "combined_odds", "num_legs",
        "leg1", "leg2", "leg3",
        "boosted_return", "breakeven_prob", "fair_prob",
        "ev_per_unit", "ev_pct", "ev_profit", "ev_flag",
        "result", "screenshot_url", "channel_id", "message_id",
    ]
    for col in expected:
        assert col in sheets.COLUMNS, f"missing column {col}"


def test_append_bet_orders_and_fills():
    ws = FakeWorksheet()
    _use_fake(ws)
    row = {
        "logged_at": "2026-06-27T00:00:00Z",
        "stake": 10,
        "ev_flag": "+EV",
        "ev_pct": 37.5,
        "ev_profit": 3.75,
        "leg1": "Spain v France — Spain @ 1.5",
        "channel_id": 123,
        "message_id": 456,
    }  # partial dict — every other column should default to ""
    sheets.append_bet(row)

    assert len(ws.appended) == 1
    values, opt = ws.appended[0]
    assert len(values) == len(sheets.COLUMNS)
    assert opt == "USER_ENTERED"

    idx = {c: i for i, c in enumerate(sheets.COLUMNS)}
    assert values[idx["logged_at"]] == "2026-06-27T00:00:00Z"
    assert values[idx["stake"]] == 10
    assert values[idx["ev_flag"]] == "+EV"
    assert values[idx["ev_pct"]] == 37.5
    assert values[idx["ev_profit"]] == 3.75
    assert values[idx["leg1"]] == "Spain v France — Spain @ 1.5"
    assert values[idx["channel_id"]] == 123
    assert values[idx["message_id"]] == 456
    assert values[idx["notes"]] == ""        # missing -> ""
    assert values[idx["potential_return"]] == ""


def test_all_records_delegates():
    ws = FakeWorksheet(records=[{"stake": 5}, {"stake": 7}])
    _use_fake(ws)
    assert sheets.all_records() == [{"stake": 5}, {"stake": 7}]


def _run_all() -> None:
    tests = [
        v for k, v in sorted(globals().items())
        if k.startswith("test_") and callable(v)
    ]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    sheets._worksheet = None  # reset cached handle
    print(f"\n{len(tests)} tests passed.")


if __name__ == "__main__":
    _run_all()
