"""The one place the backend talks to an LLM provider.

Anthropic is the only implementation today and this module does not pretend
otherwise — there are no abstract base classes and no registry. What it does is
put every outbound call behind two functions with normalized return types, so
that adding a second provider is a change here rather than a change in every
module that happens to make a call.

Before this, the client was constructed in two places (the replay runner and
the judge) and `gen_ai.provider.name` was a string literal in three. The
Playground would have made it three and four. That is the point at which the
seam costs less than the copies.

**Two functions, because there are two shapes of call.** `complete` is prose
in, prose out — the replay runner and the Playground. `tool_call` forces the
model to answer through a schema, which is what makes a judge's verdict a datum
instead of a paragraph (see the scoring.py module docstring for why that
matters). They are separated rather than unified behind one `call()` because
the second is where provider portability actually gets hard: every provider
spells forced tool use differently and some do it worse. Keeping it named and
separate means the difficulty is visible instead of buried in a kwarg.

**What this does not do.** It does not build spans, estimate cost, or retry.
Callers own their own spans because the attributes they attach differ, cost
comes from obs_sdk.pricing keyed on the *response* model, and the Anthropic SDK
already retries. This module's whole job is: given a model id, make the call
and hand back a provider-shaped-answer in a provider-neutral wrapper.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from anthropic import Anthropic

# Values for the gen_ai.provider.name span attribute. OTel GenAI semantic
# conventions define this as a well-known-value string, so it is spelled the
# way the convention spells it rather than however we'd have named it.
ANTHROPIC = "anthropic"


class UnknownModel(ValueError):
    """No provider claims this model id. Raised before anything is spent."""


@dataclass(frozen=True)
class _Call:
    """Everything a caller needs to build a span and price the call.

    `response_model` is the model the provider says answered, which is not
    always the model that was asked for — an alias resolves to a dated id, and
    pricing has to follow the answer rather than the request.
    """

    input_tokens: int
    output_tokens: int
    response_model: str
    response_id: str
    stop_reason: str
    start_nano: int
    end_nano: int

    @property
    def latency_ms(self) -> float:
        return (self.end_nano - self.start_nano) / 1e6


@dataclass(frozen=True)
class Completion(_Call):
    text: str


@dataclass(frozen=True)
class ToolCallResult(_Call):
    # None when the model returned no call at all. Nearly always max_tokens:
    # the call was cut off mid-JSON so the block never completed. Left as None
    # for the caller to interpret, because what a missing verdict *means* is a
    # scoring question, not a transport one — and `stop_reason` is right here
    # to explain it.
    payload: dict[str, Any] | None


def provider_for(model: str) -> str:
    """Which provider serves this model id.

    Raises rather than guessing. A typo'd model reaching the API costs a
    round trip to be told the same thing, and in the replay runner it costs one
    per test case before the run gives up.
    """
    if model.startswith("claude-"):
        return ANTHROPIC
    raise UnknownModel(
        f"No provider is configured for model {model!r}. Anthropic models start "
        "with 'claude-'; other providers are not wired up yet."
    )


def provider_label(model: str) -> str:
    """provider_for, for span attributes. Never raises.

    Span builders run on error paths — recording *that* a call failed is the
    whole point of the error span — and an unrecognized model is one of the
    failures being recorded. A raising lookup there would turn one failed test
    case into a failed run, so labeling gets the total function and the call
    path gets the strict one.
    """
    try:
        return provider_for(model)
    except UnknownModel:
        return ""


@lru_cache(maxsize=16)
def _anthropic(api_key: str) -> Anthropic:
    """One client per key, reused across calls.

    Cached because constructing a client builds a connection pool, and a run
    doing 80 calls should not build 80 of them. Keyed on the secret itself
    rather than a credential id, so rotating a key's value cannot serve the old
    client — and bounded, so a long-lived process cannot accumulate one client
    per key it has ever seen.

    The key is always passed in. pydantic-settings reads .env into the Settings
    object without touching os.environ, so the SDK's own env lookup would find
    nothing even when the key is sitting right there in .env.
    """
    return Anthropic(api_key=api_key)


def validate_key(api_key: str) -> None:
    """Check a key works, by listing models. Raises on a bad key.

    Called when a key is saved rather than when it is first spent. A typo'd key
    discovered at save time is an edit; the same typo discovered mid-run has
    already cost the replay calls that ran before it.

    models.list is the cheapest real call available — it is not billed and it
    still exercises authentication.
    """
    _anthropic(api_key).models.list(limit=1)


def complete(
    *,
    model: str,
    prompt: str,
    max_tokens: int,
    api_key: str,
    timeout: float | None = None,
) -> Completion:
    """One prose-in, prose-out call.

    `api_key` is required and comes from the caller's chosen credential. This
    module deliberately has no notion of a default key: "which key pays for
    this" is a decision with money attached, and a transport layer quietly
    picking one is how spend ends up on an account nobody chose.

    `timeout` bounds the HTTP call. Background work (a replay run) leaves it
    unset — a slow model there costs time, not a staring user. Anything in a
    request path should set it.
    """
    provider_for(model)  # raises before spending anything

    start = time.time_ns()
    response = _anthropic(api_key).messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        **({"timeout": timeout} if timeout is not None else {}),
    )
    end = time.time_ns()

    return Completion(
        text="".join(b.text for b in response.content if b.type == "text"),
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        response_model=response.model,
        response_id=response.id,
        stop_reason=response.stop_reason or "unknown",
        start_nano=start,
        end_nano=end,
    )


def tool_call(
    *,
    model: str,
    prompt: str,
    max_tokens: int,
    tool_name: str,
    tool_description: str,
    input_schema: dict[str, Any],
    api_key: str,
    timeout: float | None = None,
) -> ToolCallResult:
    """One call whose only legal answer is a schema-valid tool call.

    `tool_choice` pinned to the named tool is what makes this different from
    asking for JSON in the prompt: the model cannot reply with a paragraph, so
    parse failures stop being a category of error. The schema constrains
    sampling — it does not guarantee the model respected an enum or a numeric
    bound, so callers still validate what comes back.
    """
    provider_for(model)

    start = time.time_ns()
    response = _anthropic(api_key).messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        **({"timeout": timeout} if timeout is not None else {}),
        tools=[
            {
                "name": tool_name,
                "description": tool_description,
                "input_schema": input_schema,
            }
        ],
        tool_choice={"type": "tool", "name": tool_name},
    )
    end = time.time_ns()

    block = next(
        (b for b in response.content if b.type == "tool_use" and b.name == tool_name),
        None,
    )

    return ToolCallResult(
        payload=dict(block.input or {}) if block is not None else None,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        response_model=response.model,
        response_id=response.id,
        stop_reason=response.stop_reason or "unknown",
        start_nano=start,
        end_nano=end,
    )
