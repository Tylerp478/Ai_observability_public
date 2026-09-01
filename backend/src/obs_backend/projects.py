"""Projects: the boundary everything else is already scoped to.

Every table in `db.py` and every partition in the span store
(`traces/project=<id>/…`) has carried a `project_id` since step 2a, but the app
resolved exactly one of them — `ensure_project("default")` at boot — so the
column was true and inert. This module is what makes the value vary.

**A project is an isolation boundary, not a filter.** `service_name` already
answers "which of my apps sent this" within one project, and it does it without
duplicating datasets, scorers, prompts or provider keys. A project is for the
case where those *should* be duplicated — a different app with its own eval
suite and its own bill — so switching project changes what exists, not what is
shown.

That is also why there is no "all projects" view. Half the app (datasets,
scorers, prompts, guardrails) has nothing to roll up across a boundary that
exists to keep them apart, and a mode that quietly worked on the two pages
backed by the span store and ignored itself on the other six would be worse
than not offering it.

**Nothing here deletes.** A `DELETE` would cascade cleanly through Postgres and
leave every Parquet file under `traces/project=<id>/` behind — unreadable,
because nothing would map the id to a name again, and still counted in whatever
scanned the prefix. Renaming covers the real need (a typo, a rebrand) without
that; a project that has served its purpose costs one row.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from obs_backend.db import get_pool

# The name is a label, not a path segment — the id is what routes storage — so
# this only has to be a name a person can read back in a picker.
MAX_NAME = 60


class ProjectError(Exception):
    """Rejected before touching the database; the message is shown verbatim."""


def _clean_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", name).strip()
    if not cleaned:
        raise ProjectError("A project needs a name")
    if len(cleaned) > MAX_NAME:
        raise ProjectError(f"Names are at most {MAX_NAME} characters")
    return cleaned


def list_projects() -> list[dict[str, Any]]:
    """Every project, oldest first, with what each one holds.

    The counts are the honest version of "is this safe to ignore" — a picker
    entry with no keys and no datasets is a project someone made and abandoned,
    and saying so is cheaper than making them switch to find out. Ingest keys
    and datasets come from Postgres; span volume deliberately does not, because
    that would mean a DuckDB scan per project on every render of the header.
    """
    with get_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT p.id,
                   p.name,
                   p.created_at,
                   (SELECT COUNT(*) FROM api_keys k
                     WHERE k.project_id = p.id AND k.revoked_at IS NULL),
                   (SELECT COUNT(*) FROM provider_credentials c
                     WHERE c.project_id = p.id AND c.archived_at IS NULL),
                   (SELECT COUNT(*) FROM datasets d WHERE d.project_id = p.id)
            FROM projects p
            ORDER BY p.created_at, p.name
            """
        ).fetchall()
    return [
        {
            "id": str(r[0]),
            "name": r[1],
            "created_at": r[2].isoformat() if r[2] else None,
            "ingest_keys": int(r[3]),
            "provider_keys": int(r[4]),
            "datasets": int(r[5]),
        }
        for r in rows
    ]


def exists(project_id: str) -> bool:
    """Whether an id names a real project.

    Called on every request that carries a project header, which is why it is
    a primary-key probe and nothing more.
    """
    with get_pool().connection() as conn:
        row = conn.execute("SELECT 1 FROM projects WHERE id = %s", (project_id,)).fetchone()
    return row is not None


def create_project(name: str) -> str:
    """Create a project and return its id.

    Nothing is seeded into it — no provider key, no scorers. A new project
    starting empty is the point of having one, and copying the default's keys
    in would silently spend another project's credential on the first run.
    """
    cleaned = _clean_name(name)
    with get_pool().connection() as conn:
        taken = conn.execute("SELECT 1 FROM projects WHERE name = %s", (cleaned,)).fetchone()
        if taken:
            raise ProjectError(f"There is already a project called {cleaned!r}")
        project_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO projects (id, name) VALUES (%s, %s)", (project_id, cleaned)
        )
    return project_id


def rename_project(project_id: str, name: str) -> str:
    """Rename in place, keeping the id.

    The id is what every span partition, key and dataset row points at, so a
    rename is only ever a label change — which is exactly why the first project
    can be called "default" at boot and something meaningful later.
    """
    cleaned = _clean_name(name)
    with get_pool().connection() as conn:
        taken = conn.execute(
            "SELECT 1 FROM projects WHERE name = %s AND id <> %s", (cleaned, project_id)
        ).fetchone()
        if taken:
            raise ProjectError(f"There is already a project called {cleaned!r}")
        updated = conn.execute(
            "UPDATE projects SET name = %s WHERE id = %s", (cleaned, project_id)
        ).rowcount
    if not updated:
        raise ProjectError("Project not found")
    return cleaned
