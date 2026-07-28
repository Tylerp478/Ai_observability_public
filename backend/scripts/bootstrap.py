"""Create the database schema, a project, and an ingest API key.

Run once after Postgres is up:

    uv run scripts/bootstrap.py

Prints the plaintext key exactly once — it is stored only as a SHA-256 hash,
so there is no way to recover it later. Put it in the repo-root .env as
OBS_API_KEY.
"""

from __future__ import annotations

import sys

from obs_backend.auth import create_api_key, ensure_project
from obs_backend.config import get_settings
from obs_backend.db import init_schema


def main() -> None:
    settings = get_settings()
    project_name = sys.argv[1] if len(sys.argv) > 1 else "default"

    print(f"database: {settings.database_url}")
    init_schema()
    print("schema:   ok")

    project_id = ensure_project(project_name)
    print(f"project:  {project_name} ({project_id})")

    plaintext = create_api_key(project_id, name="local-dev")
    print("\n" + "=" * 62)
    print("INGEST API KEY — shown once, not recoverable:\n")
    print(f"  {plaintext}\n")
    print("Add to the repo-root .env:")
    print(f"  OBS_API_KEY={plaintext}")
    print("=" * 62)


if __name__ == "__main__":
    main()
