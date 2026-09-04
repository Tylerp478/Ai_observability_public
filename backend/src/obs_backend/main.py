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
from urllib.parse import quote

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr

from obs_backend import (
    credentials,
    datasets,
    guardrails,
    llm,
    otlp,
    playground,
    projects,
    prompts,
    runner,
    scoring,
    sessions,
)
from obs_backend.auth import AuthedKey, create_api_key, require_api_key
from obs_backend.config import get_settings
from obs_backend.credentials import CredentialError
from obs_backend.db import close_pool, get_pool, init_schema
from obs_backend.guardrails import GuardrailError, GuardrailRateLimited
from obs_backend.playground import PlaygroundError
from obs_backend.projects import ProjectError
from obs_backend.prompts import PromptError
from obs_backend.query import TraceQuery
from obs_backend.runner import RunError
from obs_backend.scoring import ScorerError
from obs_backend.sessions import SESSION_COOKIE, SessionUser
from obs_sdk.pricing import PRICING, price_tier
from obs_backend.storage import build_storage
from obs_backend.wal import SpanWriter

# Reachable without any credential. Kept minimal — everything else is denied.
PUBLIC_PATHS = {
    "/health",
    # Redeeming an invite necessarily happens before there is a session. Both
    # are gated on holding an unexpired single-use token, which is the
    # credential here.
    "/api/auth/invite",
    "/api/auth/accept",
    "/api/auth/login",
    "/docs",
    "/openapi.json",
    "/redoc",
}

# Writes a viewer must still be able to make. Exactly one: signing out is not
# a privilege, and a read-only user trapped in a session they cannot end would
# be a worse outcome than anything this gate prevents.
WRITE_EXEMPT_PATHS = PUBLIC_PATHS | {"/api/auth/logout", "/api/auth/theme"}

_settings = get_settings()
_storage = build_storage(_settings)
_writer = SpanWriter(_storage)
_query = TraceQuery(_storage)
_stop = threading.Event()
# The project a request lands in when it names none: a fresh install, a
# pre-projects client, or the SDK's own bootstrap. Resolved once at boot.
_default_project_id = ""


def _background_loop() -> None:
    while not _stop.wait(_settings.wal_flush_seconds):
        try:
            _writer.compact()
        except Exception as exc:
            print(f"[compactor] error: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _default_project_id
    _settings.require_secret_key()
    _settings.check_password_strength()
    _settings.check_cookie_security()

    init_schema()

    # ADMIN_EMAIL/ADMIN_PASSWORD are a **bootstrap**, not a permanent fixture.
    # While they are set they are authoritative: the password is re-hashed from
    # .env on every boot and that address is re-asserted as an un-revoked
    # admin, which is the escape hatch if the allowlist is ever edited into a
    # state nobody can sign in from.
    #
    # Once a real admin exists — someone who accepted an invite and chose their
    # own password — they can be removed from .env, and then the only copy of
    # anyone's password is the argon2 hash in the database. Putting them back
    # and restarting always works, because this runs every boot.
    if _settings.admin_email and _settings.admin_password:
        sessions.seed_admin(_settings.admin_email, _settings.admin_password)
    elif not sessions.has_active_admin():
        raise RuntimeError(
            "No admin exists and ADMIN_EMAIL/ADMIN_PASSWORD are not set.\n"
            "\n"
            "The UI has no way to create the first user, so set both in the "
            "repo-root .env and restart. They can be removed again once "
            "someone has accepted an admin invite."
        )

    sessions.purge_expired_sessions()

    from obs_backend.auth import ensure_project

    _default_project_id = ensure_project("default")

    # The .env key becomes the first stored credential, so an existing install
    # keeps working without anyone having to visit the UI first.
    if credentials.seed_from_env(_default_project_id):
        print("[credentials] adopted ANTHROPIC_API_KEY from .env as the default key")

    # Scores and runs from before keys existed belong to the one key that could
    # have made them. No-op once a second key exists.
    attributed = credentials.backfill_generation_credential(_default_project_id)
    if attributed:
        print(f"[credentials] attributed {attributed} earlier score(s) to the only key")

    # After seeding, not before: an install whose only key is in .env has one
    # by this point, and warning about it would be a lie.
    credentials.warn_if_no_keys(_default_project_id)

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

    # Roles are enforced here rather than as a dependency on each write route,
    # for the same reason the 401 above is: a route added later without the
    # right decorator must fail closed, not open. Thirty-odd write routes each
    # needing to remember `Depends(require_admin)` is thirty chances to forget.
    #
    # **Only on writes.** A viewer reads everything, so gating GETs would buy
    # nothing and would put a second session lookup in front of every poll on
    # a live dashboard. Mutations are rare, so the extra query lands where it
    # costs nothing.
    #
    # Bearer-authenticated requests are untouched: an ingest key is scoped by
    # its project, has no role, and POSTing spans is its whole purpose.
    if (
        not has_bearer
        and request.method not in ("GET", "HEAD", "OPTIONS")
        and path not in WRITE_EXEMPT_PATHS
    ):
        user = sessions.resolve_session(request.cookies.get(SESSION_COOKIE) or "")
        if user is not None and not user.is_admin:
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "Read-only access. Ask an admin if you need to "
                    "run, edit or spend."
                },
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


# Which project a browser request is scoped to. Sent by the web client on
# every call; absent means the default project, which is what a fresh install
# and every pre-projects client sees.
PROJECT_HEADER = "x-obs-project"


def _selected_project(request: Request) -> str:
    """The project a session request asked for, validated.

    A header rather than a cookie, deliberately. A cookie is ambient: it would
    ride along on ingest and on `curl` calls that never meant to choose a
    project, and the failure mode of *silently writing to the wrong project* is
    the one that cannot be undone. A header is chosen per request by the one
    client that has a project selector.

    An id that names no project is a 400 rather than a fallback to the default.
    The stale-id case is real — a client can hold a project id from before a
    database was reset — and quietly answering with a different project's spend
    is precisely the lie this app exists to not tell.
    """
    requested = request.headers.get(PROJECT_HEADER, "").strip()
    if not requested:
        return _default_project_id
    if requested == _default_project_id or projects.exists(requested):
        return requested
    raise HTTPException(status_code=400, detail="Unknown project")


def require_any_auth(
    request: Request,
    obs_session: Annotated[str | None, Cookie()] = None,
) -> str:
    """Read endpoints accept either path and return the project id.

    The UI reads with a cookie and names its project in a header; the SDK and
    scripts read with a key, and **the key's project always wins** — a client
    cannot widen its own scope by asking. A key presented alongside a header
    naming a different project is refused rather than quietly served from the
    key's project, because the two disagree about what was being asked for and
    guessing between them is how a script silently reports on the wrong app.
    """
    if obs_session:
        user = sessions.resolve_session(obs_session)
        if user is not None:
            return _selected_project(request)

    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token:
        from obs_backend.auth import _lookup

        authed = _lookup(token.strip())
        if authed is not None:
            requested = request.headers.get(PROJECT_HEADER, "").strip()
            if requested and requested != authed.project_id:
                raise HTTPException(
                    status_code=403,
                    detail="This key belongs to a different project",
                )
            return authed.project_id

    raise HTTPException(status_code=401, detail="Not authenticated")


def require_admin(
    user: Annotated[SessionUser, Depends(require_session)],
) -> SessionUser:
    """An admin session, for the surfaces only an admin should even see.

    The middleware already refuses every write from a viewer, so this is not
    what stops a viewer editing things — it is what stops them *reading* the
    handful of pages that are admin-only, where a 403 is the whole point rather
    than a backstop.
    """
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admins only")
    return user


def require_session_project(
    request: Request,
    user: Annotated[SessionUser, Depends(require_session)],
) -> str:
    """The project a signed-in user is working in. Rejects API keys.

    The write half of `require_any_auth`: the routes that create keys, spend on
    credentials or start runs are session-only, and every one of them used to
    reach for the module-level default. Taking the project through a dependency
    is what stops the next route added here from doing the same.
    """
    return _selected_project(request)


# --------------------------------------------------------------------------
# Auth routes
# --------------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


THEME_COOKIE = "obs_theme"


def _set_theme_cookie(response: Response, theme: str) -> None:
    """Publish the chosen accent where the *server* renderer can see it.

    Deliberately readable by JavaScript, unlike the session cookie: it is a
    colour, not a credential, and the entire point is that something other than
    this backend can read it.

    It exists so the HTML arrives already wearing the right theme. The
    alternative — a blocking inline script that rewrites the attribute before
    first paint — does work, but it makes the server's markup knowingly wrong
    and then corrects it, which costs a hydration mismatch on every load and a
    React warning for rendering a <script> inside a component. Sending the
    answer along with the document removes the problem instead of suppressing
    the symptoms.
    """
    response.set_cookie(
        key=THEME_COOKIE,
        value=theme,
        httponly=False,
        secure=_settings.cookie_secure,
        samesite=_settings.cookie_samesite,  # type: ignore[arg-type]
        max_age=int(sessions.SESSION_TTL.total_seconds()),
        path="/",
    )


def _set_session_cookie(response: Response, token: str) -> None:
    """The one place the session cookie's flags are written.

    Two routes mint a session — signing in and accepting an invite — and these
    flags are the difference between a cookie JavaScript cannot read and one it
    can. Two copies would eventually disagree, and the copy that lost a flag
    would keep working.
    """
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,  # unreadable from JS, so XSS can't exfiltrate it
        secure=_settings.cookie_secure,
        samesite=_settings.cookie_samesite,  # type: ignore[arg-type]
        max_age=int(sessions.SESSION_TTL.total_seconds()),
        path="/",
    )


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
    _set_session_cookie(response, token)
    _set_theme_cookie(response, user.theme)
    return {"email": user.email, "role": user.role}


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
    """Who is signed in, and what they may do.

    The role is here so the UI can stop offering what the server would refuse.
    It is not what enforces anything — a hidden button is not security, and the
    middleware refuses the write regardless of what the client rendered.
    """
    return {
        "email": user.email,
        "user_id": user.user_id,
        "role": user.role,
        "theme": user.theme,
    }


class ThemeRequest(BaseModel):
    theme: str


@app.patch("/api/auth/theme")
def set_theme(
    body: ThemeRequest,
    response: Response,
    user: Annotated[SessionUser, Depends(require_session)],
) -> dict[str, str]:
    """Change your own accent. Yours only — there is no user id in this route.

    Exempt from the viewer write-gate (see WRITE_EXEMPT_PATHS): it changes
    nothing anyone else can see, and read-only should not extend to how the app
    looks to you.
    """
    try:
        theme = sessions.set_theme(user.user_id, body.theme)
    except sessions.AccessError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _set_theme_cookie(response, theme)
    return {"theme": theme}


class InviteRequest(BaseModel):
    email: str
    name: str = ""
    role: str = "viewer"
    note: str = ""


class RoleRequest(BaseModel):
    role: str


class AcceptRequest(BaseModel):
    token: str
    password: str


@app.get("/api/auth/invite")
def check_invite(token: str) -> dict[str, str]:
    """Who a pending invite is for. Public, gated on holding the token."""
    email = sessions.invite_email(token)
    if email is None:
        raise HTTPException(
            status_code=404, detail="That invite link is invalid, used, or expired"
        )
    return {"email": email}


@app.post("/api/auth/accept")
def accept_invite(body: AcceptRequest, request: Request, response: Response) -> dict[str, str]:
    """Redeem an invite and sign in, in one step.

    Signing them in immediately rather than bouncing to the login form: they
    have just proved they hold the invite and just chose the password, so
    asking for it back is ceremony that teaches nothing.
    """
    try:
        user = sessions.accept_invite(body.token, body.password)
    except sessions.AccessError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    token = sessions.create_session(
        user.user_id, user_agent=request.headers.get("user-agent", "")
    )
    _set_session_cookie(response, token)
    _set_theme_cookie(response, user.theme)
    return {"email": user.email, "role": user.role}


@app.get("/api/people")
def list_people(user: Annotated[SessionUser, Depends(require_admin)]) -> dict[str, Any]:
    """The allowlist, including people who have never signed in."""
    return {"people": sessions.list_people(), "roles": list(sessions.ROLES)}


@app.post("/api/people", status_code=201)
def invite_person(
    body: InviteRequest, user: Annotated[SessionUser, Depends(require_admin)]
) -> dict[str, str]:
    """Invite someone, returning a one-time token shown exactly once.

    The token is returned rather than emailed. This app has no mail
    configuration and no fixed hostname, and inventing either to deliver a
    string would be more machinery than the string is worth — the client builds
    the link from whatever origin it is being used at, which is the one URL
    known to work.
    """
    try:
        token = sessions.invite(
            body.email, body.name, body.role, body.note, user.user_id
        )
    except sessions.AccessError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _link_response(sessions.normalize_email(body.email), token)


@app.patch("/api/people/{email}")
def set_person_role(
    email: str, body: RoleRequest, user: Annotated[SessionUser, Depends(require_admin)]
) -> dict[str, str]:
    _guard_seeded_admin(email, "change the role of")
    try:
        sessions.set_role(email, body.role)
    except sessions.AccessError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"email": sessions.normalize_email(email), "role": body.role}


@app.post("/api/people/{email}/reset")
def reset_person_password(
    email: str, user: Annotated[SessionUser, Depends(require_admin)]
) -> dict[str, str]:
    """Issue a single-use link letting someone set a new password.

    Refused for the `.env` admin, because that account's password is defined by
    the environment: `seed_admin` re-hashes it from `.env` on the next boot, so
    a reset would appear to work and then silently revert.
    """
    _guard_seeded_admin(email, "reset the password of")
    try:
        token = sessions.issue_reset(email)
    except sessions.AccessError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _link_response(sessions.normalize_email(email), token)


def _link_response(email: str, token: str) -> dict[str, str]:
    """The token, plus the finished link when this install knows its own URL.

    Without OBS_PUBLIC_URL the client builds the link from the origin it is
    being used at. That is right far more often than it is wrong — it needs no
    configuration and it is correct through a tunnel or on a deployed host —
    but it fails in exactly one case, which is the common one on a laptop:
    an admin browsing localhost mints a link that says localhost, which on the
    recipient's machine points at the recipient's machine.

    Setting the variable moves the decision from "wherever the admin happened
    to be" to "where this app actually lives", which is a fact only the
    operator knows.
    """
    payload = {"email": email, "token": token}
    if _settings.invite_origin:
        payload["link"] = (
            f"{_settings.invite_origin}/accept?token={quote(token, safe='')}"
        )
    return payload


@app.delete("/api/people/{email}")
def revoke_person(
    email: str, user: Annotated[SessionUser, Depends(require_admin)]
) -> dict[str, str]:
    """Revoke access and end their sessions."""
    _guard_seeded_admin(email, "revoke")
    try:
        sessions.revoke(email)
    except sessions.AccessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "revoked"}


@app.delete("/api/people/{email}/permanent")
def delete_person(
    email: str, user: Annotated[SessionUser, Depends(require_admin)]
) -> dict[str, str]:
    """Delete someone outright, rather than revoking them.

    Admin-only twice over: the write middleware already refuses every non-GET
    from a viewer, and `require_admin` states it on the route so the OpenAPI
    spec says so too and a future change to the middleware cannot quietly open
    it.
    """
    _guard_seeded_admin(email, "delete")
    try:
        sessions.delete_person(email, user.user_id)
    except sessions.AccessError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "deleted"}


def _guard_seeded_admin(email: str, action: str) -> None:
    """Refuse to demote or revoke the .env admin.

    Not paternalism — the change would not survive. `seed_admin` re-asserts
    that address as an un-revoked admin on every boot, so allowing it here
    would let you lock yourself out until the next restart and then silently
    undo itself. Refusing says what is actually true: this account is defined
    by .env, so change it there.
    """
    if not _settings.admin_email:
        # No seeded admin to protect: the env vars were removed after
        # bootstrap, so every account is an ordinary, manageable one.
        return
    if sessions.normalize_email(email) == sessions.normalize_email(
        _settings.admin_email
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot {action} the admin from .env — change ADMIN_EMAIL instead",
        )


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

# Longest window the dashboard will look back over. 30 days, which is the
# longest range its picker offers.
#
# The cap is about scan volume, not about the chart: the series is bucketed to
# a readable number of points at any window (see bucket_width in query.py), but
# every window still reads every Parquet file for the project, so a request for
# a year buys nothing and costs the same scan.
MAX_WINDOW_HOURS = 720


def _window(hours: int) -> int:
    """Clamp a caller-supplied window to something we will actually serve.

    Clamped rather than 400'd: `hours` arrives from a URL the user can edit,
    and quietly serving the longest window we support is a better answer to
    `?hours=99999` than an error page where a dashboard should be.
    """
    return max(1, min(hours, MAX_WINDOW_HOURS))


@app.get("/api/sources")
def list_sources(
    project_id: Annotated[str, Depends(require_any_auth)],
) -> dict[str, Any]:
    """What is reporting in. Populates the Overview and Traces filters.

    A source is the OTLP resource's service.name, set per app via
    OBS_SERVICE_NAME. Derived from the spans rather than stored in a table, so
    a new app appears the moment it sends its first span and nothing has to be
    registered first.
    """
    return {"sources": _query.sources(project_id)}


@app.get("/api/traces")
def list_traces(
    project_id: Annotated[str, Depends(require_any_auth)],
    limit: int = 50,
    source: str = "",
    credential: str = "",
    status: str = "",
    sort: str = "recent",
) -> dict[str, Any]:
    traces = _query.list_traces(
        project_id,
        limit=min(limit, 500),
        source=source or None,
        credential=credential or None,
        # 'error' | 'ok' | anything else for both. Not validated into a 400:
        # these arrive from a URL the user can edit, and the query treats an
        # unrecognised value as no filter.
        status=status or None,
        sort=sort,
    )
    return {"traces": traces, "count": len(traces)}


@app.get("/api/overview")
def get_overview(
    project_id: Annotated[str, Depends(require_any_auth)],
    hours: int = 24,
    source: str = "",
    credential: str = "",
) -> dict[str, Any]:
    return _query.overview(
        project_id,
        hours=_window(hours),
        source=source or None,
        credential=credential or None,
    )


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


class ProjectRequest(BaseModel):
    name: str


@app.get("/api/projects")
def list_projects(
    request: Request,
    user: Annotated[SessionUser, Depends(require_session)],
) -> dict[str, Any]:
    """Every project, plus which one this request resolved to.

    **Deliberately does not validate the project header**, unlike every other
    session route. A client holding an id that no longer exists gets a 400 from
    all of them, and if this route joined in, the one call that could tell it
    what to switch *to* would fail for the same reason — a stale id would need
    a manual cache clear to escape. Reporting the resolved project instead lets
    the client notice the disagreement and adopt the answer.

    Session-only: an ingest key is scoped to one project by construction, and
    enumerating the others is not something the app sending spans should be
    able to do.
    """
    requested = request.headers.get(PROJECT_HEADER, "").strip()
    current = requested if requested and projects.exists(requested) else _default_project_id
    return {
        "projects": projects.list_projects(),
        "current": current,
        "default": _default_project_id,
    }


@app.post("/api/projects", status_code=201)
def create_project(
    body: ProjectRequest,
    user: Annotated[SessionUser, Depends(require_session)],
) -> dict[str, str]:
    """Create an empty project.

    Takes no project header: you cannot be working inside the project you are
    about to create, and requiring a valid current one would make this the
    hardest route to reach from a bad state.
    """
    try:
        project_id = projects.create_project(body.name)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": project_id, "name": body.name.strip()}


@app.patch("/api/projects/{target_id}")
def rename_project(
    target_id: str,
    body: ProjectRequest,
    user: Annotated[SessionUser, Depends(require_session)],
) -> dict[str, str]:
    """Rename any project, including the one called "default" at boot."""
    try:
        name = projects.rename_project(target_id, body.name)
    except ProjectError as exc:
        status_code = 404 if str(exc) == "Project not found" else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return {"id": target_id, "name": name}


@app.get("/api/keys")
def list_keys(
    project_id: Annotated[str, Depends(require_session_project)],
    user: Annotated[SessionUser, Depends(require_admin)],
) -> dict[str, Any]:
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT id, name, key_prefix, created_at, last_used_at, revoked_at "
            "FROM api_keys WHERE project_id = %s ORDER BY created_at DESC",
            (project_id,),
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
    body: CreateKeyRequest,
    project_id: Annotated[str, Depends(require_session_project)],
) -> dict[str, str]:
    """Returns the plaintext exactly once. Only a hash is stored."""
    plaintext = create_api_key(project_id, body.name)
    return {"key": plaintext, "name": body.name}


@app.delete("/api/keys/{key_id}")
def revoke_key(
    key_id: str,
    project_id: Annotated[str, Depends(require_session_project)],
) -> dict[str, str]:
    with get_pool().connection() as conn:
        conn.execute(
            "UPDATE api_keys SET revoked_at = now() WHERE id = %s AND project_id = %s",
            (key_id, project_id),
        )
    return {"status": "revoked"}


class CredentialRequest(BaseModel):
    name: str
    secret: str
    provider: str = "anthropic"
    make_default: bool = False


@app.get("/api/providers")
def list_providers(
    user: Annotated[SessionUser, Depends(require_session)],
) -> dict[str, Any]:
    """The providers a key can be created for, plus how models rank by price.

    Served rather than duplicated in the frontend so the registry in llm.py and
    the pricing table stay the single place a provider or a model is added.

    `model_tiers` covers every *priced* model, not just the offered ones — the
    dashboard colours models that were used, which includes dated ids and
    anything retired since. Keyed by bare model id rather than by
    (provider, model): span rows carry the model but not the vendor, and the
    ids are unique across vendors anyway (`pricing.provider_of_model` relies on
    the same property, and deliberately answers None if that ever stops holding).
    """
    return {
        "providers": llm.provider_choices(),
        "model_tiers": {
            model: tier
            for (provider, model) in PRICING
            if (tier := price_tier(provider, model)) is not None
        },
    }


@app.get("/api/credentials")
def list_credentials(
    project_id: Annotated[str, Depends(require_session_project)],
    user: Annotated[SessionUser, Depends(require_admin)],
) -> dict[str, Any]:
    """Provider keys, without secrets, with what each has actually cost.

    Session-only, unlike the read API: an ingest key identifies an app sending
    spans, and that app has no business enumerating the keys this backend
    spends on. **Admin-only within that**, because this is the one read that is
    purely about spending: key names, their last four, and what each has cost.
    A viewer cannot spend, so the only thing this could tell them is whose
    money is behind the dashboard — which is administration, not observation.

    The two controls built on it degrade correctly rather than breaking: the
    credential picker and the Overview's "which key paid" filter both render
    nothing when the list is empty, which is the honest answer for someone who
    has no key to choose between.

    Spend is merged in from the span store rather than taken from the
    credential row's own run/score totals, which miss Playground and guardrail
    calls entirely. Joined on name because that is what a span records — a span
    should stay readable without a lookup into a table row that may since have
    been archived.
    """
    spend = _query.spend_by_credential(project_id)
    rows = credentials.list_credentials(project_id)
    for row in rows:
        row["spend_usd"] = spend.get(row["name"], 0.0)
    return {"credentials": rows}


@app.post("/api/credentials", status_code=201)
def create_credential(
    body: CredentialRequest,
    project_id: Annotated[str, Depends(require_session_project)],
) -> dict[str, str]:
    """Store a provider key. Validated against the provider before saving.

    Returns only the id. The plaintext is never echoed back — unlike an ingest
    key there is no reason to display it again, because nothing outside this
    backend ever needs it.
    """
    try:
        credential_id = credentials.create_credential(
            project_id,
            name=body.name,
            secret=body.secret,
            provider=body.provider,
            make_default=body.make_default,
        )
    except CredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": credential_id}


@app.post("/api/credentials/{credential_id}/default")
def set_default_credential(
    credential_id: str,
    project_id: Annotated[str, Depends(require_session_project)],
) -> dict[str, str]:
    if not credentials.set_default(project_id, credential_id):
        raise HTTPException(status_code=404, detail="Key not found")
    return {"status": "default"}


@app.delete("/api/credentials/{credential_id}")
def archive_credential(
    credential_id: str,
    project_id: Annotated[str, Depends(require_session_project)],
) -> dict[str, str]:
    try:
        if not credentials.archive_credential(project_id, credential_id):
            raise HTTPException(status_code=404, detail="Key not found")
    except CredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "archived"}


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
    # Which key this run bills to. Omitted means the project default; whatever
    # it resolves to is pinned onto the run row, so the answer survives the
    # default moving afterwards.
    credential_id: str | None = None


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
            project_id=project_id,
            dataset_id=body.dataset_id,
            name=body.name,
            prompt_template=body.prompt_template,
            model=body.model,
            max_tokens=body.max_tokens,
            scorer_ids=body.scorer_ids,
            prompt_id=body.prompt_id,
            prompt_version_id=body.prompt_version_id,
            prompt_label=body.prompt_label,
            credential_id=body.credential_id,
        )
    except (RunError, ScorerError, CredentialError) as exc:
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
    if not runner.cancel_run(project_id, run_id):
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


class PlaygroundVisibilityRequest(BaseModel):
    show: bool


class TryScorerRequest(BaseModel):
    output: str
    input: str = ""
    expected: str | None = None
    credential_id: str | None = None


class ScoreRequest(BaseModel):
    scorer_ids: list[str]
    # Which key pays. Omitted means the project default.
    credential_id: str | None = None


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


@app.patch("/api/scorers/{scorer_id}/playground")
def set_scorer_playground(
    scorer_id: str,
    body: PlaygroundVisibilityRequest,
    project_id: Annotated[str, Depends(require_any_auth)],
) -> dict[str, str]:
    """Show or hide a scorer on the Playground.

    Its own route rather than a field on PATCH /scorers/{id}: that one appends
    a prompt version, and where a scorer is offered is not a change to how it
    judges. Keeping them apart is what stops a visibility toggle from showing
    up in the version history as if the definition had moved.
    """
    if not scoring.set_playground_visibility(project_id, scorer_id, body.show):
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
            project_id,
            scorer_id,
            input_text=body.input,
            output_text=body.output,
            expected=body.expected,
            writer=_writer,
            credential=credentials.resolve(project_id, body.credential_id),
        )
    except (ScorerError, CredentialError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class PlaygroundRequest(BaseModel):
    prompt: str
    model: str
    max_tokens: int = 1024
    # Optional, and only substituted if the prompt actually contains
    # {{input}} — a replay template must carry the placeholder, a one-off
    # prompt has no reason to.
    input: str = ""
    scorer_ids: list[str] = []
    credential_id: str | None = None


@app.post("/api/playground")
def run_playground(
    body: PlaygroundRequest,
    user: Annotated[SessionUser, Depends(require_session)],
) -> dict[str, Any]:
    """Send one prompt, keep the span, score the answer. No dataset involved.

    Blocking on the completion for the same reason /try is: one call returns in
    seconds, and the whole point is a tight loop. Scoring is not blocked on —
    score_span runs its judges on a thread and the UI polls, which is how every
    other scoring path in the app behaves.
    """
    try:
        return playground.run(
            project_id=project_id,
            prompt=body.prompt,
            model=body.model,
            max_tokens=body.max_tokens,
            input_text=body.input,
            scorer_ids=body.scorer_ids,
            credential_id=body.credential_id,
            writer=_writer,
        )
    except (PlaygroundError, ScorerError, CredentialError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/scores/summary")
def score_summary(
    project_id: Annotated[str, Depends(require_any_auth)],
    hours: int = 24,
    credential: str = "",
) -> dict[str, Any]:
    """Each scorer's headline number over a window, for the dashboard.

    No `source` parameter, unlike the span-backed reads: a score belongs to the
    judge that produced it, not to the application whose output was judged, so
    there is no service.name to filter on.
    """
    # Same clamp as the overview, deliberately: this card sits on the dashboard
    # under the dashboard's range picker, and a scorer summary that silently
    # capped at 7 days while the page said "Last 30 days" would be wrong in the
    # least visible way possible.
    window = _window(hours)
    return {
        "scorers": scoring.average_by_scorer(
            project_id,
            hours=window,
            credential=credential or None,
        ),
        "window_hours": window,
    }


@app.post("/api/runs/{run_id}/score", status_code=202)
def score_run(
    run_id: str,
    body: ScoreRequest,
    user: Annotated[SessionUser, Depends(require_session)],
) -> dict[str, Any]:
    """Score a finished run. Returns immediately; the UI polls for results."""
    try:
        calls = scoring.score_run(
            project_id,
            run_id,
            body.scorer_ids,
            _writer,
            credentials.resolve(project_id, body.credential_id),
        )
    except (ScorerError, CredentialError) as exc:
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
    spans = _query.get_trace(project_id, trace_id)
    span = next((s for s in spans if s["span_id"] == span_id), None)
    if span is None:
        raise HTTPException(status_code=404, detail="Span not found")

    try:
        score_ids = scoring.score_span(
            project_id,
            trace_id=trace_id,
            span_id=span_id,
            input_text=span.get("gen_ai_input_messages") or "",
            output_text=span.get("gen_ai_output_messages") or "",
            scorer_ids=body.scorer_ids,
            writer=_writer,
            credential=credentials.resolve(project_id, body.credential_id),
            # Which key produced the text being judged. Read off the span
            # rather than assumed to be the judge's: scoring a span from last
            # week with today's key must not relabel who generated it. Spans
            # written before provider keys existed have no attribute, and stay
            # unattributed rather than being guessed at.
            generation_credential=str(
                (span.get("attributes") or {}).get("obs.credential", "")
            ),
        )
    except (ScorerError, CredentialError) as exc:
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
