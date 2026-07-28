"""Ingest authentication — bearer API keys, default-deny.

CLAUDE.md asks for default-deny as an up-front abstraction: adding a route
should fail closed, not open. That is enforced in main.py by a middleware that
rejects every path not explicitly listed as public, so forgetting a dependency
on a new route produces a 401 rather than an open endpoint.

This module owns key generation, hashing, and lookup. Session/cookie auth for
the UI is a separate path added in 2b and deliberately not unified with this
one — the SDK cannot hold a cookie, and a browser should not hold a static
ingest key.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from obs_backend.db import get_pool

KEY_PREFIX = "obsk_"
_KEY_BYTES = 32  # 256 bits of CSPRNG output


@dataclass(frozen=True)
class AuthedKey:
    key_id: str
    project_id: str


def generate_key() -> str:
    """Mint a new plaintext API key. Shown once, never stored."""
    return KEY_PREFIX + secrets.token_urlsafe(_KEY_BYTES)


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def create_api_key(project_id: str, name: str) -> str:
    """Create a key and return the plaintext exactly once."""
    plaintext = generate_key()
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO api_keys (id, project_id, name, key_hash, key_prefix) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                str(uuid.uuid4()),
                project_id,
                name,
                hash_key(plaintext),
                plaintext[: len(KEY_PREFIX) + 8],
            ),
        )
    return plaintext


def ensure_project(name: str) -> str:
    """Get or create a project by name, returning its id."""
    with get_pool().connection() as conn:
        row = conn.execute("SELECT id FROM projects WHERE name = %s", (name,)).fetchone()
        if row:
            return str(row[0])
        project_id = str(uuid.uuid4())
        conn.execute("INSERT INTO projects (id, name) VALUES (%s, %s)", (project_id, name))
        return project_id


def _lookup(plaintext: str) -> AuthedKey | None:
    """Resolve a plaintext key to its project.

    Lookup is by hash equality on an indexed column, so this is a single index
    probe rather than a scan-and-compare. There is no timing side channel to
    protect against here the way there is for password comparison: the input
    is high-entropy and the attacker learns nothing from the timing of a hash
    index lookup.
    """
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT id, project_id FROM api_keys "
            "WHERE key_hash = %s AND revoked_at IS NULL",
            (hash_key(plaintext),),
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE api_keys SET last_used_at = now() WHERE id = %s", (row[0],))
        return AuthedKey(key_id=str(row[0]), project_id=str(row[1]))


# Declared as a security scheme rather than reading the raw header, so it
# shows up in the OpenAPI spec. That gives /docs a working "Authorize" button —
# without it Swagger UI has no way to attach the bearer token and every
# try-it-out call comes back 401.
_bearer = HTTPBearer(auto_error=False, description="Ingest API key (obsk_...)")


def require_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthedKey:
    """FastAPI dependency: resolve the bearer token to a project.

    Returns the project the key belongs to, which is what routes ingest into
    the right partition. A client cannot select its own project — that comes
    from the key, not the payload.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Expected 'Authorization: Bearer <api key>'",
            headers={"WWW-Authenticate": "Bearer"},
        )

    authed = _lookup(credentials.credentials.strip())
    if authed is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return authed
