"""Settings, loaded from the repo-root .env.

The .env lives at the repo root rather than in backend/ so the SDK and the
backend read the same file — one place for ANTHROPIC_API_KEY.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/src/obs_backend/config.py -> repo root is three parents up.
REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- LLM provider ---------------------------------------------------
    # CLAUDE.md: refuse to start without this rather than failing at the first
    # call. Not used in 2a, but the scorers in step 4 will, and a boot-time
    # failure is far cheaper to diagnose than one mid-eval-run.
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")

    # Encrypts provider API keys at rest (credentials.py). A urlsafe-base64
    # 32-byte Fernet key. Deliberately separate from every other secret: this
    # one is the difference between a leaked database dump being noise and
    # being a live Anthropic key with spend attached.
    secret_key: str = Field(default="", alias="OBS_SECRET_KEY")

    # --- Postgres -------------------------------------------------------
    database_url: str = Field(
        default="postgresql://localhost:5432/obs", alias="OBS_DATABASE_URL"
    )

    # --- Trace storage --------------------------------------------------
    # "local" or "s3". Local is the default and needs no credentials; the S3
    # implementation is the same interface, toggled here.
    storage_backend: str = Field(default="local", alias="OBS_STORAGE_BACKEND")
    data_dir: Path = Field(default=REPO_ROOT / "data", alias="OBS_DATA_DIR")
    s3_bucket: str = Field(default="", alias="OBS_S3_BUCKET")

    # --- WAL compaction -------------------------------------------------
    # Spans land in an append-only NDJSON WAL on receipt, then get compacted
    # into Parquet. Flush on whichever threshold trips first. Short interval
    # keeps the "I just ran it, where's my trace?" gap small; the query layer
    # reads the WAL too, so the gap is invisible either way.
    wal_flush_seconds: float = Field(default=10.0, alias="OBS_WAL_FLUSH_SECONDS")
    wal_flush_spans: int = Field(default=500, alias="OBS_WAL_FLUSH_SPANS")

    # --- UI login (2b) --------------------------------------------------
    admin_email: str = Field(default="", alias="ADMIN_EMAIL")
    admin_password: str = Field(default="", alias="ADMIN_PASSWORD")

    # Cookies are marked Secure by default. Set false only for plain-HTTP
    # localhost development — a Secure cookie is dropped by the browser over
    # http://, which presents as "login succeeds then immediately bounces back
    # to the login page" and is genuinely nasty to diagnose.
    cookie_secure: bool = Field(default=False, alias="OBS_COOKIE_SECURE")
    # "lax" for same-origin dev. Cross-site (frontend and backend on different
    # domains, e.g. behind a tunnel) requires "none", which the browser only
    # honours together with Secure.
    cookie_samesite: str = Field(default="lax", alias="OBS_COOKIE_SAMESITE")
    cors_origins: str = Field(default="http://localhost:3000", alias="OBS_CORS_ORIGINS")

    # Bind address. The weak-password guard keys off this.
    host: str = Field(default="127.0.0.1", alias="OBS_HOST")
    allow_weak_password: bool = Field(default=False, alias="OBS_ALLOW_WEAK_PASSWORD")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def require_anthropic_key(self) -> None:
        if not self.anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Refusing to start — scorer endpoints "
                "cost money per invocation and failing at boot is cheaper to diagnose "
                "than failing mid-run. Set it in the repo-root .env."
            )

    def require_secret_key(self) -> None:
        """Refuse to start without the key that decrypts stored provider keys.

        Boot-time rather than first-use, matching require_anthropic_key: a
        backend that starts happily and then cannot decrypt the key it needs
        fails in the middle of a paid run, which is the expensive place to find
        out. Losing this value makes every stored provider key unrecoverable —
        it belongs in whatever you back up.
        """
        if not self.secret_key:
            raise RuntimeError(
                "OBS_SECRET_KEY is not set. It encrypts provider API keys at rest.\n"
                "\n"
                "Generate one:\n"
                "  python -c \"from cryptography.fernet import Fernet; "
                'print(Fernet.generate_key().decode())"\n'
                "\n"
                "Put it in the repo-root .env as OBS_SECRET_KEY and keep a backup — "
                "without it, stored provider keys cannot be decrypted and have to be "
                "entered again."
            )

    def require_admin_credentials(self) -> None:
        if not self.admin_email or not self.admin_password:
            raise RuntimeError(
                "ADMIN_EMAIL and ADMIN_PASSWORD must be set — the UI has no other "
                "way to create the first user. Set them in the repo-root .env."
            )

    def check_password_strength(self) -> None:
        """Refuse to listen on a non-loopback address with a guessable password.

        CLAUDE.md requires this app not be open to the public internet, and the
        plan is to expose it through a tunnel. A weak password is harmless on
        loopback and a real exposure the moment the bind address changes, so
        the bind address is what this keys off — not a warning you have to
        remember to act on.

        Not a password policy: it blocks a short list of passwords that appear
        in every credential-stuffing wordlist, and only when you are actually
        exposed. OBS_ALLOW_WEAK_PASSWORD=true overrides it.
        """
        if self.allow_weak_password:
            return
        if self.host in {"127.0.0.1", "localhost", "::1"}:
            return

        weak = {
            "abc123", "password", "password1", "123456", "12345678", "qwerty",
            "letmein", "admin", "changeme", "welcome", "test", "secret",
        }
        candidate = self.admin_password.strip().lower()
        if candidate in weak or len(candidate) < 8:
            raise RuntimeError(
                f"Refusing to bind {self.host} with a weak ADMIN_PASSWORD.\n"
                "\n"
                "This address is reachable beyond loopback, so the login page would be\n"
                "exposed with a password that is in common credential-stuffing lists\n"
                "(or under 8 characters).\n"
                "\n"
                "Fix: set a strong ADMIN_PASSWORD in .env and restart.\n"
                "Override (not recommended): OBS_ALLOW_WEAK_PASSWORD=true"
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
