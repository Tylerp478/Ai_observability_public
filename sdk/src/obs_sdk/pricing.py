"""Hardcoded Anthropic pricing for cost estimation.

Prices are USD per 1M tokens. Anthropic changes pricing and ships new models,
so this table needs manual upkeep — it is NOT fetched from an API. Treat cost
figures as estimates.

Current as of 2026-07-27.
"""

from __future__ import annotations

from datetime import date

# model ID -> (input $/1M tokens, output $/1M tokens)
#
# Current models use suffix-free IDs (`claude-opus-5`, not a dated variant).
# Legacy entries keep their dated IDs, since that's what older traces carry.
PRICING_PER_MILLION_TOKENS: dict[str, tuple[float, float]] = {
    # --- Claude 5 family (current) ---
    "claude-fable-5": (10.00, 50.00),
    "claude-mythos-5": (10.00, 50.00),  # Project Glasswing only
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),  # intro pricing may apply — see below
    # --- Still active, previous generation ---
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),  # dated form of the same model
    # --- Legacy ---
    "claude-opus-4-5": (5.00, 25.00),
    "claude-opus-4-1-20250805": (15.00, 75.00),
    "claude-sonnet-4-5-20250929": (3.00, 15.00),
    "claude-3-haiku-20240307": (0.25, 1.25),
}

# Claude Sonnet 5 launched with introductory pricing that undercuts its standard
# rate by a third. Billing reflects the intro rate until it lapses, so an
# estimate that ignores it overstates spend by ~33% for that model.
_SONNET_5_INTRO_RATES = (2.00, 10.00)
_SONNET_5_INTRO_ENDS = date(2026, 8, 31)


def estimate_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    on: date | None = None,
) -> float | None:
    """Estimate spend for one call.

    `on` is the date the call was made — it matters only for models with
    time-boxed promotional pricing, and defaults to today. Pass the span's own
    timestamp when re-costing historical traces, or estimates will silently
    drift once a promo lapses.

    Returns None (rather than 0.0) for unknown models so callers can
    distinguish "free" from "we don't have a price for this model."
    """
    rates = PRICING_PER_MILLION_TOKENS.get(model)
    if rates is None:
        return None

    if model == "claude-sonnet-5" and (on or date.today()) <= _SONNET_5_INTRO_ENDS:
        rates = _SONNET_5_INTRO_RATES

    input_rate, output_rate = rates
    return (input_tokens / 1_000_000) * input_rate + (output_tokens / 1_000_000) * output_rate
