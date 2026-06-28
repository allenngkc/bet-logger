# Deploying the bet-logger bot to Fly.io

The bot is a Discord **gateway worker** — it holds a persistent connection and
runs always-on. There's no web port. All data lives in Google Sheets, and the
only local state (pending ✅/❌ confirmations) is fine to lose on restart, so the
machine is disposable and needs no volume.

Target: one `shared-cpu-1x` / 512 MB machine ≈ **~$2/month**.

---

## 1. Install flyctl and sign in

Windows PowerShell:

```powershell
pwsh -Command "iwr https://fly.io/install.ps1 -useb | iex"
fly auth signup   # or: fly auth login
```

## 2. Create the app

Fly app names are global, so pick a unique one and put it in `fly.toml` (`app = ...`):

```powershell
fly apps create your-unique-bet-logger
```

Then edit `fly.toml`: set `app` to that name and `primary_region` to a nearby
region (`fly platform regions` lists them; latency isn't critical for a bot).

> Don't run `fly launch` — it would overwrite the hand-written `fly.toml`.

## 3. Set secrets (these become the bot's environment variables)

```powershell
fly secrets set `
  DISCORD_TOKEN="..." `
  ANTHROPIC_API_KEY="..." `
  ANTHROPIC_MODEL="claude-opus-4-8" `
  SPREADSHEET_ID="..." `
  WORKSHEET_NAME="Bets" `
  BET_CHANNEL_ID="..."
```

The Google service account must go in as **one-line JSON** (not the file). Minify it:

```powershell
$json = Get-Content service_account.json -Raw | ConvertFrom-Json | ConvertTo-Json -Compress
fly secrets set GOOGLE_SERVICE_ACCOUNT_JSON="$json"
```

(Git Bash equivalent: `fly secrets set GOOGLE_SERVICE_ACCOUNT_JSON="$(tr -d '\n' < service_account.json)"`.)

`sheets.py` already prefers `GOOGLE_SERVICE_ACCOUNT_JSON` over the file, so no
`service_account.json` is shipped — it's excluded by `.dockerignore`.

## 4. Deploy

```powershell
fly deploy
```

## 5. Pin to exactly one machine (important)

Two machines = two bots = duplicate replies and double-logging. Keep it at one:

```powershell
fly scale count 1
fly status        # confirm a single machine, state "started"
```

## 6. Verify

```powershell
fly logs          # should show: "Logged in as <bot> · watching ..."
```

Post a slip in the bet channel and confirm it logs to the Sheet.

---

## Day-to-day

| Task | Command |
|---|---|
| Tail logs | `fly logs` |
| Restart (drops pending confirmations) | `fly machine restart` / `fly apps restart` |
| Ship code changes | `git`-commit, then `fly deploy` |
| Rotate a secret | `fly secrets set KEY="..."` (triggers a restart) |
| Change RAM | `fly scale memory 512` |

## Notes
- Hosting cost is separate from the **Anthropic API** (billed to your API key,
  ~pennies per slip).
- The bot needs the **Message Content** privileged intent enabled in the Discord
  Developer Portal — that's a Discord setting, unaffected by hosting.
- Restarts are safe: in-flight ✅ confirmations are lost, but nothing logged is.
