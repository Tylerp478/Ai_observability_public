"""Ad-hoc prompt runs — send one prompt, keep the span, score the answer.

The gap this fills: until now the only way to get a completion generated *and*
scored was to create a dataset, add a test case, and replay it. Both halves
already existed separately — `scoring.try_scorer` judges text you paste in, and
`scoring.score_span` scores a span from any trace — but nothing generated the
text in the first place. This is the bridge, and it is deliberately thin: one
call through llm.complete, one span, then straight into `score_span`.

**The output is a real span, not a private preview.** It goes through the same
SpanWriter as production traffic, so it appears in Traces, in the waterfall, and
in the Overview cost tiles with no extra plumbing. That is the same reasoning
that put replay runs in the span store rather than a parallel results viewer
(runner.py): a second rendering of the same data starts drifting from the first
immediately. It also means this call's spend is visible — an observability tool
that quietly excluded its own convenience feature from the cost it reports
would be lying by omission.

**No {{input}} placeholder.** A replay template must have one, because without
it every test case sends a byte-identical request and the run means nothing
(runner.py). Here there is exactly one request and the prompt is the whole of
it, so requiring a placeholder would be requiring ceremony. An optional input is
substituted if the placeholder happens to be there, which is what makes it
possible to paste a saved prompt template straight in and try it.

**Validation before spending.** Scorers are resolved and the judge budget is
checked before the completion call goes out, not after. Discovering that a
scorer id is wrong *after* paying for the completion is the same bad ordering
create_run avoids.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from typing import Any

from obs_sdk.pricing import estimate_cost_usd

from obs_backend import credentials, llm, scoring
from obs_backend.models import Span
from obs_backend.wal import SpanWriter

# The placeholder substituted when an input is supplied. Optional here, unlike
# in a replay template.
INPUT_PLACEHOLDER = "{{input}}"

# Bounds the completion call. Set, unlike a replay run's: a replay runs on a
# background thread where slowness costs only time, whereas this is a request
# path with someone watching a spinner.
PLAYGROUND_TIMEOUT_SECONDS = float(
    os.environ.get("OBS_PLAYGROUND_TIMEOUT_SECONDS", "60")
)

# Its own service name, so playground traffic is one selectable source in the
# Overview filter rather than being lumped in with replay runs.
SERVICE_NAME = "obs-playground"


class PlaygroundError(ValueError):
    """Bad playground request — surfaced as a 400, not a 500."""


def _hex(n: int) -> str:
    return secrets.token_hex(n)


def run(
    *,
    project_id: str,
    prompt: str,
    model: str,
    max_tokens: int,
    input_text: str = "",
    scorer_ids: list[str] | None = None,
    credential_id: str | None = None,
    writer: SpanWriter,
) -> dict[str, Any]:
    """Send one prompt, record it, and kick off scoring. Returns the result.

    Synchronous for the completion, asynchronous for the scores: the caller
    gets the output as soon as it exists and polls for verdicts, which is the
    same shape `score_span` already uses everywhere else.
    """
    prompt = prompt.strip()
    if not prompt:
        raise PlaygroundError("Give it a prompt to send")
    if max_tokens < 1 or max_tokens > 32_000:
        raise PlaygroundError("max_tokens must be between 1 and 32000")

    # All resolved before the completion is paid for, not after. The model
    # check moved below the credential because the credential is what says
    # which provider this call is for.
    scorers = scoring.resolve_scorers(project_id, scorer_ids or [])
    if scorers:
        scoring.check_run_scoring_budget(1, len(scorers))
    credential = credentials.resolve(project_id, credential_id)

    try:
        llm.check_model_matches(credential.provider, model)
    except (llm.ModelProviderMismatch, llm.UnknownProvider) as exc:
        raise PlaygroundError(str(exc)) from exc

    rendered = prompt.replace(INPUT_PLACEHOLDER, input_text) if input_text else prompt

    trace_id = _hex(16)
    span_id = _hex(8)

    try:
        call = llm.complete(
            provider=credential.provider,
            model=model,
            prompt=rendered,
            max_tokens=max_tokens,
            api_key=credential.secret,
            timeout=PLAYGROUND_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        # The failure is traced too. A prompt that reliably times out or gets
        # rejected is exactly the thing worth having a span for, and a silent
        # 500 would leave no record that the call was ever attempted.
        message = f"{type(exc).__name__}: {exc}"
        writer.append(
            [
                _span(
                    project_id=project_id,
                    trace_id=trace_id,
                    span_id=span_id,
                    model=model,
                    max_tokens=max_tokens,
                    prompt=rendered,
                    call=None,
                    error=message,
                    provider=credential.provider,
                    credential_name=credential.name,
                )
            ]
        )
        raise PlaygroundError(message) from exc

    cost = estimate_cost_usd(
        credential.provider,
        call.response_model,
        call.input_tokens,
        call.output_tokens,
        cached_input_tokens=call.cached_input_tokens,
        cache_write_tokens=call.cache_write_tokens,
        request_model=model,
    )

    writer.append(
        [
            _span(
                project_id=project_id,
                trace_id=trace_id,
                span_id=span_id,
                model=model,
                max_tokens=max_tokens,
                prompt=rendered,
                call=call,
                cost=cost,
                provider=credential.provider,
                credential_name=credential.name,
            )
        ]
    )

    # A model can legitimately return nothing — max_tokens hit on the first
    # token, or a stop sequence at position zero. score_span refuses empty
    # output, so say why rather than surfacing its error for a completion the
    # caller can see is blank.
    score_ids: list[str] = []
    judged_by: list[str] = []
    scoring_skipped = ""
    if scorers and not call.text.strip():
        scoring_skipped = "The model returned no text, so there was nothing to score."
    elif scorers:
        judged_by = sorted(
            {
                c.name
                for c in scoring.judge_credentials(
                    project_id, scorers, credential
                ).values()
            }
        )
        score_ids = scoring.score_span(
            project_id,
            trace_id=trace_id,
            span_id=span_id,
            input_text=rendered,
            output_text=call.text,
            scorer_ids=[s.id for s in scorers],
            writer=writer,
            # The *preferred* key, not the deciding one. score_span resolves a
            # judge key per scorer from here: this key when it can serve the
            # scorer's model, otherwise that vendor's default. Generating with
            # Grok and grading with Claude is the case that matters, and a
            # judge from the same family as the model it grades is the weakest
            # judge available.
            credential=credential,
            generation_credential=credential.name,
        )

    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "prompt": rendered,
        "output": call.text,
        "model": model,
        "response_model": call.response_model,
        "input_tokens": call.input_tokens,
        "output_tokens": call.output_tokens,
        "cost_usd": cost,
        "latency_ms": call.latency_ms,
        "credential_id": credential.id,
        "credential_name": credential.name,
        # Which key(s) paid for the judging, which need not be the one above.
        "judged_by": judged_by,
        "finish_reason": call.stop_reason,
        "score_ids": score_ids,
        "scoring_skipped": scoring_skipped,
    }


def _span(
    *,
    project_id: str,
    trace_id: str,
    span_id: str,
    model: str,
    max_tokens: int,
    prompt: str,
    call: llm.Completion | None,
    provider: str,
    credential_name: str,
    cost: float | None = None,
    error: str = "",
) -> Span:
    """The completion as a gen_ai span. One span, no root — it is the root.

    `call` is None on the failure path, where there is no usage to record and
    the timing is unknown; the span still carries the prompt and the error,
    which is the part worth keeping.
    """
    now = time.time_ns()
    start = call.start_nano if call else now
    end = call.end_nano if call else now

    return Span(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=None,
        name=f"chat {model}",
        start_time_unix_nano=start,
        end_time_unix_nano=end,
        status_code="ERROR" if error else "OK",
        status_message=error,
        project_id=project_id,
        service_name=SERVICE_NAME,
        gen_ai_operation_name="chat",
        gen_ai_provider_name=llm.provider_label(provider),
        gen_ai_request_model=model,
        gen_ai_response_model=call.response_model if call else None,
        gen_ai_response_id=call.response_id if call else None,
        gen_ai_request_max_tokens=max_tokens,
        gen_ai_usage_input_tokens=call.input_tokens if call else None,
        gen_ai_usage_output_tokens=call.output_tokens if call else None,
        gen_ai_finish_reasons=json.dumps([call.stop_reason]) if call else None,
        gen_ai_input_messages=prompt,
        gen_ai_output_messages=call.text if call else None,
        obs_cost_usd=cost,
        obs_latency_seconds=(end - start) / 1e9,
        attributes_json=json.dumps(
            {
                "obs.playground": True,
                "obs.credential": credential_name,
                # Cached / reasoning counts, present only when non-zero.
                **(llm.usage_attributes(call) if call else {}),
            }
        ),
    )
