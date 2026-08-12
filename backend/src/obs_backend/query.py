"""DuckDB query layer over Parquet + WAL.

DuckDB reads Parquet directly — no load step, no separate warehouse. For S3 it
would read over httpfs with the same SQL.

The union with the WAL is the part that matters for usability. Spans live in
NDJSON until the compactor folds them into Parquet, so querying Parquet alone
means a trace you just emitted doesn't exist yet. That produces the worst
possible first impression ("I ran it and the UI is empty") and it's
indistinguishable from a real bug. Reading both makes ingest look instant.

Scale note: this is the layer that would be replaced first. DuckDB scanning
Parquet is fine for a prototype and for millions of spans; at sustained
high-cardinality ingest you'd want ClickHouse or a purpose-built trace store.
Not building for that now.
"""

from __future__ import annotations

import json
import time
from typing import Any

import duckdb

from obs_backend.storage import StorageBackend
from obs_backend.wal import DUCKDB_WAL_COLUMNS

# Columns read from both sources. Listed explicitly and in the same order so
# the UNION lines up by position regardless of NDJSON key ordering.
_SELECT = """
    trace_id, span_id, parent_span_id, name, kind,
    CAST(start_time_unix_nano AS BIGINT) AS start_time_unix_nano,
    CAST(end_time_unix_nano   AS BIGINT) AS end_time_unix_nano,
    CAST(duration_ms AS DOUBLE) AS duration_ms,
    status_code, status_message, project_id, service_name,
    gen_ai_operation_name, gen_ai_provider_name,
    gen_ai_request_model, gen_ai_response_model, gen_ai_response_id,
    CAST(gen_ai_request_max_tokens  AS BIGINT) AS gen_ai_request_max_tokens,
    CAST(gen_ai_usage_input_tokens  AS BIGINT) AS gen_ai_usage_input_tokens,
    CAST(gen_ai_usage_output_tokens AS BIGINT) AS gen_ai_usage_output_tokens,
    gen_ai_finish_reasons, gen_ai_input_messages, gen_ai_output_messages,
    gen_ai_agent_name, gen_ai_tool_name,
    CAST(obs_cost_usd        AS DOUBLE) AS obs_cost_usd,
    CAST(obs_latency_seconds AS DOUBLE) AS obs_latency_seconds,
    attributes_json, events_json
"""


# Which provider key paid for a span. Read out of the JSON attribute bag
# rather than promoted to a real column: models.py keeps that schema
# deliberately narrow, and adding a column would mean every Parquet file
# written before today lacks it — which the explicit column list in _SELECT
# would fail on rather than read as null.
_CREDENTIAL_EXPR = "json_extract_string(attributes_json, '$.\"obs.credential\"')"


class TraceQuery:
    def __init__(self, storage: StorageBackend) -> None:
        self.storage = storage
        self.conn = duckdb.connect(":memory:")

    def _spans_relation(self, project_id: str) -> str | None:
        """Build a SQL expression unioning Parquet files and WAL files.

        Returns None when neither source has anything, because DuckDB errors
        on a glob that matches no files rather than returning empty.
        """
        parquet_glob = self.storage.duckdb_glob(
            f"traces/project={project_id}/dt=*/*.parquet"
        )
        wal_glob = self.storage.duckdb_glob(f"wal/project={project_id}/dt=*.ndjson")

        has_parquet = bool(self.storage.list_keys(f"traces/project={project_id}"))
        wal_keys = [
            k
            for k in self.storage.list_keys(f"wal/project={project_id}")
            if k.endswith(".ndjson")
            and (p := self.storage.local_path(k)) is not None
            and p.exists()
            and p.stat().st_size > 0
        ]

        parts: list[str] = []
        if has_parquet:
            parts.append(f"SELECT {_SELECT} FROM read_parquet('{parquet_glob}')")
        if wal_keys:
            # Explicit column types, never read_json_auto. See DUCKDB_WAL_COLUMNS
            # in wal.py: a WAL file where some column is null in every row gets
            # that column inferred as JSON, and the UNION with Parquet's VARCHAR
            # then fails to cast a bare span id. Declaring the schema — which we
            # know exactly, since we wrote the file — removes the guess. Missing
            # keys still read as null, so this keeps what union_by_name gave us.
            parts.append(
                f"SELECT {_SELECT} FROM read_json('{wal_glob}', "
                f"format='newline_delimited', columns={DUCKDB_WAL_COLUMNS})"
            )

        if not parts:
            return None
        return " UNION ALL ".join(parts)

    def sources(self, project_id: str) -> list[dict[str, Any]]:
        """Distinct services reporting in, with volume and recency.

        This is what the Overview and Traces filters are built from. A "source"
        is `service.name` off the OTLP resource — set per application via
        OBS_SERVICE_NAME in the SDK — so telling two apps apart needs no schema
        change and no second project.

        Deduped like the aggregates below: an uncompacted span present in both
        the WAL and Parquet would otherwise count twice and make a quiet source
        look busier than it is.
        """
        relation = self._spans_relation(project_id)
        if relation is None:
            return []

        sql = f"""
        WITH spans AS ({relation}),
        deduped AS (
            SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY span_id) AS rn
                FROM spans
            ) WHERE rn = 1
        )
        SELECT service_name,
               COUNT(*)                        AS span_count,
               MAX(start_time_unix_nano)       AS last_span_unix_nano
        FROM deduped
        WHERE service_name IS NOT NULL AND service_name <> ''
        GROUP BY service_name
        ORDER BY span_count DESC
        """
        return [
            {
                "name": r["service_name"],
                "span_count": int(r["span_count"]),
                "last_span_unix_nano": int(r["last_span_unix_nano"] or 0),
            }
            for r in self._rows(sql)
        ]

    def spend_by_credential(self, project_id: str) -> dict[str, float]:
        """Total cost per provider key, keyed by credential name.

        Read from the span store rather than summed out of Postgres. The runs
        and scores tables only know about eval spend; a Playground call and a
        guardrail check are just as real and appear in neither. Every billable
        call writes a span carrying obs.credential, so this is the one place
        that sees all of it — which is the whole premise of the app applied to
        its own spending.
        """
        relation = self._spans_relation(project_id)
        if relation is None:
            return {}

        sql = f"""
        WITH spans AS ({relation}),
        deduped AS (
            SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY span_id) AS rn
                FROM spans
            ) WHERE rn = 1
        )
        SELECT {_CREDENTIAL_EXPR} AS credential,
               SUM(COALESCE(obs_cost_usd, 0)) AS cost_usd
        FROM deduped
        WHERE {_CREDENTIAL_EXPR} IS NOT NULL
        GROUP BY credential
        """
        return {r["credential"]: float(r["cost_usd"] or 0) for r in self._rows(sql)}

    def list_traces(
        self,
        project_id: str,
        limit: int = 50,
        source: str | None = None,
        credential: str | None = None,
    ) -> list[dict[str, Any]]:
        """One row per trace, rolled up from its spans.

        Cost and tokens sum across the whole trace — the number that matters
        is what the trace cost, not what one span cost. Root span name and
        status come from the span with no parent.

        `source` filters the spans, not the traces. That is correct here
        because a trace originates from exactly one service in this system —
        an eval run is all obs-runner, a scoring job is all obs-judge, an
        instrumented app is all itself. If a trace ever spans two services the
        rollups would describe only the filtered half, and this would need to
        become "traces having any span from X" instead.
        """
        relation = self._spans_relation(project_id)
        if relation is None:
            return []

        clauses: list[str] = []
        params: list[Any] = []
        if source:
            clauses.append("service_name = ?")
            params.append(source)
        if credential:
            clauses.append(f"{_CREDENTIAL_EXPR} = ?")
            params.append(credential)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        sql = f"""
        WITH spans AS (SELECT * FROM ({relation}) {where}),
        roots AS (
            SELECT trace_id, name AS root_name, status_code AS root_status,
                   start_time_unix_nano AS root_start, duration_ms AS root_duration_ms
            FROM spans WHERE parent_span_id IS NULL OR parent_span_id = ''
        )
        SELECT
            s.trace_id,
            COALESCE(r.root_name, 'unknown')      AS root_name,
            COALESCE(r.root_status, 'UNSET')      AS root_status,
            COALESCE(r.root_start, MIN(s.start_time_unix_nano)) AS start_time_unix_nano,
            COALESCE(r.root_duration_ms,
                     (MAX(s.end_time_unix_nano) - MIN(s.start_time_unix_nano)) / 1e6
            )                                     AS duration_ms,
            COUNT(*)                              AS span_count,
            SUM(COALESCE(s.obs_cost_usd, 0))      AS cost_usd,
            SUM(COALESCE(s.gen_ai_usage_input_tokens, 0))  AS input_tokens,
            SUM(COALESCE(s.gen_ai_usage_output_tokens, 0)) AS output_tokens,
            -- A trace is errored if ANY span errored, not just the root: a
            -- failed tool call the agent recovered from still matters.
            MAX(CASE WHEN s.status_code = 'ERROR' THEN 1 ELSE 0 END) AS has_error,
            ANY_VALUE(s.service_name)             AS service_name
        FROM spans s
        LEFT JOIN roots r ON r.trace_id = s.trace_id
        GROUP BY s.trace_id, r.root_name, r.root_status, r.root_start, r.root_duration_ms
        ORDER BY start_time_unix_nano DESC
        LIMIT {int(limit)}
        """
        return self._rows(sql, params)

    def overview(
        self,
        project_id: str,
        hours: int = 24,
        source: str | None = None,
        credential: str | None = None,
    ) -> dict[str, Any]:
        """Headline numbers and an hourly series for the dashboard.

        Scoped to gen_ai `chat` spans — one row per prompt actually sent to a
        model. Counting traces would answer a different question, because one
        agent run can hold several prompts, and cost only ever accrues on
        these spans anyway. Keeping all three totals on the same population
        means "$0.42 across 31 prompts averaging 800ms" is arithmetic the
        reader can do in their head and have it come out right.

        The series is always exactly `hours` buckets ending at the current
        hour, zero-filled. A chart that silently omits quiet hours compresses
        its own time axis and turns a gap in traffic into a straight line
        between two busy points, which is the opposite of what it's for.
        """
        now = int(time.time())
        # Align to the top of the current hour so buckets are wall-clock hours
        # rather than offsets from whenever the page happened to load.
        latest_bucket = now - (now % 3600)
        buckets = [latest_bucket - i * 3600 for i in range(hours - 1, -1, -1)]
        empty = {
            "window_hours": hours,
            "source": source or "",
            "credential": credential or "",
            "prompts": 0,
            "duration_ms": 0.0,
            "cost_usd": 0.0,
            "series": [{"hour_start": b, "prompts": 0} for b in buckets],
            "models": [],
        }

        relation = self._spans_relation(project_id)
        if relation is None:
            return empty

        sql = f"""
        WITH spans AS ({relation}),
        -- The WAL and Parquet overlap while a compaction is in flight, so the
        -- same span can arrive twice. get_trace dedupes in Python; an
        -- aggregate has to do it in SQL or every total is quietly inflated.
        deduped AS (
            SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY span_id) AS rn
                FROM spans
            ) WHERE rn = 1
        )
        SELECT
            -- `//`, not `/`: DuckDB's `/` is float division even on BIGINTs,
            -- so `/ 3600 * 3600` hands back the original second and every
            -- bucket key misses. Integer division is what floors to the hour.
            (start_time_unix_nano // 1000000000 // 3600) * 3600
                AS hour_start,
            COUNT(*)                          AS prompts,
            SUM(COALESCE(duration_ms, 0))     AS duration_ms,
            SUM(COALESCE(obs_cost_usd, 0))    AS cost_usd
        FROM deduped
        WHERE gen_ai_operation_name = 'chat'
          AND start_time_unix_nano >= ?
          {"AND service_name = ?" if source else ""}
          {f"AND {_CREDENTIAL_EXPR} = ?" if credential else ""}
        GROUP BY hour_start
        """
        params: list[Any] = [buckets[0] * 1_000_000_000]
        if source:
            params.append(source)
        if credential:
            params.append(credential)
        rows = self._rows(sql, params)

        # Model mix over the same window, filters and population as the totals
        # above. Deliberately a second query rather than a second GROUP BY on
        # the first: hour x model would multiply the rows and force the caller
        # to re-aggregate one of the two axes back out.
        #
        # Grouped on the REQUEST model, not the response model. The response
        # model is null whenever a call failed, so failures would vanish from a
        # chart whose whole job is "what did I run"; it also returns dated ids
        # for some models and bare ones for others, which splits one model into
        # two slices that mean the same thing.
        model_sql = f"""
        WITH spans AS ({relation}),
        deduped AS (
            SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY span_id) AS rn
                FROM spans
            ) WHERE rn = 1
        )
        SELECT COALESCE(NULLIF(gen_ai_request_model, ''), 'unknown') AS model,
               COUNT(*)                       AS prompts,
               SUM(COALESCE(obs_cost_usd, 0)) AS cost_usd
        FROM deduped
        WHERE gen_ai_operation_name = 'chat'
          AND start_time_unix_nano >= ?
          {"AND service_name = ?" if source else ""}
          {f"AND {_CREDENTIAL_EXPR} = ?" if credential else ""}
        GROUP BY model
        ORDER BY prompts DESC
        """
        model_rows = self._rows(model_sql, params)

        by_hour = {int(r["hour_start"]): r for r in rows}
        return {
            "window_hours": hours,
            # Echoed back so the client can tell a filtered zero from an
            # unfiltered one without re-deriving it from its own request.
            "source": source or "",
            "credential": credential or "",
            "prompts": sum(int(r["prompts"]) for r in rows),
            "duration_ms": sum(float(r["duration_ms"] or 0) for r in rows),
            "cost_usd": sum(float(r["cost_usd"] or 0) for r in rows),
            "series": [
                {
                    "hour_start": b,
                    "prompts": int(by_hour[b]["prompts"]) if b in by_hour else 0,
                }
                for b in buckets
            ],
            "models": [
                {
                    "model": r["model"],
                    "prompts": int(r["prompts"]),
                    "cost_usd": float(r["cost_usd"] or 0),
                }
                for r in model_rows
            ],
        }

    def get_trace(self, project_id: str, trace_id: str) -> list[dict[str, Any]]:
        """All spans in one trace, ordered for a waterfall.

        Ordered by start time so the caller can render top-to-bottom without
        sorting; the parent_span_id field carries the nesting.
        """
        relation = self._spans_relation(project_id)
        if relation is None:
            return []

        sql = f"""
        WITH spans AS ({relation})
        SELECT * FROM spans
        WHERE trace_id = ?
        ORDER BY start_time_unix_nano ASC
        """
        rows = self._rows(sql, [trace_id])

        # Deduplicate. A span can appear in both the WAL and Parquet if it was
        # compacted between building the relation and running the query.
        seen: set[str] = set()
        unique = []
        for row in rows:
            if row["span_id"] in seen:
                continue
            seen.add(row["span_id"])
            for field in ("attributes_json", "events_json"):
                if row.get(field):
                    try:
                        row[field.replace("_json", "")] = json.loads(row[field])
                    except (json.JSONDecodeError, TypeError):
                        pass
            unique.append(row)
        return unique

    def _rows(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        """Run a query on its own cursor and return dict rows.

        `self.conn.cursor()` rather than `self.conn.execute()`. DuckDB's
        `execute` returns the *connection*, not a separate cursor, so the
        result set, `description` and `fetchall` are all connection state. This
        object is a module-level singleton and FastAPI runs sync endpoints on a
        threadpool, so two overlapping requests interleave on that state and
        one of them reads the other's columns — which surfaces as a KeyError
        for a column that is right there in the SQL, on a query that works
        perfectly when run on its own.

        Not hypothetical: the Overview page fires overview, sources and
        credentials together, and that is enough to hit it.

        `cursor()` returns a fresh connection over the same database, so each
        query owns its own state.
        """
        cursor = self.conn.cursor()
        cursor.execute(sql, params or [])
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
