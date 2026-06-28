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

import base64
import json
import os
import re

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

DEFAULT_WORKSHEET = "Bets"  # legacy single-sheet name (migrated to category tabs)
DEFAULT_SERVICE_ACCOUNT_FILE = "service_account.json"

DASHBOARD_NAME = "Dashboard"
UNCATEGORIZED = "uncategorized"
RESULT_OPTIONS = ["pending", "win", "loss", "void"]

# Cached gspread handles. The model is one worksheet (tab) per bet category;
# ``_ws_cache`` maps a normalized category name to its Worksheet.
_spreadsheet = None
_ws_cache: dict = {}


def _service_account_json() -> dict | None:
    """Resolve service-account creds from env, or None to use the file fallback.

    Order: GOOGLE_SERVICE_ACCOUNT_JSON_B64 (base64 of the JSON — the most
    shell/secret-store-safe form; no quoting or newline pitfalls), then
    GOOGLE_SERVICE_ACCOUNT_JSON (raw JSON). Empty/whitespace is treated as unset;
    a set-but-unparseable value raises a clear, actionable error.
    """
    b64 = (os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON_B64") or "").strip()
    if b64:
        try:
            return json.loads(base64.b64decode(b64).decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_JSON_B64 is set but could not be "
                f"base64-decoded into JSON ({type(exc).__name__}). Recreate it from "
                "the key file — PowerShell: "
                "[Convert]::ToBase64String([IO.File]::ReadAllBytes('service_account.json'))"
            ) from exc

    raw = (os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        looks_like = "a file path" if not raw.startswith("{") else "truncated/multi-line"
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is set but is not valid JSON "
            f"(looks {looks_like}: {exc}). It must be the full service-account "
            "JSON on a single line — or use GOOGLE_SERVICE_ACCOUNT_JSON_B64 "
            "(base64) to avoid all quoting issues."
        ) from exc


def _open_spreadsheet():
    """Authorize and open the spreadsheet by key (cached via _get_spreadsheet)."""
    import gspread

    info = _service_account_json()
    if info is not None:
        gc = gspread.service_account_from_dict(info)
    else:
        filename = os.environ.get(
            "GOOGLE_SERVICE_ACCOUNT_FILE", DEFAULT_SERVICE_ACCOUNT_FILE
        )
        gc = gspread.service_account(filename=filename)
    return gc.open_by_key(os.environ["SPREADSHEET_ID"])


def _ensure_header(ws) -> None:
    """Write the COLUMNS header to row 1 if it isn't already exactly that."""
    if ws.row_values(1) != COLUMNS:
        ws.update(range_name="A1", values=[COLUMNS])  # gspread v6 keyword form


def _get_spreadsheet():
    global _spreadsheet
    if _spreadsheet is None:
        _spreadsheet = _open_spreadsheet()
    return _spreadsheet


# Tabs that are never treated as category data sheets.
_RESERVED_TABS = {DASHBOARD_NAME, "Sheet1"}


def _category_tab_name(category: object) -> str:
    """Normalize a caption category to a tab name: lowercase snake_case.

    "Profit Token" -> "profit_token"; empty/None -> "uncategorized". Only
    ``[a-z0-9_]`` survive, which also drops characters Sheets forbids in titles.
    """
    s = re.sub(r"[^a-z0-9]+", "_", str(category or "").strip().lower()).strip("_")
    return (s or UNCATEGORIZED)[:90]


def _get_category_worksheet(tab: str):
    """Return (worksheet, created) for a category tab, creating + templating it
    on first use. Cached in ``_ws_cache``."""
    if tab in _ws_cache:
        return _ws_cache[tab], False
    import gspread

    ss = _get_spreadsheet()
    created = False
    try:
        ws = ss.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=tab, rows=100, cols=len(COLUMNS))
        created = True
    _ensure_header(ws)
    if created:
        try:
            _format_data_worksheet(ss, ws)
        except Exception as exc:  # never block logging on cosmetics
            print(f"[sheets] formatting skipped for '{tab}': {exc}")
    _ws_cache[tab] = ws
    return ws, created


def _data_worksheets() -> list:
    """All category (data) worksheets — every tab except the reserved ones."""
    return [ws for ws in _get_spreadsheet().worksheets() if ws.title not in _RESERVED_TABS]


def append_bet(row: dict) -> None:
    """Append one bet to its category's tab, in COLUMNS order (missing keys -> "").

    The destination tab comes from ``row["category"]`` (see ``_category_tab_name``);
    a brand-new category creates + templates its tab and refreshes the Dashboard.
    """
    tab = _category_tab_name(row.get("category"))
    ws, created = _get_category_worksheet(tab)
    values = [row.get(col, "") for col in COLUMNS]
    # RAW (not USER_ENTERED) so values are stored literally — otherwise Sheets
    # parses a leading "+" (e.g. the "+EV" flag) as a formula and yields #NAME?.
    resp = ws.append_row(values, value_input_option="RAW")
    # actual_return + profit are result-driven formulas (computed from the result
    # dropdown); written per-row with USER_ENTERED so they actually evaluate.
    try:
        rng = resp["updates"]["updatedRange"].split("!")[-1]
        row_num = int(re.match(r"[A-Z]+(\d+)", rng).group(1))
        _write_result_formulas(ws, row_num)
    except Exception as exc:
        print(f"[sheets] result formulas skipped: {exc}")
    if created:
        try:
            _rebuild_dashboard()
        except Exception as exc:
            print(f"[sheets] dashboard rebuild skipped: {exc}")


def all_records() -> list[dict]:
    """Every bet across all category tabs, as dicts keyed by header.

    Used by !summary / !chart / !slip; each row keeps its ``category`` column so
    downstream grouping still works.
    """
    records: list[dict] = []
    for ws in _data_worksheets():
        records.extend(ws.get_all_records())
    return records


# --------------------------------------------------------------------------- #
# Template: formatting, conditional colors, result dropdown, Dashboard tab.
#
# Everything below is cosmetic/derived — it never touches the data contract in
# COLUMNS. Whole columns are formatted (and conditional-formatting / validation
# are applied to column ranges), so rows appended later inherit the styling
# automatically. Run once on an existing sheet with:
#     python -c "import sheets; sheets.apply_template()"
# New worksheets get it automatically (see _open_worksheet).
# --------------------------------------------------------------------------- #

# Colors as 0-1 RGB dicts (Sheets API format).
_HEADER_BG = {"red": 0.17, "green": 0.24, "blue": 0.31}
_WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
_GREEN_BG = {"red": 0.83, "green": 0.92, "blue": 0.83}
_RED_BG = {"red": 0.96, "green": 0.80, "blue": 0.80}
_GREY_BG = {"red": 0.90, "green": 0.90, "blue": 0.90}
_YELLOW_BG = {"red": 1.0, "green": 0.95, "blue": 0.80}
_GREEN_TX = {"red": 0.0, "green": 0.5, "blue": 0.0}
_RED_TX = {"red": 0.80, "green": 0.0, "blue": 0.0}


def _col_letter(idx0: int) -> str:
    """0-based column index -> A1 letter (0->A, 26->AA)."""
    n, s = idx0 + 1, ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _quote_sheet(title: str) -> str:
    """Quote a worksheet title for use inside a formula (escapes apostrophes)."""
    return "'" + title.replace("'", "''") + "'"


def _grid_col(sheet_id: int, name: str, start_row: int = 1) -> dict:
    """GridRange covering one column (by COLUMNS name) from start_row to the end."""
    i = COLUMNS.index(name)
    return {
        "sheetId": sheet_id,
        "startRowIndex": start_row,
        "startColumnIndex": i,
        "endColumnIndex": i + 1,
    }


def _clear_conditional_formats(spreadsheet, sheet_id: int) -> None:
    """Remove existing conditional-format rules on a sheet (keeps apply idempotent)."""
    meta = spreadsheet.fetch_sheet_metadata(
        params={"fields": "sheets(properties.sheetId,conditionalFormats)"}
    )
    count = 0
    for s in meta.get("sheets", []):
        if s.get("properties", {}).get("sheetId") == sheet_id:
            count = len(s.get("conditionalFormats", []) or [])
            break
    if count:
        spreadsheet.batch_update(
            {"requests": [{"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": 0}}] * count}
        )


def _format_data_worksheet(spreadsheet, ws) -> None:
    """Apply header styling, number formats, conditional colors, and the result
    dropdown to the data worksheet. Idempotent — safe to re-run."""

    def col(name: str) -> str:
        return _col_letter(COLUMNS.index(name))

    last = _col_letter(len(COLUMNS) - 1)

    # --- number formats + header (one batched call) ---
    fmts = [
        {
            "range": f"A1:{last}1",
            "format": {
                "backgroundColor": _HEADER_BG,
                "textFormat": {"bold": True, "foregroundColor": _WHITE},
                "horizontalAlignment": "CENTER",
            },
        }
    ]
    money = ["stake", "potential_return", "boosted_return", "ev_profit", "actual_return", "profit"]
    for name in money:
        c = col(name)
        fmts.append({"range": f"{c}2:{c}", "format": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}}})
    for name, pattern in [("combined_odds", "0.00"), ("ev_per_unit", "0.0000"), ("num_legs", "0")]:
        c = col(name)
        fmts.append({"range": f"{c}2:{c}", "format": {"numberFormat": {"type": "NUMBER", "pattern": pattern}}})
    c = col("token_pct")
    fmts.append({"range": f"{c}2:{c}", "format": {"numberFormat": {"type": "NUMBER", "pattern": '0"%"'}}})
    c = col("ev_pct")
    fmts.append({"range": f"{c}2:{c}", "format": {"numberFormat": {"type": "NUMBER", "pattern": '0.0"%"'}}})
    for name in ["breakeven_prob", "fair_prob"]:
        c = col(name)
        fmts.append({"range": f"{c}2:{c}", "format": {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}}})
    ws.batch_format(fmts)
    ws.freeze(rows=1)

    # --- conditional formatting + dropdown + widths (one batched call) ---
    sid = ws.id
    _clear_conditional_formats(spreadsheet, sid)

    def rule(name, cond_type, value, fmt):
        values = [{"userEnteredValue": value}] if value is not None else []
        return {
            "addConditionalFormatRule": {
                "index": 0,
                "rule": {
                    "ranges": [_grid_col(sid, name)],
                    "booleanRule": {"condition": {"type": cond_type, "values": values}, "format": fmt},
                },
            }
        }

    requests = [
        rule("ev_flag", "TEXT_EQ", "+EV", {"backgroundColor": _GREEN_BG}),
        rule("ev_flag", "TEXT_EQ", "-EV", {"backgroundColor": _RED_BG}),
        rule("ev_flag", "TEXT_EQ", "0 EV", {"backgroundColor": _GREY_BG}),
        rule("ev_flag", "TEXT_EQ", "unknown", {"backgroundColor": _GREY_BG}),
        rule("result", "TEXT_EQ", "win", {"backgroundColor": _GREEN_BG}),
        rule("result", "TEXT_EQ", "loss", {"backgroundColor": _RED_BG}),
        rule("result", "TEXT_EQ", "void", {"backgroundColor": _GREY_BG}),
        rule("result", "TEXT_EQ", "pending", {"backgroundColor": _YELLOW_BG}),
        rule("profit", "NUMBER_GREATER", "0", {"textFormat": {"foregroundColor": _GREEN_TX, "bold": True}}),
        rule("profit", "NUMBER_LESS", "0", {"textFormat": {"foregroundColor": _RED_TX, "bold": True}}),
        {
            "setDataValidation": {
                "range": _grid_col(sid, "result"),
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [{"userEnteredValue": o} for o in RESULT_OPTIONS],
                    },
                    "showCustomUi": True,
                    "strict": False,
                },
            }
        },
    ]
    for name, width in [("leg1", 220), ("leg2", 220), ("leg3", 220), ("notes", 260), ("screenshot_url", 200)]:
        i = COLUMNS.index(name)
        requests.append(
            {
                "updateDimensionProperties": {
                    "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                    "properties": {"pixelSize": width},
                    "fields": "pixelSize",
                }
            }
        )
    spreadsheet.batch_update({"requests": requests})


def _result_formulas(row: int) -> list:
    """``[actual_return, profit]`` formulas for a data row, driven by the result.

    win  -> return = boosted_return (col O), profit = return - stake
    void -> return = stake, profit = 0
    loss -> return = 0, profit = -stake
    else -> blank (pending / unsettled)

    profit references the return cell, so a manual override of the return flows
    through to profit automatically.
    """
    g = f"${_col_letter(COLUMNS.index('stake'))}{row}"
    o = f"${_col_letter(COLUMNS.index('boosted_return'))}{row}"
    v = f"${_col_letter(COLUMNS.index('result'))}{row}"
    w = f"${_col_letter(COLUMNS.index('actual_return'))}{row}"
    ret = f'=IF({v}="win",{o},IF({v}="void",{g},IF({v}="loss",0,"")))'
    profit = f'=IF({v}="win",{w}-{g},IF({v}="loss",-{g},IF({v}="void",0,"")))'
    return [ret, profit]


def _write_result_formulas(ws, row: int) -> None:
    """Write the return/profit formulas into one data row (actual_return + profit
    are adjacent columns W:X)."""
    w = _col_letter(COLUMNS.index("actual_return"))
    x = _col_letter(COLUMNS.index("profit"))
    ws.update(range_name=f"{w}{row}:{x}{row}", values=[_result_formulas(row)],
              value_input_option="USER_ENTERED")


def _apply_result_formulas(ws) -> None:
    """(Re)write the result-driven return/profit formulas for every data row in a
    tab — used to backfill existing/migrated rows."""
    last = len(ws.get_all_values())  # includes the header row
    if last < 2:
        return
    w = _col_letter(COLUMNS.index("actual_return"))
    x = _col_letter(COLUMNS.index("profit"))
    rows = [_result_formulas(r) for r in range(2, last + 1)]
    ws.update(range_name=f"{w}2:{x}{last}", values=rows, value_input_option="USER_ENTERED")


def _rebuild_dashboard() -> None:
    """Rebuild the Dashboard from whatever category tabs currently exist."""
    ss = _get_spreadsheet()
    _build_dashboard(ss, [ws.title for ws in _data_worksheets()])


def _delete_dashboard_charts(spreadsheet, dash_id: int) -> None:
    """Remove existing embedded charts on the Dashboard (keeps rebuild idempotent)."""
    meta = spreadsheet.fetch_sheet_metadata(
        params={"fields": "sheets(properties.sheetId,charts.chartId)"}
    )
    ids = []
    for s in meta.get("sheets", []):
        if s.get("properties", {}).get("sheetId") == dash_id:
            ids = [c["chartId"] for c in (s.get("charts", []) or [])]
            break
    if ids:
        spreadsheet.batch_update(
            {"requests": [{"deleteEmbeddedObject": {"objectId": cid}} for cid in ids]}
        )


def _basic_chart(dash_id, title, chart_type, dom, ser, anchor_row, anchor_col=7):
    """addChart request for a COLUMN/LINE chart (domain/series GridRanges; the
    first row of each range is treated as a header)."""
    return {
        "addChart": {
            "chart": {
                "spec": {
                    "title": title,
                    "basicChart": {
                        "chartType": chart_type,
                        "legendPosition": "BOTTOM_LEGEND",
                        "headerCount": 1,
                        "domains": [{"domain": {"sourceRange": {"sources": [dom]}}}],
                        "series": [{"series": {"sourceRange": {"sources": [ser]}}, "targetAxis": "LEFT_AXIS"}],
                    },
                },
                "position": {
                    "overlayPosition": {
                        "anchorCell": {"sheetId": dash_id, "rowIndex": anchor_row, "columnIndex": anchor_col},
                        "widthPixels": 540,
                        "heightPixels": 320,
                    }
                },
            }
        }
    }


def _pie_chart(dash_id, title, dom, ser, anchor_row, anchor_col=7):
    return {
        "addChart": {
            "chart": {
                "spec": {
                    "title": title,
                    "pieChart": {
                        "legendPosition": "RIGHT_LEGEND",
                        "domain": {"sourceRange": {"sources": [dom]}},
                        "series": {"sourceRange": {"sources": [ser]}},
                    },
                },
                "position": {
                    "overlayPosition": {
                        "anchorCell": {"sheetId": dash_id, "rowIndex": anchor_row, "columnIndex": anchor_col},
                        "widthPixels": 540,
                        "heightPixels": 320,
                    }
                },
            }
        }
    }


def _build_dashboard(spreadsheet, titles: list) -> None:
    """Create/refresh the Dashboard: overall totals + a per-category table (live
    formulas across all category tabs) + embedded charts."""
    import gspread

    try:
        dash = spreadsheet.worksheet(DASHBOARD_NAME)
        _delete_dashboard_charts(spreadsheet, dash.id)
        dash.clear()
    except gspread.WorksheetNotFound:
        dash = spreadsheet.add_worksheet(title=DASHBOARD_NAME, rows=200, cols=40)
    # Ensure the grid is big enough for the helper columns (AE..AH) + chart area.
    if dash.row_count < 200 or dash.col_count < 40:
        dash.resize(rows=max(dash.row_count, 200), cols=max(dash.col_count, 40))

    qs = [_quote_sheet(t) for t in titles]
    # win/loss/void are the "settled" results.
    settled_stake = '(SUMIF({q}!V2:V,"win",{q}!G2:G)+SUMIF({q}!V2:V,"loss",{q}!G2:G)+SUMIF({q}!V2:V,"void",{q}!G2:G))'
    settled_count = '(COUNTIF({q}!V2:V,"win")+COUNTIF({q}!V2:V,"loss")+COUNTIF({q}!V2:V,"void"))'

    def joined(expr):
        return ("=" + "+".join(expr.format(q=q) for q in qs)) if qs else "0"

    def flag_join(flag):
        return "+".join(f'COUNTIF({q}!U2:U,"{flag}")' for q in qs) if qs else "0"

    # --- title + overall totals (A3:B10) ---
    dash.update(range_name="A1", values=[["BET LOGGER — DASHBOARD"]])
    summary = [
        ["Total bets", joined("COUNTA({q}!A2:A)")],
        ["Staked (all)", joined("SUM({q}!G2:G)")],
        ["Pending", joined('COUNTIF({q}!V2:V,"pending")')],
        ["Settled bets", joined(settled_count)],
        ["Settled P&L", joined("SUM({q}!X2:X)")],
        ["Settled staked", joined(settled_stake)],
        ["ROI", '=IF(B8=0,"—",B7/B8)'],
        [
            "EV flags (+/-/0/unknown)",
            f'=({flag_join("+EV")})&" / "&({flag_join("-EV")})&" / "'
            f'&({flag_join("0 EV")})&" / "&({flag_join("unknown")})',
        ],
    ]
    dash.update(range_name="A3", values=summary, value_input_option="USER_ENTERED")

    # --- per-category table (header A13:F13, one row per tab from A14) ---
    dash.update(range_name="A12", values=[["Per-category"]])
    dash.update(range_name="A13", values=[["Category", "Count", "Staked", "Settled", "Profit", "ROI"]])
    rows = []
    for t in titles:
        q = _quote_sheet(t)
        rows.append([
            t,
            f"=COUNTA({q}!A2:A)",
            f"=SUM({q}!G2:G)",
            "=" + settled_count.format(q=q),
            f"=SUM({q}!X2:X)",
            f"=IFERROR(SUM({q}!X2:X)/{settled_stake.format(q=q)},0)",
        ])
    n = len(rows)
    if rows:
        dash.update(range_name="A14", values=rows, value_input_option="USER_ENTERED")

    # --- cumulative P&L helper series (far right, AE:AH) ---
    if qs:
        v_dates = "VSTACK(" + ",".join(f"{q}!A2:A" for q in qs) + ")"
        v_profit = "VSTACK(" + ",".join(f"{q}!X2:X" for q in qs) + ")"
        v_result = "VSTACK(" + ",".join(f"{q}!V2:V" for q in qs) + ")"
        mask = f'({v_result}="win")+({v_result}="loss")+({v_result}="void")'
        dash.update(range_name="AE1", values=[["date", "profit", "#", "cum P&L"]])
        dash.update(
            range_name="AE2",
            values=[[f'=IFERROR(SORT(FILTER(HSTACK({v_dates},{v_profit}),{mask}),1,TRUE),"")']],
            value_input_option="USER_ENTERED",
        )
        dash.update(
            range_name="AG2",
            values=[['=ARRAYFORMULA(IF(AE2:AE="","",ROW(AE2:AE)-ROW(AE2)+1))']],
            value_input_option="USER_ENTERED",
        )
        dash.update(
            range_name="AH2",
            values=[['=ARRAYFORMULA(IF(AF2:AF="","",SUMIF(ROW(AF2:AF),"<="&ROW(AF2:AF),AF2:AF)))']],
            value_input_option="USER_ENTERED",
        )

    # --- cell formatting ---
    dash.batch_format([
        {"range": "A1", "format": {"textFormat": {"bold": True, "fontSize": 14}}},
        {"range": "A3:A10", "format": {"textFormat": {"bold": True}}},
        {"range": "A13:F13", "format": {"textFormat": {"bold": True, "foregroundColor": _WHITE}, "backgroundColor": _HEADER_BG}},
        {"range": "B14:B", "format": {"numberFormat": {"type": "NUMBER", "pattern": "0"}}},
        {"range": "D14:D", "format": {"numberFormat": {"type": "NUMBER", "pattern": "0"}}},
        {"range": "B4", "format": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}}},
        {"range": "B7", "format": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}}},
        {"range": "B8", "format": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}}},
        {"range": "B9", "format": {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}}},
        {"range": "C14:C", "format": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}}},
        {"range": "E14:E", "format": {"numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"}}},
        {"range": "F14:F", "format": {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}}},
    ])

    # --- charts (need at least one category) ---
    if n:
        sid = dash.id
        hdr_row, last_row = 12, 13 + n  # header at row 13 (idx 12); data rows 14..13+n

        def colrange(col_idx):
            return {"sheetId": sid, "startRowIndex": hdr_row, "endRowIndex": last_row,
                    "startColumnIndex": col_idx, "endColumnIndex": col_idx + 1}

        cat_dom = colrange(0)
        reqs = [
            _basic_chart(sid, "ROI by category", "COLUMN", cat_dom, colrange(5), anchor_row=2),
            _basic_chart(sid, "Profit by category", "COLUMN", cat_dom, colrange(4), anchor_row=19),
            _pie_chart(sid, "Bet count by category", cat_dom, colrange(1), anchor_row=36),
        ]
        line_dom = {"sheetId": sid, "startRowIndex": 0, "startColumnIndex": 32, "endColumnIndex": 33}
        line_ser = {"sheetId": sid, "startRowIndex": 0, "startColumnIndex": 33, "endColumnIndex": 34}
        reqs.append(_basic_chart(sid, "Cumulative P&L", "LINE", line_dom, line_ser, anchor_row=53))
        spreadsheet.batch_update({"requests": reqs})

    spreadsheet.batch_update(
        {"requests": [{"updateDimensionProperties": {
            "range": {"sheetId": dash.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 180}, "fields": "pixelSize"}}]}
    )


def _migrate_legacy_bets() -> int:
    """Move rows from the legacy single ``Bets`` tab into per-category tabs, then
    delete it. Returns the number of rows migrated (0 if there's no legacy tab)."""
    import gspread

    ss = _get_spreadsheet()
    legacy_name = os.environ.get("WORKSHEET_NAME", DEFAULT_WORKSHEET)
    if legacy_name in _RESERVED_TABS:
        return 0
    try:
        legacy = ss.worksheet(legacy_name)
    except gspread.WorksheetNotFound:
        return 0

    moved = 0
    for rec in legacy.get_all_records():
        ws, _ = _get_category_worksheet(_category_tab_name(rec.get("category")))
        ws.append_row([rec.get(col, "") for col in COLUMNS], value_input_option="RAW")
        moved += 1
    ss.del_worksheet(legacy)
    _ws_cache.pop(legacy_name, None)
    return moved


def apply_template() -> None:
    """Migrate the legacy ``Bets`` tab into per-category tabs (once) and rebuild
    the Dashboard. Safe to re-run — migration is skipped once ``Bets`` is gone."""
    _get_spreadsheet()
    moved = _migrate_legacy_bets()
    for ws in _data_worksheets():
        try:
            _apply_result_formulas(ws)
        except Exception as exc:
            print(f"[sheets] result formulas skipped for '{ws.title}': {exc}")
    _rebuild_dashboard()
    tabs = [ws.title for ws in _data_worksheets()]
    print(f"Migrated {moved} row(s). Category tabs: {tabs}. Result formulas applied. Dashboard rebuilt.")
