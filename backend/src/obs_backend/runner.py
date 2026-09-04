"""Replay runner — execute a dataset against a prompt and capture the outputs.

The second half of step 3. Given a dataset, a prompt template and a model, this
sends one LLM call per test case and records what came back: output text,
tokens, cost, latency, finish reason.

Three things are worth knowing about the design.

**Runs are traced.** A replay is LLM traffic, so it goes into the same span
store as production traffic and shows up in the same waterfall — one trace per
run, a root `eval run` span with one `chat` child per item. The alternative
(a private results viewer) means building a second rendering of the same data
that immediately starts drifting from the first. Spans are written straight
through SpanWriter in process rather than exported over OTLP back to our own
ingest endpoint: that loop would work, but it would need the backend to hold
an ingest API key just to talk to itself.

**Runs cost money.** CLAUDE.md asks for a hard cap on invocations per run.
MAX_RUN_ITEMS is enforced at creation, before a single call goes out, and the
run records that it was truncated rather than silently evaluating a prefix.
Cancellation is checked between items so a run in progress can be stopped.

**Runs are background work.** A hundred sequential API calls is minutes, well
past any HTTP timeout, so POST /api/runs returns immediately with a run id and
the UI polls. Items run on a small thread pool — sequential would make a
50-item run a coffee break, and the client behind llm.py is thread-safe.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

def _actor_attribute(actor: str) -> dict[str, str]:
    """`obs.user` for a call a person started, nothing for one they did not.

    Omitted rather than written empty, so the attribute's absence means exactly
    one thing: nobody was signed in. Spans pushed by the SDK and guardrail
    checks made with an ingest key are machine traffic with no person behind
    them, and every span written before this existed is in the same position —
    an empty string would put all three in a bucket that looks like a user
    named "".

    The email, not the user id, matching `obs.credential` recording the key's
    name: a span should stay readable without a lookup into a row that may
    since have been deleted.
    """
    return {"obs.user": actor} if actor else {}


from obs_sdk.pricing import estimate_cost_usd

from obs_backend import credentials, llm, prompts, scoring
from obs_backend.credentials import Credential
from obs_backend.db import get_pool
from obs_backend.models import Span
from obs_backend.wal import SpanWriter

# The placeholder a prompt template must contain. Required rather than
# optional: a template without it sends byte-identical requests for every test
# case, which produces a run that looks successful and means nothing. Better to
# reject it before spending anything.
INPUT_PLACEHOLDER = "{{input}}"

# Hard cap on LLM calls per run (CLAUDE.md, cost controls). Applies to this
# step's replay calls; step 4's scorer invocations have their own cap in
# scoring.py, deliberately separate — one run of 80 cases scored by two judges
# is 80 replay calls and 160 judge calls, and a single shared budget would let
# either one starve the other.
MAX_RUN_ITEMS = int(os.environ.get("OBS_MAX_RUN_ITEMS", "100"))

# How many items run at once. Low by default — the ceiling here is the API
# rate limit, not local CPU, and a 429 storm is a worse failure than a slow run.
RUN_CONCURRENCY = int(os.environ.get("OBS_RUN_CONCURRENCY", "4"))

# Run ids asked to stop. In memory, so a backend restart forgets them — which
# is correct, because a restart also kills the thread doing the work. Runs left
# in 'running' by a crash are reconciled at startup (see reconcile_orphans).
_cancelled: set[str] = set()
_cancel_lock = threading.Lock()


class RunError(ValueError):
    """Bad run request — surfaced to the caller as a 400, not a 500."""


@dataclass(frozen=True)
class _Item:
    id: str
    run_item_id: str
    ordinal: int
    input: str


def _hex(n: int) -> str:
    return secrets.token_hex(n)


def _now_nano() -> int:
    return time.time_ns()


def render_prompt(template: str, item_input: str) -> str:
    return template.replace(INPUT_PLACEHOLDER, item_input)


# --------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------


def create_run(
    *,
    project_id: str,
    dataset_id: str,
    name: str,
    prompt_template: str = "",
    model: str,
    max_tokens: int,
    scorer_ids: list[str] | None = None,
    prompt_id: str | None = None,
    prompt_version_id: str | None = None,
    prompt_label: str = "",
    credential_id: str | None = None,
    actor: str = "",
) -> dict[str, Any]:
    """Validate, materialize the run and its items, and return the run row.

    Everything that can be rejected is rejected here, synchronously, before the
    background thread starts. A run that fails validation should fail in the
    response to the POST — not appear in the list a second later wearing a
    status of 'failed'.

    A run can name a saved prompt (by version, or by a label resolved now) or
    carry ad-hoc text. Either way the text it sent is copied onto the run row.
    The FK is provenance — "this was v3 of Support triage" — and the copy is
    the record; a run that could only show its prompt by dereferencing a
    mutable pointer would answer "what ran?" with today's answer instead of
    the one that is true.
    """
    version: dict[str, Any] | None = None
    if prompt_id or prompt_version_id:
        try:
            version = prompts.resolve_version(
                project_id,
                version_id=prompt_version_id,
                prompt_id=prompt_id,
                label=prompt_label or None,
            )
        except prompts.PromptError as exc:
            raise RunError(str(exc)) from exc
        if version["kind"] != "completion":
            raise RunError(
                "That prompt belongs to a scorer. Scorer prompts judge an output; "
                "they are not the prompt a replay sends."
            )
        prompt_template = version["template"]

    if INPUT_PLACEHOLDER not in prompt_template:
        raise RunError(
            f"Prompt template must contain {INPUT_PLACEHOLDER} — that is where each "
            "test case input is substituted. Without it every item would send an "
            "identical request."
        )
    if max_tokens < 1 or max_tokens > 32_000:
        raise RunError("max_tokens must be between 1 and 32000")

    with get_pool().connection() as conn:
        owner = conn.execute(
            "SELECT id FROM datasets WHERE id = %s AND project_id = %s",
            (dataset_id, project_id),
        ).fetchone()
        if owner is None:
            raise RunError("Dataset not found")

        rows = conn.execute(
            "SELECT id, input FROM dataset_items WHERE dataset_id = %s "
            "ORDER BY created_at ASC",
            (dataset_id,),
        ).fetchall()

    if not rows:
        raise RunError("Dataset has no test cases — add one before running")

    truncated = len(rows) > MAX_RUN_ITEMS
    selected = rows[:MAX_RUN_ITEMS]

    # Scorers are validated now, against the full item count, even though they
    # only fire when the run finishes. Both failure modes — an unknown scorer
    # id, or a job over the judge cap — are knowable before the first replay
    # call goes out, and discovering either one *after* paying for the replay
    # is the worst possible ordering.
    requested_scorers = scoring.resolve_scorers(project_id, scorer_ids or [])
    if requested_scorers:
        scoring.check_run_scoring_budget(len(selected), len(requested_scorers))

    # Resolved here, before anything is spent, for the same reason the scorers
    # are: "no default key" and "that key was removed" are both knowable now,
    # and finding either one out mid-run means paying for the calls that got
    # through first. The id is pinned onto the run, so a run goes on saying
    # which key paid for it even after the default moves.
    credential = credentials.resolve(project_id, credential_id)

    # The one place this check earns the most: a model that cannot run on the
    # chosen key would otherwise be discovered once per test case, each costing
    # a round trip, before the run gave up.
    #
    # Re-raised as RunError so it lands as a 400 with the rest of this
    # function's validation. The POST endpoint catches RunError; a bare
    # ValueError from llm would escape it and surface as a 500 for what is
    # plainly a bad request.
    try:
        llm.check_model_matches(credential.provider, model)
    except (llm.ModelProviderMismatch, llm.UnknownProvider) as exc:
        raise RunError(str(exc)) from exc

    run_id = _hex(16)
    trace_id = _hex(16)  # 32 hex chars, matching an OTLP trace id

    with get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO runs (id, dataset_id, project_id, name, prompt_template,
                              model, max_tokens, status, item_count, trace_id, error,
                              scorer_ids, prompt_version_id, prompt_label, credential_id,
                              created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                run_id,
                dataset_id,
                project_id,
                name.strip(),
                prompt_template,
                model,
                max_tokens,
                len(selected),
                trace_id,
                # Recorded as a visible note on the run, not a silent truncation.
                f"Dataset has {len(rows)} items; capped at {MAX_RUN_ITEMS} "
                f"(OBS_MAX_RUN_ITEMS)." if truncated else "",
                json.dumps([s.id for s in requested_scorers]),
                version["id"] if version else None,
                # The label as resolved, not as requested: recording "production"
                # on a run that fell back to the latest version would describe a
                # resolution that never happened.
                prompt_label.strip().lower() if version and prompt_label else "",
                credential.id,
                actor,
            ),
        )
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO run_items (id, run_id, dataset_item_id, ordinal, status) "
                "VALUES (%s, %s, %s, %s, 'pending')",
                [
                    (_hex(16), run_id, str(item_id), ordinal)
                    for ordinal, (item_id, _) in enumerate(selected)
                ],
            )

    return get_run(project_id, run_id) or {}


def start_run(run_id: str, writer: SpanWriter) -> None:
    """Kick the run off on a background thread."""
    thread = threading.Thread(
        target=_execute, args=(run_id, writer), daemon=True, name=f"run-{run_id[:8]}"
    )
    thread.start()


def cancel_run(project_id: str, run_id: str) -> bool:
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status FROM runs WHERE id = %s AND project_id = %s",
            (run_id, project_id),
        ).fetchone()
        if row is None:
            return False
        if row[0] in {"succeeded", "failed", "cancelled"}:
            return False
    with _cancel_lock:
        _cancelled.add(run_id)
    return True


def _is_cancelled(run_id: str) -> bool:
    with _cancel_lock:
        return run_id in _cancelled


def reconcile_orphans() -> int:
    """Fail runs left mid-flight by a crash or restart.

    Without this, a run interrupted by Ctrl-C sits at 'running' forever and the
    UI spins on a poll that will never change. Threads do not survive the
    process, so anything still 'running' at boot is definitionally dead.
    """
    with get_pool().connection() as conn:
        cur = conn.execute(
            "UPDATE runs SET status = 'failed', finished_at = now(), "
            "error = CASE WHEN error = '' THEN %s ELSE error END "
            "WHERE status IN ('pending', 'running')",
            ("Interrupted by a backend restart.",),
        )
        conn.execute(
            "UPDATE run_items SET status = 'skipped' "
            "WHERE status IN ('pending', 'running')"
        )
        return cur.rowcount


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


def _execute(run_id: str, writer: SpanWriter) -> None:
    """Run body. Owns the run's lifecycle, so it must not raise."""
    started_nano = _now_nano()
    spans: list[Span] = []

    try:
        with get_pool().connection() as conn:
            run = conn.execute(
                "SELECT project_id, dataset_id, name, prompt_template, model, "
                "max_tokens, trace_id, scorer_ids, credential_id, created_by "
                "FROM runs WHERE id = %s",
                (run_id,),
            ).fetchone()
            if run is None:
                return
            conn.execute(
                "UPDATE runs SET status = 'running', started_at = now() WHERE id = %s",
                (run_id,),
            )

            item_rows = conn.execute(
                "SELECT ri.dataset_item_id, ri.id, ri.ordinal, di.input "
                "FROM run_items ri JOIN dataset_items di ON di.id = ri.dataset_item_id "
                "WHERE ri.run_id = %s ORDER BY ri.ordinal ASC",
                (run_id,),
            ).fetchall()

        (
            project_id,
            _dataset_id,
            run_name,
            template,
            model,
            max_tokens,
            trace_id,
            scorer_ids_json,
            credential_id,
            actor,
        ) = run

        # Re-resolved on the worker thread rather than carried from create_run:
        # the secret should live as briefly as possible, and the id on the run
        # row is the durable record of which key was chosen.
        credential = credentials.resolve(project_id, credential_id)
        items = [
            _Item(id=str(r[0]), run_item_id=str(r[1]), ordinal=r[2], input=r[3])
            for r in item_rows
        ]

        root_span_id = _hex(8)

        def work(item: _Item) -> Span | None:
            if _is_cancelled(run_id):
                _mark_item(item.run_item_id, status="skipped")
                return None
            return _run_one(
                run_id=run_id,
                project_id=project_id,
                trace_id=trace_id,
                parent_span_id=root_span_id,
                item=item,
                template=template,
                model=model,
                max_tokens=max_tokens,
                credential=credential,
                actor=actor,
            )

        with ThreadPoolExecutor(max_workers=max(1, RUN_CONCURRENCY)) as pool:
            for span in pool.map(work, items):
                if span is not None:
                    spans.append(span)

        cancelled = _is_cancelled(run_id)
        ended_nano = _now_nano()

        # A run in which every item errored is a failed run, not a successful
        # one that happens to contain failures. Reporting 'succeeded' for zero
        # usable outputs is the kind of green check that stops meaning
        # anything. Partial failures stay 'succeeded' and carry error_count.
        failed_items = sum(1 for s in spans if s.status_code == "ERROR")
        all_failed = bool(items) and failed_items == len(items)

        # The root span goes in last because its duration is only known now.
        spans.append(
            Span(
                trace_id=trace_id,
                span_id=root_span_id,
                parent_span_id=None,
                name=f"eval run {run_name or model}",
                start_time_unix_nano=started_nano,
                end_time_unix_nano=ended_nano,
                status_code="ERROR" if (cancelled or all_failed) else "OK",
                status_message=(
                    "Cancelled"
                    if cancelled
                    else f"All {failed_items} items failed"
                    if all_failed
                    else ""
                ),
                project_id=project_id,
                service_name="obs-runner",
                gen_ai_operation_name="invoke_agent",
                gen_ai_agent_name="eval-runner",
                obs_latency_seconds=(ended_nano - started_nano) / 1e9,
                attributes_json=json.dumps(
                    {"obs.run_id": run_id, "obs.run_item_count": len(items)}
                ),
            )
        )
        writer.append(spans)

        if cancelled:
            _finalize(run_id, status="cancelled")
        elif all_failed:
            _finalize(
                run_id,
                status="failed",
                error=f"All {failed_items} items failed. See per-item errors below.",
            )
        else:
            _finalize(run_id, status="succeeded")
            _autoscore(
                run_id,
                project_id,
                json.loads(scorer_ids_json or "[]"),
                writer,
                credential,
            )

    except Exception as exc:  # noqa: BLE001 — a run must never kill the thread silently
        traceback.print_exc()
        if spans:
            # Partial results are still worth keeping; an exception here is
            # usually one bad item's blast radius, not the whole run's.
            try:
                writer.append(spans)
            except Exception:
                pass
        _finalize(run_id, status="failed", error=str(exc))
    finally:
        with _cancel_lock:
            _cancelled.discard(run_id)


def _run_one(
    *,
    run_id: str,
    project_id: str,
    trace_id: str,
    parent_span_id: str,
    item: _Item,
    template: str,
    model: str,
    max_tokens: int,
    credential: Credential,
    actor: str = "",
) -> Span:
    """One test case: render, call, record. Returns the span for the call."""
    prompt = render_prompt(template, item.input)
    span_id = _hex(8)

    # Marked running before the clock starts, so the recorded latency is the
    # model's and not the model's plus a database round trip.
    _mark_item(item.run_item_id, status="running", span_id=span_id)
    start_nano = _now_nano()

    try:
        call = llm.complete(
            provider=credential.provider,
            model=model,
            prompt=prompt,
            max_tokens=max_tokens,
            api_key=credential.secret,
        )
    except Exception as exc:
        end_nano = _now_nano()
        message = f"{type(exc).__name__}: {exc}"
        _mark_item(
            item.run_item_id,
            status="failed",
            error=message,
            latency_ms=(end_nano - start_nano) / 1e6,
        )
        _bump_run(run_id, errored=True)
        return Span(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            name=f"chat {model}",
            start_time_unix_nano=start_nano,
            end_time_unix_nano=end_nano,
            status_code="ERROR",
            status_message=message,
            project_id=project_id,
            service_name="obs-runner",
            gen_ai_operation_name="chat",
            gen_ai_provider_name=llm.provider_label(credential.provider),
            gen_ai_request_model=model,
            gen_ai_request_max_tokens=max_tokens,
            gen_ai_input_messages=prompt,
            obs_latency_seconds=(end_nano - start_nano) / 1e9,
            attributes_json=json.dumps(
                {
                "obs.run_id": run_id,
                "obs.dataset_item_id": item.id,
                "obs.credential": credential.name,
                **_actor_attribute(actor),
            }
            ),
        )

    output = call.text
    latency_ms = call.latency_ms

    # Cost is priced on the day the call happened, not today. It is the same
    # instant here, but passing it explicitly keeps this correct if a run is
    # ever re-costed and matters for models on time-boxed intro pricing.
    cost = estimate_cost_usd(
        credential.provider,
        call.response_model,
        call.input_tokens,
        call.output_tokens,
        cached_input_tokens=call.cached_input_tokens,
        cache_write_tokens=call.cache_write_tokens,
        request_model=model,
        on=datetime.now(tz=timezone.utc).date(),
    )
    finish_reason = call.stop_reason

    _mark_item(
        item.run_item_id,
        status="succeeded",
        output=output,
        input_tokens=call.input_tokens,
        output_tokens=call.output_tokens,
        cost_usd=cost,
        latency_ms=latency_ms,
        finish_reason=finish_reason,
    )
    _bump_run(run_id, cost=cost or 0.0)

    return Span(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        name=f"chat {model}",
        start_time_unix_nano=call.start_nano,
        end_time_unix_nano=call.end_nano,
        status_code="OK",
        project_id=project_id,
        service_name="obs-runner",
        gen_ai_operation_name="chat",
        gen_ai_provider_name=llm.provider_label(credential.provider),
        gen_ai_request_model=model,
        gen_ai_response_model=call.response_model,
        gen_ai_response_id=call.response_id,
        gen_ai_request_max_tokens=max_tokens,
        gen_ai_usage_input_tokens=call.input_tokens,
        gen_ai_usage_output_tokens=call.output_tokens,
        gen_ai_finish_reasons=json.dumps([finish_reason]),
        gen_ai_input_messages=prompt,
        gen_ai_output_messages=output,
        obs_cost_usd=cost,
        obs_latency_seconds=latency_ms / 1000,
        attributes_json=json.dumps(
            {
                # Cached / reasoning counts, present only when non-zero.
                **llm.usage_attributes(call),
                "obs.run_id": run_id,
                "obs.dataset_item_id": item.id,
                "obs.credential": credential.name,
                **_actor_attribute(actor),
            }
        ),
    )


def _autoscore(
    run_id: str,
    project_id: str,
    scorer_ids: list[str],
    writer: SpanWriter,
    credential: Credential,
) -> None:
    """Kick off scoring for a finished run, if scorers were attached to it.

    Deliberately after _finalize, and deliberately swallowing its own errors.
    The replay succeeded and its outputs are worth keeping; a judge that cannot
    run — a scorer archived mid-run, the API rejecting the judge model — is a
    scoring problem, and marking the whole run failed over it would throw away
    results that are perfectly good. It shows up as a run with no scores, which
    is what actually happened.
    """
    if not scorer_ids:
        return
    try:
        scoring.score_run(project_id, run_id, scorer_ids, writer, credential)
    except Exception as exc:  # noqa: BLE001
        print(f"[runner] auto-scoring for run {run_id} did not start: {exc}")


def _mark_item(run_item_id: str, **fields: Any) -> None:
    """Patch a run_item. Called from pool threads, so it takes its own connection."""
    if not fields:
        return
    assignments = ", ".join(f"{k} = %s" for k in fields)
    with get_pool().connection() as conn:
        conn.execute(
            f"UPDATE run_items SET {assignments} WHERE id = %s",
            (*fields.values(), run_item_id),
        )


def _bump_run(run_id: str, *, cost: float = 0.0, errored: bool = False) -> None:
    """Increment run progress counters.

    Done as a SQL increment rather than read-modify-write: several pool threads
    finish concurrently, and the read-then-write version loses updates, which
    shows up as a progress bar that never quite reaches the item count.
    """
    with get_pool().connection() as conn:
        conn.execute(
            "UPDATE runs SET completed_count = completed_count + 1, "
            "error_count = error_count + %s, cost_usd = cost_usd + %s WHERE id = %s",
            (1 if errored else 0, cost, run_id),
        )


def _finalize(run_id: str, *, status: str, error: str = "") -> None:
    with get_pool().connection() as conn:
        conn.execute(
            "UPDATE runs SET status = %s, finished_at = now(), "
            "error = CASE WHEN %s = '' THEN error ELSE %s END WHERE id = %s",
            (status, error, error, run_id),
        )


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

_RUN_COLUMNS = (
    "r.id, r.dataset_id, r.name, r.prompt_template, r.model, r.max_tokens, "
    "r.status, r.error, r.item_count, r.completed_count, r.error_count, "
    "r.cost_usd, r.trace_id, r.created_at, r.started_at, r.finished_at, "
    "r.scorer_ids, r.prompt_version_id, r.prompt_label, pv.version, p.id, p.name"
)

# LEFT JOINs on purpose. An ad-hoc run has no prompt version, and a run whose
# prompt was deleted has a null FK — an inner join would make either one vanish
# from the dataset's run history, which is exactly the record that should be
# hardest to lose.
_RUN_SOURCE = (
    "FROM runs r "
    "LEFT JOIN prompt_versions pv ON pv.id = r.prompt_version_id "
    "LEFT JOIN prompts p ON p.id = pv.prompt_id"
)


def _run_dict(r: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": str(r[0]),
        "dataset_id": str(r[1]),
        "name": r[2],
        "prompt_template": r[3],
        "model": r[4],
        "max_tokens": r[5],
        "status": r[6],
        "error": r[7],
        "item_count": r[8],
        "completed_count": r[9],
        "error_count": r[10],
        "cost_usd": float(r[11] or 0),
        "trace_id": r[12],
        "created_at": r[13].isoformat() if r[13] else None,
        "started_at": r[14].isoformat() if r[14] else None,
        "finished_at": r[15].isoformat() if r[15] else None,
        "scorer_ids": json.loads(r[16] or "[]"),
        "prompt_version_id": str(r[17]) if r[17] else None,
        "prompt_label": r[18],
        "prompt_version": r[19],
        "prompt_id": str(r[20]) if r[20] else None,
        "prompt_name": r[21],
    }


def list_runs(project_id: str, dataset_id: str | None = None, limit: int = 50):
    sql = f"SELECT {_RUN_COLUMNS} {_RUN_SOURCE} WHERE r.project_id = %s"
    params: list[Any] = [project_id]
    if dataset_id:
        sql += " AND r.dataset_id = %s"
        params.append(dataset_id)
    sql += " ORDER BY r.created_at DESC LIMIT %s"
    params.append(limit)

    with get_pool().connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_run_dict(r) for r in rows]


def get_run(project_id: str, run_id: str) -> dict[str, Any] | None:
    with get_pool().connection() as conn:
        row = conn.execute(
            f"SELECT {_RUN_COLUMNS} {_RUN_SOURCE} WHERE r.id = %s AND r.project_id = %s",
            (run_id, project_id),
        ).fetchone()
    return _run_dict(row) if row else None


def get_run_items(run_id: str) -> list[dict[str, Any]]:
    """Run results joined to the test case they came from.

    The join is what makes the results page readable — an output next to the
    input that produced it and the expected answer, rather than three lists the
    reader has to line up themselves.
    """
    with get_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT ri.id, ri.ordinal, ri.status, ri.output, ri.error,
                   ri.input_tokens, ri.output_tokens, ri.cost_usd, ri.latency_ms,
                   ri.finish_reason, ri.span_id,
                   di.id, di.input, di.expected_output, di.source_trace_id
            FROM run_items ri
            JOIN dataset_items di ON di.id = ri.dataset_item_id
            WHERE ri.run_id = %s
            ORDER BY ri.ordinal ASC
            """,
            (run_id,),
        ).fetchall()

    return [
        {
            "id": str(r[0]),
            "ordinal": r[1],
            "status": r[2],
            "output": r[3],
            "error": r[4],
            "input_tokens": r[5],
            "output_tokens": r[6],
            "cost_usd": float(r[7]) if r[7] is not None else None,
            "latency_ms": float(r[8]) if r[8] is not None else None,
            "finish_reason": r[9],
            "span_id": r[10],
            "dataset_item_id": str(r[11]),
            "input": r[12],
            "expected_output": r[13],
            "source_trace_id": r[14],
        }
        for r in rows
    ]
