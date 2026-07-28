"""Client for the backend's guardrail endpoint — step 6.

The endpoint screens an output and answers pass/block. This is the twenty lines
of HTTP that stand between it and an application, so that using it looks like:

    from obs_sdk import guard

    answer = call_the_model(question)
    verdict = guard(output=answer, input=question, source="support-bot")
    if verdict.blocked:
        answer = "I can't help with that."

Three things are worth knowing.

**No new dependency.** One POST of one JSON object, so this is urllib from the
standard library rather than httpx. `anthropic` happens to pull httpx in today,
which is a reason to be able to use it and not a reason to depend on it.

**It raises when the backend is unreachable, by default.** A screening step
that silently passes when it never ran is the failure this whole project exists
to make visible, so it is not the default. `fail_open=True` is there for
callers who would rather serve unscreened than not serve, and it says so at the
call site rather than in a config file.

That is different from a *judge* failing, which the backend handles itself
under each guardrail's on_error policy and reports as `degraded`. The
difference matters: degraded means the guardrail ran and one judge didn't
answer; unreachable means nothing ran at all.

**A block is a 200.** The decision is the answer to the question asked, not a
failed request, so nothing here treats it as an error.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_GUARDRAIL_ENDPOINT = "http://localhost:8000/v1/guardrail"

# Slightly above the backend's own per-judge timeout (10s), so a judge timing
# out server-side produces a `degraded` decision here rather than this socket
# giving up first and reporting the whole backend unreachable.
DEFAULT_TIMEOUT_SECONDS = 15.0


class GuardrailUnavailable(RuntimeError):
    """The endpoint could not be reached, so nothing was screened."""


@dataclass(frozen=True)
class GuardrailDecision:
    """What the endpoint decided, and everything behind it."""

    decision: str  # "pass" | "block"
    blocked: bool
    # Names of guardrails that fired. `triggered` blocks, `flagged` is shadow
    # mode and is information only.
    triggered: list[str] = field(default_factory=list)
    flagged: list[str] = field(default_factory=list)
    # True when at least one judge errored or timed out, so this decision was
    # reached with less information than it looks like.
    degraded: bool = False
    latency_ms: float = 0.0
    cost_usd: float = 0.0
    check_id: str = ""
    # The check's own trace, so a decision in production links to the judge
    # calls that produced it.
    trace_id: str = ""
    results: list[dict[str, Any]] = field(default_factory=list)

    def reasons(self) -> list[str]:
        """Why it blocked, in the judge's own words.

        Usually what you want when turning a block into a message: "blocked"
        with no explanation is not something a person or a log can act on.
        """
        return [
            f"{r['guardrail_name']}: {r['reasoning']}"
            for r in self.results
            if r.get("triggered") and r.get("action") == "block"
        ]


def guard(
    *,
    output: str,
    input: str = "",
    source: str = "",
    guardrail_ids: list[str] | None = None,
    endpoint: str | None = None,
    api_key: str | None = None,
    timeout: float | None = None,
    fail_open: bool = False,
) -> GuardrailDecision:
    """Screen `output` against the project's enabled guardrails.

    `input` is the prompt behind the output, substituted into any scorer
    template that asks for it. `source` is a free-text caller label that shows
    up in the check log, which is how a shared backend's log stays readable
    when three services are calling it.

    Raises GuardrailUnavailable if the backend cannot be reached, unless
    `fail_open` is set — in which case the returned decision is a pass with
    `degraded` set, so a caller that logs the field still sees that screening
    did not happen.
    """
    endpoint = endpoint or os.environ.get(
        "OBS_GUARDRAIL_ENDPOINT", DEFAULT_GUARDRAIL_ENDPOINT
    )
    api_key = api_key or os.environ.get("OBS_API_KEY", "")
    if not api_key:
        raise GuardrailUnavailable(
            "OBS_API_KEY is not set. The guardrail endpoint authenticates with the "
            "same ingest key the exporter uses — create one with the backend's "
            "scripts/create_key.py and put it in .env."
        )

    payload = json.dumps(
        {
            "output": output,
            "input": input,
            "source": source,
            "guardrail_ids": guardrail_ids or [],
        }
    ).encode()

    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request, timeout=timeout or DEFAULT_TIMEOUT_SECONDS
        ) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        # A 4xx is the backend answering, not failing to answer, so it is
        # surfaced with its message rather than swallowed by fail_open. A bad
        # key or an over-cap 429 is a bug to fix, not traffic to wave through.
        detail = exc.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except (ValueError, AttributeError):
            pass
        raise GuardrailUnavailable(f"Guardrail endpoint returned {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        if fail_open:
            return GuardrailDecision(
                decision="pass", blocked=False, degraded=True, results=[]
            )
        raise GuardrailUnavailable(
            f"Could not reach the guardrail endpoint at {endpoint}: {exc}. "
            "Nothing was screened. Pass fail_open=True to serve unscreened output "
            "instead of raising."
        ) from exc

    return GuardrailDecision(
        decision=body.get("decision", "pass"),
        blocked=bool(body.get("blocked")),
        triggered=list(body.get("triggered", [])),
        flagged=list(body.get("flagged", [])),
        degraded=bool(body.get("degraded")),
        latency_ms=float(body.get("latency_ms") or 0.0),
        cost_usd=float(body.get("cost_usd") or 0.0),
        check_id=body.get("check_id", ""),
        trace_id=body.get("trace_id", ""),
        results=list(body.get("results", [])),
    )
