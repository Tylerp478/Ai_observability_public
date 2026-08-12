"""Provider API keys — which Anthropic account a call bills to.

Before this there was one key, read from .env, and every LLM call in the app
spent it. This makes the key a choice made at the point of spending: a replay
run, a scorer preview, a Playground call and a guardrail each name the
credential they want, and what they used is recorded on the row afterwards.

**The secret is encrypted, not hashed, and that is a real difference.** The two
credentials this app already stores are one-way on purpose — a password is
argon2id and an ingest key is SHA-256, because both are only ever compared
against something the caller presents. An Anthropic key has to go into an
Authorization header, so it must come back out. That is a weaker position than
hashing and there is no way around it; what is available is keeping the key
material somewhere the database is not, so that a Postgres dump on its own
decrypts nothing. That is OBS_SECRET_KEY, which lives in .env.

Consequences worth stating plainly, because they bite later:

  - Lose OBS_SECRET_KEY and every stored key is unrecoverable. They have to be
    entered again. It belongs in whatever gets backed up.
  - Rotating OBS_SECRET_KEY means re-encrypting every row. There is no
    migration helper here yet; write one before rotating.
  - The plaintext is never returned by the API, never logged, and never shown
    again after creation. Unlike an ingest key there is no reason to display it
    back — nothing outside this process ever needs it.

**Keys are validated when saved.** A typo caught at save time is an edit; the
same typo caught mid-run has already paid for the calls that ran before it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from obs_backend import llm
from obs_backend.config import get_settings
from obs_backend.db import get_pool


class CredentialError(ValueError):
    """Bad credential request — surfaced as a 400, not a 500."""


@dataclass(frozen=True)
class Credential:
    """A resolved credential, carrying the decrypted secret.

    `secret` is populated only by resolve() — the list and read paths build
    these without it, so a credential that reaches a JSON response cannot be
    carrying plaintext by accident.
    """

    id: str
    name: str
    provider: str
    last4: str
    is_default: bool
    secret: str = ""


def _fernet() -> Fernet:
    key = get_settings().secret_key
    if not key:
        # Should be unreachable — main.py calls require_secret_key at boot —
        # but a clear message beats a stack trace out of the crypto library if
        # that check is ever moved or skipped.
        raise CredentialError(
            "OBS_SECRET_KEY is not set, so provider keys cannot be decrypted."
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise CredentialError(
            "OBS_SECRET_KEY is not a valid Fernet key. Generate one with: "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        ) from exc


def _encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def _decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        # Almost always a changed OBS_SECRET_KEY. Say so, because "invalid
        # token" on its own sends you looking at the Anthropic key rather than
        # at the one that actually changed.
        raise CredentialError(
            "This key could not be decrypted. OBS_SECRET_KEY has probably changed "
            "since it was saved — the key has to be entered again."
        ) from exc


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


def create_credential(
    project_id: str,
    *,
    name: str,
    secret: str,
    provider: str = "anthropic",
    make_default: bool = False,
    validate: bool = True,
) -> str:
    """Store a key. Validates it against the provider first."""
    name = name.strip()
    secret = secret.strip()
    if not name:
        raise CredentialError("Give the key a name")
    if not secret:
        raise CredentialError("Paste the API key")
    if provider != "anthropic":
        raise CredentialError(f"Unknown provider {provider!r}")

    if validate:
        try:
            llm.validate_key(secret)
        except Exception as exc:  # noqa: BLE001 — any failure here means unusable
            raise CredentialError(
                f"Anthropic rejected this key: {type(exc).__name__}. "
                "Nothing was saved."
            ) from exc

    credential_id = str(uuid.uuid4())
    with get_pool().connection() as conn:
        clash = conn.execute(
            "SELECT id FROM provider_credentials WHERE project_id = %s AND name = %s "
            "AND archived_at IS NULL",
            (project_id, name),
        ).fetchone()
        if clash:
            raise CredentialError(f"A key named {name!r} already exists")

        # First key in a project is the default whether or not it was asked
        # for. A project with keys but no default would make every unqualified
        # call fail on a technicality.
        existing = conn.execute(
            "SELECT COUNT(*) FROM provider_credentials "
            "WHERE project_id = %s AND archived_at IS NULL",
            (project_id,),
        ).fetchone()
        is_default = make_default or (existing is not None and existing[0] == 0)

        if is_default:
            conn.execute(
                "UPDATE provider_credentials SET is_default = false "
                "WHERE project_id = %s AND is_default",
                (project_id,),
            )

        conn.execute(
            "INSERT INTO provider_credentials "
            "(id, project_id, name, provider, secret_encrypted, last4, is_default) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                credential_id,
                project_id,
                name,
                provider,
                _encrypt(secret),
                secret[-4:],
                is_default,
            ),
        )
    return credential_id


def set_default(project_id: str, credential_id: str) -> bool:
    """Promote a key to the project default.

    Both statements in one transaction: the partial unique index allows only
    one default per project, so clearing and setting have to land together or
    the second one is rejected by the constraint the first one was satisfying.
    """
    with get_pool().connection() as conn:
        owned = conn.execute(
            "SELECT id FROM provider_credentials "
            "WHERE id = %s AND project_id = %s AND archived_at IS NULL",
            (credential_id, project_id),
        ).fetchone()
        if owned is None:
            return False
        conn.execute(
            "UPDATE provider_credentials SET is_default = false "
            "WHERE project_id = %s AND is_default",
            (project_id,),
        )
        conn.execute(
            "UPDATE provider_credentials SET is_default = true WHERE id = %s",
            (credential_id,),
        )
    return True


def archive_credential(project_id: str, credential_id: str) -> bool:
    """Soft delete. Runs and scores that cite this key stay readable.

    Refuses to archive the last active key, and refuses to archive the default
    while another is available — losing the default silently would turn the
    next unqualified run into an error nobody asked for. Promote a replacement
    first; that makes the choice explicit.
    """
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT is_default FROM provider_credentials "
            "WHERE id = %s AND project_id = %s AND archived_at IS NULL",
            (credential_id, project_id),
        ).fetchone()
        if row is None:
            return False

        remaining = conn.execute(
            "SELECT COUNT(*) FROM provider_credentials "
            "WHERE project_id = %s AND archived_at IS NULL AND id <> %s",
            (project_id, credential_id),
        ).fetchone()
        others = remaining[0] if remaining else 0

        if row[0] and others > 0:
            raise CredentialError(
                "This is the default key. Make another key the default first, so "
                "it is clear what the next run will spend."
            )
        if others == 0:
            raise CredentialError(
                "This is the only key left. Add another before removing it — "
                "without one, nothing in the app can call a model."
            )

        conn.execute(
            "UPDATE provider_credentials SET archived_at = now() WHERE id = %s",
            (credential_id,),
        )
    return True


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def list_credentials(project_id: str) -> list[dict[str, Any]]:
    """Active keys, without secrets, with how much each has been spent on.

    The cost figures are the reason this table earns its place in the UI:
    "which account is this billing to" is only half the question, and the other
    half is "how much".
    """
    with get_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.name, c.provider, c.last4, c.is_default, c.created_at,
                   c.last_used_at,
                   (SELECT COALESCE(SUM(r.cost_usd), 0) FROM runs r
                     WHERE r.credential_id = c.id),
                   (SELECT COALESCE(SUM(s.cost_usd), 0) FROM scores s
                     WHERE s.credential_id = c.id)
            FROM provider_credentials c
            WHERE c.project_id = %s AND c.archived_at IS NULL
            ORDER BY c.is_default DESC, c.created_at ASC
            """,
            (project_id,),
        ).fetchall()

    return [
        {
            "id": str(r[0]),
            "name": r[1],
            "provider": r[2],
            "last4": r[3],
            "is_default": r[4],
            "created_at": r[5].isoformat() if r[5] else None,
            "last_used_at": r[6].isoformat() if r[6] else None,
            "run_cost_usd": float(r[7] or 0),
            "score_cost_usd": float(r[8] or 0),
        }
        for r in rows
    ]


def resolve(project_id: str, credential_id: str | None = None) -> Credential:
    """Load a credential and decrypt its secret, falling back to the default.

    Every path that spends money goes through here, which is what makes "no key
    configured" a single clear error rather than an Anthropic 401 surfacing
    from somewhere deep in a run.
    """
    with get_pool().connection() as conn:
        if credential_id:
            row = conn.execute(
                "SELECT id, name, provider, last4, is_default, secret_encrypted "
                "FROM provider_credentials "
                "WHERE id = %s AND project_id = %s AND archived_at IS NULL",
                (credential_id, project_id),
            ).fetchone()
            if row is None:
                raise CredentialError(
                    "That API key was not found, or has been removed."
                )
        else:
            row = conn.execute(
                "SELECT id, name, provider, last4, is_default, secret_encrypted "
                "FROM provider_credentials "
                "WHERE project_id = %s AND is_default AND archived_at IS NULL",
                (project_id,),
            ).fetchone()
            if row is None:
                raise CredentialError(
                    "No default API key is set. Add one on the Keys page before "
                    "running anything that calls a model."
                )

        conn.execute(
            "UPDATE provider_credentials SET last_used_at = now() WHERE id = %s",
            (row[0],),
        )

    return Credential(
        id=str(row[0]),
        name=row[1],
        provider=row[2],
        last4=row[3],
        is_default=row[4],
        secret=_decrypt(row[5]),
    )


def seed_from_env(project_id: str) -> bool:
    """Adopt the .env key as the first credential, once.

    Without this, upgrading to a version that requires a credential would leave
    a working install unable to call anything until someone visited the UI. The
    key in .env is the one already being spent, so making it the default row is
    the change that alters nothing about behaviour.

    Not validated on the way in: refusing to boot because the network is down
    would be a worse failure than adopting a key that turns out to be stale.
    """
    key = get_settings().anthropic_api_key
    if not key:
        return False

    with get_pool().connection() as conn:
        existing = conn.execute(
            "SELECT COUNT(*) FROM provider_credentials WHERE project_id = %s",
            (project_id,),
        ).fetchone()
        if existing and existing[0] > 0:
            return False

    create_credential(
        project_id,
        name="Default (from .env)",
        secret=key,
        make_default=True,
        validate=False,
    )
    return True


def backfill_generation_credential(project_id: str) -> int:
    """Attribute pre-existing scores to the only key that could have made them.

    Everything recorded before provider keys existed necessarily ran on the one
    key in .env — the same key seed_from_env adopted as the default. Leaving
    those rows blank makes the dashboard's per-key view look broken rather than
    empty: you pick your only key and the score panel reports nothing, for data
    that plainly came from it.

    Guarded on there being exactly one active credential, which is what makes
    the attribution safe rather than a guess. With one key, "unknown" and "that
    key" are the same set. The moment a second key exists this stops firing,
    and by then new scores carry their own attribution anyway.
    """
    with get_pool().connection() as conn:
        active = conn.execute(
            "SELECT id, name FROM provider_credentials "
            "WHERE project_id = %s AND archived_at IS NULL",
            (project_id,),
        ).fetchall()
        if len(active) != 1:
            return 0

        credential_id, name = active[0]
        cur = conn.execute(
            "UPDATE scores SET generation_credential = %s "
            "WHERE project_id = %s AND generation_credential = ''",
            (name, project_id),
        )
        filled = cur.rowcount

        # Runs too, for the same reason: the Keys page reports per-key spend,
        # and a run with no key contributes to nothing.
        conn.execute(
            "UPDATE runs SET credential_id = %s "
            "WHERE project_id = %s AND credential_id IS NULL",
            (credential_id, project_id),
        )
        return filled
