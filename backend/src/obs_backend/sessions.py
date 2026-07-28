"""UI session auth — password login, server-side sessions.

Deliberately separate from the API-key path in auth.py. CLAUDE.md: "Two
separate paths. Do not try to unify them." The SDK cannot hold a cookie; a
browser should not hold a static ingest key. Sharing a code path between them
means one set of assumptions has to bend, and it is always the security one.

Sessions are server-side rows rather than signed stateless tokens so that
logout can genuinely revoke. A JWT stays valid until it expires no matter what
the server thinks.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from obs_backend.db import get_pool

# argon2id with the library's defaults, which track the RFC 9106 guidance.
# Tuning these is a memory/time tradeoff; the defaults are sane and this is a
# single-user prototype, so leaving them alone is the right call.
_hasher = PasswordHasher()

SESSION_COOKIE = "obs_session"
SESSION_TTL = timedelta(days=30)
# Sliding renewal: any request within the window pushes expiry back. CLAUDE.md
# asks for long sessions specifically so mobile use doesn't mean retyping a
# password constantly.
SESSION_RENEW_AFTER = timedelta(days=1)

RATE_LIMIT_ATTEMPTS = 5
RATE_LIMIT_WINDOW = timedelta(minutes=15)


@dataclass(frozen=True)
class SessionUser:
    user_id: str
    email: str


def hash_password(plaintext: str) -> str:
    return _hasher.hash(plaintext)


def verify_password(stored_hash: str, plaintext: str) -> bool:
    """Constant-time verification via argon2's own comparison.

    argon2-cffi compares in constant time internally, so a timing attack
    cannot distinguish "wrong password" from "nearly right".
    """
    try:
        _hasher.verify(stored_hash, plaintext)
        return True
    except (VerifyMismatchError, InvalidHashError):
        return False


def seed_admin(email: str, password: str) -> str:
    """Create or update the admin user from env vars.

    Re-hashes when the env password no longer matches the stored hash, so
    rotating a password is editing .env and restarting rather than hand-writing
    SQL. Note the consequence: the env var is authoritative, so removing it
    does NOT delete the user, and changing it changes the password.
    """
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT id, password_hash FROM users WHERE email = %s", (email,)
        ).fetchone()

        if row is None:
            user_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO users (id, email, password_hash) VALUES (%s, %s, %s)",
                (user_id, email, hash_password(password)),
            )
            return user_id

        user_id, stored = str(row[0]), str(row[1])
        if not verify_password(stored, password):
            conn.execute(
                "UPDATE users SET password_hash = %s, updated_at = now() WHERE id = %s",
                (hash_password(password), user_id),
            )
            # Existing sessions outlive a password change unless we clear them.
            # If the password changed because it may have been exposed, leaving
            # sessions alive defeats the point.
            conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
        return user_id


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def rate_limited(ip: str) -> bool:
    """True if this IP has exceeded the failed-login budget.

    Counts failures only — a successful login shouldn't consume budget, or
    normal use on a shared IP would lock itself out.
    """
    cutoff = datetime.now(tz=timezone.utc) - RATE_LIMIT_WINDOW
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM login_attempts "
            "WHERE ip = %s AND attempted_at > %s AND NOT succeeded",
            (ip, cutoff),
        ).fetchone()
    return bool(row) and int(row[0]) >= RATE_LIMIT_ATTEMPTS


def record_attempt(ip: str, succeeded: bool) -> None:
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO login_attempts (ip, succeeded) VALUES (%s, %s)", (ip, succeeded)
        )
        # Opportunistic cleanup — the table is write-heavy and read-narrow, and
        # nothing needs attempts older than the window.
        conn.execute(
            "DELETE FROM login_attempts WHERE attempted_at < now() - interval '1 day'"
        )


def authenticate(email: str, password: str) -> SessionUser | None:
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT id, email, password_hash FROM users WHERE email = %s", (email,)
        ).fetchone()

    if row is None:
        # Hash anyway so a nonexistent email doesn't return measurably faster
        # than a wrong password — otherwise the timing enumerates valid users.
        _hasher.hash(password)
        return None

    if not verify_password(str(row[2]), password):
        return None
    return SessionUser(user_id=str(row[0]), email=str(row[1]))


def create_session(user_id: str, user_agent: str = "") -> str:
    """Mint a session and return the plaintext cookie value (stored hashed)."""
    token = secrets.token_urlsafe(32)
    expires = datetime.now(tz=timezone.utc) + SESSION_TTL
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at, user_agent) "
            "VALUES (%s, %s, %s, %s)",
            (_hash_token(token), user_id, expires, user_agent[:200]),
        )
    return token


def resolve_session(token: str) -> SessionUser | None:
    """Validate a session cookie, sliding its expiry forward.

    Renewal is throttled to once a day rather than on every request — a trace
    list that polls would otherwise issue a write per poll.
    """
    now = datetime.now(tz=timezone.utc)
    token_hash = _hash_token(token)

    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT s.user_id, u.email, s.expires_at, s.last_seen_at "
            "FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = %s",
            (token_hash,),
        ).fetchone()

        if row is None:
            return None

        user_id, email, expires_at, last_seen = str(row[0]), str(row[1]), row[2], row[3]
        if expires_at <= now:
            conn.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash,))
            return None

        if now - last_seen > SESSION_RENEW_AFTER:
            conn.execute(
                "UPDATE sessions SET expires_at = %s, last_seen_at = %s "
                "WHERE token_hash = %s",
                (now + SESSION_TTL, now, token_hash),
            )
        return SessionUser(user_id=user_id, email=email)


def destroy_session(token: str) -> None:
    """Delete the server-side row. This is what makes logout real."""
    with get_pool().connection() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = %s", (_hash_token(token),))


def purge_expired_sessions() -> int:
    with get_pool().connection() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE expires_at < now()")
        return cur.rowcount
