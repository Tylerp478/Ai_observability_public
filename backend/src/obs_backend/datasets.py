"""Datasets and test cases — the step 3 primitive.

A dataset is a named bag of test cases inside a project. A test case is an
input, optionally a known-good output, and optionally a backlink to the trace
span it was lifted from.

The backlink is the part that matters. Every eval tool can hold a list of test
strings; what makes this an *observability* dataset is that a case can be
captured from real traffic with one click and still point at the trace it came
from, so "is this case representative?" is answerable by looking rather than
by trusting whoever typed it in.

Everything here is plain SQL against Postgres. No ORM: the queries are short,
the schema is in db.py where it can be read in one screen, and an ORM would
hide the one thing worth seeing (which columns a run actually writes).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from obs_backend.db import get_pool


@dataclass(frozen=True)
class Dataset:
    id: str
    name: str
    description: str


def create_dataset(project_id: str, name: str, description: str = "") -> Dataset:
    """Create a dataset. Names are unique per project.

    Raises ValueError on a duplicate name rather than silently returning the
    existing one — "create" quietly becoming "get" is how you end up appending
    test cases to a dataset you thought was empty.
    """
    name = name.strip()
    if not name:
        raise ValueError("Dataset name cannot be empty")

    dataset_id = str(uuid.uuid4())
    with get_pool().connection() as conn:
        existing = conn.execute(
            "SELECT id FROM datasets WHERE project_id = %s AND name = %s",
            (project_id, name),
        ).fetchone()
        if existing:
            raise ValueError(f"A dataset named {name!r} already exists")
        conn.execute(
            "INSERT INTO datasets (id, project_id, name, description) "
            "VALUES (%s, %s, %s, %s)",
            (dataset_id, project_id, name, description.strip()),
        )
    return Dataset(id=dataset_id, name=name, description=description.strip())


def list_datasets(project_id: str) -> list[dict[str, Any]]:
    """Datasets with their item and run counts.

    Counts come from correlated subqueries rather than a GROUP BY over two
    LEFT JOINs — joining both children multiplies the rows and inflates each
    count by the other's cardinality, which is a classic way to end up with a
    dataset reporting 12 items when it has 4.
    """
    with get_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT d.id, d.name, d.description, d.created_at,
                   (SELECT COUNT(*) FROM dataset_items i WHERE i.dataset_id = d.id),
                   (SELECT COUNT(*) FROM runs r WHERE r.dataset_id = d.id),
                   (SELECT MAX(r.created_at) FROM runs r WHERE r.dataset_id = d.id)
            FROM datasets d
            WHERE d.project_id = %s
            ORDER BY d.created_at DESC
            """,
            (project_id,),
        ).fetchall()

    return [
        {
            "id": str(r[0]),
            "name": r[1],
            "description": r[2],
            "created_at": r[3].isoformat() if r[3] else None,
            "item_count": r[4],
            "run_count": r[5],
            "last_run_at": r[6].isoformat() if r[6] else None,
        }
        for r in rows
    ]


def get_dataset(project_id: str, dataset_id: str) -> dict[str, Any] | None:
    """Fetch one dataset, scoped to the project.

    project_id is in the WHERE clause on every read here, not just checked
    afterwards: an id from the URL is attacker-controlled, and scoping at the
    query is the only version that stays correct when a second project exists.
    """
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT id, name, description, created_at FROM datasets "
            "WHERE id = %s AND project_id = %s",
            (dataset_id, project_id),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": str(row[0]),
        "name": row[1],
        "description": row[2],
        "created_at": row[3].isoformat() if row[3] else None,
    }


def delete_dataset(project_id: str, dataset_id: str) -> bool:
    with get_pool().connection() as conn:
        cur = conn.execute(
            "DELETE FROM datasets WHERE id = %s AND project_id = %s",
            (dataset_id, project_id),
        )
        return cur.rowcount > 0


# --------------------------------------------------------------------------
# Items
# --------------------------------------------------------------------------


def add_item(
    dataset_id: str,
    input_text: str,
    expected_output: str | None = None,
    source_trace_id: str = "",
    source_span_id: str = "",
) -> str:
    input_text = input_text.strip()
    if not input_text:
        raise ValueError("Test case input cannot be empty")

    item_id = str(uuid.uuid4())
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO dataset_items "
            "(id, dataset_id, input, expected_output, source_trace_id, source_span_id) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                item_id,
                dataset_id,
                input_text,
                (expected_output or "").strip() or None,
                source_trace_id,
                source_span_id,
            ),
        )
    return item_id


def list_items(dataset_id: str) -> list[dict[str, Any]]:
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT id, input, expected_output, source_trace_id, source_span_id, created_at "
            "FROM dataset_items WHERE dataset_id = %s ORDER BY created_at ASC",
            (dataset_id,),
        ).fetchall()
    return [
        {
            "id": str(r[0]),
            "input": r[1],
            "expected_output": r[2],
            "source_trace_id": r[3],
            "source_span_id": r[4],
            "created_at": r[5].isoformat() if r[5] else None,
        }
        for r in rows
    ]


def delete_item(dataset_id: str, item_id: str) -> bool:
    with get_pool().connection() as conn:
        cur = conn.execute(
            "DELETE FROM dataset_items WHERE id = %s AND dataset_id = %s",
            (item_id, dataset_id),
        )
        return cur.rowcount > 0
