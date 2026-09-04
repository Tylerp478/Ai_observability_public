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


#: The two roles that exist. `member` — can spend, no admin surfaces — is
#: deliberately not here: nothing needs it yet, and a role nothing grants is a
#: branch nothing tests.
ROLES = ("admin", "viewer")

#: The accents a person can choose. Validated here rather than by a CHECK
#: constraint, so adding one is a deploy and not a migration.
THEMES = ("purple", "blue", "green", "red", "orange", "yellow", "black")

#: How long an unaccepted invite stays usable. Long enough to survive a
#: weekend, short enough that a link left in a chat log stops working.
INVITE_TTL = timedelta(days=7)


class AccessError(Exception):
    """Rejected before touching state; the message is shown verbatim."""


@dataclass(frozen=True)
class SessionUser:
    user_id: str
    email: str
    #: Read from `allowed_emails` on every request, never cached on the user
    #: row — see the schema note. "viewer" is the floor, never an escalation.
    role: str = "viewer"
    #: This person's accent. Travels on the session so it arrives with the
    #: identity, rather than costing a second round trip on every page.
    theme: str = "purple"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def normalize_email(email: str) -> str:
    """Lowercased and trimmed, everywhere, always.

    The allowlist joins on this string. If an invite is stored as
    "Sarah@Work.com" and the login arrives as "sarah@work.com", the join misses
    and a person who was invited is told they are not allowed in.
    """
    return email.strip().lower()


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

    It also re-asserts this email's allowlist row as an accepted, un-revoked
    **admin** on every boot. That is the lockout escape hatch: the allowlist is
    now checked on every request, so a bad edit to it could otherwise leave
    nobody able to reach the page that fixes it. Restarting with the .env admin
    always restores one working way in — which is also why the API refuses to
    revoke or demote this address, rather than letting it be changed and then
    silently undone at the next restart.
    """
    email = normalize_email(email)
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
            _assert_admin_allowlist(conn, email)
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
        _assert_admin_allowlist(conn, email)
        return user_id


def _assert_admin_allowlist(conn, email: str) -> None:
    """Make this address an accepted, un-revoked admin. Idempotent."""
    conn.execute(
        """
        INSERT INTO allowed_emails (email, name, role, note, accepted_at)
        VALUES (%s, 'Admin', 'admin', 'Seeded from ADMIN_EMAIL in .env', now())
        ON CONFLICT (email) DO UPDATE
        SET role = 'admin',
            revoked_at = NULL,
            accepted_at = COALESCE(allowed_emails.accepted_at, now())
        """,
        (email,),
    )


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
    """Password check *and* allowlist check, in that order.

    A revoked user still has a `users` row and a valid password hash — the
    account is not deleted, so their history and the note explaining who they
    were survive. The allowlist is what says they may come in, so a revoked
    person fails here exactly like a wrong password, and is told the same
    thing: which is deliberate, since "your access was removed" is a fact about
    the tool that an unauthenticated stranger should not be able to confirm.
    """
    email = normalize_email(email)
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            SELECT u.id, u.email, u.password_hash, a.role, a.revoked_at
            FROM users u
            LEFT JOIN allowed_emails a ON a.email = u.email
            WHERE u.email = %s
            """,
            (email,),
        ).fetchone()

    if row is None:
        # Hash anyway so a nonexistent email doesn't return measurably faster
        # than a wrong password — otherwise the timing enumerates valid users.
        _hasher.hash(password)
        return None

    if not verify_password(str(row[2]), password):
        return None

    role, revoked_at = row[3], row[4]
    if role is None or revoked_at is not None:
        return None
    return SessionUser(user_id=str(row[0]), email=str(row[1]), role=str(role))


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
            """
            SELECT s.user_id, u.email, s.expires_at, s.last_seen_at,
                   a.role, a.revoked_at, u.theme
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            LEFT JOIN allowed_emails a ON a.email = u.email
            WHERE s.token_hash = %s
            """,
            (token_hash,),
        ).fetchone()

        if row is None:
            return None

        user_id, email, expires_at, last_seen = str(row[0]), str(row[1]), row[2], row[3]
        if expires_at <= now:
            conn.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash,))
            return None

        # The allowlist is joined on **every** request, not just at login.
        # Sessions run 30 days with sliding renewal, so checking only at login
        # would mean someone you revoked keeps working for up to a month — a
        # bad property for the one screen whose entire job is access. Sessions
        # are server-side precisely so revocation can be real; this uses that
        # rather than working around it, and costs one indexed lookup on a
        # table with a handful of rows.
        role, revoked_at = row[4], row[5]
        if role is None or revoked_at is not None:
            conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))
            return None

        if now - last_seen > SESSION_RENEW_AFTER:
            conn.execute(
                "UPDATE sessions SET expires_at = %s, last_seen_at = %s "
                "WHERE token_hash = %s",
                (now + SESSION_TTL, now, token_hash),
            )
        return SessionUser(
            user_id=user_id, email=email, role=str(role), theme=str(row[6])
        )


def destroy_session(token: str) -> None:
    """Delete the server-side row. This is what makes logout real."""
    with get_pool().connection() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = %s", (_hash_token(token),))


def purge_expired_sessions() -> int:
    with get_pool().connection() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE expires_at < now()")
        return cur.rowcount


# --- the allowlist: invite, accept, revoke ---------------------------------


def list_people() -> list[dict]:
    """Everyone invited, whether or not they have ever signed in.

    **Last seen, not last login.** Sessions renew slidingly for 30 days, so
    someone who signed in once in January and used the tool daily since would
    show a January login date — a misleading number on precisely the screen
    where you decide whether to revoke them. `sessions.last_seen_at` is updated
    on activity, so its max per user is "last actually used the tool".

    It goes null once their sessions age out or are purged, which reads
    correctly as "not here recently" and is why the invite/accept dates stay on
    the row as the durable record.
    """
    with get_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT a.email, a.name, a.role, a.note, a.added_at,
                   a.accepted_at, a.revoked_at, a.invite_expires_at,
                   a.invite_hash IS NOT NULL AS has_invite,
                   (SELECT MAX(s.last_seen_at) FROM sessions s
                     JOIN users u ON u.id = s.user_id
                    WHERE u.email = a.email) AS last_seen_at
            FROM allowed_emails a
            ORDER BY a.added_at
            """
        ).fetchall()
    return [
        {
            "email": r[0],
            "name": r[1],
            "role": r[2],
            "note": r[3],
            "added_at": r[4].isoformat() if r[4] else None,
            "accepted_at": r[5].isoformat() if r[5] else None,
            "revoked_at": r[6].isoformat() if r[6] else None,
            "invite_expires_at": r[7].isoformat() if r[7] else None,
            "invite_pending": bool(r[8]) and r[5] is None and r[6] is None,
            # An outstanding token on an account that already exists. Shown
            # apart from an unused invite because the person can still sign in
            # with their old password until they use it — labelling that
            # "invited" would say they had no access when they do.
            "reset_pending": bool(r[8]) and r[5] is not None and r[6] is None,
            "last_seen_at": r[9].isoformat() if r[9] else None,
        }
        for r in rows
    ]


def invite(
    email: str, name: str, role: str, note: str, added_by: str
) -> str:
    """Add or re-invite someone, returning the one-time token.

    Upsert rather than insert, because "invite", "re-invite after the link
    expired" and "let someone back in after revoking them" are the same
    intention and splitting them into three routes would mean three ways to get
    the role wrong. Re-inviting keeps the note — the sentence explaining who
    this person is survives their access being removed and restored.

    Someone who has already accepted is refused: they have a password and can
    simply sign in, and minting a fresh invite for a live account would be a
    password-reset path wearing an invitation's clothes.
    """
    email = normalize_email(email)
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise AccessError("That does not look like an email address")
    if role not in ROLES:
        raise AccessError(f"Role must be one of {', '.join(ROLES)}")

    token = secrets.token_urlsafe(32)
    expires = datetime.now(tz=timezone.utc) + INVITE_TTL

    with get_pool().connection() as conn:
        existing = conn.execute(
            "SELECT accepted_at, revoked_at FROM allowed_emails WHERE email = %s",
            (email,),
        ).fetchone()
        if existing and existing[0] is not None and existing[1] is None:
            raise AccessError(f"{email} already has an account")

        conn.execute(
            """
            INSERT INTO allowed_emails
                (email, name, role, note, added_by, invite_hash, invite_expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (email) DO UPDATE
            SET name = EXCLUDED.name,
                role = EXCLUDED.role,
                note = CASE WHEN EXCLUDED.note <> '' THEN EXCLUDED.note
                            ELSE allowed_emails.note END,
                invite_hash = EXCLUDED.invite_hash,
                invite_expires_at = EXCLUDED.invite_expires_at,
                accepted_at = NULL,
                revoked_at = NULL
            """,
            (email, name.strip(), role, note.strip(), added_by,
             _hash_token(token), expires),
        )
    return token


def invite_email(token: str) -> str | None:
    """Who a pending invite is for, or None if it is not usable.

    Exists so the accept page can say "you are setting up an account for
    sarah@…" rather than asking someone to trust an opaque link. It reveals one
    email to whoever already holds that email's single-use token, which is not
    a disclosure — it is the thing they were sent.
    """
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT email FROM allowed_emails "
            "WHERE invite_hash = %s AND revoked_at IS NULL "
            "AND invite_expires_at > now()",
            (_hash_token(token),),
        ).fetchone()
    return str(row[0]) if row else None


def accept_invite(token: str, password: str) -> SessionUser:
    """Redeem an invite: create the user, burn the token, return them signed in.

    The token is cleared in the same statement that records acceptance, so it
    is single-use even if the link is replayed. Password rules are the admin's
    own: the same minimum length the settings check applies to ADMIN_PASSWORD.
    """
    if len(password) < 12:
        raise AccessError("Password must be at least 12 characters")

    with get_pool().connection() as conn:
        # No `accepted_at IS NULL` here: the same flow redeems a first-time
        # invite and a password reset, and a reset is issued against an account
        # that has very much been accepted. The token is the credential either
        # way — single-use, expiring, and stored only as a hash.
        row = conn.execute(
            "SELECT email FROM allowed_emails "
            "WHERE invite_hash = %s AND revoked_at IS NULL "
            "AND invite_expires_at > now()",
            (_hash_token(token),),
        ).fetchone()
        if row is None:
            raise AccessError("That link is invalid, used, or expired")

        email = str(row[0])
        user = conn.execute("SELECT id FROM users WHERE email = %s", (email,)).fetchone()
        if user is None:
            user_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO users (id, email, password_hash) VALUES (%s, %s, %s)",
                (user_id, email, hash_password(password)),
            )
        else:
            # A previously revoked person being let back in: same account, same
            # history, new password. Their old sessions are already gone.
            user_id = str(user[0])
            conn.execute(
                "UPDATE users SET password_hash = %s, updated_at = now() WHERE id = %s",
                (hash_password(password), user_id),
            )

        role = conn.execute(
            "UPDATE allowed_emails "
            "SET accepted_at = now(), invite_hash = NULL, invite_expires_at = NULL "
            "WHERE email = %s RETURNING role",
            (email,),
        ).fetchone()

    return SessionUser(user_id=user_id, email=email, role=str(role[0]))


def set_role(email: str, role: str) -> None:
    """Change someone's role. Takes effect on their next request, not next login."""
    email = normalize_email(email)
    if role not in ROLES:
        raise AccessError(f"Role must be one of {', '.join(ROLES)}")
    with get_pool().connection() as conn:
        updated = conn.execute(
            "UPDATE allowed_emails SET role = %s WHERE email = %s", (role, email)
        ).rowcount
    if not updated:
        raise AccessError("No such person")


def revoke(email: str) -> None:
    """Remove access, keeping the row.

    Two steps, both needed. The soft delete is what `resolve_session` checks on
    every request, so access stops at their next click. Deleting their sessions
    is belt and braces — it makes the cutoff immediate rather than
    next-request, and costs one statement.
    """
    email = normalize_email(email)
    with get_pool().connection() as conn:
        updated = conn.execute(
            "UPDATE allowed_emails SET revoked_at = now(), "
            "invite_hash = NULL, invite_expires_at = NULL "
            "WHERE email = %s AND revoked_at IS NULL",
            (email,),
        ).rowcount
        conn.execute(
            "DELETE FROM sessions WHERE user_id IN "
            "(SELECT id FROM users WHERE email = %s)",
            (email,),
        )
    if not updated:
        raise AccessError("No such person, or already revoked")


def has_active_admin() -> bool:
    """Whether anyone can still administer this install.

    What makes `ADMIN_EMAIL`/`ADMIN_PASSWORD` a *bootstrap* rather than a
    permanent fixture: once a real admin exists, the env vars can be removed
    and the plaintext password stops living in a file on disk. Until then they
    are the only way in and the app refuses to start without them.

    Putting them back and restarting remains the escape hatch, because
    `seed_admin` re-asserts that address as an un-revoked admin every boot.
    """
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM allowed_emails a JOIN users u ON u.email = a.email "
            "WHERE a.role = 'admin' AND a.revoked_at IS NULL "
            "AND a.accepted_at IS NOT NULL LIMIT 1"
        ).fetchone()
    return row is not None


def issue_reset(email: str) -> str:
    """Mint a single-use link that lets someone set a new password.

    Deliberately not `invite()` with the guard removed. An invite creates
    access; a reset replaces a credential on access that already exists, and
    the two want different answers to "what if this person already has an
    account" — `invite` refuses precisely so that it can never be used as an
    unlogged password reset. Same token machinery, different intent, separate
    name.

    The account keeps working on the old password until the link is used, so
    issuing one cannot lock anybody out — including by accident, which matters
    when the only admin is resetting themselves.
    """
    email = normalize_email(email)
    token = secrets.token_urlsafe(32)
    expires = datetime.now(tz=timezone.utc) + INVITE_TTL

    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT accepted_at, revoked_at FROM allowed_emails WHERE email = %s",
            (email,),
        ).fetchone()
        if row is None:
            raise AccessError("No such person")
        if row[1] is not None:
            raise AccessError("That access has been revoked — re-invite instead")
        if row[0] is None:
            raise AccessError("They have not accepted their invite yet — re-invite instead")

        conn.execute(
            "UPDATE allowed_emails SET invite_hash = %s, invite_expires_at = %s "
            "WHERE email = %s",
            (_hash_token(token), expires, email),
        )
    return token


def set_theme(user_id: str, theme: str) -> str:
    """Change one person's accent.

    Not an admin action and not gated like one: it changes nothing anybody else
    can see, which is why the write middleware exempts it. A viewer picking a
    colour is not a privileged write, and refusing it would make "read-only"
    mean "cannot even choose how this looks to you".
    """
    if theme not in THEMES:
        raise AccessError(f"Unknown theme. Choose one of: {', '.join(THEMES)}")
    with get_pool().connection() as conn:
        updated = conn.execute(
            "UPDATE users SET theme = %s, updated_at = now() WHERE id = %s",
            (theme, user_id),
        ).rowcount
    if not updated:
        raise AccessError("No such user")
    return theme


def delete_person(email: str, actor_user_id: str) -> None:
    """Remove someone completely — the allowlist row and the account behind it.

    Deliberately not what `revoke` does. Revoke is a soft delete that keeps the
    row so the removal is auditable and the note explaining who they were
    survives; it is the right answer for a real person who should no longer
    have access. This is the right answer for an entry that should never have
    been history at all — a test invite, a typo'd address — and it is
    irreversible.

    The database does the rest of the cleanup: deleting the `users` row takes
    their sessions with it (ON DELETE CASCADE) and blanks `added_by` on anyone
    they invited (ON DELETE SET NULL), so nothing is left pointing at a row
    that no longer exists.
    """
    email = normalize_email(email)
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT role, revoked_at FROM allowed_emails WHERE email = %s", (email,)
        ).fetchone()
        if row is None:
            raise AccessError("No such person")

        user = conn.execute(
            "SELECT id FROM users WHERE email = %s", (email,)
        ).fetchone()

        # Deleting the account you are signed in as would revoke your own
        # session mid-request and hand you a 401 with no way back in. Refusing
        # is not paternalism: there is no version of this that ends well, and
        # another admin can always do it for you.
        if user is not None and str(user[0]) == actor_user_id:
            raise AccessError("You cannot delete the account you are signed in as")

        # The allowlist is checked on every request, so removing the last admin
        # who can actually sign in locks *everyone* out of the page that would
        # fix it. The .env bootstrap could recover it, but only by being put
        # back and the app restarted — a worse afternoon than this message.
        if row[0] == "admin" and row[1] is None:
            remaining = conn.execute(
                """
                SELECT COUNT(*) FROM allowed_emails a
                JOIN users u ON u.email = a.email
                WHERE a.role = 'admin' AND a.revoked_at IS NULL
                  AND a.accepted_at IS NOT NULL AND a.email <> %s
                """,
                (email,),
            ).fetchone()[0]
            if not remaining:
                raise AccessError(
                    "That is the only admin who can sign in. Make someone else "
                    "an admin first."
                )

        conn.execute("DELETE FROM users WHERE email = %s", (email,))
        conn.execute("DELETE FROM allowed_emails WHERE email = %s", (email,))
