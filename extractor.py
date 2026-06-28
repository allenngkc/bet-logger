"""Claude vision extraction for the bet logger.

Reads a bet-slip screenshot (image bytes) plus an optional user caption and
returns structured bet data via a single forced ``record_bet`` tool call. See
PROJECT_PLAN.md §7.

The Anthropic SDK call is synchronous and blocking — ``bot.py`` wraps
``extract_bet`` in ``asyncio.to_thread(...)`` so it doesn't stall the gateway.
"""

from __future__ import annotations

import base64
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
    "3.5, American +150 = 2.5). Record the printed combined odds in "
    "`combined_odds_decimal` when shown. For EACH leg, set `odds_decimal` to "
    "that leg's OWN odds ONLY if they are shown on the slip; if an individual "
    "leg's odds aren't visible, set it to null — never guess them or split the "
    "combined odds across legs. Also classify each leg's market into exactly one "
    "allowed `market_category` ('other' if unsure; this drives de-vigging). Use "
    "the caption only for the user's category tag, who placed it, and token %. "
    "Use null when a value is absent. Call `record_bet` exactly once."
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
                "type": "number",
                "description": "Total parlay odds in DECIMAL",
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
                                "This leg's OWN decimal odds — only if shown on the slip. "
                                "If the individual leg's odds aren't visible, set null; "
                                "never guess them or split the combined odds."
                            ),
                        },
                    },
                    "required": ["event", "selection", "market_category"],
                },
            },
            "notes": {"type": ["string", "null"]},
        },
        "required": ["stake", "combined_odds_decimal", "legs"],
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


def combined_odds_decimal(data: dict) -> float:
    """Resolve combined decimal odds, falling back to the product of leg odds.

    Per §7: if ``combined_odds_decimal`` is missing/0 but legs exist, compute it
    as the product of each leg's ``odds_decimal``.

    Raises:
        ExtractionError: if neither a combined value nor any leg odds are usable.
    """
    val = _to_float(data.get("combined_odds_decimal"))
    if val is not None and val > 0:
        return val

    product = 1.0
    found = False
    for leg in data.get("legs") or []:
        odds = _to_float(leg.get("odds_decimal")) if isinstance(leg, dict) else None
        if odds is not None and odds > 0:
            product *= odds
            found = True
    if not found:
        raise ExtractionError(
            "no combined_odds_decimal and no usable leg odds to derive it"
        )
    return product


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
    print("\nresolved combined_odds_decimal ->", combined_odds_decimal(extracted))
