"""Decode OTLP protobuf into our internal Span model.

The OTLP payload is nested three levels deep and the nesting carries meaning:

    ExportTraceServiceRequest
    └── resource_spans[]        <- resource attributes (service.name) live here
        └── scope_spans[]       <- instrumentation library that emitted them
            └── spans[]         <- the actual spans

Resource attributes are stored once per batch rather than repeated on every
span, so service.name has to be pulled from the resource level and pushed down
onto each span. Missing that is the classic first-OTLP-receiver bug: every
span comes out with an empty service name.
"""

from __future__ import annotations

import json
from typing import Any

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import Span as PBSpan

from obs_backend.models import Span, SpanEvent, split_attributes

# OTLP SpanKind enum -> readable string. The enum is an int on the wire.
_SPAN_KIND = {
    PBSpan.SPAN_KIND_UNSPECIFIED: "UNSPECIFIED",
    PBSpan.SPAN_KIND_INTERNAL: "INTERNAL",
    PBSpan.SPAN_KIND_SERVER: "SERVER",
    PBSpan.SPAN_KIND_CLIENT: "CLIENT",
    PBSpan.SPAN_KIND_PRODUCER: "PRODUCER",
    PBSpan.SPAN_KIND_CONSUMER: "CONSUMER",
}

_STATUS_CODE = {0: "UNSET", 1: "OK", 2: "ERROR"}


def _any_value(value: AnyValue) -> Any:
    """Unwrap OTLP's AnyValue union.

    AnyValue is a oneof — exactly one field is set — so we dispatch on which.
    Nested array/kvlist values recurse.
    """
    which = value.WhichOneof("value")
    if which == "string_value":
        return value.string_value
    if which == "bool_value":
        return value.bool_value
    if which == "int_value":
        return value.int_value
    if which == "double_value":
        return value.double_value
    if which == "array_value":
        return [_any_value(v) for v in value.array_value.values]
    if which == "kvlist_value":
        return {kv.key: _any_value(kv.value) for kv in value.kvlist_value.values}
    if which == "bytes_value":
        return value.bytes_value.hex()
    return None


def _attributes(kvs: list[KeyValue]) -> dict[str, Any]:
    return {kv.key: _any_value(kv.value) for kv in kvs}


def _hex(raw: bytes) -> str:
    """OTLP carries IDs as raw bytes; everyone displays them as hex."""
    return raw.hex()


def decode(payload: bytes, *, project_id: str) -> list[Span]:
    """Decode a serialized ExportTraceServiceRequest into Spans.

    project_id comes from the API key used to authenticate the request, not
    from the payload — a client cannot write into another project's partition
    by lying about it.
    """
    request = ExportTraceServiceRequest()
    request.ParseFromString(payload)

    spans: list[Span] = []
    for resource_spans in request.resource_spans:
        resource_attrs = _attributes(resource_spans.resource.attributes)
        service_name = str(resource_attrs.get("service.name", ""))

        for scope_spans in resource_spans.scope_spans:
            for pb in scope_spans.spans:
                spans.append(
                    _decode_span(
                        pb, project_id=project_id, service_name=service_name
                    )
                )
    return spans


def _decode_span(pb: PBSpan, *, project_id: str, service_name: str) -> Span:
    attrs = _attributes(pb.attributes)
    promoted, rest_json = split_attributes(attrs)

    finish_reasons = promoted.get("gen_ai.response.finish_reasons")

    return Span(
        trace_id=_hex(pb.trace_id),
        span_id=_hex(pb.span_id),
        # An all-zero parent_span_id means "no parent" (root span). OTLP uses
        # zeroed bytes rather than omitting the field, so an emptiness check
        # on the raw bytes would wrongly mark every root as parented.
        parent_span_id=_hex(pb.parent_span_id) if any(pb.parent_span_id) else None,
        name=pb.name,
        kind=_SPAN_KIND.get(pb.kind, "INTERNAL"),
        start_time_unix_nano=pb.start_time_unix_nano,
        end_time_unix_nano=pb.end_time_unix_nano,
        status_code=_STATUS_CODE.get(pb.status.code, "UNSET"),
        status_message=pb.status.message,
        project_id=project_id,
        service_name=service_name,
        gen_ai_operation_name=promoted.get("gen_ai.operation.name"),
        gen_ai_provider_name=promoted.get("gen_ai.provider.name"),
        gen_ai_request_model=promoted.get("gen_ai.request.model"),
        gen_ai_response_model=promoted.get("gen_ai.response.model"),
        gen_ai_response_id=promoted.get("gen_ai.response.id"),
        gen_ai_request_max_tokens=promoted.get("gen_ai.request.max_tokens"),
        gen_ai_usage_input_tokens=promoted.get("gen_ai.usage.input_tokens"),
        gen_ai_usage_output_tokens=promoted.get("gen_ai.usage.output_tokens"),
        # Stored as a JSON array string: the attribute is list-typed and
        # Parquet list columns are awkward to query from DuckDB by comparison.
        gen_ai_finish_reasons=(
            json.dumps(finish_reasons) if finish_reasons is not None else None
        ),
        gen_ai_input_messages=promoted.get("gen_ai.input.messages"),
        gen_ai_output_messages=promoted.get("gen_ai.output.messages"),
        gen_ai_agent_name=promoted.get("gen_ai.agent.name"),
        gen_ai_tool_name=promoted.get("gen_ai.tool.name"),
        obs_cost_usd=promoted.get("obs.cost_usd"),
        obs_latency_seconds=promoted.get("obs.latency_seconds"),
        attributes_json=rest_json,
        events=[
            SpanEvent(
                name=event.name,
                time_unix_nano=event.time_unix_nano,
                attributes_json=json.dumps(_attributes(event.attributes), default=str),
            )
            for event in pb.events
        ],
    )
