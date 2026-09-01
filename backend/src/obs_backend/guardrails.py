"""Real-time guardrails — step 6.

A guardrail is a scorer plus a policy. The scorer (step 4) already knows how to
judge text and return a typed verdict; the guardrail is the part that says what
that verdict *means* — whether it blocks, what counts as a trigger, and what to
do when the judge itself fails. The endpoint screens an output and answers
pass/block before it reaches a user.

That inline position is the whole design constraint, and everything below
follows from it.

**A guardrail triggers when its scorer fails.** One rule across all three
output types, so a safety scorer reads the way you'd write it: `passed = true`
means safe. Boolean scorers say that directly; numeric ones need a
`pass_threshold` and are refused as guardrails without one, because a scale with
no failing end cannot decide anything. Categorical scorers deliberately have no
pass/fail (steps_for_user 3.7), so a categorical guardrail names its blocking
labels — this is the one place that judgement has to be made, and making it
here keeps it out of the scorer, where it would be a field most scorers ignore.

**Judges run concurrently and under a timeout.** A check's latency is its
slowest judge, not the sum of all of them, and no judge gets to hang the
caller's request. Scoring jobs need neither — they run on a background thread
where slow costs nothing but time.

**Failure is a policy, not an exception.** A judge that errors or times out
leaves the guardrail with no verdict. `on_error` decides what that means, and
it defaults to allow: fail-closed converts a provider blip into a total
outage of the calling application, which is the larger failure. Every response
carries `degraded`, so a caller who wants the opposite policy gets it in one
line without this default changing.

**`flag` is shadow mode.** A guardrail set to flag runs its judge, records the
result and reports it, and never blocks. A new guardrail is one bad prompt away
from rejecting every response in production, so the useful default workflow is
to watch it against real traffic first and promote it to `block` once the log
says it fires on the right things.

**Spend is capped per minute, not per job.** Replay and scoring caps bound one
job (steps_for_user 2.10); a guardrail is called once per application response
forever, so the thing worth bounding is rate. The cap counts judge calls rather
than checks, since that is what is billable.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from obs_backend import credentials
from obs_backend.db import get_pool
from obs_backend.models import Span
from obs_backend.scoring import (
    Scorer,
    ScorerError,
    get_scorer,
    judge,
    judge_credentials,
    judge_span,
)
from obs_backend.wal import SpanWriter

# What a triggered guardrail does. `flag` records and reports without blocking.
ACTIONS = ("block", "flag")

# What a judge that errored or timed out means for this guardrail.
ON_ERROR = ("allow", "block")

# Seconds a single judge call gets before it is abandoned. The caller is
# holding an HTTP request open behind this, so the ceiling is a person's
# patience rather than the model's.
GUARDRAIL_TIMEOUT_SECONDS = float(os.environ.get("OBS_GUARDRAIL_TIMEOUT_SECONDS", "10"))

# All of a check's guardrails run at once — latency is the slowest judge, not
# the sum. Bounded anyway so a project with twenty guardrails doesn't open
# twenty concurrent connections per request.
GUARDRAIL_CONCURRENCY = int(os.environ.get("OBS_GUARDRAIL_CONCURRENCY", "8"))

# CLAUDE.md cost controls, adapted to an endpoint that runs forever rather than
# a job that ends. Counted in judge calls, because that is the billable unit.
MAX_GUARDRAIL_CALLS_PER_MIN = int(
    os.environ.get("OBS_MAX_GUARDRAIL_CALLS_PER_MIN", "120")
)


class GuardrailError(ValueError):
    """Bad guardrail definition or request — surfaced as a 400, not a 500."""


class GuardrailRateLimited(GuardrailError):
    """Over the per-minute judge-call cap — a 429, distinct from a bad request."""


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------
#
# In-process and per-minute, deliberately. The alternative is a Postgres table
# read and written on every screened response, which puts a database round trip
# in the hot path to protect against something a single-process prototype can
# already see perfectly well. It resets on restart; so does the spend risk it
# exists to bound, which is a runaway caller in one session.

_call_times: deque[float] = deque()
_call_lock = threading.Lock()


def _reserve_calls(count: int) -> None:
    """Claim `count` judge calls against the per-minute cap, or refuse.

    Reserved before the calls are made rather than counted after: a check that
    only learns it was over budget once the money is spent has enforced
    nothing.
    """
    now = time.monotonic()
    with _call_lock:
        while _call_times and now - _call_times[0] > 60:
            _call_times.popleft()
        if len(_call_times) + count > MAX_GUARDRAIL_CALLS_PER_MIN:
            raise GuardrailRateLimited(
                f"{len(_call_times)} judge calls already made in the last minute; "
                f"this check needs {count} more and the cap is "
                f"{MAX_GUARDRAIL_CALLS_PER_MIN} (OBS_MAX_GUARDRAIL_CALLS_PER_MIN). "
                "Refusing rather than screening part of the output — a check that "
                "silently skipped half its guardrails would return `pass` while "
                "meaning `unknown`."
            )
        _call_times.extend([now] * count)


# --------------------------------------------------------------------------
# Definition
# --------------------------------------------------------------------------


def _hex(n: int) -> str:
    return secrets.token_hex(n)


def _validate(
    scorer: Scorer, *, name: str, action: str, on_error: str, block_labels: list[str]
) -> list[str]:
    """Reject a guardrail that could never reach a decision.

    All of this is checkable for free at definition time. A guardrail that only
    reveals it cannot decide anything when a real response is waiting on it is
    the worst possible moment to find out.
    """
    if not name.strip():
        raise GuardrailError("Guardrail name cannot be empty")
    if action not in ACTIONS:
        raise GuardrailError(f"action must be one of {', '.join(ACTIONS)}")
    if on_error not in ON_ERROR:
        raise GuardrailError(f"on_error must be one of {', '.join(ON_ERROR)}")

    if scorer.output_type == "numeric" and scorer.pass_threshold is None:
        raise GuardrailError(
            f"{scorer.name!r} scores {scorer.score_min}–{scorer.score_max} with no "
            "pass threshold, so nothing about its answer says 'block'. Set a "
            "pass threshold on the scorer first — a guardrail has to reach a "
            "decision, and a scale alone is not one."
        )

    if scorer.output_type == "categorical":
        cleaned = [c.strip() for c in block_labels if c.strip()]
        unknown = [c for c in cleaned if c not in scorer.categories]
        if unknown:
            raise GuardrailError(
                f"{', '.join(unknown)} is not a category {scorer.name!r} can return. "
                f"It answers with one of: {', '.join(scorer.categories)}."
            )
        if not cleaned:
            raise GuardrailError(
                f"{scorer.name!r} answers with a category, not a verdict, so this "
                "guardrail needs to say which of "
                f"({', '.join(scorer.categories)}) should trigger it."
            )
        if len(cleaned) == len(scorer.categories):
            raise GuardrailError(
                "Every category is marked as blocking, so this guardrail would "
                "block everything it ever saw. Leave at least one category out."
            )
        return cleaned

    # Boolean and numeric scorers answer pass/fail themselves; a blocking-label
    # list would be a field the evaluator never reads.
    return []


def _resolve_scorer(project_id: str, scorer_id: str) -> Scorer:
    scorer = get_scorer(project_id, scorer_id)
    if scorer is None:
        raise GuardrailError("Scorer not found — it may have been archived")
    return scorer


def create_guardrail(
    project_id: str,
    *,
    name: str,
    scorer_id: str,
    description: str = "",
    action: str = "block",
    block_labels: list[str] | None = None,
    on_error: str = "allow",
    enabled: bool = True,
) -> str:
    scorer = _resolve_scorer(project_id, scorer_id)
    labels = _validate(
        scorer,
        name=name,
        action=action,
        on_error=on_error,
        block_labels=block_labels or [],
    )

    guardrail_id = str(uuid.uuid4())
    with get_pool().connection() as conn:
        clash = conn.execute(
            "SELECT id FROM guardrails WHERE project_id = %s AND name = %s "
            "AND archived_at IS NULL",
            (project_id, name.strip()),
        ).fetchone()
        if clash:
            raise GuardrailError(f"A guardrail named {name.strip()!r} already exists")

        conn.execute(
            "INSERT INTO guardrails (id, project_id, name, description, scorer_id, "
            "action, block_labels, on_error, enabled) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                guardrail_id,
                project_id,
                name.strip(),
                description.strip(),
                scorer_id,
                action,
                json.dumps(labels),
                on_error,
                enabled,
            ),
        )
    return guardrail_id


def update_guardrail(project_id: str, guardrail_id: str, **fields: Any) -> bool:
    """Edit a guardrail's policy.

    Not versioned, unlike a scorer or a prompt. The reason a scorer needs
    history is that its past scores are numbers whose meaning lives in the
    definition that produced them; a guardrail's past checks already record the
    action and the verdict inline, so they keep explaining themselves without a
    version chain behind them.
    """
    current = get_guardrail(project_id, guardrail_id)
    if current is None:
        return False

    merged = {
        "name": fields.get("name", current["name"]),
        "description": fields.get("description", current["description"]),
        "scorer_id": fields.get("scorer_id", current["scorer_id"]),
        "action": fields.get("action", current["action"]),
        "block_labels": fields.get("block_labels", current["block_labels"]),
        "on_error": fields.get("on_error", current["on_error"]),
        "enabled": fields.get("enabled", current["enabled"]),
    }

    scorer = _resolve_scorer(project_id, str(merged["scorer_id"]))
    labels = _validate(
        scorer,
        name=str(merged["name"]),
        action=str(merged["action"]),
        on_error=str(merged["on_error"]),
        block_labels=list(merged["block_labels"] or []),
    )

    with get_pool().connection() as conn:
        clash = conn.execute(
            "SELECT id FROM guardrails WHERE project_id = %s AND name = %s "
            "AND archived_at IS NULL AND id <> %s",
            (project_id, str(merged["name"]).strip(), guardrail_id),
        ).fetchone()
        if clash:
            raise GuardrailError(
                f"A guardrail named {str(merged['name']).strip()!r} already exists"
            )

        cur = conn.execute(
            "UPDATE guardrails SET name = %s, description = %s, scorer_id = %s, "
            "action = %s, block_labels = %s, on_error = %s, enabled = %s, "
            "updated_at = now() "
            "WHERE id = %s AND project_id = %s AND archived_at IS NULL",
            (
                str(merged["name"]).strip(),
                str(merged["description"]).strip(),
                merged["scorer_id"],
                merged["action"],
                json.dumps(labels),
                merged["on_error"],
                bool(merged["enabled"]),
                guardrail_id,
                project_id,
            ),
        )
        return cur.rowcount > 0


def archive_guardrail(project_id: str, guardrail_id: str) -> bool:
    """Soft delete, so the checks it decided stay readable and attributable."""
    with get_pool().connection() as conn:
        cur = conn.execute(
            "UPDATE guardrails SET archived_at = now() "
            "WHERE id = %s AND project_id = %s AND archived_at IS NULL",
            (guardrail_id, project_id),
        )
        return cur.rowcount > 0


_GUARDRAIL_COLUMNS = (
    "g.id, g.name, g.description, g.scorer_id, g.action, g.block_labels, "
    "g.on_error, g.enabled, g.created_at, g.credential_id, "
    "s.name, s.output_type, s.score_min, s.score_max, s.pass_threshold, "
    "s.categories, s.model"
)


def _guardrail_dict(r: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": str(r[0]),
        "name": r[1],
        "description": r[2],
        "scorer_id": str(r[3]),
        "action": r[4],
        "block_labels": json.loads(r[5] or "[]"),
        "on_error": r[6],
        "enabled": r[7],
        "created_at": r[8].isoformat() if r[8] else None,
        # Null means "whatever the project default is when this fires", which
        # is the right behaviour for a rule configured once and left running.
        "credential_id": str(r[9]) if r[9] else None,
        "scorer_name": r[10],
        "output_type": r[11],
        "score_min": float(r[12]) if r[12] is not None else None,
        "score_max": float(r[13]) if r[13] is not None else None,
        "pass_threshold": float(r[14]) if r[14] is not None else None,
        "categories": json.loads(r[15] or "[]"),
        "model": r[16],
    }


def get_guardrail(project_id: str, guardrail_id: str) -> dict[str, Any] | None:
    with get_pool().connection() as conn:
        row = conn.execute(
            f"SELECT {_GUARDRAIL_COLUMNS} FROM guardrails g "
            "JOIN scorers s ON s.id = g.scorer_id "
            "WHERE g.id = %s AND g.project_id = %s AND g.archived_at IS NULL",
            (guardrail_id, project_id),
        ).fetchone()
    return _guardrail_dict(row) if row else None


def list_guardrails(project_id: str) -> list[dict[str, Any]]:
    """Active guardrails, each with how often it has fired.

    The trigger count is the number that tells you whether a guardrail is
    earning its place: one that has never fired in a thousand checks is either
    unnecessary or broken, and both are worth knowing before it is promoted out
    of shadow mode.
    """
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"""
            SELECT {_GUARDRAIL_COLUMNS},
                   (SELECT COUNT(*) FROM guardrail_results gr
                     WHERE gr.guardrail_id = g.id),
                   (SELECT COUNT(*) FROM guardrail_results gr
                     WHERE gr.guardrail_id = g.id AND gr.triggered)
            FROM guardrails g
            JOIN scorers s ON s.id = g.scorer_id
            WHERE g.project_id = %s AND g.archived_at IS NULL
            ORDER BY g.created_at DESC
            """,
            (project_id,),
        ).fetchall()

    return [
        {**_guardrail_dict(r), "check_count": r[17], "trigger_count": r[18]}
        for r in rows
    ]


def _load_for_check(project_id: str, guardrail_ids: list[str]) -> list[dict[str, Any]]:
    """The guardrails a check will run: enabled ones, or a named subset.

    A named id that is disabled still runs. Asking for a guardrail by name is
    an explicit act, and silently skipping it would return `pass` for a check
    that never happened.

    Deliberately not list_guardrails: the trigger counts it computes scan a
    table that grows by a row per guardrail per check forever, and this runs on
    every screened response.
    """
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"SELECT {_GUARDRAIL_COLUMNS} FROM guardrails g "
            "JOIN scorers s ON s.id = g.scorer_id "
            "WHERE g.project_id = %s AND g.archived_at IS NULL "
            "AND s.archived_at IS NULL",
            (project_id,),
        ).fetchall()
    active = [_guardrail_dict(r) for r in rows]
    if not guardrail_ids:
        return [g for g in active if g["enabled"]]

    by_id = {g["id"]: g for g in active}
    chosen: list[dict[str, Any]] = []
    for gid in guardrail_ids:
        if gid not in by_id:
            raise GuardrailError(f"Guardrail {gid} not found")
        chosen.append(by_id[gid])
    return chosen


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


def _triggered(guardrail: dict[str, Any], *, passed: bool | None, label: str) -> bool:
    """Did this rule fire? One rule: a guardrail triggers when its scorer fails.

    Categorical scorers are the exception that proves it — they have no
    pass/fail at all (3.7), so the guardrail's own blocking labels stand in for
    the verdict the scorer declines to give.
    """
    if guardrail["output_type"] == "categorical":
        return label in guardrail["block_labels"]
    return passed is False


def evaluate(
    project_id: str,
    *,
    output: str,
    input_text: str = "",
    source: str = "",
    guardrail_ids: list[str] | None = None,
    writer: SpanWriter,
) -> dict[str, Any]:
    """Screen one output. Synchronous — the caller is waiting on the answer.

    Returns the decision plus every guardrail's reasoning. The reasoning is
    returned rather than only logged because a blocked response usually needs
    to become a message to a user, and "blocked" with no explanation is not
    something an application can act on.
    """
    if not output.strip():
        raise GuardrailError("Nothing to screen — `output` is empty")

    guardrails = _load_for_check(project_id, guardrail_ids or [])
    check_id = str(uuid.uuid4())
    trace_id = _hex(16)
    root_span_id = _hex(8)
    started = time.time_ns()

    # No guardrails configured is a pass, not an error. An application that
    # calls the endpoint before any are defined should keep serving rather than
    # fail on a screening step that has nothing to screen with.
    if guardrails:
        _reserve_calls(len(guardrails))

    spans: list[Span] = []
    spans_lock = threading.Lock()

    def run(guardrail: dict[str, Any]) -> dict[str, Any]:
        scorer = get_scorer(project_id, guardrail["scorer_id"])
        span_id = _hex(8)
        meta: dict[str, Any] = {"start_nano": time.time_ns()}

        if scorer is None:
            # Archived between the list read and here. Treated as a judge
            # failure so on_error decides, rather than quietly not screening.
            return _error_result(guardrail, span_id, "Scorer is no longer available")

        # Seeded before the try because the resolve below is inside it: a
        # missing or undecryptable key leaves `credential` unbound, and the
        # except path still has to build a span. provider_label("") is "",
        # which is the case that function's never-raises contract exists for.
        provider = ""

        try:
            # Resolved per guardrail, inside the try: a missing or undecryptable
            # key is a judge failure like any other, so on_error decides what it
            # means rather than it taking down the whole check.
            credential = credentials.resolve(project_id, guardrail.get("credential_id"))
            # A guardrail's configured key is a preference, same as everywhere
            # else: its scorer's own model decides which vendor has to be
            # called, so a Claude safety scorer still works on a guardrail
            # pinned to an xAI key.
            credential = judge_credentials(project_id, [scorer], credential)[scorer.id]
            provider = credential.provider
            result, meta = judge(
                scorer,
                input_text=input_text,
                output_text=output,
                expected=None,
                provider=provider,
                api_key=credential.secret,
                timeout=GUARDRAIL_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            meta.setdefault("end_nano", time.time_ns())
            message = (
                str(exc) if isinstance(exc, ScorerError) else f"{type(exc).__name__}: {exc}"
            )
            with spans_lock:
                spans.append(
                    judge_span(
                        scorer=scorer,
                        provider=provider,
                        project_id=project_id,
                        trace_id=trace_id,
                        parent_span_id=root_span_id,
                        span_id=span_id,
                        meta=meta,
                        result=None,
                        error=message,
                        service_name="obs-guardrail",
                        extra={
                            "obs.guardrail_id": guardrail["id"],
                            "obs.guardrail_name": guardrail["name"],
                            "obs.guardrail_action": guardrail["action"],
                            "obs.check_id": check_id,
                        },
                    )
                )
            return _error_result(guardrail, span_id, message, meta=meta)

        fired = _triggered(guardrail, passed=result.passed, label=result.label)
        with spans_lock:
            spans.append(
                judge_span(
                    scorer=scorer,
                    provider=provider,
                    project_id=project_id,
                    trace_id=trace_id,
                    parent_span_id=root_span_id,
                    span_id=span_id,
                    meta=meta,
                    result=result,
                    service_name="obs-guardrail",
                    extra={
                        "obs.guardrail_id": guardrail["id"],
                        "obs.guardrail_name": guardrail["name"],
                        "obs.guardrail_action": guardrail["action"],
                        "obs.guardrail_triggered": fired,
                        "obs.check_id": check_id,
                    },
                )
            )

        return {
            "id": str(uuid.uuid4()),
            "guardrail_id": guardrail["id"],
            "guardrail_name": guardrail["name"],
            "scorer_id": scorer.id,
            "scorer_name": scorer.name,
            "output_type": guardrail["output_type"],
            "prompt_version_id": scorer.version_id,
            "action": guardrail["action"],
            "triggered": fired,
            "status": "succeeded",
            "value": result.value,
            "label": result.label,
            "passed": result.passed,
            "reasoning": result.reasoning,
            "error": "",
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cost_usd": result.cost_usd,
            "latency_ms": result.latency_ms,
            "span_id": span_id,
        }

    if guardrails:
        with ThreadPoolExecutor(
            max_workers=max(1, min(GUARDRAIL_CONCURRENCY, len(guardrails)))
        ) as pool:
            results = list(pool.map(run, guardrails))
    else:
        results = []

    ended = time.time_ns()
    latency_ms = (ended - started) / 1e6
    cost = sum(r["cost_usd"] or 0 for r in results)
    degraded = any(r["status"] == "failed" for r in results)
    blocked = any(r["triggered"] and r["action"] == "block" for r in results)
    decision = "block" if blocked else "pass"

    spans.append(
        Span(
            trace_id=trace_id,
            span_id=root_span_id,
            parent_span_id=None,
            name=f"guardrail {decision}",
            start_time_unix_nano=started,
            end_time_unix_nano=ended,
            # Not ERROR on a block. Blocking is this span's job working, and a
            # trace list that paints every enforcement red would make the error
            # column mean "a guardrail did something" instead of "something
            # broke". A judge that failed is the real error, and it is on the
            # child span where it happened.
            status_code="OK",
            status_message="",
            project_id=project_id,
            service_name="obs-guardrail",
            gen_ai_operation_name="invoke_agent",
            gen_ai_agent_name="guardrail",
            obs_cost_usd=cost or None,
            obs_latency_seconds=(ended - started) / 1e9,
            attributes_json=json.dumps(
                {
                    "obs.check_id": check_id,
                    "obs.guardrail_decision": decision,
                    "obs.guardrail_count": len(results),
                    "obs.guardrail_triggered_count": sum(
                        1 for r in results if r["triggered"]
                    ),
                    "obs.guardrail_degraded": degraded,
                    "obs.guardrail_source": source,
                }
            ),
        )
    )
    writer.append(spans)

    _persist(
        check_id=check_id,
        project_id=project_id,
        input_text=input_text,
        output=output,
        source=source,
        decision=decision,
        degraded=degraded,
        latency_ms=latency_ms,
        cost=cost,
        trace_id=trace_id,
        results=results,
    )

    return {
        "check_id": check_id,
        "decision": decision,
        "blocked": blocked,
        # Split by what they do, so a caller reads one field and knows whether
        # to act. A flagged guardrail firing is information; a blocking one
        # firing is an instruction.
        "triggered": [
            r["guardrail_name"] for r in results if r["triggered"] and r["action"] == "block"
        ],
        "flagged": [
            r["guardrail_name"] for r in results if r["triggered"] and r["action"] == "flag"
        ],
        "degraded": degraded,
        "latency_ms": latency_ms,
        "cost_usd": cost,
        "trace_id": trace_id,
        "results": results,
    }


def _error_result(
    guardrail: dict[str, Any],
    span_id: str,
    message: str,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A guardrail whose judge never answered, resolved by its on_error policy."""
    meta = meta or {}
    start = meta.get("start_nano")
    end = meta.get("end_nano")
    return {
        "id": str(uuid.uuid4()),
        "guardrail_id": guardrail["id"],
        "guardrail_name": guardrail["name"],
        "scorer_id": guardrail["scorer_id"],
        "scorer_name": guardrail["scorer_name"],
        "output_type": guardrail["output_type"],
        "prompt_version_id": None,
        "action": guardrail["action"],
        "triggered": guardrail["on_error"] == "block",
        "status": "failed",
        "value": None,
        "label": "",
        "passed": None,
        "reasoning": "",
        "error": message,
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
        "latency_ms": (end - start) / 1e6 if start and end else None,
        "span_id": span_id,
    }


def _persist(
    *,
    check_id: str,
    project_id: str,
    input_text: str,
    output: str,
    source: str,
    decision: str,
    degraded: bool,
    latency_ms: float,
    cost: float,
    trace_id: str,
    results: list[dict[str, Any]],
) -> None:
    """Write the check and its results, after the decision is reached.

    Synchronous, and inside the request. A judge call is hundreds of
    milliseconds and these two inserts are a fraction of one, so moving them
    off the hot path would buy noise at the cost of a log that can silently
    lose the check that mattered.
    """
    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO guardrail_checks (id, project_id, input, output, source, "
            "decision, degraded, latency_ms, cost_usd, trace_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                check_id,
                project_id,
                input_text,
                output,
                source.strip()[:120],
                decision,
                degraded,
                latency_ms,
                cost or None,
                trace_id,
            ),
        )
        if results:
            cur.executemany(
                "INSERT INTO guardrail_results (id, check_id, guardrail_id, "
                "guardrail_name, scorer_id, prompt_version_id, action, triggered, "
                "status, value, label, passed, reasoning, error, input_tokens, "
                "output_tokens, cost_usd, latency_ms, span_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "%s, %s, %s, %s, %s)",
                [
                    (
                        r["id"],
                        check_id,
                        r["guardrail_id"],
                        r["guardrail_name"],
                        r["scorer_id"],
                        r["prompt_version_id"],
                        r["action"],
                        r["triggered"],
                        r["status"],
                        r["value"],
                        r["label"],
                        r["passed"],
                        r["reasoning"],
                        r["error"],
                        r["input_tokens"],
                        r["output_tokens"],
                        r["cost_usd"],
                        r["latency_ms"],
                        r["span_id"],
                    )
                    for r in results
                ],
            )


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def list_checks(
    project_id: str, *, limit: int = 50, decision: str | None = None
) -> list[dict[str, Any]]:
    """Recent checks with their per-guardrail results.

    Two queries rather than a join: a check has several results, and flattening
    them would return the check's text once per guardrail — which for a long
    completion screened by four guardrails is the whole payload four times.
    """
    sql = (
        "SELECT id, input, output, source, decision, degraded, latency_ms, "
        "cost_usd, trace_id, created_at FROM guardrail_checks "
        "WHERE project_id = %s"
    )
    params: list[Any] = [project_id]
    if decision in ("pass", "block"):
        sql += " AND decision = %s"
        params.append(decision)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(min(limit, 200))

    with get_pool().connection() as conn:
        rows = conn.execute(sql, params).fetchall()
        if not rows:
            return []
        results = conn.execute(
            "SELECT check_id, guardrail_id, guardrail_name, action, triggered, "
            "status, value, label, passed, reasoning, error, cost_usd, latency_ms, "
            "span_id FROM guardrail_results WHERE check_id = ANY(%s) "
            "ORDER BY guardrail_name",
            ([str(r[0]) for r in rows],),
        ).fetchall()

    by_check: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        by_check.setdefault(str(r[0]), []).append(
            {
                "guardrail_id": str(r[1]) if r[1] else None,
                "guardrail_name": r[2],
                "action": r[3],
                "triggered": r[4],
                "status": r[5],
                "value": float(r[6]) if r[6] is not None else None,
                "label": r[7],
                "passed": r[8],
                "reasoning": r[9],
                "error": r[10],
                "cost_usd": float(r[11]) if r[11] is not None else None,
                "latency_ms": float(r[12]) if r[12] is not None else None,
                "span_id": r[13],
            }
        )

    return [
        {
            "id": str(r[0]),
            "input": r[1],
            "output": r[2],
            "source": r[3],
            "decision": r[4],
            "degraded": r[5],
            "latency_ms": float(r[6]) if r[6] is not None else None,
            "cost_usd": float(r[7]) if r[7] is not None else None,
            "trace_id": r[8],
            "created_at": r[9].isoformat() if r[9] else None,
            "results": by_check.get(str(r[0]), []),
        }
        for r in rows
    ]


def stats(project_id: str) -> dict[str, Any]:
    """Headline numbers for the guardrails page.

    Block rate over the last 24 hours rather than all time: the number people
    act on is "is this blocking more than it was", and an all-time rate moves
    too slowly to answer it.
    """
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*),
                   COUNT(*) FILTER (WHERE decision = 'block'),
                   COUNT(*) FILTER (WHERE degraded),
                   AVG(latency_ms),
                   COALESCE(SUM(cost_usd), 0)
            FROM guardrail_checks
            WHERE project_id = %s AND created_at > now() - interval '24 hours'
            """,
            (project_id,),
        ).fetchone()

    total = row[0] if row else 0
    return {
        "window_hours": 24,
        "checks": total,
        "blocked": row[1] if row else 0,
        "degraded": row[2] if row else 0,
        "block_rate": (row[1] / total) if total else None,
        "avg_latency_ms": float(row[3]) if row and row[3] is not None else None,
        "cost_usd": float(row[4]) if row else 0.0,
    }
