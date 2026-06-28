"""Discord bot: slip screenshot -> Claude extraction -> EV -> confirm -> Sheet.

See PROJECT_PLAN.md §8. Stage 4 covers the core confirm loop and `!help`; the
read/util commands (`!summary`, `!chart`, `!slip`) are added in Stage 5.

Design note: the pure helpers (`format_leg`, `build_row`) live at module top and
import only `ev`/`extractor`/`sheets` (all dependency-light), so this module
imports without `discord`/`dotenv` and the row-mapping is unit-testable. All
Discord wiring lives inside `main()`, which imports `discord` and `dotenv`.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from io import BytesIO

import devig
import extractor
import sheets
from ev import EVResult, compute_ev

BOOKMAKER_DEFAULT = "bet365"
MAX_LEG_COLUMNS = 3
CONFIRM = "✅"
DISCARD = "❌"

HELP_TEXT = (
    "**Bet Logger**\n"
    "Post a screenshot of your bet slip with a caption like:\n"
    "`token: 50, category: WC group stage, placed by: Alex`\n\n"
    "I read the slip, classify each leg's market, de-vig it to estimate fair "
    "odds, compute the token-boosted EV, and post it back. "
    f"React {CONFIRM} to log it to the sheet, or {DISCARD} to discard "
    "(only the person who posted can confirm).\n\n"
    "Caption fields (all optional): `token` (boost %), `category` (your tag), "
    "`placed by`, `date`. EV is estimated automatically from the slip.\n\n"
    "**Commands:** `!help` · `!summary` (running stats) · `!chart` (cumulative P&L) · "
    "`!slip <message_id>` (re-fetch a slip image whose link expired)."
)


# --------------------------------------------------------------------------- #
# Pure helpers (no Discord) — unit-tested in test_bot.py
# --------------------------------------------------------------------------- #

def _to_float(value: object) -> float | None:
    """Best-effort float coercion; returns None on failure (defensive, §11)."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _blank(value: object) -> object:
    """None -> "" so the Sheet shows a blank cell rather than the text 'None'."""
    return "" if value is None else value


def _round(value: float | None, ndigits: int) -> float | None:
    return None if value is None else round(value, ndigits)


def _fmt_odds(value: object) -> str:
    """Compact numeric label, e.g. 1.5, 2, 3.75 (used in leg strings / embed)."""
    num = _to_float(value)
    return f"{num:g}" if num is not None else str(value)


def _money(amount: float, currency: str) -> str:
    """Format a money amount, prefixing a symbol or suffixing a code."""
    cur = (currency or "").strip()
    if cur in {"£", "$", "€", "¥"}:
        return f"{cur}{amount:,.2f}"
    return f"{amount:,.2f} {cur}".strip()


def format_leg(leg: dict) -> str:
    """Render one leg as 'Event — Selection [(Market)] [category] @ odds' (§5).

    The ``[category]`` tag (the de-vig market_category) is appended when present
    so the per-leg classification is visible/stored in the leg cell.
    """
    event = str(leg.get("event", "")).strip()
    selection = str(leg.get("selection", "")).strip()
    market = leg.get("market")
    category = leg.get("market_category")
    odds = _to_float(leg.get("odds_decimal"))

    label = f"{event} — {selection}" if (event or selection) else "(leg)"
    if market:
        label += f" ({str(market).strip()})"
    if category:
        label += f" [{str(category).strip()}]"
    if odds is not None:
        label += f" @ {_fmt_odds(odds)}"
    return label


def build_row(
    data: dict,
    ev: EVResult,
    *,
    placed_by: str,
    logged_at: str,
    screenshot_url: str,
    channel_id: int,
    message_id: int,
    same_game: bool = False,
) -> dict:
    """Map extraction output + EVResult to a row dict keyed by sheets.COLUMNS.

    `logged_at` is set at confirm time (left "" until then). Legs beyond the
    three columns overflow into `notes`. `fair_prob` holds the de-vig estimate.
    When `same_game` is set, a note marks the EV as approximate (correlated legs).
    """
    legs = [leg for leg in (data.get("legs") or []) if isinstance(leg, dict)]
    leg_strs = [format_leg(leg) for leg in legs]

    notes = (data.get("notes") or "").strip()
    if same_game:
        notes = (f"{notes} " if notes else "") + "[SGP — EV approximate (correlated legs)]"
    if len(leg_strs) > MAX_LEG_COLUMNS:
        extra = " | ".join(leg_strs[MAX_LEG_COLUMNS:])
        notes = (f"{notes} " if notes else "") + f"[extra legs: {extra}]"

    row = {
        "logged_at": logged_at,
        "placed_by": placed_by,
        "bet_date": _blank(data.get("bet_date")),
        "category": _blank(data.get("category")),
        "bookmaker": (data.get("bookmaker") or BOOKMAKER_DEFAULT),
        "token_pct": _blank(data.get("token_pct")),
        "stake": _blank(_to_float(data.get("stake"))),
        "currency": _blank(data.get("currency")),
        "combined_odds": round(ev.combined_decimal, 4),
        "num_legs": len(leg_strs),
        "potential_return": _blank(_to_float(data.get("potential_return"))),
        "boosted_return": round(ev.boosted_return, 2),
        "breakeven_prob": round(ev.breakeven_prob, 4),
        "fair_prob": _blank(ev.fair_prob),
        "ev_per_unit": _blank(_round(ev.ev_per_unit, 4)),
        "ev_pct": _blank(_round(ev.ev_pct, 2)),
        "ev_profit": _blank(_round(ev.ev_profit, 2)),
        "ev_flag": ev.flag,
        "result": "pending",
        "actual_return": "",
        "profit": "",
        "screenshot_url": screenshot_url,
        "notes": notes,
        "channel_id": channel_id,
        "message_id": message_id,
    }
    for i in range(MAX_LEG_COLUMNS):
        row[f"leg{i + 1}"] = leg_strs[i] if i < len(leg_strs) else ""
    return row


def _to_int(value: object) -> int | None:
    num = _to_float(value)
    return int(num) if num is not None else None


def summarize(records: list[dict]) -> dict:
    """Aggregate logged + settled stats from sheet records (pure; testable)."""

    def num(value: object) -> float:
        coerced = _to_float(value)
        return coerced if coerced is not None else 0.0

    flags = {"+EV": 0, "-EV": 0, "0 EV": 0, "unknown": 0}
    for r in records:
        flag = str(r.get("ev_flag", "")).strip()
        if flag in flags:
            flags[flag] += 1

    pending = sum(
        1 for r in records if str(r.get("result", "")).strip().lower() == "pending"
    )
    settled = [
        r for r in records
        if str(r.get("result", "")).strip().lower() in ("win", "loss", "void")
    ]
    settled_staked = sum(num(r.get("stake")) for r in settled)
    settled_profit = sum(num(r.get("profit")) for r in settled)

    by_category: dict[str, dict] = {}
    for r in settled:
        cat = str(r.get("category", "")).strip() or "(uncategorized)"
        agg = by_category.setdefault(cat, {"count": 0, "staked": 0.0, "profit": 0.0})
        agg["count"] += 1
        agg["staked"] += num(r.get("stake"))
        agg["profit"] += num(r.get("profit"))

    return {
        "total": len(records),
        "staked_all": sum(num(r.get("stake")) for r in records),
        "flags": flags,
        "pending": pending,
        "settled_count": len(settled),
        "settled_staked": settled_staked,
        "settled_profit": settled_profit,
        "roi": settled_profit / settled_staked if settled_staked else None,
        "by_category": by_category,
    }


def cumulative_pnl(records: list[dict]) -> list[tuple[str, float]]:
    """Settled bets ordered by logged_at, as (timestamp, running_profit) points."""
    settled = []
    for r in records:
        if str(r.get("result", "")).strip().lower() in ("win", "loss", "void"):
            profit = _to_float(r.get("profit"))
            if profit is not None:
                settled.append((str(r.get("logged_at", "")), profit))
    settled.sort(key=lambda item: item[0])

    series: list[tuple[str, float]] = []
    running = 0.0
    for ts, profit in settled:
        running += profit
        series.append((ts, running))
    return series


def render_pnl_chart(series: list[tuple[str, float]]) -> bytes:
    """Render a cumulative P&L line chart to PNG bytes (matplotlib, Agg backend)."""
    import matplotlib

    matplotlib.use("Agg")  # no display on the server
    import matplotlib.pyplot as plt

    ys = [point[1] for point in series]
    xs = list(range(1, len(series) + 1))

    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.plot(xs, ys, marker="o")
    ax.axhline(0, color="grey", linewidth=0.8)
    ax.set_title("Cumulative P&L (settled bets)")
    ax.set_xlabel("settled bet #")
    ax.set_ylabel("cumulative profit")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    buf = BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Discord wiring
# --------------------------------------------------------------------------- #

def _first_image(message) -> object | None:
    """Return the first image attachment on a message, or None."""
    image_exts = (".png", ".jpg", ".jpeg", ".gif", ".webp")
    for att in message.attachments:
        if (att.content_type or "").startswith("image/"):
            return att
        if att.filename.lower().endswith(image_exts):
            return att
    return None


def main() -> None:
    import discord
    from dotenv import load_dotenv

    load_dotenv()  # local dev: pull DISCORD_TOKEN etc. from .env

    raw_channel = os.environ.get("BET_CHANNEL_ID", "").strip()
    bet_channel_id = int(raw_channel) if raw_channel else None

    intents = discord.Intents.default()
    intents.message_content = True  # privileged — enable in the Developer Portal too
    client = discord.Client(intents=intents)

    # reply.id -> {"row": dict, "author_id": int}. In-memory; dropped on restart (§11).
    pending: dict[int, dict] = {}

    def build_embed(
        data: dict, ev: EVResult, row: dict, *, same_game: bool = False
    ) -> "discord.Embed":
        currency = data.get("currency") or ""
        if ev.flag == "+EV":
            color, emoji = discord.Color.green(), "✅"
        elif ev.flag == "-EV":
            color, emoji = discord.Color.red(), "🚫"
        elif ev.flag == "0 EV":
            color, emoji = discord.Color.greyple(), "➖"
        else:
            color, emoji = discord.Color.light_grey(), "❓"

        category = (data.get("category") or "").strip()
        embed = discord.Embed(
            title=f"Bet slip — {category}" if category else "Bet slip",
            color=color,
        )

        legs = [row[f"leg{i}"] for i in (1, 2, 3) if row[f"leg{i}"]]
        embed.add_field(
            name=f"Legs ({row['num_legs']})",
            value="\n".join(legs) or "—",
            inline=False,
        )
        embed.add_field(
            name="Stake",
            value=_money(row["stake"], currency) if row["stake"] != "" else "—",
        )
        embed.add_field(name="Combined odds", value=_fmt_odds(ev.combined_decimal))
        token = row["token_pct"]
        embed.add_field(
            name="Token",
            value=f"{_fmt_odds(token)}%" if token != "" else "none",
        )
        embed.add_field(name="Boosted return", value=_money(ev.boosted_return, currency))
        embed.add_field(name="Breakeven", value=f"{ev.breakeven_prob * 100:.1f}%")
        if ev.fair_prob is not None:
            embed.add_field(name="Fair prob (est.)", value=f"{ev.fair_prob * 100:.1f}%")

        if ev.flag == "0 EV":
            ev_value = f"{emoji} 0 EV — individual leg odds missing, EV not counted"
        elif ev.ev_pct is not None and ev.ev_profit is not None:
            sign = "+" if ev.ev_profit >= 0 else "-"
            profit = _money(abs(ev.ev_profit), currency)
            ev_value = f"{ev.ev_pct:+.1f}%  ({sign}{profit})  {emoji} {ev.flag}  · est."
        else:
            ev_value = f"{emoji} unknown — couldn't estimate fair odds from the legs"
        embed.add_field(name="EV", value=ev_value, inline=False)

        if same_game:
            embed.add_field(
                name="⚠️ Same-game parlay",
                value="Legs are correlated — the independence assumption breaks, "
                "so this EV is only a rough estimate.",
                inline=False,
            )

        embed.set_footer(
            text=f"Placed by {row['placed_by']} · "
            f"{CONFIRM} to log · {DISCARD} to discard (poster only)"
        )
        return embed

    async def handle_summary(message) -> None:
        records = await asyncio.to_thread(sheets.all_records)
        s = summarize(records)
        embed = discord.Embed(
            title="Bet Logger — Summary", color=discord.Color.blurple()
        )
        embed.add_field(
            name="Logged",
            value=f"{s['total']} bets · staked {s['staked_all']:.2f} · {s['pending']} pending",
            inline=False,
        )
        f = s["flags"]
        embed.add_field(
            name="EV flags",
            value=f"+EV {f['+EV']} · -EV {f['-EV']} · 0 EV {f['0 EV']} · unknown {f['unknown']}",
            inline=False,
        )
        if s["settled_count"]:
            roi = s["roi"] * 100 if s["roi"] is not None else 0.0
            embed.add_field(
                name="Settled",
                value=(
                    f"{s['settled_count']} bets · staked {s['settled_staked']:.2f} · "
                    f"profit {s['settled_profit']:+.2f} · ROI {roi:+.1f}%"
                ),
                inline=False,
            )
            lines = []
            for cat, a in sorted(s["by_category"].items()):
                croi = (a["profit"] / a["staked"] * 100) if a["staked"] else 0.0
                lines.append(
                    f"**{cat}** — {a['count']} · profit {a['profit']:+.2f} · ROI {croi:+.1f}%"
                )
            embed.add_field(name="By category", value="\n".join(lines), inline=False)
        else:
            embed.add_field(
                name="Settled",
                value="No settled bets yet — set `result`/`profit` in the sheet to include them.",
                inline=False,
            )
        await message.channel.send(embed=embed)

    async def handle_chart(message) -> None:
        records = await asyncio.to_thread(sheets.all_records)
        series = cumulative_pnl(records)
        if not series:
            await message.channel.send("No settled bets to chart yet.")
            return
        png = await asyncio.to_thread(render_pnl_chart, series)
        await message.channel.send(file=discord.File(BytesIO(png), filename="pnl.png"))

    async def handle_slip_refresh(message, arg) -> None:
        mid = _to_int(arg)
        if mid is None:
            await message.channel.send("Usage: `!slip <message_id>`")
            return
        records = await asyncio.to_thread(sheets.all_records)
        row = next(
            (r for r in records if str(r.get("message_id", "")).strip() == str(mid)),
            None,
        )
        if row is None:
            await message.channel.send(f"No logged bet found with message_id `{mid}`.")
            return
        channel_id = _to_int(row.get("channel_id"))
        if channel_id is None:
            await message.channel.send("That row has no channel_id to fetch from.")
            return
        try:
            source = client.get_channel(channel_id) or await client.fetch_channel(
                channel_id
            )
            original = await source.fetch_message(mid)
        except Exception:
            await message.channel.send(
                "Couldn't fetch the original message (deleted or no access)."
            )
            return
        image = _first_image(original)
        if image is None:
            await message.channel.send("That message no longer has an image attachment.")
            return
        await message.channel.send(
            f"Slip for message `{mid}`:", file=await image.to_file()
        )

    async def handle_command(message) -> None:
        parts = message.content.strip().split()
        cmd = parts[0].lower() if parts else ""
        if cmd == "!help":
            await message.channel.send(HELP_TEXT)
        elif cmd == "!summary":
            await handle_summary(message)
        elif cmd == "!chart":
            await handle_chart(message)
        elif cmd == "!slip":
            await handle_slip_refresh(message, parts[1] if len(parts) > 1 else "")
        else:
            await message.channel.send("Unknown command. Try `!help`.")

    async def handle_slip(message, image) -> None:
        try:
            async with message.channel.typing():
                image_bytes = await image.read()
                data = await asyncio.to_thread(
                    extractor.extract_bet,
                    image_bytes,
                    image.content_type or "",
                    message.content,
                )
                combined = extractor.combined_odds_decimal(data)

                stake = _to_float(data.get("stake"))
                if stake is None or stake <= 0:
                    await message.reply(
                        "⚠️ I couldn't read a valid stake. Re-post with the stake "
                        "visible, or include it in the caption."
                    )
                    return

                boost_pct = _to_float(data.get("token_pct")) or 0.0
                # Fair prob is estimated by de-vigging each leg (per its
                # market_category) and multiplying — see devig.py.
                legs = data.get("legs")
                # EV is only counted when every leg has odds to de-vig; if any
                # leg's odds are missing we report 0 EV rather than guess from a
                # partial parlay (see compute_ev's fair_prob=None branch).
                if devig.all_legs_priced(legs):
                    fair = devig.parlay_fair_prob(legs)
                    if fair is not None and not (0.0 < fair <= 1.0):
                        fair = None
                else:
                    fair = None
                sgp = devig.same_game(legs)

                ev = compute_ev(combined, boost_pct, stake=stake, fair_prob=fair)
                placed_by = (data.get("placed_by") or "").strip() or message.author.display_name

                row = build_row(
                    data,
                    ev,
                    placed_by=placed_by,
                    logged_at="",  # set at confirm time
                    screenshot_url=image.url,
                    channel_id=message.channel.id,
                    message_id=message.id,
                    same_game=sgp,
                )
                reply = await message.reply(embed=build_embed(data, ev, row, same_game=sgp))
                await reply.add_reaction(CONFIRM)
                await reply.add_reaction(DISCARD)
                pending[reply.id] = {"row": row, "author_id": message.author.id}
        except extractor.ExtractionError as exc:
            await message.reply(f"⚠️ Couldn't read the slip: {exc}")
        except Exception as exc:  # don't let one bad slip crash the bot
            print(f"[slip error] {type(exc).__name__}: {exc}")
            await message.reply("⚠️ Something went wrong reading that slip. Try re-posting.")

    @client.event
    async def on_ready() -> None:
        scope = f"#{bet_channel_id}" if bet_channel_id else "all channels"
        print(f"Logged in as {client.user} · watching {scope}")

    @client.event
    async def on_message(message) -> None:
        if message.author.bot:
            return
        image = _first_image(message)
        if image is not None:
            if bet_channel_id is not None and message.channel.id != bet_channel_id:
                return  # image posted outside the configured bet channel
            await handle_slip(message, image)
            return
        if message.content.strip().startswith("!"):
            await handle_command(message)

    @client.event
    async def on_raw_reaction_add(payload) -> None:
        if payload.user_id == client.user.id:
            return  # ignore the bot's own ✅/❌ seed reactions
        entry = pending.get(payload.message_id)
        if entry is None:
            return
        if payload.user_id != entry["author_id"]:
            return  # only the original poster may confirm/discard

        emoji = str(payload.emoji)
        channel = client.get_channel(payload.channel_id) or await client.fetch_channel(
            payload.channel_id
        )

        if emoji == CONFIRM:
            # Claim before writing — a rapid second ✅ during the await can't double-log.
            entry = pending.pop(payload.message_id, None)
            if entry is None:
                return
            row = entry["row"]
            row["logged_at"] = datetime.now(timezone.utc).isoformat()
            try:
                await asyncio.to_thread(sheets.append_bet, row)
                await channel.send("✅ Logged.")
            except Exception as exc:  # re-queue so they can retry
                pending[payload.message_id] = entry
                print(f"[append error] {type(exc).__name__}: {exc}")
                await channel.send(f"⚠️ Couldn't log it: {exc}. React {CONFIRM} again to retry.")
        elif emoji == DISCARD:
            pending.pop(payload.message_id, None)
            await channel.send("❌ Discarded — re-post with corrections in the caption.")

    client.run(os.environ["DISCORD_TOKEN"])


if __name__ == "__main__":
    main()
