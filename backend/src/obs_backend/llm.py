"""The one place the backend talks to an LLM provider.

Two shapes of call, several providers behind them. `complete` is prose in,
prose out — the replay runner and the Playground. `tool_call` forces the model
to answer through a schema, which is what makes a judge's verdict a datum
instead of a paragraph (see the scoring.py module docstring for why that
matters). They stay separated rather than unified behind one `call()` because
the second is where provider portability actually gets hard: every provider
spells forced tool use differently and some do it worse. Keeping it named and
separate means the difficulty is visible instead of buried in a kwarg.

**The credential routes the call, not the model id.** This module used to
prefix-match `claude-` to find a provider, which worked while there was one.
Extending that to `grok-`/`gemini-`/`gpt-` looks obvious and is wrong: prefixes
collide, vendors rename, and a model released next week would be rejected by a
table nobody has edited yet. Every path that spends money already resolves a
credential, and that row already names a provider — so the provider is passed
in, and an unrecognized model id is the provider's problem to report, not ours
to guess at.

What survives from the old prefix match is the part that was actually earning
its keep: a pre-flight check that stops a typo before it is paid for. It is now
a *mismatch* check (`check_model_matches`) — it refuses a model that the
pricing table says belongs to some other provider, which is the error a person
actually makes, and stays quiet about ids it has never seen.

**What this does not do.** It does not build spans, estimate cost, or retry.
Callers own their own spans because the attributes they attach differ, cost
comes from obs_sdk.pricing keyed on provider and the *response* model, and the
vendor SDKs already retry. This module's whole job is: given a provider, a
model id and a key, make the call and hand back a provider-shaped answer in a
provider-neutral wrapper.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

from anthropic import Anthropic
from google import genai
from google.genai import types as genai_types
from openai import OpenAI

# Values for the gen_ai.provider.name span attribute. OTel GenAI semantic
# conventions define this as a well-known-value string, so these are spelled
# the way the convention spells them rather than however we'd have named them.
ANTHROPIC = "anthropic"
XAI = "xai"
# The convention's value for the Gemini Developer API. Dotted, and not the
# prettiest database value, but this module already committed to spelling these
# the way the convention spells them — and `gcp.vertex_ai` is a *different*
# well-known value for the same models reached a different way, which is
# exactly the distinction a hand-picked "gemini" would erase.
GEMINI = "gcp.gemini"


class UnknownProvider(ValueError):
    """No adapter is registered under this provider name."""


class ModelProviderMismatch(ValueError):
    """This model belongs to a different provider than the key that was chosen.

    Raised before anything is spent. Distinct from UnknownProvider because the
    fix is different: pick a different model, or pick a different key.
    """


@dataclass(frozen=True, kw_only=True)
class _Call:
    """Everything a caller needs to build a span and price the call.

    `response_model` is the model the provider says answered, which is not
    always the model that was asked for — an alias resolves to a dated id, and
    pricing has to follow the answer rather than the request.

    **The token fields are totals, with named subsets.** `input_tokens` is the
    whole prompt and `cached_input_tokens` / `cache_write_tokens` are parts of
    it; `output_tokens` is the whole completion and `reasoning_tokens` is part
    of it. That is a normalization, not a passthrough — the vendors disagree:

      - Anthropic's `usage.input_tokens` **excludes** anything read from or
        written to cache, so the total has to be reassembled by adding them
        back. Passing its raw value through would under-report the prompt.
      - An OpenAI-compatible `usage.prompt_tokens` **includes** cached tokens,
        and `completion_tokens` includes reasoning tokens.

    Getting this wrong is quiet: both readings produce a plausible number, and
    only the bill disagrees. Everything downstream — cost, span attributes — is
    written against the inclusive reading defined here.

    kw_only so subclasses can add required fields after these defaulted ones.
    """

    input_tokens: int
    output_tokens: int
    response_model: str
    response_id: str
    stop_reason: str
    start_nano: int
    end_nano: int
    # Subsets of input_tokens. Zero when a vendor reports no caching, which is
    # also the correct value when caching did not happen.
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    # Subset of output_tokens. Billed at the output rate like any other output
    # token, so this is carried for reporting rather than for pricing —
    # "what did thinking cost me" is otherwise unanswerable.
    reasoning_tokens: int = 0

    @property
    def latency_ms(self) -> float:
        return (self.end_nano - self.start_nano) / 1e6

    @property
    def truncated(self) -> bool:
        """Whether the token budget cut the answer short.

        Every vendor spells this differently — `max_tokens` on Anthropic,
        `length` on an OpenAI-compatible endpoint, `MAX_TOKENS` on Gemini — so
        anything comparing `stop_reason` against one spelling silently stops
        working for the others. That is not hypothetical: the judge's
        "raise max_tokens" hint tested for Anthropic's spelling and therefore
        never fired for a Grok judge, which is precisely the case where the
        advice was needed.

        `stop_reason` keeps the vendor's own word, because that is what the
        span should record; this is the derived question callers actually ask.
        """
        return self.stop_reason.lower() in {"max_tokens", "length", "max_output_tokens"}


@dataclass(frozen=True, kw_only=True)
class Completion(_Call):
    text: str


@dataclass(frozen=True, kw_only=True)
class ToolCallResult(_Call):
    # None when the model returned no usable call. On Anthropic that is nearly
    # always max_tokens: the call was cut off mid-JSON so the block never
    # completed. On an OpenAI-compatible endpoint it additionally covers
    # arguments that arrived as unparseable JSON, which is the same failure
    # wearing a different hat — a truncated argument string. Left as None for
    # the caller to interpret, because what a missing verdict *means* is a
    # scoring question, not a transport one — and `stop_reason` is right here
    # to explain it.
    payload: dict[str, Any] | None


# --------------------------------------------------------------------------
# The provider interface
# --------------------------------------------------------------------------


class Provider(Protocol):
    """What every provider adapter has to be able to do.

    A Protocol rather than an ABC: the adapters share no implementation, only
    a shape, and nothing here needs isinstance. Three methods because those are
    the three things the app asks a vendor for — prose, a forced schema, and
    "is this key real".
    """

    name: str
    label: str
    # The models this provider offers in a picker. Curated, not every model the
    # vendor sells: the pricing table also carries legacy and dated ids that
    # exist to cost old traces, and offering those in a dropdown would invite
    # new spend on a deprecated model.
    models: tuple[str, ...]
    # What this vendor's keys look like, for the paste field's placeholder. A
    # display detail, kept here so that registering a provider stays one change
    # in this file rather than one here and one in the frontend.
    key_hint: str

    def complete(
        self,
        *,
        model: str,
        prompt: str,
        max_tokens: int,
        api_key: str,
        timeout: float | None = None,
    ) -> Completion: ...

    def tool_call(
        self,
        *,
        model: str,
        prompt: str,
        max_tokens: int,
        tool_name: str,
        tool_description: str,
        input_schema: dict[str, Any],
        api_key: str,
        timeout: float | None = None,
    ) -> ToolCallResult: ...

    def validate_key(self, api_key: str) -> None: ...


# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------


def _int(obj: Any, *path: str) -> int:
    """Walk an optional nested usage field, returning 0 when any hop is absent.

    Written defensively on purpose. These are optional sub-objects on a
    vendor's response, some OpenAI-compatible endpoints omit them entirely, and
    a None in the middle of the chain is the normal case rather than an error.
    An AttributeError here would fail a call that already succeeded and was
    already billed.
    """
    for name in path:
        if obj is None:
            return 0
        obj = getattr(obj, name, None)
    return int(obj) if isinstance(obj, (int, float)) else 0


@lru_cache(maxsize=16)
def _anthropic_client(api_key: str) -> Anthropic:
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


def _anthropic_usage(usage: Any) -> dict[str, int]:
    """Anthropic usage -> the normalized token fields.

    `usage.input_tokens` counts only tokens that were neither read from nor
    written to the cache, so the prompt total is the sum of all three. Reading
    it as the total is the mistake this function exists to prevent: it would
    under-report the prompt by exactly the part caching was supposed to make
    cheap, and the error grows as caching works better.

    Anthropic names its reasoning counter `thinking_tokens`; the field is
    normalized to `reasoning_tokens` so no caller has to know which vendor
    answered.
    """
    cache_read = _int(usage, "cache_read_input_tokens")
    cache_write = _int(usage, "cache_creation_input_tokens")
    return {
        "input_tokens": _int(usage, "input_tokens") + cache_read + cache_write,
        "output_tokens": _int(usage, "output_tokens"),
        "cached_input_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "reasoning_tokens": _int(usage, "output_tokens_details", "thinking_tokens"),
    }


def _openai_usage(usage: Any) -> dict[str, int]:
    """OpenAI-compatible usage -> the normalized token fields.

    The opposite convention to Anthropic: `prompt_tokens` already includes
    cached tokens and `completion_tokens` already includes reasoning tokens, so
    the totals pass through and only the subsets are extracted. Adding the
    cached count here would double-bill it.

    Every field below is optional in the wire format and absent on some
    compatible endpoints, which is why each one goes through `_int`.
    """
    return {
        "input_tokens": _int(usage, "prompt_tokens"),
        "output_tokens": _int(usage, "completion_tokens"),
        "cached_input_tokens": _int(usage, "prompt_tokens_details", "cached_tokens"),
        "cache_write_tokens": _int(usage, "prompt_tokens_details", "cache_write_tokens"),
        "reasoning_tokens": _int(
            usage, "completion_tokens_details", "reasoning_tokens"
        ),
    }


class AnthropicProvider:
    """Anthropic's Messages API. The original implementation, moved intact."""

    name = ANTHROPIC
    label = "Anthropic"
    models = ("claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5")
    key_hint = "sk-ant-…"

    def complete(
        self,
        *,
        model: str,
        prompt: str,
        max_tokens: int,
        api_key: str,
        timeout: float | None = None,
    ) -> Completion:
        start = time.time_ns()
        response = _anthropic_client(api_key).messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            **({"timeout": timeout} if timeout is not None else {}),
        )
        end = time.time_ns()

        return Completion(
            text="".join(b.text for b in response.content if b.type == "text"),
            **_anthropic_usage(response.usage),
            response_model=response.model,
            response_id=response.id,
            stop_reason=response.stop_reason or "unknown",
            start_nano=start,
            end_nano=end,
        )

    def tool_call(
        self,
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
        start = time.time_ns()
        response = _anthropic_client(api_key).messages.create(
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
            (
                b
                for b in response.content
                if b.type == "tool_use" and b.name == tool_name
            ),
            None,
        )

        return ToolCallResult(
            payload=dict(block.input or {}) if block is not None else None,
            **_anthropic_usage(response.usage),
            response_model=response.model,
            response_id=response.id,
            stop_reason=response.stop_reason or "unknown",
            start_nano=start,
            end_nano=end,
        )

    def validate_key(self, api_key: str) -> None:
        """models.list is the cheapest real call available — unbilled, and it
        still exercises authentication."""
        _anthropic_client(api_key).models.list(limit=1)


# --------------------------------------------------------------------------
# OpenAI-compatible endpoints (xAI today)
# --------------------------------------------------------------------------


@lru_cache(maxsize=16)
def _openai_client(base_url: str, api_key: str) -> OpenAI:
    """Same caching contract as the Anthropic one, keyed additionally on the
    base URL so two compatible vendors cannot share a client."""
    return OpenAI(api_key=api_key, base_url=base_url)


class OpenAICompatProvider:
    """Any endpoint speaking the OpenAI chat-completions wire format.

    One adapter covers xAI, and would cover DeepSeek, Groq, Together,
    Fireworks or a local vLLM by registering another base URL. That breadth is
    why this is the first non-Anthropic provider built rather than Gemini.

    Two differences from Anthropic are worth naming, because both are places a
    naive port goes quietly wrong:

      - Tool arguments arrive as a JSON **string**, not a dict, and a truncated
        response yields a string that does not parse. That is folded into the
        existing `payload is None` contract rather than raising, so a cut-off
        judge reads the same to `scoring.judge` whichever vendor produced it.
      - Usage is `prompt_tokens`/`completion_tokens`, and `finish_reason` is
        per-choice rather than per-response. Normalized here so no caller has
        to know which vendor answered.
    """

    def __init__(
        self,
        *,
        name: str,
        label: str,
        base_url: str,
        models: tuple[str, ...],
        key_hint: str,
    ) -> None:
        self.name = name
        self.label = label
        self.base_url = base_url
        self.models = models
        self.key_hint = key_hint

    def _client(self, api_key: str) -> OpenAI:
        return _openai_client(self.base_url, api_key)

    def complete(
        self,
        *,
        model: str,
        prompt: str,
        max_tokens: int,
        api_key: str,
        timeout: float | None = None,
    ) -> Completion:
        start = time.time_ns()
        response = self._client(api_key).chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            **({"timeout": timeout} if timeout is not None else {}),
        )
        end = time.time_ns()

        choice = response.choices[0] if response.choices else None

        return Completion(
            text=(choice.message.content or "") if choice else "",
            **_openai_usage(response.usage),
            response_model=response.model,
            response_id=response.id,
            stop_reason=(choice.finish_reason if choice else None) or "unknown",
            start_nano=start,
            end_nano=end,
        )

    def tool_call(
        self,
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
        start = time.time_ns()
        response = self._client(api_key).chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
            **({"timeout": timeout} if timeout is not None else {}),
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": tool_description,
                        # The judge's schema is already plain JSON Schema, which
                        # is what goes here verbatim. That is the whole reason
                        # the judge ports at all.
                        "parameters": input_schema,
                    },
                }
            ],
            tool_choice={"type": "function", "function": {"name": tool_name}},
        )
        end = time.time_ns()

        choice = response.choices[0] if response.choices else None

        payload: dict[str, Any] | None = None
        calls = (choice.message.tool_calls if choice else None) or []
        block = next((c for c in calls if c.function.name == tool_name), None)
        if block is not None:
            try:
                parsed = json.loads(block.function.arguments or "{}")
            except json.JSONDecodeError:
                # Truncated mid-JSON. Same meaning as Anthropic returning no
                # block at all, so it gets the same representation.
                parsed = None
            if isinstance(parsed, dict):
                payload = parsed

        return ToolCallResult(
            payload=payload,
            **_openai_usage(response.usage),
            response_model=response.model,
            response_id=response.id,
            stop_reason=(choice.finish_reason if choice else None) or "unknown",
            start_nano=start,
            end_nano=end,
        )

    def validate_key(self, api_key: str) -> None:
        self._client(api_key).models.list()


# --------------------------------------------------------------------------
# Google Gemini
# --------------------------------------------------------------------------


@lru_cache(maxsize=16)
def _gemini_client(api_key: str) -> genai.Client:
    """Same caching contract as the other two — one client per key, bounded."""
    return genai.Client(api_key=api_key)


def _gemini_usage(usage: Any) -> dict[str, int]:
    """Gemini usage -> the normalized token fields.

    **A third convention, and the one that bites hardest.** The SDK documents
    `total_token_count` as the sum of `prompt_token_count`,
    `candidates_token_count`, `tool_use_prompt_token_count` and
    `thoughts_token_count` — which means those four are *disjoint*:

      - `candidates_token_count` does **not** include thinking tokens, unlike
        Anthropic's and OpenAI's output counts, which do. Gemini 2.5 models
        think by default and thinking is billed as output, so reading
        `candidates_token_count` as the output total under-reports the bill by
        however much the model thought — often most of it.
      - `prompt_token_count` does **not** include tool-use prompt tokens, which
        are billed as input, so they are added back here.
      - `prompt_token_count` *does* include cached content, which the field
        documentation states explicitly. So cached stays a subset and is not
        added again.

    Cache *writes* are deliberately zero. Gemini's explicit caching bills
    storage per hour rather than a per-token write rate, which is not a thing
    this table can express and not a thing this app can trigger — nothing here
    creates a cached content handle.
    """
    thoughts = _int(usage, "thoughts_token_count")
    return {
        "input_tokens": (
            _int(usage, "prompt_token_count") + _int(usage, "tool_use_prompt_token_count")
        ),
        "output_tokens": _int(usage, "candidates_token_count") + thoughts,
        "cached_input_tokens": _int(usage, "cached_content_token_count"),
        "cache_write_tokens": 0,
        "reasoning_tokens": thoughts,
    }


def _gemini_parts(response: Any) -> list[Any]:
    """Every part of the first candidate, tolerating each hop being None.

    A blocked or empty response has candidates with no content, or content with
    no parts. `response.text` papers over this but warns or raises when parts
    are non-text — and a judge's answer is *always* non-text.
    """
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        return list(getattr(content, "parts", None) or [])
    return []


def _gemini_finish(response: Any) -> str:
    """The first candidate's finish reason as a plain string.

    It arrives as an enum; `.name` gives "MAX_TOKENS" rather than the enum's
    repr. Left in Gemini's own spelling on the span — `_Call.truncated` is what
    callers compare against.
    """
    for candidate in getattr(response, "candidates", None) or []:
        reason = getattr(candidate, "finish_reason", None)
        if reason is None:
            return "unknown"
        return str(getattr(reason, "name", None) or reason)
    return "unknown"


class GeminiProvider:
    """Google's Gemini Developer API, via the google-genai SDK.

    Its own adapter rather than an OpenAI-compatible base URL. Google does
    publish a compatibility endpoint, but going native buys the thing that
    matters here: `parameters_json_schema` takes the judge's plain JSON Schema
    verbatim, so the scorer schema needs no translation into Gemini's own
    `Schema` type and cannot drift from what the other two providers are sent.
    """

    name = GEMINI
    label = "Google Gemini"
    # Narrowed to the one model a free-tier key is expected to serve.
    #
    # This is an *offer* list, not a pricing list: the Flash and Pro entries
    # stay fully priced in the SDK's table, so spans already carrying them keep
    # costing correctly and `provider_of_model` still claims them for the
    # cross-vendor guard. Four Gemini ids were already priced-but-not-offered
    # before this, so the shape is the established one rather than a new idea.
    #
    # Widen it again by adding ids back here — nothing else has to change.
    #
    # Note this cannot itself enforce a quota. Google's `models.list()` returns
    # the same catalogue whichever tier a key is on; free tier differs by rate
    # limit, not by availability. So this list expresses an intention about
    # spend, and a 429 from Google remains the thing that actually says no.
    models = ("gemini-3.5-flash-lite",)
    key_hint = "AIza…"

    @staticmethod
    def _http_options(timeout: float | None) -> dict[str, Any]:
        """Gemini's timeout is an int of **milliseconds**, ours is float seconds.

        Passing seconds straight through would ask for a 30ms deadline and fail
        every call — with a timeout error, which reads like a slow model rather
        than a units bug.
        """
        if timeout is None:
            return {}
        return {"http_options": genai_types.HttpOptions(timeout=int(timeout * 1000))}

    def complete(
        self,
        *,
        model: str,
        prompt: str,
        max_tokens: int,
        api_key: str,
        timeout: float | None = None,
    ) -> Completion:
        start = time.time_ns()
        response = _gemini_client(api_key).models.generate_content(
            model=model,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                max_output_tokens=max_tokens,
                **self._http_options(timeout),
            ),
        )
        end = time.time_ns()

        return Completion(
            text="".join(p.text for p in _gemini_parts(response) if getattr(p, "text", None)),
            **_gemini_usage(response.usage_metadata),
            # model_version is what actually answered — an alias like
            # "gemini-2.5-pro" resolves to a dated build, and pricing follows
            # the answer. Falls back to the request when absent.
            response_model=getattr(response, "model_version", None) or model,
            response_id=getattr(response, "response_id", None) or "",
            stop_reason=_gemini_finish(response),
            start_nano=start,
            end_nano=end,
        )

    def tool_call(
        self,
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
        start = time.time_ns()
        response = _gemini_client(api_key).models.generate_content(
            model=model,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                max_output_tokens=max_tokens,
                tools=[
                    genai_types.Tool(
                        function_declarations=[
                            genai_types.FunctionDeclaration(
                                name=tool_name,
                                description=tool_description,
                                # Not `parameters`: that wants Gemini's own
                                # Schema type. This field takes JSON Schema as
                                # written, which is what the scorer produces.
                                parameters_json_schema=input_schema,
                            )
                        ]
                    )
                ],
                # ANY + a single allowed name is Gemini's spelling of "you must
                # answer through this function". AUTO would let the model reply
                # in prose, which is the failure mode forced tool use exists to
                # remove.
                tool_config=genai_types.ToolConfig(
                    function_calling_config=genai_types.FunctionCallingConfig(
                        mode=genai_types.FunctionCallingConfigMode.ANY,
                        allowed_function_names=[tool_name],
                    )
                ),
                **self._http_options(timeout),
            ),
        )
        end = time.time_ns()

        payload: dict[str, Any] | None = None
        for part in _gemini_parts(response):
            call = getattr(part, "function_call", None)
            if call is not None and getattr(call, "name", None) == tool_name:
                # Already a dict here, like Anthropic and unlike an
                # OpenAI-compatible endpoint's JSON string. No parse step, so
                # no parse failure to fold into `payload is None`.
                payload = dict(getattr(call, "args", None) or {})
                break

        return ToolCallResult(
            payload=payload,
            **_gemini_usage(response.usage_metadata),
            response_model=getattr(response, "model_version", None) or model,
            response_id=getattr(response, "response_id", None) or "",
            stop_reason=_gemini_finish(response),
            start_nano=start,
            end_nano=end,
        )

    def validate_key(self, api_key: str) -> None:
        """Cheapest unbilled call that still exercises authentication.

        The listing is lazy, so it has to be consumed for the request to
        actually go out — a bare `models.list()` would validate nothing.
        """
        next(iter(_gemini_client(api_key).models.list()), None)


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------

_PROVIDERS: dict[str, Provider] = {
    ANTHROPIC: AnthropicProvider(),
    XAI: OpenAICompatProvider(
        name=XAI,
        label="xAI",
        base_url="https://api.x.ai/v1",
        # Current lineup as of 2026-08-30. The previous three (grok-4,
        # grok-4-fast-reasoning, grok-3-mini) were retired on 2026-05-15 and
        # are still priced — because xAI answers a retired id with its
        # replacement rather than rejecting it, so old spans are real spend —
        # but offering them would invite new calls that silently run a
        # different model than the one on screen.
        models=("grok-4.6", "grok-4.5", "grok-4.3"),
        key_hint="xai-…",
    ),
    GEMINI: GeminiProvider(),
}


def _check_offered_models_are_priced() -> None:
    """Every model offered in a picker must have a price. Checked at import.

    The frontend's old model list carried a comment asking whoever edited it to
    keep it in step with the pricing table, because a model missing from that
    table runs fine and silently reports no cost. In a tool whose job is
    watching spend, a model you can select and cannot cost is the worst kind of
    bug — it looks like it worked. An invariant that fails at boot is worth
    more than the comment asking someone to remember.
    """
    from obs_sdk.pricing import PRICING

    missing = [
        f"{p.name}/{model}"
        for p in _PROVIDERS.values()
        for model in p.models
        if (p.name, model) not in PRICING
    ]
    if missing:
        raise RuntimeError(
            "These models are offered but have no price, so they would report "
            f"no cost: {', '.join(missing)}. Add them to "
            "obs_sdk.pricing.PRICING or stop offering them."
        )


_check_offered_models_are_priced()


def get_provider(provider: str) -> Provider:
    """The adapter for a provider name. Raises rather than defaulting.

    There is deliberately no fallback to Anthropic: "which vendor is this"
    is a question with money attached, and a transport layer quietly picking
    one is how spend ends up on an account nobody chose.
    """
    try:
        return _PROVIDERS[provider]
    except KeyError:
        raise UnknownProvider(
            f"No provider is configured under {provider!r}. Known providers: "
            f"{', '.join(sorted(_PROVIDERS))}."
        ) from None


def provider_names() -> list[str]:
    return sorted(_PROVIDERS)


def provider_choices() -> list[dict[str, Any]]:
    """Name, label and offered models — for the Keys and model dropdowns.

    Served rather than duplicated in the frontend so that registering a
    provider stays one change in this file.
    """
    return [
        {
            "name": p.name,
            "label": p.label,
            "models": list(p.models),
            "key_hint": p.key_hint,
        }
        for p in sorted(_PROVIDERS.values(), key=lambda p: p.label)
    ]


def provider_label(provider: str) -> str:
    """The span-attribute value for a provider name. Never raises.

    Span builders run on error paths — recording *that* a call failed is the
    whole point of the error span — and a bad provider name is one of the
    failures being recorded. A raising lookup there would turn one failed test
    case into a failed run, so labeling gets the total function and the call
    path gets the strict one.
    """
    return provider if provider in _PROVIDERS else ""


def usage_attributes(call: _Call) -> dict[str, int]:
    """The token subsets worth putting on a span, omitting the zeros.

    Namespaced `obs.*` rather than `gen_ai.*` deliberately. The GenAI semantic
    conventions are still pre-stable here and have not settled on names for
    cached or reasoning tokens; claiming a `gen_ai.usage.*` name now risks
    meaning something different from what the convention eventually defines,
    and a span attribute that quietly changes meaning is worse than one that
    was never standard. Rename when the convention lands.

    Zeros are omitted rather than written as 0. Most calls cache nothing and
    reason not at all, and three always-present zero fields would triple the
    size of the attribute bag on every span to say "this did not happen" —
    which their absence already says.
    """
    attrs: dict[str, int] = {}
    if call.cached_input_tokens:
        attrs["obs.cached_input_tokens"] = call.cached_input_tokens
    if call.cache_write_tokens:
        attrs["obs.cache_write_tokens"] = call.cache_write_tokens
    if call.reasoning_tokens:
        attrs["obs.reasoning_tokens"] = call.reasoning_tokens
    return attrs


def provider_label_for(provider: str) -> str:
    """The human name for a provider ("xAI"), falling back to the registry name.

    For error text read by a person choosing from a dropdown, where "xai" is
    not what the dropdown said.
    """
    p = _PROVIDERS.get(provider)
    return p.label if p is not None else provider


def check_model_matches(provider: str, model: str) -> None:
    """Refuse a model that demonstrably belongs to a different provider.

    What is left of the old `provider_for` guard, and it keeps the property
    that made that guard worth having: a mistake is caught before it is paid
    for, rather than after one round trip per test case in a replay run.

    Deliberately narrow. It only fires when the pricing table already places
    this model with someone else — running `claude-opus-5` against an xAI key,
    which is the mistake a dropdown makes easy. A model id nobody has heard of
    passes through, because a model released this morning is indistinguishable
    from a typo here and only one of those should be a hard error.
    """
    from obs_sdk.pricing import provider_of_model

    owner = provider_of_model(model)
    if owner is not None and owner != provider:
        # Labels, not registry names: this string is read by a person choosing
        # from a dropdown, and "xAI" is what that dropdown said.
        owner_label = _PROVIDERS[owner].label if owner in _PROVIDERS else owner
        chosen_label = _PROVIDERS[provider].label if provider in _PROVIDERS else provider
        # Phrased without indefinite articles before a vendor name: "a
        # Anthropic" / "an Google" is the bug that writes itself the next time
        # a provider is registered.
        raise ModelProviderMismatch(
            f"{model} belongs to {owner_label}, but the selected key is for "
            f"{chosen_label}. Choose a model from {chosen_label}, or a key "
            f"from {owner_label}."
        )


# --------------------------------------------------------------------------
# The two call shapes
# --------------------------------------------------------------------------


def complete(
    *,
    provider: str,
    model: str,
    prompt: str,
    max_tokens: int,
    api_key: str,
    timeout: float | None = None,
) -> Completion:
    """One prose-in, prose-out call.

    `provider` and `api_key` both come from the caller's chosen credential.

    `timeout` bounds the HTTP call. Background work (a replay run) leaves it
    unset — a slow model there costs time, not a staring user. Anything in a
    request path should set it.
    """
    adapter = get_provider(provider)
    check_model_matches(provider, model)  # raises before spending anything
    return adapter.complete(
        model=model,
        prompt=prompt,
        max_tokens=max_tokens,
        api_key=api_key,
        timeout=timeout,
    )


def tool_call(
    *,
    provider: str,
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

    Forcing the named tool is what makes this different from asking for JSON in
    the prompt: the model cannot reply with a paragraph, so parse failures stop
    being a category of error. The schema constrains sampling — it does not
    guarantee the model respected an enum or a numeric bound, so callers still
    validate what comes back.
    """
    adapter = get_provider(provider)
    check_model_matches(provider, model)
    return adapter.tool_call(
        model=model,
        prompt=prompt,
        max_tokens=max_tokens,
        tool_name=tool_name,
        tool_description=tool_description,
        input_schema=input_schema,
        api_key=api_key,
        timeout=timeout,
    )


def validate_key(provider: str, api_key: str) -> None:
    """Check a key works. Raises on a bad key.

    Called when a key is saved rather than when it is first spent. A typo'd key
    discovered at save time is an edit; the same typo discovered mid-run has
    already cost the replay calls that ran before it.
    """
    get_provider(provider).validate_key(api_key)
