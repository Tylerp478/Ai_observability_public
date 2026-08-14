"""LLM-as-judge scorers — step 4.

A scorer is three things: a prompt, a model, and an output schema. The schema
is what separates this from "ask Claude if the answer is good": the judge is
forced to answer through a tool call whose `input_schema` is generated from the
scorer's definition, so the result arrives already typed and range-checked
rather than as prose someone has to parse with a regex.

Four things are worth knowing about the design.

**Structured output is a forced tool call, not a JSON instruction.** Asking for
JSON in the prompt gets JSON most of the time, and the failures are the
interesting cases — a judge that hedges in prose when the answer is ambiguous
is exactly the judge whose output you most want recorded. `tool_choice` set to
a specific tool makes the model's only move a schema-valid call, so parse
failures stop being a category of error. Enums and numeric bounds go in the
schema, and are re-checked here anyway: the schema constrains sampling, it is
not a guarantee.

**There is no hidden system prompt.** A fixed judge persona injected behind the
scenes would make a scorer's behaviour not fully described by its own
definition — in a tool whose entire purpose is showing what actually ran, that
is the wrong trade. Everything the judge sees comes from the template.

**Judge calls are traced and billed like any other.** Scoring emits its own
trace (`obs-judge`) with one span per judge call carrying cost and tokens.
Scoring a 40-case run against two scorers is 80 live API calls; the tool that
exists to show LLM spend should not be the one hiding its own.

**The cap refuses rather than truncates.** Replay truncation is harmless — each
test case is independent, so evaluating the first 100 is a smaller experiment.
Scoring truncation is not: a mean over whichever items fit under the cap is
presented as the run's score, and it is silently biased by dataset order. So
exceeding OBS_MAX_SCORER_CALLS is a 400 with the arithmetic in it, not a
partial result.

Step 5 added the fifth: **a scorer's definition is versioned.** Every scorer
owns a prompt of kind 'scorer' (prompts.py), the scorers row holds the current
definition and prompt_versions holds the history. Saving an edit appends a
version; every score records which version judged it. Before this, editing a
scorer left its past scores holding a number with no way to see the prompt or
the scale behind it — the numbers survived and their meaning didn't.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from obs_sdk.pricing import estimate_cost_usd

from obs_backend import llm, prompts
from obs_backend.credentials import Credential
from obs_backend.db import get_pool
from obs_backend.models import Span
from obs_backend.wal import SpanWriter

# Template placeholders. {{output}} is required — a judge that never sees the
# output is not judging the output, and a template missing it scores every case
# identically while looking like it worked.
OUTPUT_PLACEHOLDER = "{{output}}"
INPUT_PLACEHOLDER = "{{input}}"
EXPECTED_PLACEHOLDER = "{{expected}}"

# The tool the judge is forced to call. Named for what it does rather than
# something generic like "respond", because the name is part of the prompt the
# model sees.
TOOL_NAME = "record_score"

OUTPUT_TYPES = ("boolean", "numeric", "categorical")

# CLAUDE.md, cost controls: a hard cap on scorer invocations per eval run.
# Counted as items x scorers, since that is the number of billable calls.
MAX_SCORER_CALLS = int(os.environ.get("OBS_MAX_SCORER_CALLS", "100"))

# Judge calls run concurrently for the same reason replay items do. Kept low:
# the ceiling is the API rate limit, not local CPU.
SCORING_CONCURRENCY = int(os.environ.get("OBS_SCORING_CONCURRENCY", "4"))

# Substituted when a template asks for an expected output and the test case has
# none. Stated plainly rather than left blank so the judge doesn't read an
# empty section as "the expected answer is nothing".
NO_EXPECTED = "(no expected output was recorded for this case)"


class ScorerError(ValueError):
    """Bad scorer definition or scoring request — surfaced as a 400, not a 500."""


@dataclass(frozen=True)
class Scorer:
    id: str
    name: str
    description: str
    prompt_template: str
    model: str
    max_tokens: int
    output_type: str
    score_min: float | None
    score_max: float | None
    categories: list[str]
    pass_threshold: float | None
    # Whether the Playground offers this scorer. Presentation, not definition:
    # it is not in the version config, and toggling it appends no version.
    show_in_playground: bool = True
    # The versioned artifact behind this definition (step 5). Nullable only
    # for the instant between a scorer's insert and its backfill; every scorer
    # created or edited since step 5 has both.
    prompt_id: str | None = None
    version_id: str | None = None
    version: int | None = None


@dataclass(frozen=True)
class JudgeResult:
    """One judge call's outcome. Not persisted directly — see _write_score."""

    value: float | None
    label: str
    passed: bool | None
    reasoning: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    latency_ms: float


@dataclass(frozen=True)
class _Target:
    """Something to score: the text, plus where the score should attach."""

    score_id: str
    input: str
    output: str
    expected: str | None


def _hex(n: int) -> str:
    return secrets.token_hex(n)


# --------------------------------------------------------------------------
# Definition
# --------------------------------------------------------------------------


def _validate_definition(
    *,
    name: str,
    prompt_template: str,
    output_type: str,
    score_min: float | None,
    score_max: float | None,
    categories: list[str],
    pass_threshold: float | None,
    max_tokens: int,
) -> tuple[float | None, float | None, list[str], float | None]:
    """Reject an unusable scorer at definition time, not at first invocation.

    Everything here is checkable without spending anything, and a scorer that
    only reveals its problem partway through a paid run is the expensive way to
    find out.
    """
    if not name.strip():
        raise ScorerError("Scorer name cannot be empty")
    if OUTPUT_PLACEHOLDER not in prompt_template:
        raise ScorerError(
            f"Prompt template must contain {OUTPUT_PLACEHOLDER} — that is where the "
            "text being judged is substituted. Without it the judge scores the same "
            "prompt every time and the run means nothing."
        )
    if output_type not in OUTPUT_TYPES:
        raise ScorerError(f"output_type must be one of {', '.join(OUTPUT_TYPES)}")
    if max_tokens < 1 or max_tokens > 32_000:
        raise ScorerError("max_tokens must be between 1 and 32000")

    if output_type == "numeric":
        if score_min is None or score_max is None:
            raise ScorerError("A numeric scorer needs score_min and score_max")
        if score_min >= score_max:
            raise ScorerError("score_min must be less than score_max")
        if pass_threshold is not None and not (score_min <= pass_threshold <= score_max):
            raise ScorerError("pass_threshold must fall within the score range")
        return score_min, score_max, [], pass_threshold

    if output_type == "categorical":
        cleaned = [c.strip() for c in categories if c.strip()]
        if len(cleaned) < 2:
            raise ScorerError("A categorical scorer needs at least two categories")
        if len(set(cleaned)) != len(cleaned):
            raise ScorerError("Categories must be unique")
        # No pass/fail for categorical: which labels count as passing is a
        # per-question judgement that would need its own field, and a
        # distribution is the honest summary of a category.
        return None, None, cleaned, None

    # boolean: pass/fail is the output itself, so the other knobs don't apply.
    return None, None, [], None


def create_scorer(
    project_id: str,
    *,
    name: str,
    description: str,
    prompt_template: str,
    model: str,
    max_tokens: int,
    output_type: str,
    score_min: float | None = None,
    score_max: float | None = None,
    categories: list[str] | None = None,
    pass_threshold: float | None = None,
    show_in_playground: bool | None = None,
) -> str:
    lo, hi, cats, threshold = _validate_definition(
        name=name,
        prompt_template=prompt_template,
        output_type=output_type,
        score_min=score_min,
        score_max=score_max,
        categories=categories or [],
        pass_threshold=pass_threshold,
        max_tokens=max_tokens,
    )

    scorer_id = str(uuid.uuid4())

    # Default the Playground off for a judge that wants a golden answer: the
    # Playground has no expected output to give it, so it would judge every
    # response against NO_EXPECTED and report a number that means nothing.
    # Only the default — the toggle on the Scorers page overrides it either way.
    if show_in_playground is None:
        show_in_playground = EXPECTED_PLACEHOLDER not in prompt_template

    config = {
        "model": model,
        "max_tokens": max_tokens,
        "output_type": output_type,
        "score_min": lo,
        "score_max": hi,
        "categories": cats,
        "pass_threshold": threshold,
    }

    # One transaction for the scorer and its prompt. A scorer whose prompt
    # insert failed would be a scorer with no history and no way to acquire
    # one, since versions only ever get appended by an edit.
    with get_pool().connection() as conn:
        clash = conn.execute(
            "SELECT id FROM scorers WHERE project_id = %s AND name = %s "
            "AND archived_at IS NULL",
            (project_id, name.strip()),
        ).fetchone()
        if clash:
            raise ScorerError(f"A scorer named {name.strip()!r} already exists")

        try:
            prompt_id, _ = prompts.create_prompt_tx(
                conn,
                project_id,
                name=name.strip(),
                description=description.strip(),
                kind="scorer",
                template=prompt_template,
                config=config,
                note="Created.",
            )
        except prompts.PromptError as exc:
            raise ScorerError(str(exc)) from exc

        conn.execute(
            """
            INSERT INTO scorers (id, project_id, name, description, prompt_template,
                                 model, max_tokens, output_type, score_min, score_max,
                                 categories, pass_threshold, show_in_playground, prompt_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                scorer_id,
                project_id,
                name.strip(),
                description.strip(),
                prompt_template,
                model,
                max_tokens,
                output_type,
                lo,
                hi,
                json.dumps(cats),
                threshold,
                show_in_playground,
                prompt_id,
            ),
        )
    return scorer_id


def update_scorer(project_id: str, scorer_id: str, **fields: Any) -> bool:
    """Edit a scorer, appending a version of its definition (step 5).

    Two rows move together: the scorers row becomes the new current definition
    and prompt_versions gains the version that the old scores go on pointing
    at. Step 4 did only the first half, which meant a scorer's past scores kept
    their numbers and lost the wording and scale that produced them.

    An edit that changes nothing appends nothing — add_version_tx compares
    content hashes — so saving the form twice does not mint v3 identical to v2.
    """
    current = get_scorer(project_id, scorer_id)
    if current is None:
        return False

    merged = {
        "name": fields.get("name", current.name),
        "description": fields.get("description", current.description),
        "prompt_template": fields.get("prompt_template", current.prompt_template),
        "model": fields.get("model", current.model),
        "max_tokens": fields.get("max_tokens", current.max_tokens),
        "output_type": fields.get("output_type", current.output_type),
        "score_min": fields.get("score_min", current.score_min),
        "score_max": fields.get("score_max", current.score_max),
        "categories": fields.get("categories", current.categories),
        "pass_threshold": fields.get("pass_threshold", current.pass_threshold),
    }

    lo, hi, cats, threshold = _validate_definition(
        name=merged["name"],
        prompt_template=merged["prompt_template"],
        output_type=merged["output_type"],
        score_min=merged["score_min"],
        score_max=merged["score_max"],
        categories=merged["categories"],
        pass_threshold=merged["pass_threshold"],
        max_tokens=merged["max_tokens"],
    )

    config = {
        "model": merged["model"],
        "max_tokens": merged["max_tokens"],
        "output_type": merged["output_type"],
        "score_min": lo,
        "score_max": hi,
        "categories": cats,
        "pass_threshold": threshold,
    }

    with get_pool().connection() as conn:
        clash = conn.execute(
            "SELECT id FROM scorers WHERE project_id = %s AND name = %s "
            "AND archived_at IS NULL AND id <> %s",
            (project_id, merged["name"].strip(), scorer_id),
        ).fetchone()
        if clash:
            raise ScorerError(f"A scorer named {merged['name'].strip()!r} already exists")

        try:
            prompt_id = current.prompt_id
            if prompt_id is None:
                # A scorer created before step 5 that the startup backfill
                # hasn't reached. Seed its history from the definition being
                # replaced, not the new one — otherwise the version chain opens
                # with the edit and loses the thing it is meant to preserve.
                prompt_id, _ = prompts.create_prompt_tx(
                    conn,
                    project_id,
                    name=current.name,
                    description=current.description,
                    kind="scorer",
                    template=current.prompt_template,
                    config=prompts.scorer_config(current),
                    note="The definition as it stood before this edit.",
                )
                conn.execute(
                    "UPDATE scorers SET prompt_id = %s WHERE id = %s",
                    (prompt_id, scorer_id),
                )

            if merged["name"].strip() != current.name or (
                merged["description"].strip() != current.description
            ):
                prompts.rename_prompt_tx(
                    conn, prompt_id, merged["name"], merged["description"]
                )

            prompts.add_version_tx(
                conn,
                prompt_id,
                template=merged["prompt_template"],
                config=config,
                note=str(fields.get("note", "")),
            )
        except prompts.PromptError as exc:
            raise ScorerError(str(exc)) from exc

        cur = conn.execute(
            """
            UPDATE scorers SET name = %s, description = %s, prompt_template = %s,
                   model = %s, max_tokens = %s, output_type = %s, score_min = %s,
                   score_max = %s, categories = %s, pass_threshold = %s,
                   updated_at = now()
            WHERE id = %s AND project_id = %s AND archived_at IS NULL
            """,
            (
                merged["name"].strip(),
                merged["description"].strip(),
                merged["prompt_template"],
                merged["model"],
                merged["max_tokens"],
                merged["output_type"],
                lo,
                hi,
                json.dumps(cats),
                threshold,
                scorer_id,
                project_id,
            ),
        )
        return cur.rowcount > 0


def set_playground_visibility(project_id: str, scorer_id: str, show: bool) -> bool:
    """Show or hide a scorer on the Playground.

    Separate from update_scorer on purpose. That path appends a prompt version,
    which is right for anything that changes how the judge scores and wrong
    here: whether a scorer appears in one form is not a fact about the judge,
    and a history full of "toggled visibility" entries buries the edits that
    actually changed a number.
    """
    with get_pool().connection() as conn:
        cur = conn.execute(
            "UPDATE scorers SET show_in_playground = %s, updated_at = now() "
            "WHERE id = %s AND project_id = %s AND archived_at IS NULL",
            (show, scorer_id, project_id),
        )
        return cur.rowcount > 0


def archive_scorer(project_id: str, scorer_id: str) -> bool:
    """Soft delete. Scores it produced stay readable and keep their name.

    Its prompt is archived alongside, so the artifact stops cluttering the
    prompt list, while every version — and every score citing one — survives.
    """
    with get_pool().connection() as conn:
        cur = conn.execute(
            "UPDATE scorers SET archived_at = now() "
            "WHERE id = %s AND project_id = %s AND archived_at IS NULL "
            "RETURNING prompt_id",
            (scorer_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            return False
        if row[0]:
            conn.execute(
                "UPDATE prompts SET archived_at = now() WHERE id = %s", (row[0],)
            )
        return True


# Qualified with `s.` throughout because the version subqueries below correlate
# against the scorers row, and an unqualified column would bind to whichever
# table the enclosing query happened to name first.
_SCORER_COLUMNS = (
    "s.id, s.name, s.description, s.prompt_template, s.model, s.max_tokens, "
    "s.output_type, s.score_min, s.score_max, s.categories, s.pass_threshold, "
    "s.show_in_playground, s.prompt_id, "
    "(SELECT pv.id FROM prompt_versions pv WHERE pv.prompt_id = s.prompt_id "
    " ORDER BY pv.version DESC LIMIT 1), "
    "(SELECT MAX(pv.version) FROM prompt_versions pv WHERE pv.prompt_id = s.prompt_id)"
)


def _scorer(row: tuple[Any, ...]) -> Scorer:
    return Scorer(
        id=str(row[0]),
        name=row[1],
        description=row[2],
        prompt_template=row[3],
        model=row[4],
        max_tokens=row[5],
        output_type=row[6],
        score_min=float(row[7]) if row[7] is not None else None,
        score_max=float(row[8]) if row[8] is not None else None,
        categories=json.loads(row[9] or "[]"),
        pass_threshold=float(row[10]) if row[10] is not None else None,
        show_in_playground=bool(row[11]),
        prompt_id=str(row[12]) if row[12] else None,
        version_id=str(row[13]) if row[13] else None,
        version=row[14],
    )


def scorer_dict(scorer: Scorer) -> dict[str, Any]:
    return {
        "id": scorer.id,
        "name": scorer.name,
        "description": scorer.description,
        "prompt_template": scorer.prompt_template,
        "model": scorer.model,
        "max_tokens": scorer.max_tokens,
        "output_type": scorer.output_type,
        "score_min": scorer.score_min,
        "score_max": scorer.score_max,
        "categories": scorer.categories,
        "pass_threshold": scorer.pass_threshold,
        "show_in_playground": scorer.show_in_playground,
        "prompt_id": scorer.prompt_id,
        "version": scorer.version,
    }


def get_scorer(project_id: str, scorer_id: str) -> Scorer | None:
    with get_pool().connection() as conn:
        row = conn.execute(
            f"SELECT {_SCORER_COLUMNS} FROM scorers s "
            "WHERE s.id = %s AND s.project_id = %s AND s.archived_at IS NULL",
            (scorer_id, project_id),
        ).fetchone()
    return _scorer(row) if row else None


def list_scorers(project_id: str) -> list[dict[str, Any]]:
    """Active scorers, each with how many scores it has produced."""
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"""
            SELECT {_SCORER_COLUMNS}, s.created_at,
                   (SELECT COUNT(*) FROM scores sc
                     WHERE sc.scorer_id = s.id AND sc.status = 'succeeded')
            FROM scorers s
            WHERE s.project_id = %s AND s.archived_at IS NULL
            ORDER BY s.created_at DESC
            """,
            (project_id,),
        ).fetchall()

    return [
        {
            **scorer_dict(_scorer(r)),
            "created_at": r[15].isoformat() if r[15] else None,
            "score_count": r[16],
        }
        for r in rows
    ]


def resolve_scorers(project_id: str, scorer_ids: list[str]) -> list[Scorer]:
    """Load scorers by id, rejecting anything missing.

    Silently dropping an unknown id would produce a run that reports fewer
    scorers than were asked for, which reads as "the judge didn't fire" rather
    than "you sent a bad id".
    """
    resolved: list[Scorer] = []
    for scorer_id in scorer_ids:
        scorer = get_scorer(project_id, scorer_id)
        if scorer is None:
            raise ScorerError(f"Scorer {scorer_id} not found")
        resolved.append(scorer)
    return resolved


# --------------------------------------------------------------------------
# The judge call
# --------------------------------------------------------------------------


def build_tool_schema(scorer: Scorer) -> dict[str, Any]:
    """The JSON schema the judge is forced to answer with.

    `reasoning` is declared first on purpose. Tool input is generated as a
    token stream in property order, so a schema that puts the verdict first
    makes the model commit before it has reasoned and then rationalize
    backwards. Reasoning first is the cheap version of chain-of-thought, and it
    is also the field that makes a score auditable rather than an oracle.
    """
    properties: dict[str, Any] = {
        "reasoning": {
            "type": "string",
            "description": "Brief justification, citing the specific part of the output that drove the verdict.",
        }
    }

    if scorer.output_type == "boolean":
        properties["passed"] = {
            "type": "boolean",
            "description": "true if the output meets the criterion, false if it does not.",
        }
        required = ["reasoning", "passed"]
    elif scorer.output_type == "numeric":
        properties["score"] = {
            "type": "number",
            "minimum": scorer.score_min,
            "maximum": scorer.score_max,
            "description": f"Score from {scorer.score_min} to {scorer.score_max}.",
        }
        required = ["reasoning", "score"]
    else:
        properties["label"] = {
            "type": "string",
            "enum": scorer.categories,
            "description": "The single category that best describes the output.",
        }
        required = ["reasoning", "label"]

    return {"type": "object", "properties": properties, "required": required}


def render_judge_prompt(
    scorer: Scorer, *, input_text: str, output_text: str, expected: str | None
) -> str:
    return (
        scorer.prompt_template.replace(OUTPUT_PLACEHOLDER, output_text)
        .replace(INPUT_PLACEHOLDER, input_text)
        .replace(EXPECTED_PLACEHOLDER, (expected or "").strip() or NO_EXPECTED)
    )


def _interpret(scorer: Scorer, payload: dict[str, Any]) -> tuple[float | None, str, bool | None]:
    """Turn the tool call's arguments into (value, label, passed).

    Re-validates against the schema's own constraints. `tool_choice` guarantees
    a call with the right shape; it does not guarantee the model respected an
    enum or a numeric bound, and a 6 recorded on a 1-5 scale would quietly skew
    every mean built on it afterwards.
    """
    if scorer.output_type == "boolean":
        passed = payload.get("passed")
        if not isinstance(passed, bool):
            raise ScorerError(f"Judge returned a non-boolean verdict: {passed!r}")
        return (1.0 if passed else 0.0), ("pass" if passed else "fail"), passed

    if scorer.output_type == "numeric":
        raw = payload.get("score")
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            raise ScorerError(f"Judge returned a non-numeric score: {raw!r}")
        value = float(raw)
        assert scorer.score_min is not None and scorer.score_max is not None
        if not (scorer.score_min <= value <= scorer.score_max):
            raise ScorerError(
                f"Judge returned {value}, outside the {scorer.score_min}–"
                f"{scorer.score_max} range this scorer defines."
            )
        passed = None if scorer.pass_threshold is None else value >= scorer.pass_threshold
        return value, f"{value:g}", passed

    label = payload.get("label")
    if label not in scorer.categories:
        raise ScorerError(
            f"Judge returned {label!r}, which is not one of: {', '.join(scorer.categories)}"
        )
    return None, str(label), None


def judge(
    scorer: Scorer,
    *,
    input_text: str,
    output_text: str,
    expected: str | None,
    api_key: str,
    timeout: float | None = None,
) -> tuple[JudgeResult, dict[str, Any]]:
    """Run one judge call. Returns the result and the raw call metadata.

    Raises ScorerError for a judge that answered unusably; the caller decides
    whether that fails one score or the whole job.

    `timeout` bounds the HTTP call. Scoring jobs leave it unset — they run on a
    background thread and a slow judge costs nothing but time. Guardrails set
    it, because there a slow judge is a user staring at a spinner (step 6).
    """
    prompt = render_judge_prompt(
        scorer, input_text=input_text, output_text=output_text, expected=expected
    )

    # The forced tool call itself lives in llm.py; what stays here is what the
    # verdict has to satisfy to count as one.
    call = llm.tool_call(
        model=scorer.model,
        prompt=prompt,
        max_tokens=scorer.max_tokens,
        tool_name=TOOL_NAME,
        tool_description=(
            "Record the evaluation of the output. Call this exactly once, "
            "after reasoning about the criterion."
        ),
        input_schema=build_tool_schema(scorer),
        api_key=api_key,
        timeout=timeout,
    )

    if call.payload is None:
        # Nearly always max_tokens: the tool call was cut off mid-JSON, so the
        # block never completed. Say so, because "no tool call" is otherwise a
        # baffling result from a forced tool call.
        hint = (
            " The judge hit max_tokens before finishing its answer — raise the "
            "scorer's max_tokens."
            if call.stop_reason == "max_tokens"
            else ""
        )
        raise ScorerError(f"Judge did not return a {TOOL_NAME} call.{hint}")

    value, label, passed = _interpret(scorer, call.payload)

    cost = estimate_cost_usd(
        call.response_model,
        call.input_tokens,
        call.output_tokens,
        on=datetime.now(tz=timezone.utc).date(),
    )

    result = JudgeResult(
        value=value,
        label=label,
        passed=passed,
        reasoning=str(call.payload.get("reasoning", "")),
        input_tokens=call.input_tokens,
        output_tokens=call.output_tokens,
        cost_usd=cost,
        latency_ms=call.latency_ms,
    )
    meta = {
        "prompt": prompt,
        "response_model": call.response_model,
        "response_id": call.response_id,
        "stop_reason": call.stop_reason,
        "start_nano": call.start_nano,
        "end_nano": call.end_nano,
        "payload": call.payload,
    }
    return result, meta


def judge_span(
    *,
    scorer: Scorer,
    project_id: str,
    trace_id: str,
    parent_span_id: str,
    span_id: str,
    meta: dict[str, Any],
    result: JudgeResult | None,
    error: str = "",
    extra: dict[str, Any] | None = None,
    service_name: str = "obs-judge",
) -> Span:
    """The judge call as a gen_ai span, so scoring spend shows up in traces.

    Shared with guardrails (step 6), which pass their own service_name — the
    call is the same shape and hiding it under a second span builder would let
    the two drift on which attributes a judge call carries.
    """
    return Span(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        name=f"judge {scorer.name}",
        start_time_unix_nano=meta["start_nano"],
        end_time_unix_nano=meta["end_nano"],
        status_code="ERROR" if error else "OK",
        status_message=error,
        project_id=project_id,
        service_name=service_name,
        gen_ai_operation_name="chat",
        gen_ai_provider_name=llm.provider_label(scorer.model),
        gen_ai_request_model=scorer.model,
        gen_ai_response_model=meta.get("response_model"),
        gen_ai_response_id=meta.get("response_id"),
        gen_ai_request_max_tokens=scorer.max_tokens,
        gen_ai_usage_input_tokens=result.input_tokens if result else None,
        gen_ai_usage_output_tokens=result.output_tokens if result else None,
        gen_ai_finish_reasons=json.dumps([meta.get("stop_reason", "unknown")]),
        gen_ai_input_messages=meta.get("prompt", ""),
        # The verdict, not prose: this is what the judge actually produced.
        gen_ai_output_messages=json.dumps(meta.get("payload", {})),
        obs_cost_usd=result.cost_usd if result else None,
        obs_latency_seconds=(meta["end_nano"] - meta["start_nano"]) / 1e9,
        attributes_json=json.dumps(
            {
                "obs.scorer_id": scorer.id,
                "obs.scorer_name": scorer.name,
                "obs.scorer_output_type": scorer.output_type,
                **(extra or {}),
            }
        ),
    )


# --------------------------------------------------------------------------
# Scoring jobs
# --------------------------------------------------------------------------


def _create_score_rows(
    *,
    project_id: str,
    scorers: list[Scorer],
    targets: list[dict[str, Any]],
    target_kind: str,
    run_id: str | None,
    credential_id: str,
    generation_credential: str,
) -> list[tuple[Scorer, _Target]]:
    """Materialize pending score rows so the UI can show work in flight.

    Rows first, calls after: a scoring job that only becomes visible once its
    first result lands looks like nothing happened for however long the judge
    takes.
    """
    pairs: list[tuple[Scorer, _Target]] = []
    params: list[tuple[Any, ...]] = []

    for target in targets:
        for scorer in scorers:
            score_id = str(uuid.uuid4())
            pairs.append(
                (
                    scorer,
                    _Target(
                        score_id=score_id,
                        input=target.get("input", ""),
                        output=target.get("output", ""),
                        expected=target.get("expected"),
                    ),
                )
            )
            params.append(
                (
                    score_id,
                    project_id,
                    scorer.id,
                    target_kind,
                    run_id,
                    target.get("run_item_id"),
                    target.get("trace_id", ""),
                    target.get("span_id", ""),
                    # Pinned now, not when the judge answers. A scorer edited
                    # while its own scoring job is in flight must not have the
                    # new version credited with verdicts the old one produced.
                    scorer.version_id,
                    credential_id,
                    generation_credential,
                )
            )

    with get_pool().connection() as conn, conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO scores (id, project_id, scorer_id, target_kind, run_id, "
            "run_item_id, trace_id, span_id, prompt_version_id, credential_id, "
            "generation_credential, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')",
            params,
        )
    return pairs


def _write_score(score_id: str, **fields: Any) -> None:
    assignments = ", ".join(f"{k} = %s" for k in fields)
    with get_pool().connection() as conn:
        conn.execute(
            f"UPDATE scores SET {assignments} WHERE id = %s",
            (*fields.values(), score_id),
        )


def _execute_job(
    *,
    project_id: str,
    pairs: list[tuple[Scorer, _Target]],
    writer: SpanWriter,
    trace_id: str,
    root_name: str,
    root_attributes: dict[str, Any],
    credential: Credential,
) -> None:
    """Run every (scorer, target) pair and emit one trace for the job.

    Owns its thread, so like the replay runner it must not raise: a scoring
    failure has to leave the score rows in a terminal state rather than a
    process-wide pending.
    """
    started = time.time_ns()
    root_span_id = _hex(8)
    spans: list[Span] = []
    spans_lock = threading.Lock()

    # Tagged onto the root span and every judge span below, so "what did this
    # key cost me?" is answerable from the trace store as well as from the
    # scores table. The name, never the key.
    root_attributes = {**root_attributes, "obs.credential": credential.name}

    def work(pair: tuple[Scorer, _Target]) -> bool:
        scorer, target = pair
        _write_score(target.score_id, status="running")
        span_id = _hex(8)
        meta: dict[str, Any] = {"start_nano": time.time_ns()}

        try:
            result, meta = judge(
                scorer,
                input_text=target.input,
                output_text=target.output,
                expected=target.expected,
                api_key=credential.secret,
            )
        except Exception as exc:
            meta.setdefault("end_nano", time.time_ns())
            message = (
                str(exc) if isinstance(exc, ScorerError) else f"{type(exc).__name__}: {exc}"
            )
            _write_score(
                target.score_id,
                status="failed",
                error=message,
                judge_trace_id=trace_id,
                judge_span_id=span_id,
            )
            with spans_lock:
                spans.append(
                    judge_span(
                        scorer=scorer,
                        project_id=project_id,
                        trace_id=trace_id,
                        parent_span_id=root_span_id,
                        span_id=span_id,
                        meta=meta,
                        result=None,
                        error=message,
                        extra={"obs.score_id": target.score_id, **root_attributes},
                    )
                )
            return False

        _write_score(
            target.score_id,
            status="succeeded",
            value=result.value,
            label=result.label,
            passed=result.passed,
            reasoning=result.reasoning,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.cost_usd,
            latency_ms=result.latency_ms,
            judge_trace_id=trace_id,
            judge_span_id=span_id,
        )
        with spans_lock:
            spans.append(
                judge_span(
                    scorer=scorer,
                    project_id=project_id,
                    trace_id=trace_id,
                    parent_span_id=root_span_id,
                    span_id=span_id,
                    meta=meta,
                    result=result,
                    extra={"obs.score_id": target.score_id, **root_attributes},
                )
            )
        return True

    try:
        with ThreadPoolExecutor(max_workers=max(1, SCORING_CONCURRENCY)) as pool:
            outcomes = list(pool.map(work, pairs))

        failed = sum(1 for ok in outcomes if not ok)
        ended = time.time_ns()
        spans.append(
            Span(
                trace_id=trace_id,
                span_id=root_span_id,
                parent_span_id=None,
                name=root_name,
                start_time_unix_nano=started,
                end_time_unix_nano=ended,
                status_code="ERROR" if failed == len(pairs) and pairs else "OK",
                status_message=f"All {failed} judge calls failed" if failed == len(pairs) and pairs else "",
                project_id=project_id,
                service_name="obs-judge",
                gen_ai_operation_name="invoke_agent",
                gen_ai_agent_name="scorer",
                obs_latency_seconds=(ended - started) / 1e9,
                attributes_json=json.dumps(
                    {"obs.score_count": len(pairs), "obs.score_failures": failed, **root_attributes}
                ),
            )
        )
        writer.append(spans)
    except Exception as exc:  # noqa: BLE001 — must not die silently on a thread
        traceback.print_exc()
        # Anything still pending was never attempted; leaving it pending makes
        # the UI poll forever.
        with get_pool().connection() as conn:
            conn.execute(
                "UPDATE scores SET status = 'failed', error = %s "
                "WHERE id = ANY(%s) AND status IN ('pending', 'running')",
                (f"Scoring job failed: {exc}", [t.score_id for _, t in pairs]),
            )
        if spans:
            try:
                writer.append(spans)
            except Exception:
                pass


def _start(target_fn: Any, name: str) -> None:
    threading.Thread(target=target_fn, daemon=True, name=name).start()


def check_run_scoring_budget(item_count: int, scorer_count: int) -> None:
    """Raise if this job would exceed the invocation cap.

    Separate from score_run so run creation can enforce it before spending
    anything on the replay itself — finding out that the scoring you asked for
    is over budget *after* paying for 80 replay calls is the worst ordering.
    """
    calls = item_count * scorer_count
    if calls > MAX_SCORER_CALLS:
        raise ScorerError(
            f"{item_count} items x {scorer_count} scorers = {calls} judge calls, "
            f"over the {MAX_SCORER_CALLS} cap (OBS_MAX_SCORER_CALLS). Refusing rather "
            "than scoring part of the run: a mean over whichever items fit under the "
            "cap would be biased by dataset order and would not say so."
        )


def score_run(
    project_id: str,
    run_id: str,
    scorer_ids: list[str],
    writer: SpanWriter,
    credential: Credential,
) -> int:
    """Score every succeeded item of a run. Returns the number of judge calls.

    Only succeeded items with output are scored. A failed replay item has
    nothing to judge, and paying a judge to read an empty string in order to
    record a zero would drag every aggregate down with a number that describes
    an API error rather than output quality.
    """
    scorers = resolve_scorers(project_id, scorer_ids)
    if not scorers:
        raise ScorerError("Pick at least one scorer")

    with get_pool().connection() as conn:
        run = conn.execute(
            """
            SELECT r.id, r.name, r.trace_id, COALESCE(pc.name, '')
            FROM runs r
            LEFT JOIN provider_credentials pc ON pc.id = r.credential_id
            WHERE r.id = %s AND r.project_id = %s
            """,
            (run_id, project_id),
        ).fetchone()
        if run is None:
            raise ScorerError("Run not found")

        rows = conn.execute(
            """
            SELECT ri.id, di.input, ri.output, di.expected_output
            FROM run_items ri
            JOIN dataset_items di ON di.id = ri.dataset_item_id
            WHERE ri.run_id = %s AND ri.status = 'succeeded' AND ri.output <> ''
            ORDER BY ri.ordinal ASC
            """,
            (run_id,),
        ).fetchall()

    if not rows:
        raise ScorerError("This run has no successful outputs to score")

    check_run_scoring_budget(len(rows), len(scorers))

    targets = [
        {
            "run_item_id": str(r[0]),
            "input": r[1],
            "output": r[2],
            "expected": r[3],
        }
        for r in rows
    ]
    pairs = _create_score_rows(
        project_id=project_id,
        scorers=scorers,
        targets=targets,
        target_kind="run_item",
        run_id=run_id,
        credential_id=credential.id,
        # run[3] is the run's own key, deliberately not `credential`: a run
        # re-scored later with a different key was still generated by the one
        # it recorded at creation.
        generation_credential=run[3],
    )

    trace_id = _hex(16)
    _start(
        lambda: _execute_job(
            project_id=project_id,
            pairs=pairs,
            writer=writer,
            trace_id=trace_id,
            root_name=f"eval scoring {run[1] or run_id[:8]}",
            root_attributes={"obs.run_id": run_id},
            credential=credential,
        ),
        f"score-{run_id[:8]}",
    )
    return len(pairs)


def score_span(
    project_id: str,
    *,
    trace_id: str,
    span_id: str,
    input_text: str,
    output_text: str,
    scorer_ids: list[str],
    writer: SpanWriter,
    credential: Credential,
    generation_credential: str = "",
) -> list[str]:
    """Score a single span from a trace. Returns the new score ids.

    The production-traffic half of "attach scores to traces and dataset runs":
    a span that looked wrong in the waterfall can be handed to a judge without
    first being turned into a dataset.
    """
    scorers = resolve_scorers(project_id, scorer_ids)
    if not scorers:
        raise ScorerError("Pick at least one scorer")
    if not output_text.strip():
        raise ScorerError("This span has no output to score")

    check_run_scoring_budget(1, len(scorers))

    pairs = _create_score_rows(
        project_id=project_id,
        scorers=scorers,
        targets=[
            {
                "trace_id": trace_id,
                "span_id": span_id,
                "input": input_text,
                "output": output_text,
                "expected": None,
            }
        ],
        target_kind="span",
        run_id=None,
        credential_id=credential.id,
        generation_credential=generation_credential,
    )

    judge_trace_id = _hex(16)
    _start(
        lambda: _execute_job(
            project_id=project_id,
            pairs=pairs,
            writer=writer,
            trace_id=judge_trace_id,
            root_name=f"span scoring {span_id}",
            root_attributes={"obs.target_trace_id": trace_id, "obs.target_span_id": span_id},
            credential=credential,
        ),
        f"score-span-{span_id[:8]}",
    )
    return [t.score_id for _, t in pairs]


def try_scorer(
    project_id: str,
    scorer_id: str,
    *,
    input_text: str,
    output_text: str,
    expected: str | None,
    writer: SpanWriter,
    credential: Credential,
) -> dict[str, Any]:
    """One judge call against ad-hoc text, run synchronously and not persisted.

    The cheapest way to find out a scorer is broken. Without it the shortest
    feedback loop on a new scorer is a whole run — which is how you spend 40
    calls discovering that the prompt asks for a 1-10 score while the schema
    says 1-5.

    It still emits a span. This is real spend on the same key, and an
    observability tool that quietly excluded its own preview calls from the
    cost it reports would be lying by omission.
    """
    scorer = get_scorer(project_id, scorer_id)
    if scorer is None:
        raise ScorerError("Scorer not found")
    if not output_text.strip():
        raise ScorerError("Give the judge some output text to score")

    trace_id = _hex(16)
    span_id = _hex(8)
    meta: dict[str, Any] = {"start_nano": time.time_ns()}

    extra = {"obs.scorer_try": True, "obs.credential": credential.name}

    try:
        result, meta = judge(
            scorer,
            input_text=input_text,
            output_text=output_text,
            expected=expected,
            api_key=credential.secret,
        )
    except Exception as exc:
        meta.setdefault("end_nano", time.time_ns())
        message = str(exc) if isinstance(exc, ScorerError) else f"{type(exc).__name__}: {exc}"
        writer.append(
            [
                judge_span(
                    scorer=scorer,
                    project_id=project_id,
                    trace_id=trace_id,
                    parent_span_id="",
                    span_id=span_id,
                    meta=meta,
                    result=None,
                    error=message,
                    extra=extra,
                )
            ]
        )
        raise ScorerError(message) from exc

    span = judge_span(
        scorer=scorer,
        project_id=project_id,
        trace_id=trace_id,
        parent_span_id="",
        span_id=span_id,
        meta=meta,
        result=result,
        extra=extra,
    )
    writer.append([span])

    return {
        "value": result.value,
        "label": result.label,
        "passed": result.passed,
        "reasoning": result.reasoning,
        "cost_usd": result.cost_usd,
        "latency_ms": result.latency_ms,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "prompt": meta["prompt"],
        "trace_id": trace_id,
    }


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------

_SCORE_COLUMNS = (
    "sc.id, sc.scorer_id, s.name, s.output_type, s.score_min, s.score_max, "
    "s.pass_threshold, sc.status, sc.value, sc.label, sc.passed, sc.reasoning, "
    "sc.error, sc.cost_usd, sc.latency_ms, sc.judge_trace_id, sc.judge_span_id, "
    "sc.run_item_id, sc.trace_id, sc.span_id, sc.created_at, "
    # Which definition judged this, and where to read it. Null for scores
    # taken before step 5 — see prompts.backfill_scorer_prompts for why those
    # are left null rather than pointed at a v1 that may not be what ran.
    "s.prompt_id, sc.prompt_version_id, pv.version"
)

# Joined everywhere _SCORE_COLUMNS is selected. LEFT, because a pre-step-5
# score has no version and an inner join would quietly drop it from the run it
# belongs to.
_SCORE_JOINS = (
    "FROM scores sc JOIN scorers s ON s.id = sc.scorer_id "
    "LEFT JOIN prompt_versions pv ON pv.id = sc.prompt_version_id"
)


def _score_dict(r: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": str(r[0]),
        "scorer_id": str(r[1]),
        "scorer_name": r[2],
        "output_type": r[3],
        "score_min": float(r[4]) if r[4] is not None else None,
        "score_max": float(r[5]) if r[5] is not None else None,
        "pass_threshold": float(r[6]) if r[6] is not None else None,
        "status": r[7],
        "value": float(r[8]) if r[8] is not None else None,
        "label": r[9],
        "passed": r[10],
        "reasoning": r[11],
        "error": r[12],
        "cost_usd": float(r[13]) if r[13] is not None else None,
        "latency_ms": float(r[14]) if r[14] is not None else None,
        "judge_trace_id": r[15],
        "judge_span_id": r[16],
        "run_item_id": str(r[17]) if r[17] else None,
        "trace_id": r[18],
        "span_id": r[19],
        "created_at": r[20].isoformat() if r[20] else None,
        "prompt_id": str(r[21]) if r[21] else None,
        "prompt_version_id": str(r[22]) if r[22] else None,
        "scorer_version": r[23],
    }


def average_by_scorer(
    project_id: str, hours: int = 24, credential: str | None = None
) -> list[dict[str, Any]]:
    """Each scorer's headline number over a time window.

    Three things this deliberately does not do.

    **It does not average across output types.** summarize() already explains
    why: a mean over a 1-5 faithfulness scale and a 0-1 pass rate are different
    quantities, and putting them in one column produces a number that renders
    convincingly and means nothing. Each row carries its own type and the
    caller renders it on its own scale.

    **It does not count a re-scored item twice.** Scoring a run again is
    allowed and keeps its history, so the raw table holds several verdicts for
    the same target. DISTINCT ON takes the newest per (scorer, target), the
    same rule scores_for_run uses — without it, re-scoring one run quietly
    reweights the whole average toward whatever that run happens to contain.

    **It does not filter by source.** A score has no service.name: it belongs
    to the judge, not to the application whose output was judged. The window
    and the provider key are the two filters that genuinely apply.
    """
    where = [
        "sc.project_id = %s",
        "sc.status = 'succeeded'",
        "sc.created_at >= now() - make_interval(hours => %s)",
    ]
    params: list[Any] = [project_id, hours]
    if credential:
        # The key that produced the judged output, not the one that paid the
        # judge. "How good is what key A produces" is a question about the
        # generation; grading it with a different key does not change the
        # answer. Matched on name, which is also what a span records.
        where.append("sc.generation_credential = %s")
        params.append(credential)

    sql = f"""
    WITH latest AS (
        SELECT DISTINCT ON (
                   sc.scorer_id,
                   COALESCE(sc.run_item_id, sc.trace_id || ':' || sc.span_id)
               )
               sc.scorer_id, sc.value, sc.passed, sc.label
        FROM scores sc
        WHERE {" AND ".join(where)}
        ORDER BY sc.scorer_id,
                 COALESCE(sc.run_item_id, sc.trace_id || ':' || sc.span_id),
                 sc.created_at DESC
    )
    SELECT s.id, s.name, s.output_type, s.score_min, s.score_max, s.pass_threshold,
           COUNT(*) AS scored,
           AVG(l.value) AS mean,
           AVG(CASE WHEN l.passed IS NULL THEN NULL
                    WHEN l.passed THEN 1.0 ELSE 0.0 END) AS pass_rate,
           MODE() WITHIN GROUP (ORDER BY l.label) AS top_label
    FROM scorers s
    JOIN latest l ON l.scorer_id = s.id
    WHERE s.archived_at IS NULL
    GROUP BY s.id, s.name, s.output_type, s.score_min, s.score_max, s.pass_threshold
    ORDER BY s.name
    """

    with get_pool().connection() as conn:
        rows = conn.execute(sql, params).fetchall()

    return [
        {
            "scorer_id": str(r[0]),
            "scorer_name": r[1],
            "output_type": r[2],
            "score_min": float(r[3]) if r[3] is not None else None,
            "score_max": float(r[4]) if r[4] is not None else None,
            "pass_threshold": float(r[5]) if r[5] is not None else None,
            "scored": int(r[6]),
            "mean": float(r[7]) if r[7] is not None else None,
            "pass_rate": float(r[8]) if r[8] is not None else None,
            "top_label": r[9] or "",
        }
        for r in rows
    ]


def scores_for_run(run_id: str) -> list[dict[str, Any]]:
    """Latest score per (scorer, item).

    DISTINCT ON rather than a plain select: re-scoring a run is allowed and
    keeps its history, but showing three generations of the same judge on one
    row would make the results table unreadable and the mean wrong.
    """
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"""
            SELECT DISTINCT ON (sc.scorer_id, sc.run_item_id) {_SCORE_COLUMNS}
            {_SCORE_JOINS}
            WHERE sc.run_id = %s
            ORDER BY sc.scorer_id, sc.run_item_id, sc.created_at DESC
            """,
            (run_id,),
        ).fetchall()
    return [_score_dict(r) for r in rows]


def scores_for_trace(project_id: str, trace_id: str) -> list[dict[str, Any]]:
    with get_pool().connection() as conn:
        rows = conn.execute(
            f"""
            SELECT DISTINCT ON (sc.scorer_id, sc.span_id) {_SCORE_COLUMNS}
            {_SCORE_JOINS}
            WHERE sc.trace_id = %s AND sc.project_id = %s AND sc.target_kind = 'span'
            ORDER BY sc.scorer_id, sc.span_id, sc.created_at DESC
            """,
            (trace_id, project_id),
        ).fetchall()
    return [_score_dict(r) for r in rows]


def summarize(scores: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-scorer rollup, aggregated the way each output type deserves.

    A single "average score" column across all three types would be wrong twice
    over: it would average category indices into a meaningless decimal, and it
    would put a 1-5 faithfulness mean in the same column as a 0-1 pass rate.
    """
    by_scorer: dict[str, list[dict[str, Any]]] = {}
    for score in scores:
        by_scorer.setdefault(score["scorer_id"], []).append(score)

    summaries: list[dict[str, Any]] = []
    for scorer_id, group in by_scorer.items():
        done = [s for s in group if s["status"] == "succeeded"]
        first = group[0]
        summary: dict[str, Any] = {
            "scorer_id": scorer_id,
            "scorer_name": first["scorer_name"],
            "output_type": first["output_type"],
            "score_min": first["score_min"],
            "score_max": first["score_max"],
            "total": len(group),
            "scored": len(done),
            "pending": sum(1 for s in group if s["status"] in ("pending", "running")),
            "failed": sum(1 for s in group if s["status"] == "failed"),
            "cost_usd": sum(s["cost_usd"] or 0 for s in group),
            "mean": None,
            "pass_rate": None,
            "distribution": None,
            "prompt_id": first["prompt_id"],
            # Which scorer versions this number was built from. Usually one.
            # More than one means the scorer was edited between scoring passes
            # and the mean averages verdicts from two different judges — worth
            # saying out loud rather than presenting a single clean figure.
            "versions": sorted(
                {s["scorer_version"] for s in group if s["scorer_version"] is not None}
            ),
        }

        if first["output_type"] == "categorical":
            counts: dict[str, int] = {}
            for s in done:
                counts[s["label"]] = counts.get(s["label"], 0) + 1
            summary["distribution"] = counts
        elif done:
            summary["mean"] = sum(s["value"] or 0 for s in done) / len(done)
            judged = [s for s in done if s["passed"] is not None]
            if judged:
                summary["pass_rate"] = sum(1 for s in judged if s["passed"]) / len(judged)

        summaries.append(summary)

    summaries.sort(key=lambda s: s["scorer_name"])
    return summaries
