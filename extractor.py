"""Claude vision extraction for the bet logger.

Reads a bet-slip screenshot (image bytes) plus an optional user caption and
returns structured bet data via a single forced ``record_bet`` tool call. See
PROJECT_PLAN.md §7.

The Anthropic SDK call is synchronous and blocking — ``bot.py`` wraps
``extract_bet`` in ``asyncio.to_thread(...)`` so it doesn't stall the gateway.
"""

from __future__ import annotations

import base64
import math
import os

from devig import MARKET_CATEGORIES

DEFAULT_MODEL = "claude-opus-4-8"

# Anthropic vision accepts these image media types; anything else is coerced to
# PNG (Discord usually serves PNG/JPEG for screenshots).
_VALID_MEDIA_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_DEFAULT_MEDIA_TYPE = "image/png"

_SYSTEM_PROMPT = (
    "You extract structured betting data from a screenshot of a bet slip plus "
    "an optional user caption. Always express odds in DECIMAL (fractional 5/2 = "
    "3.5, American +150 = 2.5, American -200 = 1.5).\n"
    "PROFIT BOOSTS: a profit-boost token increases the winnings, and the boost "
    "is applied downstream from the values you record — so you must keep the "
    "ORIGINAL (pre-boost) combined odds separate from the boosted ones. Put the "
    "original pre-boost combined odds in `combined_odds_decimal`, the boost "
    "percentage in `token_pct`, and — only when the slip already shows a boosted "
    "price — the boosted combined odds in `boosted_odds_decimal`. Some books "
    "(e.g. FanDuel) print the already-boosted price in large type next to a "
    "struck-through original: e.g. '+133' crossed out, '+198' shown big, with a "
    "'PROFIT BOOST 50%' badge. There, record the struck-through ORIGINAL "
    "(+133 -> 2.33) in `combined_odds_decimal`, the big boosted price "
    "(+198 -> 2.98) in `boosted_odds_decimal`, and 50 in `token_pct`. If a boost "
    "is shown but only ONE combined price is visible, put that price in "
    "`boosted_odds_decimal` (not `combined_odds_decimal`). NEVER put a boosted "
    "price in `combined_odds_decimal` — it would be boosted twice.\n"
    "LEG ODDS come from the IMAGE, not the caption. Read each leg's OWN price "
    "straight off the slip and put it in `odds_decimal` whenever it is printed "
    "next to that selection — do this even when the caption says nothing about "
    "odds (users rarely type per-leg odds; the slip is the source of truth). Many "
    "slips, including parlays, list a price beside each leg — capture every one "
    "you can read. Set `odds_decimal` to null ONLY when that leg's price is "
    "genuinely not shown (common on same-game parlays that print just the combined "
    "price). NEVER guess, split the combined odds across legs, or use 1/1.0/the "
    "combined odds as a placeholder — a leg whose price isn't shown must be null "
    "(1.0 is a real near-certain price and would corrupt the EV). Classify each "
    "leg's market into exactly one allowed `market_category` ('other' if unsure; "
    "this drives de-vigging). Use the caption only for the user's category tag, "
    "who placed it, and token %. Use null when a value is absent. Call "
    "`record_bet` exactly once."
)

# Forced tool-use schema. Plain JSON Schema (no `strict`) — see PROJECT_PLAN.md §7;
# the call forces this tool, but the *input* isn't schema-validated, so callers
# coerce types defensively (see `combined_odds_decimal` / bot.py).
BET_TOOL = {
    "name": "record_bet",
    "description": (
        "Record the structured details of a single sports bet (typically a "
        "3-leg parlay) extracted from a bet-slip screenshot and its caption."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "bookmaker": {"type": "string"},
            "currency": {"type": "string"},
            "stake": {"type": "number"},
            "combined_odds_decimal": {
                "type": ["number", "null"],
                "description": (
                    "ORIGINAL combined parlay odds in DECIMAL, BEFORE any "
                    "profit boost (e.g. FanDuel's struck-through +133 -> 2.33). "
                    "null if only an already-boosted price is shown."
                ),
            },
            "boosted_odds_decimal": {
                "type": ["number", "null"],
                "description": (
                    "Combined parlay odds in DECIMAL AFTER the profit boost, "
                    "ONLY when the slip already displays a boosted price "
                    "(e.g. FanDuel's big +198 -> 2.98). null otherwise."
                ),
            },
            "potential_return": {
                "type": "number",
                "description": "Payout before any boost, if visible",
            },
            "token_pct": {
                "type": ["number", "null"],
                "description": "Boost token % (30/50/100), null if none",
            },
            "category": {
                "type": ["string", "null"],
                "description": "User category/tag from caption",
            },
            "placed_by": {"type": ["string", "null"]},
            "bet_date": {
                "type": ["string", "null"],
                "description": "ISO date if stated",
            },
            "legs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "event": {"type": "string"},
                        "selection": {"type": "string"},
                        "market": {
                            "type": ["string", "null"],
                            "description": "Free-text market label as printed, e.g. 'Total Goals Over 2.5'",
                        },
                        "market_category": {
                            "type": "string",
                            "enum": list(MARKET_CATEGORIES),
                            "description": "This leg's market type, used to de-vig its odds. 'other' if unsure.",
                        },
                        "odds_decimal": {
                            "type": ["number", "null"],
                            "description": (
                                "This leg's OWN decimal odds — only if printed next to "
                                "the selection. If not shown (e.g. an SGP showing only the "
                                "combined price), set null; never guess, split the combined "
                                "odds, or use 1/1.0 as a placeholder."
                            ),
                        },
                    },
                    "required": ["event", "selection", "market_category"],
                },
            },
            "notes": {"type": ["string", "null"]},
        },
        "required": ["stake", "legs"],
    },
}


class ExtractionError(Exception):
    """Raised when the model response contains no usable record_bet tool call."""


_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    """Lazily create and cache the Anthropic client (reads ANTHROPIC_API_KEY).

    The SDK import is deferred so the pure helpers (``combined_odds_decimal``,
    ``_to_float``) and ``BET_TOOL`` stay importable without the SDK installed;
    actual extraction requires ``pip install anthropic``.
    """
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic()
    return _client


def _sniff_media_type(image_bytes: bytes) -> str | None:
    """Detect the media type from the file's magic bytes, or None if unknown.

    Discord (and other sources) sometimes mislabels an attachment's
    ``content_type`` — e.g. a PNG served as ``image/webp`` — and Anthropic
    rejects the request when the declared type doesn't match the bytes. We trust
    the bytes over the caller-supplied label.
    """
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return None


def _normalize_media_type(media_type: str | None) -> str:
    if media_type in _VALID_MEDIA_TYPES:
        return media_type  # type: ignore[return-value]
    return _DEFAULT_MEDIA_TYPE


def _resolve_media_type(image_bytes: bytes, media_type: str | None) -> str:
    """Prefer the type sniffed from the bytes; fall back to the caller's label."""
    return _sniff_media_type(image_bytes) or _normalize_media_type(media_type)


def extract_bet(image_bytes: bytes, media_type: str, caption: str = "") -> dict:
    """Extract structured bet data from a slip screenshot + caption.

    Args:
        image_bytes: Raw image bytes of the bet slip.
        media_type: The caller's declared media type (e.g. "image/png"). Used
            only as a fallback hint — the type is detected from the image bytes
            when possible, since callers (Discord) sometimes mislabel it.
        caption: The user's caption text (may be empty).

    Returns:
        The ``record_bet`` tool input as a dict.

    Raises:
        ExtractionError: if the model returned no record_bet tool call.
    """
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    model = os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)

    response = _get_client().messages.create(
        model=model,
        max_tokens=2000,
        system=_SYSTEM_PROMPT,
        tools=[BET_TOOL],
        tool_choice={"type": "tool", "name": "record_bet"},
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": _resolve_media_type(image_bytes, media_type),
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": caption or "(no caption provided)"},
                ],
            }
        ],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "record_bet":
            return dict(block.input)
    raise ExtractionError("model did not return a record_bet tool call")


def _to_float(value: object) -> float | None:
    """Best-effort float coercion; returns None on failure (defensive, see §11)."""
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _leg_odds_product(data: dict) -> float | None:
    """Product of each priced leg's decimal odds, or None if none are usable.

    Leg odds are always PRE-boost, so their product is a pre-boost combined
    figure — a reliable fallback when the slip's combined price isn't read.
    """
    product = 1.0
    found = False
    for leg in data.get("legs") or []:
        odds = _to_float(leg.get("odds_decimal")) if isinstance(leg, dict) else None
        # Decimal odds must exceed 1.0 to be a real price; a 1.0 placeholder for a
        # leg whose odds weren't shown is not usable (mirrors devig._usable_odds).
        if odds is not None and odds > 1.0:
            product *= odds
            found = True
    return product if found else None


def combined_odds_decimal(data: dict) -> float:
    """Resolve combined decimal odds, falling back to the product of leg odds.

    Returns the value the model recorded in ``combined_odds_decimal`` (per §7,
    the PRE-boost original), or the product of leg odds if that's missing/0.
    Use :func:`resolve_boost` for the boost-aware value to feed ``compute_ev``.

    Raises:
        ExtractionError: if neither a combined value nor any leg odds are usable.
    """
    val = _to_float(data.get("combined_odds_decimal"))
    if val is not None and val > 0:
        return val
    product = _leg_odds_product(data)
    if product is None:
        raise ExtractionError(
            "no combined_odds_decimal and no usable leg odds to derive it"
        )
    return product


# How far the boosted price implied by the pre-boost odds may drift from the
# slip's displayed boosted price before we treat the recorded "pre-boost" value
# as itself already boosted. American-odds rounding (e.g. 2.33 -> "+133") can
# shift the implied boosted decimal by a couple percent, so allow a little slack.
_BOOST_CONSISTENCY_REL_TOL = 0.05


def resolve_boost(data: dict) -> tuple[float, float, str | None]:
    """Resolve the PRE-boost combined odds + token % to feed ``compute_ev``.

    ``ev.compute_ev`` takes PRE-boost combined odds and applies the token boost
    itself. Some books (FanDuel) print the ALREADY-boosted combined odds in
    large type next to a struck-through original, so a naive read records the
    boosted figure as ``combined_odds_decimal`` and the boost then gets applied
    twice (a +198 slip showing $149 return is logged as a $198 return). This
    reconciles the recorded original odds, the slip's displayed boosted odds
    (``boosted_odds_decimal``), and the token so the boost is applied exactly
    once.

    Returns ``(pre_boost_combined_decimal, boost_pct, note)``. ``note`` is a
    short human-readable string when we had to correct an already-boosted figure
    (surfaced to the poster / logged), else ``None``.

    Raises:
        ExtractionError: if no usable odds (recorded, boosted, or leg) exist.
    """
    token = _to_float(data.get("token_pct")) or 0.0
    if token < 0:
        token = 0.0

    boosted = _to_float(data.get("boosted_odds_decimal"))
    if boosted is not None and boosted <= 1.0:
        boosted = None  # a "boosted" price <= 1.0 is noise, not a real payout

    recorded = _to_float(data.get("combined_odds_decimal"))
    base = recorded if (recorded is not None and recorded > 0) else _leg_odds_product(data)

    # Nothing to reconcile: no active token, or no separately-shown boosted price.
    if token <= 0 or boosted is None:
        chosen = base if base is not None else boosted
        if chosen is None:
            raise ExtractionError(
                "no combined_odds_decimal and no usable leg odds to derive it"
            )
        return chosen, token, None

    # A token is active AND the slip shows a boosted price. Back out the pre-boost
    # odds the boosted price implies, so compute_ev re-applying the token lands
    # back on the slip's boosted price.
    factor = 1 + token / 100
    derived_base = 1 + (boosted - 1) / factor

    if base is None:
        return (
            derived_base,
            token,
            "derived pre-boost odds from the slip's boosted price",
        )

    # Both an original and a boosted price are in hand. If boosting `base` by the
    # token reproduces the displayed boosted price, `base` is a genuine pre-boost
    # figure — trust it. Otherwise `base` is itself (near) the boosted price (the
    # double-count), so fall back to the price implied by the boosted figure.
    expected_boosted = 1 + (base - 1) * factor
    if math.isclose(expected_boosted, boosted, rel_tol=_BOOST_CONSISTENCY_REL_TOL):
        return base, token, None
    return (
        derived_base,
        token,
        "used the slip's boosted price (the recorded odds were already boosted)",
    )


if __name__ == "__main__":  # tiny smoke-test harness — needs ANTHROPIC_API_KEY
    import json
    import mimetypes
    import sys

    if len(sys.argv) < 2:
        print('usage: python extractor.py <image_path> ["caption"]', file=sys.stderr)
        raise SystemExit(2)

    image_path = sys.argv[1]
    caption_arg = sys.argv[2] if len(sys.argv) > 2 else ""

    with open(image_path, "rb") as fh:
        raw = fh.read()
    guessed, _ = mimetypes.guess_type(image_path)

    extracted = extract_bet(raw, guessed or _DEFAULT_MEDIA_TYPE, caption_arg)
    print(json.dumps(extracted, indent=2, ensure_ascii=False))
    pre_boost, boost_pct, note = resolve_boost(extracted)
    print(f"\nresolved pre-boost odds -> {pre_boost:.4f}  (token {boost_pct:g}%)")
    if note:
        print(f"boost note -> {note}")
