"""FastAPI app: OTLP ingest, session login, read API.

Two auth paths, deliberately not unified (CLAUDE.md):

  - Ingest  -> Authorization: Bearer <api key>   (the SDK can't hold a cookie)
  - UI      -> obs_session cookie                (a browser shouldn't hold a
                                                  static ingest key)

Default-deny is enforced by middleware, not per-route decorators: a path not
in PUBLIC_PATHS with neither credential returns 401 before reaching the route,
so a new endpoint added without a dependency fails closed.
"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr

from obs_backend import datasets, guardrails, otlp, prompts, runner, scoring, sessions
from obs_backend.auth import AuthedKey, create_api_key, require_api_key
from obs_backend.config import get_settings
from obs_backend.db import close_pool, get_pool, init_schema
from obs_backend.guardrails import GuardrailError, GuardrailRateLimited
from obs_backend.prompts import PromptError
from obs_backend.query import TraceQuery
from obs_backend.runner import RunError
from obs_backend.scoring import ScorerError
from obs_backend.sessions import SESSION_COOKIE, SessionUser
from obs_backend.storage import build_storage
from obs_backend.wal import SpanWriter

# Reachable without any credential. Kept minimal — everything else is denied.
PUBLIC_PATHS = {
    "/health",
    "/api/auth/login",
    "/docs",
    "/openapi.json",
    "/redoc",
}

_settings = get_settings()
_storage = build_storage(_settings)
_writer = SpanWriter(_storage)
_query = TraceQuery(_storage)
_stop = threading.Event()
_admin_project_id = ""


def _background_loop() -> None:
    while not _stop.wait(_settings.wal_flush_seconds):
        try:
            _writer.compact()
        except Exception as exc:
            print(f"[compactor] error: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _admin_project_id
    _settings.require_anthropic_key()
    _settings.require_admin_credentials()
    _settings.check_password_strength()

    init_schema()
    sessions.seed_admin(_settings.admin_email, _settings.admin_password)
    sessions.purge_expired_sessions()

    from obs_backend.auth import ensure_project

    _admin_project_id = ensure_project("default")

    # A replay run lives on a thread, so anything still marked running at boot
    # died with the last process. Left alone it would poll forever in the UI.
    orphaned = runner.reconcile_orphans()
    if orphaned:
        print(f"[runner] marked {orphaned} interrupted run(s) as failed")

    # Scorers that predate step 5 have no version history. Give them one, so
    # every scorer has somewhere to append to on its next edit. No-op once done.
    linked = prompts.backfill_scorer_prompts()
    if linked:
        print(f"[prompts] gave {linked} existing scorer(s) a version history")

    thread = threading.Thread(target=_background_loop, daemon=True, name="compactor")
    thread.start()
    yield
    _stop.set()
    _writer.compact()
    close_pool()


app = FastAPI(title="obs-backend", version="0.2.0", lifespan=lifespan)


@app.middleware("http")
async def default_deny(request: Request, call_next):
    """Backstop: reject anything with neither credential.

    Route dependencies do the real validation. This exists so that forgetting
    one produces a 401 rather than an open endpoint.
    """
    path = request.url.path
    if path in PUBLIC_PATHS or request.method == "OPTIONS":
        return await call_next(request)

    has_bearer = bool(request.headers.get("authorization"))
    has_cookie = bool(request.cookies.get(SESSION_COOKIE))
    if not (has_bearer or has_cookie):
        return JSONResponse(
            status_code=401,
            content={"detail": "Authentication required"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await call_next(request)


# Registered AFTER default_deny on purpose. Starlette makes the last-added
# middleware the outermost, so this ordering puts CORS on the outside — which
# is required, because default_deny returns 401 directly without calling the
# rest of the stack. With CORS inside, those 401s carry no CORS headers, the
# browser blocks the response, and fetch() surfaces an opaque network error
# instead of the status. The UI then reports "can't reach the backend" for what
# is really just "not signed in".
#
# Credentials must be allowed for the session cookie to cross origins, and the
# allowlist must be explicit — browsers reject a wildcard with credentials.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------


def require_session(
    obs_session: Annotated[str | None, Cookie()] = None,
) -> SessionUser:
    """UI auth. Rejects API keys — a browser session is a different thing."""
    if not obs_session:
        raise HTTPException(status_code=401, detail="Not signed in")
    user = sessions.resolve_session(obs_session)
    if user is None:
        raise HTTPException(status_code=401, detail="Session expired or invalid")
    return user


def require_any_auth(
    request: Request,
    obs_session: Annotated[str | None, Cookie()] = None,
) -> str:
    """Read endpoints accept either path and return the project id.

    The UI reads with a cookie; the SDK and scripts read with a key. Both
    resolve to a project, which is all the read layer needs.
    """
    if obs_session:
        user = sessions.resolve_session(obs_session)
        if user is not None:
            return _admin_project_id

    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token:
        from obs_backend.auth import _lookup

        authed = _lookup(token.strip())
        if authed is not None:
            return authed.project_id

    raise HTTPException(status_code=401, detail="Not authenticated")


# --------------------------------------------------------------------------
# Auth routes
# --------------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


@app.post("/api/auth/login")
def login(body: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    ip = request.client.host if request.client else "unknown"

    if sessions.rate_limited(ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed attempts. Try again in 15 minutes.",
        )

    user = sessions.authenticate(body.email, body.password)
    sessions.record_attempt(ip, succeeded=user is not None)

    if user is None:
        # One message for both wrong-email and wrong-password: distinguishing
        # them tells an attacker which emails exist.
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = sessions.create_session(
        user.user_id, user_agent=request.headers.get("user-agent", "")
    )
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,  # unreadable from JS, so XSS can't exfiltrate it
        secure=_settings.cookie_secure,
        samesite=_settings.cookie_samesite,  # type: ignore[arg-type]
        max_age=int(sessions.SESSION_TTL.total_seconds()),
        path="/",
    )
    return {"email": user.email}


@app.post("/api/auth/logout")
def logout(
    response: Response, obs_session: Annotated[str | None, Cookie()] = None
) -> dict[str, str]:
    """Deletes the server-side row, not just the cookie.

    Clearing only the cookie would leave a session that still works if the
    value was captured.
    """
    if obs_session:
        sessions.destroy_session(obs_session)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "signed out"}


@app.get("/api/auth/me")
def whoami(user: Annotated[SessionUser, Depends(require_session)]) -> dict[str, str]:
    return {"email": user.email, "user_id": user.user_id}


# --------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "storage": _settings.storage_backend, "pending_spans": _writer.pending}


@app.post("/v1/traces")
async def ingest_traces(
    request: Request, key: Annotated[AuthedKey, Depends(require_api_key)]
) -> Response:
    """OTLP/HTTP ingest. project_id comes from the key, never the payload."""
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty request body")

    try:
        spans = otlp.decode(body, project_id=key.project_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Malformed OTLP payload: {exc}") from exc

    written = _writer.append(spans)
    if _writer.pending >= _settings.wal_flush_spans:
        _writer.compact()

    return Response(
        status_code=200,
        content=b"",
        media_type="application/x-protobuf",
        headers={"X-Obs-Spans-Accepted": str(written)},
    )


# --------------------------------------------------------------------------
# Read API
# --------------------------------------------------------------------------


@app.get("/api/traces")
def list_traces(
    project_id: Annotated[str, Depends(require_any_auth)], limit: int = 50
) -> dict[str, Any]:
    traces = _query.list_traces(project_id, limit=min(limit, 500))
    return {"traces": traces, "count": len(traces)}


@app.get("/api/overview")
def get_overview(
    project_id: Annotated[str, Depends(require_any_auth)], hours: int = 24
) -> dict[str, Any]:
    # Capped: the series is one point per hour and the dashboard draws it in a
    # fixed-width chart, so a request for a year of buckets would render as an
    # unreadable smear and scan every Parquet file to build it.
    return _query.overview(project_id, hours=max(1, min(hours, 168)))


@app.get("/api/traces/{trace_id}")
def get_trace(
    trace_id: str, project_id: Annotated[str, Depends(require_any_auth)]
) -> dict[str, Any]:
    spans = _query.get_trace(project_id, trace_id)
    if not spans:
        raise HTTPException(status_code=404, detail=f"No trace {trace_id}")
    return {
        "trace_id": trace_id,
        "spans": spans,
        "span_count": len(spans),
        "cost_usd": sum(s.get("obs_cost_usd") or 0 for s in spans),
        # Scores live in Postgres, not in the span store: a score is attached
        # to a span after the fact, and Parquet is append-only.
        "scores": scoring.scores_for_trace(project_id, trace_id),
    }


# --------------------------------------------------------------------------
# API key management (UI session only — not reachable with an API key, so a
# leaked ingest key cannot mint more keys for itself)
# --------------------------------------------------------------------------


class CreateKeyRequest(BaseModel):
    name: str


@app.get("/api/keys")
def list_keys(user: Annotated[SessionUser, Depends(require_session)]) -> dict[str, Any]:
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT id, name, key_prefix, created_at, last_used_at, revoked_at "
            "FROM api_keys WHERE project_id = %s ORDER BY created_at DESC",
            (_admin_project_id,),
        ).fetchall()
    return {
        "keys": [
            {
                "id": str(r[0]),
                "name": r[1],
                "prefix": r[2],
                "created_at": r[3].isoformat() if r[3] else None,
                "last_used_at": r[4].isoformat() if r[4] else None,
                "revoked": r[5] is not None,
            }
            for r in rows
        ]
    }


@app.post("/api/keys")
def create_key(
    body: CreateKeyRequest, user: Annotated[SessionUser, Depends(require_session)]
) -> dict[str, str]:
    """Returns the plaintext exactly once. Only a hash is stored."""
    plaintext = create_api_key(_admin_project_id, body.name)
    return {"key": plaintext, "name": body.name}


@app.delete("/api/keys/{key_id}")
def revoke_key(
    key_id: str, user: Annotated[SessionUser, Depends(require_session)]
) -> dict[str, str]:
    with get_pool().connection() as conn:
        conn.execute(
            "UPDATE api_keys SET revoked_at = now() WHERE id = %s AND project_id = %s",
            (key_id, _admin_project_id),
        )
    return {"status": "revoked"}


@app.post("/api/admin/compact")
def force_compact(project_id: Annotated[str, Depends(require_any_auth)]) -> dict[str, int]:
    return {"compacted": _writer.compact()}


# --------------------------------------------------------------------------
# Datasets (step 3)
#
# Reads accept either credential, same as the trace read API — a script that
# holds an ingest key should be able to push test cases without a browser
# session. Writes that spend money (runs) are session-only: an ingest key is
# handed to instrumented applications, and one leaking should not also mean
# someone can bill the account by starting replays with it.
# --------------------------------------------------------------------------


class CreateDatasetRequest(BaseModel):
    name: str
    description: str = ""


class CreateItemRequest(BaseModel):
    input: str
    expected_output: str | None = None
    source_trace_id: str = ""
    source_span_id: str = ""


@app.get("/api/datasets")
def list_datasets(
    project_id: Annotated[str, Depends(require_any_auth)],
) -> dict[str, Any]:
    return {"datasets": datasets.list_datasets(project_id)}


@app.post("/api/datasets", status_code=201)
def create_dataset(
    body: CreateDatasetRequest,
    project_id: Annotated[str, Depends(require_any_auth)],
) -> dict[str, Any]:
    try:
        ds = datasets.create_dataset(project_id, body.name, body.description)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": ds.id, "name": ds.name, "description": ds.description}


def _require_dataset(project_id: str, dataset_id: str) -> dict[str, Any]:
    ds = datasets.get_dataset(project_id, dataset_id)
    if ds is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return ds


@app.get("/api/datasets/{dataset_id}")
def get_dataset(
    dataset_id: str, project_id: Annotated[str, Depends(require_any_auth)]
) -> dict[str, Any]:
    ds = _require_dataset(project_id, dataset_id)
    return {
        **ds,
        "items": datasets.list_items(dataset_id),
        "runs": runner.list_runs(project_id, dataset_id=dataset_id),
    }


@app.delete("/api/datasets/{dataset_id}")
def delete_dataset(
    dataset_id: str, project_id: Annotated[str, Depends(require_any_auth)]
) -> dict[str, str]:
    if not datasets.delete_dataset(project_id, dataset_id):
        raise HTTPException(status_code=404, detail="Dataset not found")
    return {"status": "deleted"}


@app.post("/api/datasets/{dataset_id}/items", status_code=201)
def add_dataset_item(
    dataset_id: str,
    body: CreateItemRequest,
    project_id: Annotated[str, Depends(require_any_auth)],
) -> dict[str, str]:
    """Add a test case. This is the 'save a trace as a test case' endpoint.

    The trace-derived path differs only in carrying source_trace_id and
    source_span_id — the UI reads the input off a span and posts it here, so
    there is one code path for captured and hand-written cases.
    """
    _require_dataset(project_id, dataset_id)
    try:
        item_id = datasets.add_item(
            dataset_id,
            body.input,
            body.expected_output,
            body.source_trace_id,
            body.source_span_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": item_id}


@app.delete("/api/datasets/{dataset_id}/items/{item_id}")
def delete_dataset_item(
    dataset_id: str,
    item_id: str,
    project_id: Annotated[str, Depends(require_any_auth)],
) -> dict[str, str]:
    _require_dataset(project_id, dataset_id)
    if not datasets.delete_item(dataset_id, item_id):
        raise HTTPException(status_code=404, detail="Test case not found")
    return {"status": "deleted"}


# --------------------------------------------------------------------------
# Replay runs (step 3)
# --------------------------------------------------------------------------


class CreateRunRequest(BaseModel):
    dataset_id: str
    # Ad-hoc prompt text. Ignored when a saved prompt is referenced below —
    # the version's own text is what runs, because "v3 but with edits" is a
    # thing that would be recorded as v3 and would not be v3.
    prompt_template: str = ""
    model: str = "claude-sonnet-5"
    max_tokens: int = 1024
    name: str = ""
    # Scorers to apply automatically once the replay finishes (step 4). Empty
    # means replay only; scoring can still be requested later against the
    # finished run, which is what you want when the scorer didn't exist yet.
    scorer_ids: list[str] = []
    # Reference a saved prompt (step 5). Either a specific version, or a prompt
    # plus an optional label — no label means its latest version. Whichever way
    # in, the run records the version it resolved to.
    prompt_id: str | None = None
    prompt_version_id: str | None = None
    prompt_label: str = ""


@app.get("/api/runs")
def list_runs(
    project_id: Annotated[str, Depends(require_any_auth)],
    dataset_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    return {"runs": runner.list_runs(project_id, dataset_id, min(limit, 200))}


@app.post("/api/runs", status_code=202)
def create_run(
    body: CreateRunRequest,
    user: Annotated[SessionUser, Depends(require_session)],
) -> dict[str, Any]:
    """Start a replay. Returns 202 with the run — results arrive by polling.

    Sequential LLM calls over a whole dataset run for minutes, well past any
    HTTP timeout, so this cannot be a blocking call. Validation still happens
    synchronously: a bad prompt template is a 400 here, not a failed run you
    discover on the next poll.
    """
    try:
        run = runner.create_run(
            project_id=_admin_project_id,
            dataset_id=body.dataset_id,
            name=body.name,
            prompt_template=body.prompt_template,
            model=body.model,
            max_tokens=body.max_tokens,
            scorer_ids=body.scorer_ids,
            prompt_id=body.prompt_id,
            prompt_version_id=body.prompt_version_id,
            prompt_label=body.prompt_label,
        )
    except (RunError, ScorerError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    runner.start_run(run["id"], _writer)
    return run


@app.get("/api/runs/{run_id}")
def get_run(
    run_id: str, project_id: Annotated[str, Depends(require_any_auth)]
) -> dict[str, Any]:
    run = runner.get_run(project_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")

    scores = scoring.scores_for_run(run_id)
    by_item: dict[str, list[dict[str, Any]]] = {}
    for score in scores:
        by_item.setdefault(score["run_item_id"] or "", []).append(score)

    return {
        **run,
        "items": [
            {**item, "scores": by_item.get(item["id"], [])}
            for item in runner.get_run_items(run_id)
        ],
        "score_summary": scoring.summarize(scores),
    }


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(
    run_id: str, user: Annotated[SessionUser, Depends(require_session)]
) -> dict[str, str]:
    """Stop a run in flight. Checked between items, so in-flight calls finish."""
    if not runner.cancel_run(_admin_project_id, run_id):
        raise HTTPException(status_code=400, detail="Run is not cancellable")
    return {"status": "cancelling"}


# --------------------------------------------------------------------------
# Scorers (step 4)
#
# Same split as datasets: defining a scorer costs nothing and accepts either
# credential, while anything that invokes a judge is session-only. Every
# scoring route spends money on the account's key, and an ingest key handed to
# an instrumented application should not be able to run up a judge bill.
# --------------------------------------------------------------------------


class ScorerRequest(BaseModel):
    name: str
    prompt_template: str
    output_type: str = "boolean"
    description: str = ""
    model: str = "claude-sonnet-5"
    max_tokens: int = 1024
    score_min: float | None = None
    score_max: float | None = None
    categories: list[str] = []
    pass_threshold: float | None = None
    # Why this edit — recorded on the version it creates (step 5). Ignored on
    # create, where the note is always "Created."
    note: str = ""


class TryScorerRequest(BaseModel):
    output: str
    input: str = ""
    expected: str | None = None


class ScoreRequest(BaseModel):
    scorer_ids: list[str]


@app.get("/api/scorers")
def list_scorers(
    project_id: Annotated[str, Depends(require_any_auth)],
) -> dict[str, Any]:
    return {"scorers": scoring.list_scorers(project_id)}


@app.post("/api/scorers", status_code=201)
def create_scorer(
    body: ScorerRequest, project_id: Annotated[str, Depends(require_any_auth)]
) -> dict[str, Any]:
    try:
        scorer_id = scoring.create_scorer(
            project_id,
            name=body.name,
            description=body.description,
            prompt_template=body.prompt_template,
            model=body.model,
            max_tokens=body.max_tokens,
            output_type=body.output_type,
            score_min=body.score_min,
            score_max=body.score_max,
            categories=body.categories,
            pass_threshold=body.pass_threshold,
        )
    except ScorerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": scorer_id}


@app.get("/api/scorers/{scorer_id}")
def get_scorer(
    scorer_id: str, project_id: Annotated[str, Depends(require_any_auth)]
) -> dict[str, Any]:
    scorer = scoring.get_scorer(project_id, scorer_id)
    if scorer is None:
        raise HTTPException(status_code=404, detail="Scorer not found")
    return scoring.scorer_dict(scorer)


@app.patch("/api/scorers/{scorer_id}")
def update_scorer(
    scorer_id: str,
    body: ScorerRequest,
    project_id: Annotated[str, Depends(require_any_auth)],
) -> dict[str, str]:
    try:
        updated = scoring.update_scorer(
            project_id,
            scorer_id,
            name=body.name,
            description=body.description,
            prompt_template=body.prompt_template,
            model=body.model,
            max_tokens=body.max_tokens,
            output_type=body.output_type,
            score_min=body.score_min,
            score_max=body.score_max,
            categories=body.categories,
            pass_threshold=body.pass_threshold,
            note=body.note,
        )
    except ScorerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="Scorer not found")
    return {"status": "updated"}


@app.delete("/api/scorers/{scorer_id}")
def archive_scorer(
    scorer_id: str, project_id: Annotated[str, Depends(require_any_auth)]
) -> dict[str, str]:
    """Archive, not delete. Scores it produced keep their name and stay readable."""
    if not scoring.archive_scorer(project_id, scorer_id):
        raise HTTPException(status_code=404, detail="Scorer not found")
    return {"status": "archived"}


@app.post("/api/scorers/{scorer_id}/try")
def try_scorer(
    scorer_id: str,
    body: TryScorerRequest,
    user: Annotated[SessionUser, Depends(require_session)],
) -> dict[str, Any]:
    """One judge call against text you paste in. Synchronous, not persisted.

    Blocking is fine here where it isn't for a run: this is a single call, so
    it returns in seconds rather than minutes, and the whole point is a tight
    edit-try-edit loop on a new scorer before committing to a paid run.
    """
    try:
        return scoring.try_scorer(
            _admin_project_id,
            scorer_id,
            input_text=body.input,
            output_text=body.output,
            expected=body.expected,
            writer=_writer,
        )
    except ScorerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/runs/{run_id}/score", status_code=202)
def score_run(
    run_id: str,
    body: ScoreRequest,
    user: Annotated[SessionUser, Depends(require_session)],
) -> dict[str, Any]:
    """Score a finished run. Returns immediately; the UI polls for results."""
    try:
        calls = scoring.score_run(_admin_project_id, run_id, body.scorer_ids, _writer)
    except ScorerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "scoring", "judge_calls": calls}


@app.post("/api/traces/{trace_id}/spans/{span_id}/score", status_code=202)
def score_span(
    trace_id: str,
    span_id: str,
    body: ScoreRequest,
    user: Annotated[SessionUser, Depends(require_session)],
) -> dict[str, Any]:
    """Score one span of a trace — the production-traffic scoring path.

    The text comes from the span store here rather than from the request body.
    Letting the client post the text would mean the score attached to a span
    need not be a score *of* that span, which is exactly the kind of quiet
    disconnect an observability tool cannot afford.
    """
    spans = _query.get_trace(_admin_project_id, trace_id)
    span = next((s for s in spans if s["span_id"] == span_id), None)
    if span is None:
        raise HTTPException(status_code=404, detail="Span not found")

    try:
        score_ids = scoring.score_span(
            _admin_project_id,
            trace_id=trace_id,
            span_id=span_id,
            input_text=span.get("gen_ai_input_messages") or "",
            output_text=span.get("gen_ai_output_messages") or "",
            scorer_ids=body.scorer_ids,
            writer=_writer,
        )
    except ScorerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "scoring", "score_ids": score_ids}


# --------------------------------------------------------------------------
# Prompts (step 5)
#
# Either credential, like datasets and scorer definitions: writing a prompt
# spends nothing, and a script holding an ingest key should be able to push a
# new version from CI. The endpoints that spend money are still session-only —
# a prompt version is inert until a run references it.
# --------------------------------------------------------------------------


class CreatePromptRequest(BaseModel):
    name: str
    template: str
    description: str = ""
    # Model and max_tokens travel with the text so a version is a complete
    # answer to "what did this run?", not a fragment of one.
    config: dict[str, Any] = {}
    note: str = ""


class NewVersionRequest(BaseModel):
    template: str
    config: dict[str, Any] = {}
    note: str = ""


class UpdatePromptRequest(BaseModel):
    name: str
    description: str = ""


class SetLabelRequest(BaseModel):
    version_id: str


@app.get("/api/prompts")
def list_prompts(
    project_id: Annotated[str, Depends(require_any_auth)], kind: str | None = None
) -> dict[str, Any]:
    return {
        "prompts": prompts.list_prompts(project_id, kind),
        "suggested_labels": list(prompts.SUGGESTED_LABELS),
    }


@app.post("/api/prompts", status_code=201)
def create_prompt(
    body: CreatePromptRequest, project_id: Annotated[str, Depends(require_any_auth)]
) -> dict[str, str]:
    try:
        return prompts.create_prompt(
            project_id,
            name=body.name,
            description=body.description,
            kind="completion",
            template=body.template,
            config=body.config,
            note=body.note,
        )
    except PromptError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/prompts/{prompt_id}")
def get_prompt(
    prompt_id: str, project_id: Annotated[str, Depends(require_any_auth)]
) -> dict[str, Any]:
    prompt = prompts.get_prompt(project_id, prompt_id)
    if prompt is None:
        raise HTTPException(status_code=404, detail="Prompt not found")
    return prompt


@app.patch("/api/prompts/{prompt_id}")
def update_prompt(
    prompt_id: str,
    body: UpdatePromptRequest,
    project_id: Annotated[str, Depends(require_any_auth)],
) -> dict[str, str]:
    """Rename. Deliberately not a version — see prompts.rename_prompt_tx."""
    try:
        updated = prompts.update_prompt(
            project_id, prompt_id, name=body.name, description=body.description
        )
    except PromptError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="Prompt not found")
    return {"status": "updated"}


@app.delete("/api/prompts/{prompt_id}")
def archive_prompt(
    prompt_id: str, project_id: Annotated[str, Depends(require_any_auth)]
) -> dict[str, str]:
    """Archive, not delete. Runs that cite a version stay correct and readable."""
    if not prompts.archive_prompt(project_id, prompt_id):
        raise HTTPException(status_code=404, detail="Prompt not found")
    return {"status": "archived"}


@app.post("/api/prompts/{prompt_id}/versions", status_code=201)
def add_prompt_version(
    prompt_id: str,
    body: NewVersionRequest,
    project_id: Annotated[str, Depends(require_any_auth)],
) -> dict[str, Any]:
    """Append a version. Saving unchanged content returns the existing one.

    201 either way, with `created` saying which happened. A 409 would be
    technically defensible and practically annoying: the caller's intent —
    "make sure this text is the head of the chain" — is satisfied in both cases.
    """
    try:
        return prompts.add_version(
            project_id,
            prompt_id,
            template=body.template,
            config=body.config,
            note=body.note,
        )
    except PromptError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/prompts/{prompt_id}/diff")
def diff_prompt(
    prompt_id: str,
    from_version: str,
    to_version: str,
    project_id: Annotated[str, Depends(require_any_auth)],
) -> dict[str, Any]:
    """Line diff plus config changes between two versions of one prompt."""
    try:
        return prompts.diff_versions(project_id, from_version, to_version)
    except PromptError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/prompts/{prompt_id}/labels/{label}")
def set_prompt_label(
    prompt_id: str,
    label: str,
    body: SetLabelRequest,
    project_id: Annotated[str, Depends(require_any_auth)],
) -> dict[str, str]:
    """Point a label at a version, moving it if it already exists.

    Idempotent by construction — an upsert keyed on (prompt, label) — which is
    what makes PUT the right verb and makes a double-click harmless.
    """
    try:
        applied = prompts.set_label(project_id, prompt_id, label, body.version_id)
    except PromptError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "set", "label": applied}


@app.delete("/api/prompts/{prompt_id}/labels/{label}")
def delete_prompt_label(
    prompt_id: str, label: str, project_id: Annotated[str, Depends(require_any_auth)]
) -> dict[str, str]:
    if not prompts.remove_label(project_id, prompt_id, label):
        raise HTTPException(status_code=404, detail="Label not found")
    return {"status": "removed"}


# --------------------------------------------------------------------------
# Guardrails (step 6)
#
# The evaluation endpoint deliberately breaks the rule the last three steps
# followed. Everywhere else, an endpoint that spends money on the Anthropic key
# is session-only, so a leaked ingest key cannot run up a bill. This one has to
# accept an ingest key, because the thing calling it is the instrumented
# application deciding whether to show a response to a user — and that
# application holds a key, not a cookie. Making it session-only would leave the
# endpoint unusable by the only caller it exists for.
#
# What replaces the missing protection is a rate cap rather than a trust
# boundary: OBS_MAX_GUARDRAIL_CALLS_PER_MIN bounds the spend a stolen key can
# reach per minute, where the eval-run caps bound a single job. That is a
# weaker guarantee and worth knowing about — a key that screens responses is a
# key that can be made to spend, and the honest mitigation is a separate
# guardrail-scoped key, which is a step-7 concern.
#
# Managing guardrails is the usual split: definitions cost nothing and take
# either credential; nothing here except /v1/guardrail invokes a judge.
# --------------------------------------------------------------------------


class GuardrailRequest(BaseModel):
    name: str
    scorer_id: str
    description: str = ""
    # block | flag. `flag` is shadow mode — the judge runs and the result is
    # reported, but the decision is never block.
    action: str = "block"
    # Categorical scorers only: which labels count as a trigger.
    block_labels: list[str] = []
    # allow | block — what a judge that errored or timed out means.
    on_error: str = "allow"
    enabled: bool = True


class CheckRequest(BaseModel):
    """What to screen. `output` is the text a user would see."""

    output: str
    # The prompt or question behind it, if the scorer's template asks for one.
    # Optional because plenty of safety scorers judge the output alone.
    input: str = ""
    # Free-text caller label, e.g. "support-bot", for reading the log per call
    # site. Truncated server-side rather than validated — it is a log field.
    source: str = ""
    # Empty means every enabled guardrail, which is what an application wants.
    # Naming a subset is for testing one rule without disabling the rest.
    guardrail_ids: list[str] = []


@app.post("/v1/guardrail")
def check_guardrails(
    body: CheckRequest, project_id: Annotated[str, Depends(require_any_auth)]
) -> dict[str, Any]:
    """Screen an output and return pass/block. Synchronous by necessity.

    Under `/v1/` beside the ingest endpoint rather than under `/api/`: both are
    called by the instrumented application with a bearer key, while `/api/` is
    the browser's read and management surface. The path says which audience an
    endpoint is for.

    Every reply is 200, including a block. A block is this endpoint answering
    the question correctly, not the request failing — returning 403 would make
    "the guardrail blocked it" indistinguishable from "your key is wrong" in
    every HTTP client's default error handling.
    """
    try:
        return guardrails.evaluate(
            project_id,
            output=body.output,
            input_text=body.input,
            source=body.source,
            guardrail_ids=body.guardrail_ids,
            writer=_writer,
        )
    except GuardrailRateLimited as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)
        ) from exc
    except (GuardrailError, ScorerError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/guardrails")
def list_guardrails(
    project_id: Annotated[str, Depends(require_any_auth)],
) -> dict[str, Any]:
    return {
        "guardrails": guardrails.list_guardrails(project_id),
        "stats": guardrails.stats(project_id),
    }


@app.get("/api/guardrails/checks")
def list_guardrail_checks(
    project_id: Annotated[str, Depends(require_any_auth)],
    limit: int = 50,
    decision: str | None = None,
) -> dict[str, Any]:
    """The check log — what was screened, what fired, and what it cost."""
    return {"checks": guardrails.list_checks(project_id, limit=limit, decision=decision)}


@app.post("/api/guardrails", status_code=201)
def create_guardrail(
    body: GuardrailRequest, project_id: Annotated[str, Depends(require_any_auth)]
) -> dict[str, str]:
    try:
        guardrail_id = guardrails.create_guardrail(
            project_id,
            name=body.name,
            scorer_id=body.scorer_id,
            description=body.description,
            action=body.action,
            block_labels=body.block_labels,
            on_error=body.on_error,
            enabled=body.enabled,
        )
    except GuardrailError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": guardrail_id}


@app.patch("/api/guardrails/{guardrail_id}")
def update_guardrail(
    guardrail_id: str,
    body: GuardrailRequest,
    project_id: Annotated[str, Depends(require_any_auth)],
) -> dict[str, str]:
    try:
        updated = guardrails.update_guardrail(
            project_id,
            guardrail_id,
            name=body.name,
            scorer_id=body.scorer_id,
            description=body.description,
            action=body.action,
            block_labels=body.block_labels,
            on_error=body.on_error,
            enabled=body.enabled,
        )
    except GuardrailError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not updated:
        raise HTTPException(status_code=404, detail="Guardrail not found")
    return {"status": "updated"}


@app.delete("/api/guardrails/{guardrail_id}")
def archive_guardrail(
    guardrail_id: str, project_id: Annotated[str, Depends(require_any_auth)]
) -> dict[str, str]:
    """Archive, not delete. The checks it decided stay readable and attributable."""
    if not guardrails.archive_guardrail(project_id, guardrail_id):
        raise HTTPException(status_code=404, detail="Guardrail not found")
    return {"status": "archived"}
