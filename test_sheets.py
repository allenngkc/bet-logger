"""Offline tests for sheets.py — exercises the COLUMNS contract, category
routing, and cross-tab aggregation with fakes, so no gspread or live Sheet is
needed. Tests seed ``_ws_cache`` / ``_spreadsheet`` to avoid the gspread import
paths entirely.

Run either way:
    python test_sheets.py
    pytest test_sheets.py
"""

import os

import sheets


class FakeWorksheet:
    """Stand-in for a gspread Worksheet that records what would be written."""

    def __init__(self, title, records=None, header=None):
        self.title = title
        self.appended = []
        self.updates = []
        self._records = records if records is not None else []
        self._header = header if header is not None else list(sheets.COLUMNS)

    def append_row(self, values, value_input_option=None):
        self.appended.append((values, value_input_option))
        row = len(self._records) + 2  # header + existing rows
        return {"updates": {"updatedRange": f"{self.title}!A{row}:AB{row}"}}

    def update(self, range_name=None, values=None, value_input_option=None, **kw):
        self.updates.append((range_name, values, value_input_option))

    def get_all_records(self):
        return self._records

    def row_values(self, n):
        return self._header


class FakeSpreadsheet:
    """Stand-in for a gspread Spreadsheet — just exposes its worksheets."""

    def __init__(self, worksheets):
        self._ws = list(worksheets)

    def worksheets(self):
        return self._ws


def _reset():
    sheets._spreadsheet = None
    sheets._ws_cache.clear()


def test_columns_contract():
    assert len(sheets.COLUMNS) == len(set(sheets.COLUMNS)), "duplicate column names"
    expected = [
        "logged_at", "category", "stake", "combined_odds", "num_legs",
        "leg1", "leg2", "leg3",
        "boosted_return", "breakeven_prob", "fair_prob",
        "ev_per_unit", "ev_pct", "ev_profit", "ev_flag",
        "result", "screenshot_url", "channel_id", "message_id",
    ]
    for col in expected:
        assert col in sheets.COLUMNS, f"missing column {col}"


def test_category_tab_name_normalization():
    assert sheets._category_tab_name("Profit Token") == "profit_token"
    assert sheets._category_tab_name("Entertainment!!") == "entertainment"
    assert sheets._category_tab_name("  WC group stage ") == "wc_group_stage"
    assert sheets._category_tab_name("") == "uncategorized"
    assert sheets._category_tab_name(None) == "uncategorized"
    assert sheets._category_tab_name("a/b:c") == "a_b_c"  # Sheets-forbidden chars dropped


def test_append_bet_routes_to_category_tab():
    _reset()
    ws = FakeWorksheet("profit_token")
    sheets._ws_cache["profit_token"] = ws  # pre-seed -> no gspread/network
    sheets.append_bet({"category": "Profit Token", "stake": 10, "leg1": "x"})

    assert len(ws.appended) == 1
    values, opt = ws.appended[0]
    assert opt == "RAW"   # literal storage so "+EV" isn't parsed as a formula
    assert len(values) == len(sheets.COLUMNS)
    idx = {c: i for i, c in enumerate(sheets.COLUMNS)}
    assert values[idx["stake"]] == 10
    assert values[idx["leg1"]] == "x"
    assert values[idx["category"]] == "Profit Token"
    assert values[idx["notes"]] == ""        # missing -> ""
    _reset()


def test_result_formulas_reference_correct_cells():
    cl = sheets._col_letter
    g = cl(sheets.COLUMNS.index("stake"))
    o = cl(sheets.COLUMNS.index("boosted_return"))
    v = cl(sheets.COLUMNS.index("result"))
    w = cl(sheets.COLUMNS.index("actual_return"))
    ret, profit = sheets._result_formulas(5)
    assert ret == f'=IF(${v}5="win",${o}5,IF(${v}5="void",${g}5,IF(${v}5="loss",0,"")))'
    assert profit == f'=IF(${v}5="win",${w}5-${g}5,IF(${v}5="loss",-${g}5,IF(${v}5="void",0,"")))'


def test_append_bet_writes_result_formulas():
    _reset()
    ws = FakeWorksheet("profit_token")
    sheets._ws_cache["profit_token"] = ws
    sheets.append_bet({"category": "profit_token", "stake": 10})
    w = sheets._col_letter(sheets.COLUMNS.index("actual_return"))
    x = sheets._col_letter(sheets.COLUMNS.index("profit"))
    expected = f"{w}2:{x}2"
    assert any(rng == expected and opt == "USER_ENTERED" for rng, _, opt in ws.updates)
    _reset()


def test_append_bet_uncategorized_when_no_category():
    _reset()
    ws = FakeWorksheet("uncategorized")
    sheets._ws_cache["uncategorized"] = ws
    sheets.append_bet({"stake": 5})
    assert len(ws.appended) == 1
    _reset()


def test_all_records_aggregates_across_tabs_excluding_reserved():
    _reset()
    a = FakeWorksheet("profit_token", records=[{"stake": 1}, {"stake": 2}])
    b = FakeWorksheet("entertainment", records=[{"stake": 3}])
    dash = FakeWorksheet("Dashboard", records=[{"junk": 99}])
    sheet1 = FakeWorksheet("Sheet1", records=[{"junk": 100}])
    sheets._spreadsheet = FakeSpreadsheet([a, b, dash, sheet1])

    recs = sheets.all_records()
    assert recs == [{"stake": 1}, {"stake": 2}, {"stake": 3}]  # Dashboard/Sheet1 excluded
    _reset()


def test_service_account_json_unset_or_blank_is_none():
    os.environ.pop("GOOGLE_SERVICE_ACCOUNT_JSON", None)
    assert sheets._service_account_json() is None
    os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = "   "   # whitespace -> treated as unset
    try:
        assert sheets._service_account_json() is None
    finally:
        os.environ.pop("GOOGLE_SERVICE_ACCOUNT_JSON", None)


def test_service_account_json_valid_parses():
    os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = (
        '{"type": "service_account", "client_email": "x@y.iam.gserviceaccount.com"}'
    )
    try:
        info = sheets._service_account_json()
        assert info["client_email"] == "x@y.iam.gserviceaccount.com"
    finally:
        os.environ.pop("GOOGLE_SERVICE_ACCOUNT_JSON", None)


def test_service_account_json_invalid_raises_clear_error():
    os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = "service_account.json"  # a path, not JSON
    try:
        sheets._service_account_json()
    except RuntimeError as exc:
        assert "GOOGLE_SERVICE_ACCOUNT_JSON" in str(exc)
        assert "GOOGLE_SERVICE_ACCOUNT_FILE" in str(exc)   # points at the easy fix
    else:
        raise AssertionError("expected RuntimeError for invalid JSON")
    finally:
        os.environ.pop("GOOGLE_SERVICE_ACCOUNT_JSON", None)


def _run_all() -> None:
    tests = [
        v for k, v in sorted(globals().items())
        if k.startswith("test_") and callable(v)
    ]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    _reset()
    print(f"\n{len(tests)} tests passed.")


if __name__ == "__main__":
    _run_all()
