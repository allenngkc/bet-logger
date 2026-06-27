"""Google Sheets persistence for the bet logger (gspread). See PROJECT_PLAN.md §9.

``COLUMNS`` is the contract between this module and ``bot.py``: the bot builds a
row dict keyed by these names, and ``append_bet`` writes them in this order.
``all_records`` reads them back for ``!summary`` / ``!chart``.

gspread calls are synchronous/blocking — ``bot.py`` wraps them in
``asyncio.to_thread(...)``. The gspread import is deferred so ``COLUMNS`` and the
row-ordering logic stay importable/testable without gspread or a live Sheet.

The target Sheet must be shared with the service account's ``client_email`` as
an Editor.
"""

from __future__ import annotations

import json
import os

# Column order IS the contract. Keep in sync with bot.py's row dict and §5.
COLUMNS = [
    "logged_at",
    "placed_by",
    "bet_date",
    "category",
    "bookmaker",
    "token_pct",
    "stake",
    "currency",
    "combined_odds",
    "leg1",
    "leg2",
    "leg3",
    "num_legs",
    "potential_return",
    "boosted_return",
    "breakeven_prob",
    "fair_prob",
    "ev_per_unit",
    "ev_pct",
    "ev_profit",
    "ev_flag",
    "result",
    "actual_return",
    "profit",
    "screenshot_url",
    "notes",
    "channel_id",
    "message_id",
]

DEFAULT_WORKSHEET = "Bets"
DEFAULT_SERVICE_ACCOUNT_FILE = "service_account.json"

_worksheet = None  # cached gspread Worksheet handle (avoids re-auth per call)


def _open_worksheet():
    """Authorize, open the spreadsheet, and ensure the worksheet + header exist."""
    import gspread

    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        gc = gspread.service_account_from_dict(json.loads(raw))
    else:
        filename = os.environ.get(
            "GOOGLE_SERVICE_ACCOUNT_FILE", DEFAULT_SERVICE_ACCOUNT_FILE
        )
        gc = gspread.service_account(filename=filename)

    spreadsheet = gc.open_by_key(os.environ["SPREADSHEET_ID"])
    name = os.environ.get("WORKSHEET_NAME", DEFAULT_WORKSHEET)

    try:
        ws = spreadsheet.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=name, rows=100, cols=len(COLUMNS))

    _ensure_header(ws)
    return ws


def _ensure_header(ws) -> None:
    """Write the COLUMNS header to row 1 if it isn't already exactly that."""
    if ws.row_values(1) != COLUMNS:
        ws.update(range_name="A1", values=[COLUMNS])  # gspread v6 keyword form


def _get_worksheet():
    global _worksheet
    if _worksheet is None:
        _worksheet = _open_worksheet()
    return _worksheet


def append_bet(row: dict) -> None:
    """Append one bet as a single row, in COLUMNS order (missing keys -> "")."""
    ws = _get_worksheet()
    values = [row.get(col, "") for col in COLUMNS]
    ws.append_row(values, value_input_option="USER_ENTERED")


def all_records() -> list[dict]:
    """Return every data row as a dict keyed by header (for !summary / !chart)."""
    return _get_worksheet().get_all_records()
