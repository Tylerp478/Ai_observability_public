"""Hardcoded provider pricing for cost estimation.

Prices are USD per 1M tokens. Vendors change pricing and ship new models, so
this table needs manual upkeep — it is NOT fetched from an API. Treat cost
figures as estimates.

**Keyed on (provider, model), not model alone.** Model ids are only unique
within a vendor, and nothing stops two vendors shipping the same string. The
provider comes from the credential that paid for the call, so it is always
known at the point cost is computed.

This table doubles as the model registry: `provider_of_model` is what lets
`llm.check_model_matches` refuse a Claude model billed to an xAI key before the
call is made. A model missing from here still *runs* — it simply reports no
cost, and is not claimed by any provider.

**A token is not a token.** Four kinds are billed differently and the rate can
depend on how many there are:

  - *Uncached input* at the base rate.
  - *Cache reads* — an order of magnitude cheaper on Anthropic, and produced
    automatically by some vendors whether or not you asked for caching. Pricing
    these at the base rate overstates spend on exactly the workloads people
    adopt caching for.
  - *Cache writes* — more expensive than base input, not less.
  - *Output*, which already includes reasoning tokens where a model emits them.
    Reasoning is billed as output, so it needs no separate rate; it is carried
    for reporting, because "what did thinking cost me" is otherwise unanswerable.

Long-context tiers sit on top of all of it: above a threshold, every rate steps
up. That is why a price is a small structure rather than two floats.

All three providers verified 2026-08-30/31:

  - Anthropic against the claude-api skill's model table, which also sources
    the cache multipliers below. Every current model matched as entered; the
    1M context window carries no long-context premium, which is why no
    Anthropic entry has a `long` tier.
  - xAI against https://docs.x.ai/developers/pricing
  - Gemini against https://ai.google.dev/gemini-api/docs/pricing

**Not modelled: Anthropic fast mode.** `speed: "fast"` on Claude Opus 5 bills
at $10/$50 rather than $5/$25 — a different rate for the same model id, which
this table cannot express since it keys on (provider, model) alone. Nothing
here sets `speed`, so no call can currently produce it. Batch API pricing (50%
of standard) is unmodelled for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Rates:
    """USD per 1M tokens, for one pricing tier.

    `cache_read` and `cache_write` are None when a vendor publishes no separate
    rate, in which case cached tokens cost the same as any other input. That is
    the honest default: it is what the vendor charges when there is no cache
    discount, and it never invents a discount this table cannot source.
    """

    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None

    def read_rate(self) -> float:
        return self.input if self.cache_read is None else self.cache_read

    def write_rate(self) -> float:
        return self.input if self.cache_write is None else self.cache_write


@dataclass(frozen=True)
class ModelPrice:
    """What one model costs, across tiers and promotional windows.

    Precedence, which matters only for a model that has both: the long-context
    tier wins over a promotional rate. Introductory pricing is advertised
    against standard context, so applying it to a long-context call would
    understate the bill.
    """

    base: Rates
    # The largest total input that still gets `base`; anything larger gets
    # `long`. Phrased as a boundary rather than a threshold because the vendors
    # disagree by one token — Google charges the high tier *above* 200k, xAI
    # *at or above* it — and "the last input that is still cheap" is the one
    # reading that expresses both without a second flag.
    long_context_threshold: int | None = None
    long: Rates | None = None
    # A time-boxed introductory rate, applied through `promo_ends` inclusive.
    promo: Rates | None = None
    promo_ends: date | None = None

    def rates_for(self, *, input_tokens: int, on: date) -> Rates:
        if (
            self.long is not None
            and self.long_context_threshold is not None
            and input_tokens > self.long_context_threshold
        ):
            return self.long
        if self.promo is not None and self.promo_ends is not None and on <= self.promo_ends:
            return self.promo
        return self.base


def _anthropic(inp: float, out: float, **kw: object) -> ModelPrice:
    """An Anthropic price, with that vendor's documented cache multipliers.

    Cache reads are 0.1x base input and 5-minute cache writes are 1.25x
    (verified 2026-08-31), and those multipliers hold across the model line — so encoding them once here
    keeps every row to the two numbers that actually differ, and stops a
    hand-copied 0.1 from drifting on one line.

    **Only the 5-minute write TTL is modelled.** A 1-hour write is 2x base, and
    `usage.cache_creation` reports the split, but nothing in this app sets
    `cache_control` at all — so pricing the distinction would be building for a
    caller that does not exist. Revisit alongside whatever first opts in.
    """
    # Rounded because 3.00 * 0.1 is 0.30000000000000004 in binary floating
    # point. It changes no bill at these magnitudes, but the number gets
    # printed, and a rate that renders as 0.30000000000000004 reads like a bug
    # in the table rather than in IEEE 754.
    return ModelPrice(
        base=Rates(
            input=inp,
            output=out,
            cache_read=round(inp * 0.1, 6),
            cache_write=round(inp * 1.25, 6),
        ),
        **kw,  # type: ignore[arg-type]
    )


def _xai(inp: float, cached: float, out: float) -> ModelPrice:
    """An xAI price. Every rate doubles at 200k input tokens.

    The boundary is 199_999 rather than 200_000 because xAI charges the high
    band *at or above* 200k, where Google charges it only *above* 200k. One
    token, but it is the kind of difference that is invisible until someone
    reconciles an invoice.
    """
    return ModelPrice(
        base=Rates(input=inp, output=out, cache_read=cached),
        long_context_threshold=199_999,
        long=Rates(input=inp * 2, output=out * 2, cache_read=cached * 2),
    )


# (provider, model ID) -> ModelPrice
#
# Current Anthropic models use suffix-free IDs (`claude-opus-5`, not a dated
# variant). Legacy entries keep their dated IDs, since that's what older traces
# carry.
PRICING: dict[tuple[str, str], ModelPrice] = {
    # --- Claude 5 family (current) ---
    ("anthropic", "claude-fable-5"): _anthropic(10.00, 50.00),
    ("anthropic", "claude-mythos-5"): _anthropic(10.00, 50.00),  # Glasswing only
    ("anthropic", "claude-opus-5"): _anthropic(5.00, 25.00),
    # Launched with introductory pricing that undercuts the standard rate by a
    # third. Billing reflects the intro rate until it lapses, so an estimate
    # ignoring it overstates spend by ~33% for this model.
    ("anthropic", "claude-sonnet-5"): _anthropic(
        3.00,
        15.00,
        promo=Rates(input=2.00, output=10.00, cache_read=0.20, cache_write=2.50),
        promo_ends=date(2026, 8, 31),
    ),
    # --- Still active, previous generation ---
    ("anthropic", "claude-opus-4-8"): _anthropic(5.00, 25.00),
    ("anthropic", "claude-opus-4-7"): _anthropic(5.00, 25.00),
    ("anthropic", "claude-opus-4-6"): _anthropic(5.00, 25.00),
    ("anthropic", "claude-sonnet-4-6"): _anthropic(3.00, 15.00),
    ("anthropic", "claude-haiku-4-5"): _anthropic(1.00, 5.00),
    ("anthropic", "claude-haiku-4-5-20251001"): _anthropic(1.00, 5.00),  # dated form
    # --- Legacy --- the only rates in this file no current source confirms.
    #
    # They are absent from the vendor's current model table because the models
    # are retired or superseded, so there is nothing left to check them
    # against. Kept because this table doubles as the historical cost table and
    # dropping a row would silently un-cost any old span carrying it — but no
    # span in this project uses one, so the exposure today is zero.
    ("anthropic", "claude-opus-4-5"): _anthropic(5.00, 25.00),
    ("anthropic", "claude-opus-4-1-20250805"): _anthropic(15.00, 75.00),  # retired
    ("anthropic", "claude-sonnet-4-5-20250929"): _anthropic(3.00, 15.00),
    ("anthropic", "claude-3-haiku-20240307"): _anthropic(0.25, 1.25),  # retired
    # --- xAI (Grok) --- verified 2026-08-30 against docs.x.ai/developers/pricing
    #
    # Every xAI text model is priced in two bands that double at 200k input
    # tokens, and the higher band applies to *all* tokens in the request rather
    # than only the excess — so `_xai` encodes the doubling once instead of
    # repeating six pairs of numbers that are all exactly 2x.
    ("xai", "grok-4.6"): _xai(2.00, 0.50, 6.00),
    ("xai", "grok-4.5"): _xai(2.00, 0.30, 6.00),
    ("xai", "grok-4.3"): _xai(1.25, 0.20, 2.50),
    ("xai", "grok-4.20-0309-reasoning"): _xai(1.25, 0.20, 2.50),
    ("xai", "grok-4.20-0309-non-reasoning"): _xai(1.25, 0.20, 2.50),
    ("xai", "grok-4.20-multi-agent-0309"): _xai(1.25, 0.20, 2.50),
    ("xai", "grok-build-0.1"): _xai(1.00, 0.20, 2.00),
    #
    # Retired 2026-05-15, kept and priced at what they *actually bill*.
    #
    # xAI does not reject a retired id — it answers with the replacement. A
    # span in this project proves it: the request said `grok-3-mini` and the
    # response said `grok-4.3`. So the honest price for one of these ids is the
    # replacement's price, not whatever the id used to cost. They stay in the
    # table because the dashboard groups by the *requested* model, and dropping
    # them would grey out real history and blind `provider_of_model` — which is
    # what stops a Grok id being billed to an Anthropic key.
    ("xai", "grok-4"): _xai(1.25, 0.20, 2.50),  # -> grok-4.3
    ("xai", "grok-3-mini"): _xai(1.25, 0.20, 2.50),  # -> grok-4.3
    ("xai", "grok-3"): _xai(1.25, 0.20, 2.50),  # -> grok-4.3
    ("xai", "grok-4-0709"): _xai(1.25, 0.20, 2.50),  # -> grok-4.3
    ("xai", "grok-4-fast-reasoning"): _xai(1.25, 0.20, 2.50),  # -> grok-4.3
    ("xai", "grok-4-fast-non-reasoning"): _xai(1.25, 0.20, 2.50),  # -> grok-4.3
    ("xai", "grok-4-1-fast-reasoning"): _xai(1.25, 0.20, 2.50),  # -> grok-4.3
    ("xai", "grok-4-1-fast-non-reasoning"): _xai(1.25, 0.20, 2.50),  # -> grok-4.3
    ("xai", "grok-code-fast-1"): _xai(1.00, 0.20, 2.00),  # -> grok-build-0.1
    # --- Google Gemini --- verified 2026-08-30 against
    # ai.google.dev/gemini-api/docs/pricing (Standard tier; this app does not
    # use Batch, Flex or Priority, which are half and 1.8x respectively).
    #
    # 2.5 Pro is why the long-context tier exists: above 200k input tokens
    # every rate steps up. A judge fed a long transcript crosses that line
    # without anyone deciding to, which is exactly what a flat rate would
    # silently under-report.
    ("gcp.gemini", "gemini-2.5-pro"): ModelPrice(
        base=Rates(input=1.25, output=10.00, cache_read=0.125),
        long_context_threshold=200_000,
        long=Rates(input=2.50, output=15.00, cache_read=0.25),
    ),
    ("gcp.gemini", "gemini-2.5-flash"): ModelPrice(
        base=Rates(input=0.30, output=2.50, cache_read=0.03)
    ),
    ("gcp.gemini", "gemini-2.5-flash-lite"): ModelPrice(
        base=Rates(input=0.10, output=0.40, cache_read=0.01)
    ),
    #
    # Gemini 3.x. The two newest Flash models are on introductory pricing that
    # *doubles* on 2027-01-01, so `base` is the post-promo list price and
    # `promo` is what is actually charged until then — the same shape as the
    # Sonnet 5 intro rate. Entering the current price as `base` would look
    # right today and quietly halve every estimate in January.
    ("gcp.gemini", "gemini-3.7-flash"): ModelPrice(
        base=Rates(input=1.50, output=7.50, cache_read=0.15),
        promo=Rates(input=0.75, output=3.75, cache_read=0.075),
        promo_ends=date(2026, 12, 31),
    ),
    ("gcp.gemini", "gemini-3.6-flash"): ModelPrice(
        base=Rates(input=1.50, output=7.50, cache_read=0.15),
        promo=Rates(input=0.75, output=3.75, cache_read=0.075),
        promo_ends=date(2026, 12, 31),
    ),
    ("gcp.gemini", "gemini-3.5-flash"): ModelPrice(
        base=Rates(input=1.50, output=9.00, cache_read=0.15)
    ),
    ("gcp.gemini", "gemini-3.5-flash-lite"): ModelPrice(
        base=Rates(input=0.30, output=2.50, cache_read=0.03)
    ),
    # Preview, and priced but deliberately not offered — see the models tuple
    # in llm.py. Priced anyway so a span carrying it still costs, and so
    # provider_of_model can claim it for the mismatch guard.
    ("gcp.gemini", "gemini-3.1-pro-preview"): ModelPrice(
        base=Rates(input=2.00, output=12.00, cache_read=0.20),
        long_context_threshold=200_000,
        long=Rates(input=4.00, output=18.00, cache_read=0.40),
    ),
}

# Built once. Only meaningful for ids that are unique across vendors, which is
# every id in the table today; if two vendors ever ship the same string, the
# model stops being attributable and is deliberately claimed by nobody.
_MODEL_OWNER: dict[str, str | None] = {}
for _provider, _model in PRICING:
    _MODEL_OWNER[_model] = None if _model in _MODEL_OWNER else _provider


def provider_of_model(model: str) -> str | None:
    """Which provider this model id belongs to, or None if unknown/ambiguous.

    None is not an error. It means "this table cannot say", which is the right
    answer both for a model released this morning and for a typo — and callers
    (see `llm.check_model_matches`) must not treat those as failures.
    """
    return _MODEL_OWNER.get(model)


# Output $/1M boundaries between price tiers. Fixed rather than derived from
# the table's own distribution, because a model's colour on the dashboard must
# not move when a different model is added next to it.
_TIER_BOUNDS = (2.00, 8.00, 20.00)


def price_tier(provider: str, model: str) -> int | None:
    """Which price band a model sits in: 0 cheapest, 3 frontier. None if unknown.

    Banded on the *output* rate, which is the number that actually separates
    these models — input rates cluster far more tightly, and output is where a
    real workload's bill is decided.

    This exists so the dashboard can order models without a hand-maintained
    ladder of name prefixes. The old one matched `claude-haiku` … `claude-fable`
    and therefore greyed out every non-Anthropic model the moment a second
    provider arrived. Price is the ordering the ramp was always encoding; now
    it is read from the same table that does the billing, so a model cannot be
    coloured as a tier it is not priced as.
    """
    price = PRICING.get((provider, model))
    if price is None:
        return None
    # `base`, deliberately, not whatever is charged today. A promotional rate
    # lapses on a date, and a model that changed colour on the dashboard
    # overnight — without its capability or its place in the lineup changing —
    # would be reporting a calendar event as a category change.
    out = price.base.output
    return sum(1 for bound in _TIER_BOUNDS if out >= bound)


def estimate_cost_usd(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
    request_model: str | None = None,
    on: date | None = None,
) -> float | None:
    """Estimate spend for one call.

    `provider` comes from the credential that paid; `model` should be the model
    the provider says *answered*, which is not always the one that was asked for.

    `input_tokens` is the **total** prompt size — cached and uncached together —
    and `cached_input_tokens` / `cache_write_tokens` are subsets of it, priced
    at their own rates with the remainder at the base input rate. Vendors
    disagree about whether their raw `input_tokens` includes cached tokens;
    llm.py normalizes that before anything gets here, so this function can
    assume the inclusive reading.

    Reasoning tokens take no argument: they are already inside `output_tokens`
    and are billed at the output rate, so passing them would double-count. They
    are carried on the span for reporting only.

    `on` is the date the call was made — it matters for promotional windows and
    defaults to today. Pass the span's own timestamp when re-costing historical
    traces, or estimates will silently drift once a promo lapses.

    `request_model` is what was *asked for*, used only when the answering model
    is not in the table. Pricing normally follows the answer, because an alias
    resolves to a dated id and the dated id is what was billed — but that only
    works when the dated id is priced. Gemini reports a build suffix
    (`gemini-2.5-pro-002`) that no table will carry, and without this fallback
    every Gemini call would silently report no cost at all: a plausible-looking
    zero on a dashboard whose entire job is spend. The requested model is a
    safe floor because it is always one this app offered, and every offered
    model is priced (llm._check_offered_models_are_priced).

    Returns None (rather than 0.0) for unknown models so callers can
    distinguish "free" from "we don't have a price for this model."
    """
    price = PRICING.get((provider, model))
    if price is None and request_model is not None:
        price = PRICING.get((provider, request_model))
    if price is None:
        return None

    rates = price.rates_for(input_tokens=input_tokens, on=on or date.today())

    # Clamped rather than trusted: these are three numbers from a vendor's JSON,
    # and a negative remainder from an unexpected accounting change would turn
    # into a credit that quietly understates the bill.
    billed_cached = max(0, cached_input_tokens)
    billed_written = max(0, cache_write_tokens)
    uncached = max(0, input_tokens - billed_cached - billed_written)

    return (
        uncached * rates.input
        + billed_cached * rates.read_rate()
        + billed_written * rates.write_rate()
        + max(0, output_tokens) * rates.output
    ) / 1_000_000

