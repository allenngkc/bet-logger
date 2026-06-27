# Bet Logger

A Discord bot for a small group logging **token-boosted +EV parlays**. Post a
screenshot of a bet slip with a short caption; a Claude vision model extracts the
structured bet, computes the token-boosted EV, and — once the poster confirms
with ✅ — appends **one flat row per bet** to a Google Sheet for analysis.

See [`PROJECT_PLAN.md`](./PROJECT_PLAN.md) for the full design spec.

## How it works

```
post screenshot + caption
  -> Claude vision extracts the bet (forced `record_bet` tool-use)
  -> EV math (token-boosted odds, breakeven, EV% + expected profit)
  -> bot replies with an embed + ✅ / ❌
  -> poster reacts ✅  ->  one row appended to the Sheet
```

## Caption format

Post the slip image with a caption like:

```
token: 50, category: WC group stage, fair: 0.55, placed by: Alex
```

All fields are optional — the model reads what it can from the slip and caption:

| field      | meaning                                              |
|------------|------------------------------------------------------|
| `token`    | profit-boost token % (e.g. 30 / 50 / 100)            |
| `category` | your tag, e.g. "WC group stage"                      |
| `fair`     | your fair win probability 0–1 (enables the EV number)|
| `placed by`| who placed it (else your Discord display name)       |
| `date`     | bet/event date                                       |

> **EV needs a fair probability.** A single slip can't be de-vigged (that needs
> both sides of each market), so EV is only computed when you provide `fair`.
> Without it, the bot still shows boosted odds + breakeven.

## Commands

| command              | what it does                                                      |
|----------------------|-------------------------------------------------------------------|
| `!help`              | usage                                                             |
| `!summary`           | logged + settled totals (count, staked, profit, ROI, by category)|
| `!chart`             | cumulative P&L line chart of settled bets                        |
| `!slip <message_id>` | re-fetch a slip image whose Discord link expired                 |

> Settled stats need `result` (`win`/`loss`/`void`) and `profit` filled in the
> sheet. Settlement is manual for now (a `!settle` command is a future addition).

## Setup

### 1. Discord
- Create an app + bot at the [Discord Developer Portal](https://discord.com/developers/applications).
- Copy the **bot token** → `DISCORD_TOKEN`.
- Enable the **Message Content Intent** (Bot → Privileged Gateway Intents).
- Invite the bot with permissions: Read Messages, Send Messages, Attach Files,
  Add Reactions, Read Message History.

### 2. Anthropic
- Get an API key → `ANTHROPIC_API_KEY`.
- Default model is `claude-opus-4-8`; set `ANTHROPIC_MODEL=claude-sonnet-4-6` for lower cost.

### 3. Google Sheets
- Create a Google Cloud project → enable the **Google Sheets API** and **Google Drive API**.
- Create a **service account** → create a **JSON key**.
- **Share the target Sheet with the service account's `client_email` as Editor.**
- Provide the JSON via `GOOGLE_SERVICE_ACCOUNT_JSON` (full JSON on one line),
  or a local `service_account.json` via `GOOGLE_SERVICE_ACCOUNT_FILE`.
- Put the Sheet ID (from its URL) in `SPREADSHEET_ID`.

### 4. Configure
Copy `.env.example` → `.env` and fill it in. Never commit `.env` or
`service_account.json` (both are gitignored).

## Run locally

Targets **Python 3.11+** (developed/tested on 3.12).

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python bot.py
```

## Deploy

Any always-on worker host works — run `python bot.py` as a long-running worker
(no web port needed). Set the same environment variables in the host's dashboard
and paste the service-account JSON into `GOOGLE_SERVICE_ACCOUNT_JSON`. (Host-
specific files like a `Procfile` aren't included yet — add one for your chosen
platform when you pick it.)

## Tests

Pure-logic units run with no third-party deps or network:

```powershell
python test_ev.py        # EV math
python test_sheets.py    # COLUMNS contract + append ordering
python test_bot.py       # row mapping, leg formatting, summary/chart stats
```

The Anthropic, Sheets, and Discord paths need live credentials — see the
smoke-test snippets in each module / the project notes.

## Notes & gotchas

- **`pending` confirmations are in-memory** — a restart drops un-confirmed bets
  (already-logged rows are safe in the Sheet).
- **Only the poster can confirm** a bet.
- **Discord image URLs expire** (~24h). The bot stores `channel_id` + `message_id`
  and re-fetches a fresh link via `!slip`; `screenshot_url` is a convenience link only.
- **Odds are normalized to decimal** by the model; the EV math assumes decimal.
  Spot-check early extractions.
- **Don't** scrape bet365 or store third-party logins anywhere in this system.
