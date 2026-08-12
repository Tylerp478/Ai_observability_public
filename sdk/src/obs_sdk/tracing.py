"""Wraps Anthropic chat completions in an OTel span using GenAI semantic
conventions.

Attribute names are not pinned to a convention version — they are hand-written
and verified against the installed semconv package. See SEMCONV_SOURCE below
for the provenance and the two names that actually changed. These names are
still pre-stable upstream, so re-verify when bumping the dependency.

Exporters are selected by env var (see _get_tracer):

    OBS_EXPORTER=console   pretty-print spans to stdout (default)
    OBS_EXPORTER=otlp      POST to the backend's OTLP endpoint
    OBS_EXPORTER=both      both of the above

The OTLP path uses BatchSpanProcessor, which buffers spans on a background
thread rather than blocking the caller on every span. Console stays on
SimpleSpanProcessor so output appears in order while you're reading it.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Iterator
from typing import Any

from anthropic import Anthropic
from anthropic.types import Message
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.trace import Status, StatusCode

from obs_sdk.pricing import estimate_cost_usd

# Attribute-name provenance, verified 2026-07-27 against the installed
# opentelemetry-semantic-conventions 0.65b0.
#
# Every gen_ai.* constant in that package is marked Deprecated, for two
# different reasons — don't read that as "these names are wrong":
#   - Most are "Moved to the OpenTelemetry GenAI semantic conventions
#     repository" (open-telemetry/semantic-conventions-genai). The names are
#     unchanged; only governance moved. There is no published PyPI package for
#     the new repo yet, so the strings below are hand-written rather than
#     imported. Revisit when one ships.
#   - gen_ai.system was genuinely renamed to gen_ai.provider.name.
#   - gen_ai.prompt / gen_ai.completion were "Removed, no replacement at this
#     time"; the structured successors are gen_ai.input.messages /
#     gen_ai.output.messages. See the note at the call site below.
#
# Custom attributes live under obs.* rather than squatting on the
# spec-governed gen_ai.* namespace.
SEMCONV_SOURCE = "opentelemetry-semantic-conventions==0.65b0 (gen_ai, incubating)"

_tracer: trace.Tracer | None = None

DEFAULT_OTLP_ENDPOINT = "http://localhost:8000/v1/traces"


def _build_otlp_exporter() -> Any:
    """Construct the OTLP/HTTP exporter.

    Imported lazily: the exporter package is an optional dependency, and the
    console-only path shouldn't require it to be installed.

    Auth is a bearer token — the ingest API key issued by the backend. The SDK
    can't hold a session cookie, which is why ingest has its own auth path.
    """
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    endpoint = os.environ.get("OBS_OTLP_ENDPOINT", DEFAULT_OTLP_ENDPOINT)
    api_key = os.environ.get("OBS_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OBS_EXPORTER requests OTLP export but OBS_API_KEY is not set.\n"
            "\n"
            "Create an ingest key on the Keys page of the web UI — the plaintext is "
            "shown once, at creation — and put it in .env as OBS_API_KEY. On a fresh "
            "install with no UI running yet, backend/scripts/bootstrap.py creates the "
            "schema and a first key.\n"
            "\n"
            "Set OBS_SERVICE_NAME too if this is a second app: it is what separates "
            "one source from another in the Overview and Traces filters.\n"
            "\n"
            "Or set OBS_EXPORTER=console to skip the backend entirely."
        )
    return OTLPSpanExporter(endpoint=endpoint, headers={"Authorization": f"Bearer {api_key}"})


def _get_tracer() -> trace.Tracer:
    global _tracer
    if _tracer is None:
        provider = TracerProvider(
            resource=Resource.create({"service.name": os.environ.get("OBS_SERVICE_NAME", "obs-sdk-demo")})
        )

        mode = os.environ.get("OBS_EXPORTER", "console").lower()
        if mode not in {"console", "otlp", "both"}:
            raise ValueError(f"OBS_EXPORTER must be console, otlp, or both — got {mode!r}")

        if mode in {"console", "both"}:
            # Simple (not batched) so spans print in the order they close,
            # which is what makes the console output readable.
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        if mode in {"otlp", "both"}:
            # Batched: buffers on a background thread and flushes on size or
            # timeout, so a network round trip never sits in the caller's path.
            provider.add_span_processor(BatchSpanProcessor(_build_otlp_exporter()))

        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("obs_sdk", "0.1.0")
    return _tracer


def shutdown() -> None:
    """Flush buffered spans and stop the exporter threads.

    BatchSpanProcessor holds spans in memory until a size or time threshold is
    hit, so a short-lived script can exit with spans still unflushed. Call this
    before the process ends. The example does it in a finally block.
    """
    provider = trace.get_tracer_provider()
    if isinstance(provider, TracerProvider):
        provider.shutdown()


@contextlib.contextmanager
def _traced(span_name: str, attributes: dict[str, Any]) -> Iterator[trace.Span]:
    """Shared span body for the nesting helpers.

    record_exception/set_status_on_exception are both disabled because we do
    it ourselves below. Left at their defaults, OTel records the exception a
    second time on context exit — two identical exception events per failure,
    which doubles the stacktrace payload in Parquet and makes error spans read
    as two failures in the UI.
    """
    tracer = _get_tracer()
    with tracer.start_as_current_span(
        span_name, record_exception=False, set_status_on_exception=False
    ) as span:
        for key, value in attributes.items():
            span.set_attribute(key, value)
        start = time.perf_counter()
        try:
            yield span
        except Exception as exc:
            span.set_attribute("obs.latency_seconds", time.perf_counter() - start)
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise
        span.set_attribute("obs.latency_seconds", time.perf_counter() - start)
        span.set_status(Status(StatusCode.OK))


@contextlib.contextmanager
def agent_step(name: str, **attributes: Any) -> Iterator[trace.Span]:
    """Wrap a unit of agent work — the parent span other spans nest under.

    This is what gives a trace depth. Without a parent span, every LLM call is
    a separate root and the waterfall is a flat list of one-bar traces.

        with agent_step("answer_question"):
            with tool_call("search"):
                ...
            traced_completion(client, ...)
    """
    with _traced(
        f"agent {name}",
        {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": name, **attributes},
    ) as span:
        yield span


@contextlib.contextmanager
def tool_call(name: str, **attributes: Any) -> Iterator[trace.Span]:
    """Wrap a tool invocation (retrieval, API call, calculation).

    Tool latency is often the real cost in an agent trace even though the LLM
    call gets the attention — that contrast is the point of the waterfall.
    """
    with _traced(
        f"tool {name}",
        {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name, **attributes},
    ) as span:
        yield span


def _require_api_key() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Refusing to make a paid API call without it — "
            "set it in your environment or .env file before calling traced_completion()."
        )


def traced_completion(
    client: Anthropic,
    *,
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    **kwargs: Any,
) -> Message:
    """Drop-in wrapper around client.messages.create() that emits an OTel
    span with gen_ai.* attributes. Returns the same Message object the
    Anthropic SDK would return unmodified.
    """
    _require_api_key()
    tracer = _get_tracer()

    # record_exception/set_status_on_exception disabled — we handle both below.
    # See the note in _traced() for why the defaults double-record.
    with tracer.start_as_current_span(
        f"chat {model}", record_exception=False, set_status_on_exception=False
    ) as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.provider.name", "anthropic")
        span.set_attribute("gen_ai.request.model", model)
        span.set_attribute("gen_ai.request.max_tokens", max_tokens)
        # gen_ai.prompt/completion were removed from semconv with no direct
        # replacement; the structured successors are gen_ai.input.messages /
        # gen_ai.output.messages. We keep flat strings for now because step 2
        # offloads these payloads to S3 blobs anyway — the shape is decided
        # there, not here. Deliberate deviation, revisit at ingest.
        span.set_attribute("gen_ai.input.messages", str(messages))

        start = time.perf_counter()
        try:
            response = client.messages.create(
                model=model, messages=messages, max_tokens=max_tokens, **kwargs
            )
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            raise
        latency_s = time.perf_counter() - start

        completion_text = "".join(
            block.text for block in response.content if block.type == "text"
        )

        span.set_attribute("gen_ai.response.model", response.model)
        span.set_attribute("gen_ai.response.id", response.id)
        span.set_attribute("gen_ai.output.messages", completion_text)
        span.set_attribute("gen_ai.usage.input_tokens", response.usage.input_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", response.usage.output_tokens)
        # Spec models this as a list — a turn can stop for more than one reason.
        span.set_attribute("gen_ai.response.finish_reasons", [response.stop_reason or "unknown"])
        # Custom (no spec attribute exists). Span duration already covers wall
        # time; this survives into Parquet without a computed column.
        span.set_attribute("obs.latency_seconds", latency_s)

        cost_usd = estimate_cost_usd(
            response.model, response.usage.input_tokens, response.usage.output_tokens
        )
        if cost_usd is not None:
            span.set_attribute("obs.cost_usd", cost_usd)

        span.set_status(Status(StatusCode.OK))
        return response
