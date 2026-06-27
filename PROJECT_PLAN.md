# Bet Logger — Discord → Claude → Google Sheets

A Discord bot for a small group (~3 people) placing **token-boosted +EV parlays** (e.g. World Cup). A user posts a **screenshot of a bet slip plus a short caption**; a Claude vision agent extracts the structured bet details, computes the token-boosted EV, asks the poster to confirm, and on confirmation appends **one flat row per bet** to a Google Sheet for later analysis and visualization.

This document is the implementation spec. It captures **decisions already made** and the intended architecture so the build can proceed without re-litigating choices.

---

## 1. Goals & context

- The group makes parlays +EV by applying **profit-boost tokens** (30% / 50% / 100%, etc.). A token boosts the **profit** portion of a winning bet. To use a token, bet365 requires a **3-leg parlay**. After accounting for the vig and the token boost, the slip should be +EV.
- Today they log bets in spreadsheets manually — primitive and inconsistent across people. This bot standardizes capture.
- bet365 has **no export/API**, so reading the bet-slip **screenshot** with a vision model is the deliberate, correct approach (not a workaround).
- Output is a **Google Sheet** the whole group can read, with **one row per bet** (the 3-leg parlay is condensed inline into that row). Visualizations come later, off the same sheet.

### Non-goals
- No scraping/automation of bet365 (violates their T&Cs).
- No handling of third-party logins/credentials.
- Not a public, multi-tenant product — it's an internal tool for one trusted Discord server.

---

## 2. Decisions already locked in

| Decision | Choice | Notes |
|---|---|---|
| Storage (source of truth) | **Google Sheets**, flat one-row-per-bet | Parlay legs condensed into columns on the same row. |
| Hosting | **Cloud (Railway or Fly.io)** | Always-on worker process. |
| Language | **Python** | Best fit for Discord + Anthropic SDK + data viz in one stack. |
| Discord library | **discord.py** (>=2.3) | Handles attachments + reactions cleanly. |
| Extraction model | **Claude vision via the Anthropic API** | Default model `claude-opus-4-8` (low volume, so default to quality — cost is pennies/day at this scale). Configurable via `ANTHROPIC_MODEL`; `claude-sonnet-4-6` stays available if volume ever grows. |
| Extraction method | **Forced tool-use** (`tool_choice` → one `record_bet` tool) | Most version-tolerant way to get reliable structured JSON from a vision call. Do NOT free-text parse. |
| Confirmation | **Human-in-the-loop** — bot posts parsed result + ✅/❌ reactions; only writes on ✅ by the poster | Critical for data quality with multiple loggers. |

---

## 3. High-level architecture

```
Discord user
  └─ posts screenshot + caption ("token: 50%, category: WC group stage, fair: 0.55")
       │
       ▼
discord.py bot (on_message)
  ├─ detect image attachment
  ├─ download image bytes
  ├─ extractor.extract_bet(image, media_type, caption)  ──► Anthropic API (vision + forced tool-use)
  │                                                            └─ returns structured JSON (legs, stake, odds, token, ...)
  ├─ ev.compute_ev(...)  ──► boosted odds, breakeven prob, EV flag
  ├─ build row dict
  └─ reply with embed (parsed bet + EV) + ✅ / ❌ reactions
       │
       ▼
on_raw_reaction_add
  ├─ ✅ by poster ──► sheets.append_bet(row)  ──► Google Sheets (append one row)
  └─ ❌ by poster ──► discard; ask to re-post with corrected caption

Commands (!summary, !chart, !help) read the same sheet for stats / visualization.
```

### Process model
Single always-on Python process (`bot.py`) holding the Discord gateway connection. In-memory `pending` dict holds un-confirmed extractions keyed by the bot's reply message ID. (Acceptable for a small bot; note the persistence caveat in §11.)

The bot runs on a single asyncio event loop. The Anthropic SDK and gspread calls are **synchronous and blocking** — offload them (`await asyncio.to_thread(...)`) so a multi-second extraction or sheet write doesn't freeze the gateway (heartbeat included). See §11.

---

## 4. Project structure

```
bet-logger/
├── bot.py              # Discord bot: events, confirmation flow, commands (entry point)
├── extractor.py        # Claude vision extraction (forced tool-use) → dict
├── ev.py               # Token-boosted EV math (pure functions, no I/O)
├── sheets.py           # Google Sheets auth + append + read
├── requirements.txt
├── Procfile            # Railway/Fly process declaration: `worker: python bot.py`
├── .env.example        # documented env vars
├── .gitignore          # ignore .env, service_account.json, __pycache__
└── README.md           # setup + deploy instructions (see §10)
```

Keep `ev.py` pure (no network, no Discord) so it's unit-testable in isolation.

---

## 5. Data model — Google Sheet schema

One worksheet (default name `Bets`). The bot ensures row 1 is the header. **Column order is the contract** between `sheets.py` and `bot.py` — define it once as a `COLUMNS` list in `sheets.py` and have `bot.py` build a dict keyed by these names.

| Column | Meaning | Filled when |
|---|---|---|
| `logged_at` | UTC ISO timestamp when confirmed/logged | on log |
| `placed_by` | Who placed it (caption, else Discord display name) | on log |
| `bet_date` | Date of bet/event if stated in caption | on log |
| `category` | User tag, e.g. "WC group stage" | on log |
| `bookmaker` | Default "bet365" | on log |
| `token_pct` | Boost token %, e.g. 30 / 50 / 100 (blank if none) | on log |
| `stake` | Wager amount (number) | on log |
| `currency` | £ / $ / EUR etc. | on log |
| `combined_odds` | Combined parlay odds in **decimal** | on log |
| `leg1` | `"Event — Selection @ odds"` | on log |
| `leg2` | `"Event — Selection @ odds"` | on log |
| `leg3` | `"Event — Selection @ odds"` | on log |
| `num_legs` | Number of legs (normally 3) | on log |
| `potential_return` | Bookmaker payout **before** boost, if visible | on log |
| `boosted_return` | Total return incl. stake **with** token boost (computed) | on log |
| `breakeven_prob` | Win probability needed to break even **after** boost (computed) | on log |
| `fair_prob` | User-supplied fair win probability (0–1), blank if none | on log |
| `ev_per_unit` | EV per 1 unit staked (computed, blank if no `fair_prob`) | on log |
| `ev_pct` | EV as % of stake = `ev_per_unit × 100` (computed, blank if no `fair_prob`) | on log |
| `ev_profit` | Expected profit in currency = `ev_per_unit × stake` (computed, blank if no `fair_prob`) | on log |
| `ev_flag` | `+EV` / `-EV` / `unknown` | on log |
| `result` | `pending` → later `win`/`loss`/`void` | starts `pending` |
| `actual_return` | Settled return | manual / future settle command |
| `profit` | Settled profit (return − stake) | manual / future settle command |
| `screenshot_url` | Discord CDN URL of the slip image — **signed and expires (~24h); convenience link only** | on log |
| `notes` | Extra notes from caption | on log |
| `channel_id` | Discord channel ID (paired with `message_id` to refresh the slip image) | on log |
| `message_id` | Discord message ID (traceability + image refresh) | on log |

> The 3-leg parlay is condensed into `leg1`/`leg2`/`leg3` columns so each bet is exactly one row — per the chosen flat-rows design. If a parlay ever has >3 legs, overflow into `notes` (or extend columns); 3 legs is the expected norm.

---

## 6. EV math (`ev.py`) — get this exact

All math in **decimal odds**. A "profit-boost token" multiplies the **profit** portion of a winning bet (not the stake).

**Conversions / helpers**
- `american_to_decimal(odds)`: `1 + odds/100` if `odds > 0`, else `1 + 100/abs(odds)`.
- `implied_prob(decimal)`: `1 / decimal`.

**Boosted odds.** For combined decimal odds `D` and boost percent `b` (e.g. 50 → 0.5):
```
boosted_decimal = 1 + (D - 1) * (1 + b/100)
```
- 0% token → `D` unchanged.
- 50% token → profit ×1.5.
- 100% token → profit ×2.

**Breakeven probability** (after boost): `1 / boosted_decimal`.

**Boosted return per unit staked**: `boosted_decimal` (total return incl. stake). Multiply by `stake` for the sheet's `boosted_return`.

> **Boost caps:** bet365 profit boosts usually have a max bonus cap. Above the cap, real return < `boosted_decimal × stake`, so `boosted_return`/`ev_per_unit` would overstate. At our usual unit sizes the cap is effectively never binding, so v1 ignores it; if stakes grow, add an optional `max_boost` (or cap the boosted profit) before computing EV.

**Expected value per unit** (only when the user supplies a fair win probability `p`):
```
ev_per_unit = p * (boosted_decimal - 1) - (1 - p)
```
`ev_flag = "+EV"` if `ev_per_unit > 0`, else `"-EV"`. If no `p` supplied, `ev_flag = "unknown"` and `ev_per_unit` is blank — but still record `breakeven_prob` so the user can eyeball it.

**Suggested API**
```python
@dataclass
class EVResult:
    combined_decimal: float
    boost_pct: float
    boosted_decimal: float
    breakeven_prob: float
    boosted_return_per_unit: float   # = boosted_decimal
    fair_prob: float | None
    ev_per_unit: float | None
    flag: str                         # "+EV" | "-EV" | "unknown"

def compute_ev(combined_decimal: float, boost_pct: float,
               stake: float = 1.0, fair_prob: float | None = None) -> EVResult: ...
```

> **De-vig note for the agent and the README:** true de-vigging needs *both* sides of each market, which a single bet slip doesn't contain. So real EV requires the user to provide a fair probability (or fair odds) per the caption. The bot computes EV when `fair_prob` is given; otherwise it reports boosted odds + breakeven only. Keep `fair_prob` as one overall number for v1 (per-leg fair odds is a future enhancement).

---

## 7. Extraction (`extractor.py`)

Use the Anthropic Python SDK with **vision + a single forced tool call**.

- Client: `anthropic.Anthropic()` (reads `ANTHROPIC_API_KEY` from env).
- Model: `os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")`.
- Image: base64-encode bytes; pass as an `image` content block with the attachment's `media_type` (one of `image/png`, `image/jpeg`, `image/gif`, `image/webp`; default `image/png`).
- Caption: pass as a `text` block.
- `tools=[BET_TOOL]`, `tool_choice={"type": "tool", "name": "record_bet"}`, `max_tokens≈2000`.
- Leave thinking **off** (the default on `claude-opus-4-8` when the `thinking` field is omitted) — extraction is simple and doesn't need it. Do **not** pass `thinking={"type":"enabled","budget_tokens":N}`: that form is removed on Opus 4.8 and returns a 400. If thinking is ever wanted, the only valid form is `thinking={"type":"adaptive"}`.
- Read the `tool_use` block whose `name == "record_bet"` and return `block.input` (a dict). Raise if absent.

**System prompt (intent):** "You extract structured betting data from a screenshot of a bet slip plus an optional user caption. Always express odds in DECIMAL (fractional 5/2 = 3.5, American +150 = 2.5). If combined odds aren't printed, compute them as the product of the leg decimal odds. Use the caption for category, who placed it, token %, and any stated fair probability. Use null when a value is absent. Call `record_bet` exactly once."

**`record_bet` tool input schema** (plain JSON Schema — avoid unsupported constraints):
```json
{
  "type": "object",
  "properties": {
    "bookmaker":   {"type": "string"},
    "currency":    {"type": "string"},
    "stake":       {"type": "number"},
    "combined_odds_decimal": {"type": "number", "description": "Total parlay odds in DECIMAL"},
    "potential_return":      {"type": "number", "description": "Payout before any boost, if visible"},
    "token_pct":   {"type": ["number","null"], "description": "Boost token % (30/50/100), null if none"},
    "category":    {"type": ["string","null"], "description": "User category/tag from caption"},
    "placed_by":   {"type": ["string","null"]},
    "bet_date":    {"type": ["string","null"], "description": "ISO date if stated"},
    "fair_probability": {"type": ["number","null"], "description": "Overall fair win prob 0-1 if user provides it"},
    "legs": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "event":        {"type": "string"},
          "selection":    {"type": "string"},
          "market":       {"type": ["string","null"]},
          "odds_decimal": {"type": "number"}
        },
        "required": ["event", "selection", "odds_decimal"]
      }
    },
    "notes": {"type": ["string","null"]}
  },
  "required": ["stake", "combined_odds_decimal", "legs"]
}
```

**Fallback:** if `combined_odds_decimal` is missing/0 but legs exist, compute it as the product of leg `odds_decimal` before EV.

**Schema validity:** forced tool-use guarantees Claude *calls* `record_bet`, but not that the input is schema-valid — so keep the defensive coercion (stake/odds → float, see §11). For a hard guarantee, `strict: true` on the tool (requires `additionalProperties: false` on every object) or structured outputs (`output_config.format`) are available on 4.8, but both add rigidity; the forced-tool approach is a fine default.

---

## 8. Bot behavior (`bot.py`)

**Intents:** `discord.Intents.default()` + `message_content = True` (privileged — must also be enabled in the Discord Developer Portal). Reactions are covered by default intents; use `on_raw_reaction_add` (survives restarts / uncached messages).

**`on_message`:**
1. Ignore bots.
2. Find the first attachment whose `content_type` is an image type.
3. If none and message starts with `!`, route to command handler; else return.
4. With `channel.typing()`: download bytes, call `extractor.extract_bet` **via `await asyncio.to_thread(...)`** (blocking SDK call — don't run it on the event loop), compute combined-odds fallback, call `ev.compute_ev`, build the row dict (capture `channel_id` + `message_id` for later image refresh), post an **embed** summarizing the bet + EV, add ✅ and ❌ reactions, and store `pending[reply.id] = {"row": row, "author_id": message.author.id}`.
5. On extraction error, reply with a friendly error (don't crash).

**Embed contents:** legs (one per line), stake + currency, combined odds, token %, boosted return, breakeven prob, `fair_prob` (if any), `ev_flag` (+ EV value if computed). Make `+EV`/`-EV`/`unknown` visually obvious (e.g. color or emoji).

**`on_raw_reaction_add`:**
- Ignore the bot's own reactions.
- Look up `pending[message_id]`; ignore if absent.
- Only the **original poster** (`author_id`) may confirm/discard.
- ✅ → **pop from pending first (claim it), then** `await asyncio.to_thread(sheets.append_bet, row)`, reply "Logged". Popping before the write closes a double-confirm race (a rapid second ✅ landing during the `await` could otherwise log the bet twice). On sheet error, **re-insert into pending** and reply with the error so they can retry.
- ❌ → reply "Discarded — re-post with corrections in the caption", pop from pending.

**Commands:**
- `!help` — usage: post a screenshot with a caption like `token: 50, category: WC group stage, fair: 0.55, placed by: Alex`.
- `!summary` — read sheet via `sheets.all_records()`; compute totals over settled rows (count, total staked, total profit, ROI = profit/staked) and a breakdown by `category`. Coerce numeric strings safely; skip `pending` rows for profit.
- `!chart` — generate a **cumulative P&L** line chart with matplotlib (Agg backend), write to an in-memory `BytesIO`, send as a `discord.File`. Order by `logged_at`. This proves the visualization path off the sheet.
- `!slip <message_id>` — re-fetch the original slip image (its CDN URL expires). Resolve the row's `channel_id` → `channel.fetch_message(message_id)` → re-post `attachments[0].url` (freshly signed on each fetch) or re-upload the bytes as a `discord.File`. This is why we store `channel_id` + `message_id` instead of a permanent link.

**Run:** `client.run(os.environ["DISCORD_TOKEN"])`. Load `.env` via `python-dotenv` at top of `bot.py` for local dev.

---

## 9. Google Sheets (`sheets.py`)

- Auth via a **Google service account**:
  - If `GOOGLE_SERVICE_ACCOUNT_JSON` env var is set, `json.loads` it and use `gspread.service_account_from_dict(info)` (best for Railway/Fly — paste JSON as an env var).
  - Else fall back to `gspread.service_account(filename=GOOGLE_SERVICE_ACCOUNT_FILE)` (default `service_account.json`) for local dev.
- Open by key: `gc.open_by_key(os.environ["SPREADSHEET_ID"])`; worksheet name from `WORKSHEET_NAME` (default `Bets`), create it if missing.
- Ensure header: if row 1 ≠ `COLUMNS`, write `COLUMNS` to `A1` (gspread v6: `ws.update(range_name="A1", values=[COLUMNS])`).
- `append_bet(row: dict)`: `ws.append_row([row.get(c, "") for c in COLUMNS], value_input_option="USER_ENTERED")`.
- `all_records() -> list[dict]`: `ws.get_all_records()`.
- Cache the worksheet handle in a module global to avoid re-auth per call.
- **The Google Sheet must be shared with the service account's `client_email` (Editor).**

---

## 10. Configuration & deployment

### Environment variables (`.env.example`)
```
DISCORD_TOKEN=                     # Discord bot token
ANTHROPIC_API_KEY=                 # Anthropic API key
ANTHROPIC_MODEL=claude-opus-4-8    # or claude-sonnet-4-6 for lower cost
SPREADSHEET_ID=                    # Google Sheet ID from its URL
WORKSHEET_NAME=Bets
GOOGLE_SERVICE_ACCOUNT_JSON=       # full service-account JSON (one line) — preferred on cloud
# GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json   # local-dev alternative
```

### requirements.txt
```
discord.py>=2.3
anthropic>=0.69        # bumped from 0.40 (very old); pin to the current release at build time
gspread>=6.0
google-auth>=2.20
matplotlib>=3.7
python-dotenv>=1.0
```

### Procfile
```
worker: python bot.py
```

### Setup steps (put full versions in README)
1. **Discord app:** create app + bot at the Discord Developer Portal; copy the bot token; enable **Message Content Intent**; invite the bot to the server with permissions to read messages, send messages, attach files, add reactions, read message history.
2. **Anthropic:** get an API key; set `ANTHROPIC_API_KEY`.
3. **Google:** create a Google Cloud project → enable **Google Sheets API** + **Google Drive API** → create a **service account** → create a JSON key → **share the target Sheet with the service account email (Editor)**. Put the JSON in `GOOGLE_SERVICE_ACCOUNT_JSON`.
4. **Deploy:** push to Railway/Fly as a **worker** (no web port needed); set all env vars in the dashboard; it runs `python bot.py`.

> Secrets only via env vars. `.gitignore` must exclude `.env` and `service_account.json`. Never commit tokens/keys.

---

## 11. Implementation notes & gotchas

- **Message Content Intent** is privileged — the bot can't read captions without it enabled in both code and the Developer Portal.
- **`pending` is in-memory** — a redeploy/restart drops un-confirmed bets (already-logged rows are safe in the Sheet). Acceptable for v1; a future version could persist pending state. Mention this in README.
- **Blocking calls on the event loop** — the Anthropic SDK and gspread are synchronous. Call them with `await asyncio.to_thread(...)` (or use `AsyncAnthropic`); calling them directly in `on_message`/`on_raw_reaction_add` freezes the whole bot (heartbeat included) for the duration.
- **Only the poster confirms** — prevents someone else's ✅ from logging a bet under the wrong person.
- **Double-confirm race** — on ✅, pop from `pending` *before* the sheet write and re-insert on failure. Otherwise a rapid second ✅ during the `await` can append the same bet twice.
- **Discord image URLs expire** — attachment CDN URLs are signed and 403 after ~24h. Store `channel_id` + `message_id` (durable) and refresh via `!slip` rather than relying on `screenshot_url`.
- **Odds normalization** — bet365 shows fractional/decimal/American depending on region. The model normalizes to decimal; the EV math assumes decimal. Spot-check early extractions.
- **Parse tool input as structured data** — it's already a dict from `block.input`; validate types defensively (stake/odds may come back as strings — coerce to float).
- **media_type** must be a supported image type; map the Discord `content_type`, default to `image/png`.
- **Cost** — at our volume (a few bets/day among ~3 people) an Opus 4.8 extraction is pennies/day, so default to `claude-opus-4-8` for quality. `claude-sonnet-4-6` via `ANTHROPIC_MODEL` stays available if volume grows — a deliberate switch, not a silent downgrade.
- **matplotlib** must use the `Agg` backend (`matplotlib.use("Agg")` before `pyplot`) since there's no display on the server.
- **Don't** scrape bet365 or store third-party credentials anywhere in this system.

---

## 12. Build order (suggested)

1. `ev.py` + a tiny unit test (pure math, no deps).
2. `extractor.py` — verify against 2–3 real bet-slip screenshots; confirm decimal normalization and token/caption parsing.
3. `sheets.py` — verify header creation + append against a test Sheet.
4. `bot.py` — wire `on_message` → embed → reactions → `on_raw_reaction_add` → append; test the full confirm loop in a private channel.
5. `!summary`, then `!chart`.
6. Deploy to Railway/Fly; run an end-to-end test in the real server.

---

## 13. Future enhancements (not in v1)

- `!settle <message_id> win|loss|void` command to fill `result`/`actual_return`/`profit` from Discord.
- Per-leg fair odds → proper per-leg de-vig and a more rigorous parlay EV.
- Persist `pending` (SQLite/Redis) so confirmations survive restarts.
- Richer dashboard (Streamlit/Metabase) reading the Sheet: EV-over-time, hit rate by category, ROI by token type, per-person leaderboards.
- DB-as-source-of-truth (SQLite/Postgres) with the Sheet auto-synced, if flat rows become limiting.
