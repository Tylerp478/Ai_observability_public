"""Write-ahead log and Parquet compaction.

Why a WAL rather than an in-memory buffer. CLAUDE.md is explicit that we must
not write one object per span — the batching is non-negotiable for cost and
latency. But a pure in-memory buffer means a backend crash silently drops
every span accepted since the last flush, and the ingest endpoint has already
returned 200 by then. Appending to a local NDJSON file first costs one
buffered write and turns data loss into recoverable state.

Flow:

    POST /v1/traces -> append NDJSON to wal/{project}/{date}.ndjson  (durable)
                    -> compactor reads WAL, writes Parquet, truncates WAL

The query layer reads the WAL as well as Parquet, so a span is visible
immediately on ingest rather than after the next compaction.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from datetime import datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq

from obs_backend.models import Span
from obs_backend.storage import StorageBackend

# Explicit Arrow schema rather than letting pyarrow infer from the first batch.
# Inference would type an all-null column (a batch of tool spans has no token
# counts) as null, and the next batch with real values would fail to append
# with a schema mismatch.
ARROW_SCHEMA = pa.schema(
    [
        ("trace_id", pa.string()),
        ("span_id", pa.string()),
        ("parent_span_id", pa.string()),
        ("name", pa.string()),
        ("kind", pa.string()),
        ("start_time_unix_nano", pa.int64()),
        ("end_time_unix_nano", pa.int64()),
        ("duration_ms", pa.float64()),
        ("status_code", pa.string()),
        ("status_message", pa.string()),
        ("project_id", pa.string()),
        ("service_name", pa.string()),
        ("gen_ai_operation_name", pa.string()),
        ("gen_ai_provider_name", pa.string()),
        ("gen_ai_request_model", pa.string()),
        ("gen_ai_response_model", pa.string()),
        ("gen_ai_response_id", pa.string()),
        ("gen_ai_request_max_tokens", pa.int64()),
        ("gen_ai_usage_input_tokens", pa.int64()),
        ("gen_ai_usage_output_tokens", pa.int64()),
        ("gen_ai_finish_reasons", pa.string()),
        ("gen_ai_input_messages", pa.string()),
        ("gen_ai_output_messages", pa.string()),
        ("gen_ai_agent_name", pa.string()),
        ("gen_ai_tool_name", pa.string()),
        ("obs_cost_usd", pa.float64()),
        ("obs_latency_seconds", pa.float64()),
        ("attributes_json", pa.string()),
        ("events_json", pa.string()),
    ]
)

_COLUMNS = [f.name for f in ARROW_SCHEMA]


def span_to_row(span: Span) -> dict:
    row = span.model_dump(exclude={"events"})
    row["duration_ms"] = span.duration_ms
    row["events_json"] = json.dumps([e.model_dump() for e in span.events])
    return {k: row.get(k) for k in _COLUMNS}


def wal_key(project_id: str, date: str) -> str:
    return f"wal/project={project_id}/dt={date}.ndjson"


def parquet_prefix(project_id: str, date: str) -> str:
    return f"traces/project={project_id}/dt={date}"


class SpanWriter:
    """Appends spans to the WAL and compacts them into Parquet.

    Thread-safe: uvicorn serves requests on a worker pool and the compactor
    runs on a background thread, so WAL append and WAL truncate can race. One
    lock covers both, which is plenty at prototype write volume.
    """

    def __init__(self, storage: StorageBackend) -> None:
        self.storage = storage
        self._lock = threading.Lock()
        self._pending = 0

    def append(self, spans: list[Span]) -> int:
        """Append spans to the WAL, grouped by project and UTC date."""
        if not spans:
            return 0

        grouped: dict[tuple[str, str], list[Span]] = defaultdict(list)
        for span in spans:
            grouped[(span.project_id, span.partition_date)].append(span)

        with self._lock:
            for (project_id, date), group in grouped.items():
                lines = "".join(
                    json.dumps(span_to_row(s), default=str) + "\n" for s in group
                )
                self.storage.append_text(wal_key(project_id, date), lines)
            self._pending += len(spans)
        return len(spans)

    @property
    def pending(self) -> int:
        return self._pending

    def compact(self) -> int:
        """Fold every WAL file into Parquet and truncate what was folded.

        Returns the number of spans compacted. Safe to call when there's
        nothing to do.
        """
        with self._lock:
            wal_keys = [k for k in self.storage.list_keys("wal") if k.endswith(".ndjson")]
            total = 0

            for key in wal_keys:
                path = self.storage.local_path(key)
                if path is None or not path.exists():
                    continue

                raw = path.read_text(encoding="utf-8")
                if not raw.strip():
                    continue

                rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
                if not rows:
                    continue

                # project=X/dt=Y.ndjson -> the same partition, as Parquet
                partition = key[len("wal/") : -len(".ndjson")]
                project_id, date = partition.split("/")
                stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S%f")
                out_key = f"traces/{project_id}/{date}/spans-{stamp}.parquet"

                table = pa.Table.from_pylist(rows, schema=ARROW_SCHEMA)
                sink = pa.BufferOutputStream()
                pq.write_table(table, sink, compression="zstd")
                self.storage.write_bytes(out_key, sink.getvalue().to_pybytes())

                # Truncate only what we just folded in. Anything appended
                # while we were writing stays — hence re-reading length rather
                # than deleting the file outright.
                remainder = path.read_text(encoding="utf-8")[len(raw) :]
                path.write_text(remainder, encoding="utf-8")

                total += len(rows)

            self._pending = max(0, self._pending - total)
            return total
