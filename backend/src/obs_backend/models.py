"""The internal span shape.

One model, used end to end: OTLP decode produces it, the WAL serializes it,
Parquet columns mirror it, and the read API returns it. Changing the shape
here means changing the Parquet schema, so it's deliberately narrow.

Design note — fixed columns plus a JSON bag. The gen_ai.* and obs.* attributes
we know about get real columns so DuckDB can filter and aggregate on them
without parsing. Everything else goes into `attributes_json`. Spans are
heterogeneous and the conventions are still pre-stable, so a fixed-only schema
would need a migration every time an attribute appears.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

# Attributes promoted to real Parquet columns. Everything else lands in the
# JSON bag. Keep in sync with Span.from_attributes and the Arrow schema.
PROMOTED_ATTRIBUTES = {
    "gen_ai.operation.name",
    "gen_ai.provider.name",
    "gen_ai.request.model",
    "gen_ai.response.model",
    "gen_ai.response.id",
    "gen_ai.request.max_tokens",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.response.finish_reasons",
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.agent.name",
    "gen_ai.tool.name",
    "obs.cost_usd",
    "obs.latency_seconds",
}


class SpanEvent(BaseModel):
    name: str
    time_unix_nano: int
    attributes_json: str = "{}"


class Span(BaseModel):
    # --- identity / structure ---
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    name: str
    kind: str = "INTERNAL"

    # --- timing ---
    # Kept as nanosecond ints (OTLP's native unit) rather than datetimes so
    # waterfall offsets are exact. start_time is derived for partitioning and
    # for humans reading the Parquet directly.
    start_time_unix_nano: int
    end_time_unix_nano: int

    # --- status ---
    status_code: str = "UNSET"
    status_message: str = ""

    # --- provenance ---
    project_id: str
    service_name: str = ""

    # --- promoted GenAI attributes (nullable: not every span is an LLM call) ---
    gen_ai_operation_name: str | None = None
    gen_ai_provider_name: str | None = None
    gen_ai_request_model: str | None = None
    gen_ai_response_model: str | None = None
    gen_ai_response_id: str | None = None
    gen_ai_request_max_tokens: int | None = None
    gen_ai_usage_input_tokens: int | None = None
    gen_ai_usage_output_tokens: int | None = None
    gen_ai_finish_reasons: str | None = None  # JSON array, list-typed in OTLP
    gen_ai_input_messages: str | None = None
    gen_ai_output_messages: str | None = None
    gen_ai_agent_name: str | None = None
    gen_ai_tool_name: str | None = None

    # --- promoted custom attributes ---
    obs_cost_usd: float | None = None
    obs_latency_seconds: float | None = None

    # --- everything else ---
    attributes_json: str = "{}"
    events: list[SpanEvent] = Field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        return (self.end_time_unix_nano - self.start_time_unix_nano) / 1_000_000

    @property
    def partition_date(self) -> str:
        """UTC date of span start, for the dt= partition key.

        UTC deliberately: partitioning on local time makes the partition
        boundary move with the reader's timezone and DST.
        """
        return datetime.fromtimestamp(
            self.start_time_unix_nano / 1_000_000_000, tz=timezone.utc
        ).strftime("%Y-%m-%d")


def split_attributes(attrs: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Split a flat attribute dict into promoted values and a JSON remainder."""
    promoted = {k: v for k, v in attrs.items() if k in PROMOTED_ATTRIBUTES}
    rest = {k: v for k, v in attrs.items() if k not in PROMOTED_ATTRIBUTES}
    return promoted, json.dumps(rest, default=str)
