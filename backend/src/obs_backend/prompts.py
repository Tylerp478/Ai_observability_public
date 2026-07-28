"""Prompts as versioned artifacts — step 5.

A prompt has a name and a description, which are mutable identity, and a chain
of immutable versions, which are content. Editing appends; nothing ever
rewrites a version. That single rule is what makes the rest work: a run from
three weeks ago can name the exact text it sent, and it stays true no matter
what happened to the prompt afterwards.

Four things are worth knowing about the design.

**Versions carry config, not just text.** A version freezes model and
max_tokens alongside the template, and for a scorer it also freezes the output
schema. A "version" that recorded only wording would be a version of a third of
the artifact — change a scorer's scale from 1-5 to 1-10 and every past score
means something different while the recorded text is untouched.

**A no-op save is refused, not recorded.** content_hash covers template and
config together, so pressing save without changing anything returns the
existing version instead of minting a duplicate. History nobody edited should
not grow.

**Labels are movable, versions are not.** `production` is a pointer you promote
between versions; v3 is v3 forever. A run resolves a label at creation and
records the version it landed on, so moving the label afterwards never
retroactively changes what a run says it ran. `latest` is not stored — it is
MAX(version), because a stored copy is a second source of truth for something
the version numbers already say.

**Diffs are computed here, not in the browser.** difflib is in the standard
library and the alternative is a diff package in the web bundle for the same
result. The UI gets a list of line ops and renders them.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import uuid
from typing import Any

from psycopg import Connection

from obs_backend.db import get_pool

# What a prompt is for. Both kinds share the versioning machinery; they differ
# in what their config bag holds and in which page edits them.
KINDS = ("completion", "scorer")

# The placeholder a completion prompt must contain, mirroring runner.py. Kept
# as a literal rather than imported to avoid a circular import — runner imports
# this module for version resolution.
INPUT_PLACEHOLDER = "{{input}}"
OUTPUT_PLACEHOLDER = "{{output}}"

# Labels are used in URLs and read by humans in a hurry, so they are a narrow
# slug rather than free text.
LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# Not a real label: "latest" already means MAX(version) everywhere in this
# module, and letting someone pin it to v2 would make the word lie.
RESERVED_LABELS = {"latest"}

# Offered in the UI as one-click promotions. Not enforced — any slug is legal.
SUGGESTED_LABELS = ("production", "staging")


class PromptError(ValueError):
    """Bad prompt request — surfaced as a 400, not a 500."""


# --------------------------------------------------------------------------
# Content identity
# --------------------------------------------------------------------------


def canonical_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Drop nulls and sort keys, so equal configs hash equal.

    Without this, a form that sends `pass_threshold: null` and one that omits
    the field entirely produce different hashes for the same scorer, and the
    no-op check stops catching no-ops.
    """
    return {k: v for k, v in sorted((config or {}).items()) if v is not None}


def content_hash(template: str, config: dict[str, Any] | None) -> str:
    payload = json.dumps(
        {"template": template, "config": canonical_config(config)},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _validate_label(label: str) -> str:
    label = label.strip().lower()
    if label in RESERVED_LABELS:
        raise PromptError(
            f"{label!r} is reserved — it always means the highest version number."
        )
    if not LABEL_RE.match(label):
        raise PromptError(
            "A label must be 1-32 characters of lowercase letters, digits, "
            "hyphens or underscores, starting with a letter or digit."
        )
    return label


# --------------------------------------------------------------------------
# Writes — transaction-scoped, so callers can compose them
# --------------------------------------------------------------------------
#
# scoring.py creates a scorer and its prompt in one transaction: a scorer row
# whose prompt insert failed would be a scorer with no history and no way to
# get one. The `_tx` functions take an open connection for that reason; the
# public wrappers below open their own for everything else.


def create_prompt_tx(
    conn: Connection,
    project_id: str,
    *,
    name: str,
    description: str = "",
    kind: str = "completion",
    template: str,
    config: dict[str, Any] | None = None,
    note: str = "",
) -> tuple[str, str]:
    """Create a prompt and its v1. Returns (prompt_id, version_id)."""
    name = name.strip()
    if not name:
        raise PromptError("Prompt name cannot be empty")
    if kind not in KINDS:
        raise PromptError(f"kind must be one of {', '.join(KINDS)}")
    if not template.strip():
        raise PromptError("Prompt text cannot be empty")

    clash = conn.execute(
        "SELECT id FROM prompts WHERE project_id = %s AND kind = %s AND name = %s "
        "AND archived_at IS NULL",
        (project_id, kind, name),
    ).fetchone()
    if clash:
        raise PromptError(f"A {kind} prompt named {name!r} already exists")

    prompt_id = str(uuid.uuid4())
    version_id = str(uuid.uuid4())

    conn.execute(
        "INSERT INTO prompts (id, project_id, name, description, kind) "
        "VALUES (%s, %s, %s, %s, %s)",
        (prompt_id, project_id, name, description.strip(), kind),
    )
    conn.execute(
        "INSERT INTO prompt_versions (id, prompt_id, version, template, "
        "config_json, note, content_hash) VALUES (%s, %s, 1, %s, %s, %s, %s)",
        (
            version_id,
            prompt_id,
            template,
            json.dumps(canonical_config(config)),
            note.strip(),
            content_hash(template, config),
        ),
    )
    return prompt_id, version_id


def add_version_tx(
    conn: Connection,
    prompt_id: str,
    *,
    template: str,
    config: dict[str, Any] | None = None,
    note: str = "",
) -> tuple[str, int, bool]:
    """Append a version. Returns (version_id, version, created).

    `created` is False when the content is byte-identical to the newest
    version, in which case the existing version is returned untouched. Callers
    that treat "save" as idempotent — the scorer editor, which fires on every
    save whether or not the prompt changed — depend on that rather than on
    checking first.
    """
    if not template.strip():
        raise PromptError("Prompt text cannot be empty")

    digest = content_hash(template, config)
    latest = conn.execute(
        "SELECT id, version, content_hash FROM prompt_versions "
        "WHERE prompt_id = %s ORDER BY version DESC LIMIT 1",
        (prompt_id,),
    ).fetchone()

    if latest and latest[2] == digest:
        return str(latest[0]), latest[1], False

    version_id = str(uuid.uuid4())
    next_version = (latest[1] if latest else 0) + 1
    conn.execute(
        "INSERT INTO prompt_versions (id, prompt_id, version, template, "
        "config_json, note, content_hash) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            version_id,
            prompt_id,
            next_version,
            template,
            json.dumps(canonical_config(config)),
            note.strip(),
            digest,
        ),
    )
    conn.execute("UPDATE prompts SET updated_at = now() WHERE id = %s", (prompt_id,))
    return version_id, next_version, True


def rename_prompt_tx(conn: Connection, prompt_id: str, name: str, description: str) -> None:
    """Rename in place. Name and description are identity, not content.

    Deliberately not versioned. A version exists to explain a result, and
    nothing about a result changes because the artifact was renamed — putting
    the name in the version chain would fill the history with diffs that
    changed no behaviour.
    """
    conn.execute(
        "UPDATE prompts SET name = %s, description = %s, updated_at = now() WHERE id = %s",
        (name.strip(), description.strip(), prompt_id),
    )


def create_prompt(
    project_id: str,
    *,
    name: str,
    description: str = "",
    kind: str = "completion",
    template: str,
    config: dict[str, Any] | None = None,
    note: str = "",
) -> dict[str, str]:
    if kind == "completion" and INPUT_PLACEHOLDER not in template:
        raise PromptError(
            f"A completion prompt must contain {INPUT_PLACEHOLDER} — that is where "
            "each test case input is substituted. Without it every case would send "
            "an identical request."
        )
    with get_pool().connection() as conn:
        prompt_id, version_id = create_prompt_tx(
            conn,
            project_id,
            name=name,
            description=description,
            kind=kind,
            template=template,
            config=config,
            note=note,
        )
    return {"id": prompt_id, "version_id": version_id}


def add_version(
    project_id: str,
    prompt_id: str,
    *,
    template: str,
    config: dict[str, Any] | None = None,
    note: str = "",
) -> dict[str, Any]:
    """Append a version to a completion prompt.

    Scorer prompts are refused here on purpose. A scorer's current definition
    lives in the scorers row and its history in prompt_versions; appending
    directly would leave the two disagreeing about what the scorer is, with
    the history claiming a definition the judge would never actually use.
    """
    prompt = _prompt_row(project_id, prompt_id)
    if prompt is None:
        raise PromptError("Prompt not found")
    if prompt["kind"] == "scorer":
        raise PromptError(
            "This prompt belongs to a scorer. Edit it on the Scorers page — saving "
            "there writes the new version and keeps the scorer's live definition in "
            "step with it."
        )
    if INPUT_PLACEHOLDER not in template:
        raise PromptError(
            f"A completion prompt must contain {INPUT_PLACEHOLDER} — that is where "
            "each test case input is substituted."
        )

    with get_pool().connection() as conn:
        version_id, version, created = add_version_tx(
            conn, prompt_id, template=template, config=config, note=note
        )
    return {"id": version_id, "version": version, "created": created}


def update_prompt(
    project_id: str, prompt_id: str, *, name: str, description: str
) -> bool:
    prompt = _prompt_row(project_id, prompt_id)
    if prompt is None:
        return False
    name = name.strip()
    if not name:
        raise PromptError("Prompt name cannot be empty")

    with get_pool().connection() as conn:
        clash = conn.execute(
            "SELECT id FROM prompts WHERE project_id = %s AND kind = %s AND name = %s "
            "AND archived_at IS NULL AND id <> %s",
            (project_id, prompt["kind"], name, prompt_id),
        ).fetchone()
        if clash:
            raise PromptError(f"A {prompt['kind']} prompt named {name!r} already exists")
        rename_prompt_tx(conn, prompt_id, name, description)
    return True


def archive_prompt(project_id: str, prompt_id: str) -> bool:
    """Soft delete. Runs that cite a version of it stay readable and correct."""
    with get_pool().connection() as conn:
        cur = conn.execute(
            "UPDATE prompts SET archived_at = now() "
            "WHERE id = %s AND project_id = %s AND archived_at IS NULL",
            (prompt_id, project_id),
        )
        return cur.rowcount > 0


# --------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------


def set_label(project_id: str, prompt_id: str, label: str, version_id: str) -> str:
    """Point a label at a version, moving it if it already exists."""
    label = _validate_label(label)
    if _prompt_row(project_id, prompt_id) is None:
        raise PromptError("Prompt not found")

    with get_pool().connection() as conn:
        owned = conn.execute(
            "SELECT id FROM prompt_versions WHERE id = %s AND prompt_id = %s",
            (version_id, prompt_id),
        ).fetchone()
        if owned is None:
            raise PromptError("That version does not belong to this prompt")
        conn.execute(
            "INSERT INTO prompt_labels (prompt_id, label, version_id) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (prompt_id, label) "
            "DO UPDATE SET version_id = EXCLUDED.version_id, moved_at = now()",
            (prompt_id, label, version_id),
        )
    return label


def remove_label(project_id: str, prompt_id: str, label: str) -> bool:
    if _prompt_row(project_id, prompt_id) is None:
        return False
    with get_pool().connection() as conn:
        cur = conn.execute(
            "DELETE FROM prompt_labels WHERE prompt_id = %s AND label = %s",
            (prompt_id, label.strip().lower()),
        )
        return cur.rowcount > 0


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def _prompt_row(project_id: str, prompt_id: str) -> dict[str, Any] | None:
    """The prompt itself, archived or not.

    Archived prompts are still readable by id: a run links to the version it
    used, and that link should not 404 because the artifact was tidied away.
    """
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT id, name, description, kind, created_at, updated_at, archived_at "
            "FROM prompts WHERE id = %s AND project_id = %s",
            (prompt_id, project_id),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": str(row[0]),
        "name": row[1],
        "description": row[2],
        "kind": row[3],
        "created_at": row[4].isoformat() if row[4] else None,
        "updated_at": row[5].isoformat() if row[5] else None,
        "archived": row[6] is not None,
    }


def _version_dict(r: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": str(r[0]),
        "prompt_id": str(r[1]),
        "version": r[2],
        "template": r[3],
        "config": json.loads(r[4] or "{}"),
        "note": r[5],
        "created_at": r[6].isoformat() if r[6] else None,
    }


_VERSION_COLUMNS = (
    "pv.id, pv.prompt_id, pv.version, pv.template, pv.config_json, pv.note, pv.created_at"
)


def list_prompts(project_id: str, kind: str | None = None) -> list[dict[str, Any]]:
    """Active prompts with their latest version, version count and labels.

    Counts come from correlated subqueries rather than joins, for the reason
    spelled out in datasets.list_datasets: joining versions and labels together
    multiplies the rows and each count comes back inflated by the other's
    cardinality.
    """
    sql = """
        SELECT p.id, p.name, p.description, p.kind, p.created_at, p.updated_at,
               (SELECT MAX(pv.version) FROM prompt_versions pv WHERE pv.prompt_id = p.id),
               (SELECT COUNT(*) FROM prompt_versions pv WHERE pv.prompt_id = p.id),
               (SELECT COUNT(*) FROM runs r
                 JOIN prompt_versions pv ON pv.id = r.prompt_version_id
                WHERE pv.prompt_id = p.id)
        FROM prompts p
        WHERE p.project_id = %s AND p.archived_at IS NULL
    """
    params: list[Any] = [project_id]
    if kind:
        sql += " AND p.kind = %s"
        params.append(kind)
    sql += " ORDER BY p.updated_at DESC"

    with get_pool().connection() as conn:
        rows = conn.execute(sql, params).fetchall()
        labels = _labels_by_prompt(conn, [str(r[0]) for r in rows])

    return [
        {
            "id": str(r[0]),
            "name": r[1],
            "description": r[2],
            "kind": r[3],
            "created_at": r[4].isoformat() if r[4] else None,
            "updated_at": r[5].isoformat() if r[5] else None,
            "latest_version": r[6],
            "version_count": r[7],
            "run_count": r[8],
            "labels": labels.get(str(r[0]), []),
        }
        for r in rows
    ]


def _labels_by_prompt(
    conn: Connection, prompt_ids: list[str]
) -> dict[str, list[dict[str, Any]]]:
    if not prompt_ids:
        return {}
    rows = conn.execute(
        """
        SELECT pl.prompt_id, pl.label, pl.version_id, pv.version, pl.moved_at
        FROM prompt_labels pl
        JOIN prompt_versions pv ON pv.id = pl.version_id
        WHERE pl.prompt_id = ANY(%s)
        ORDER BY pl.label
        """,
        (prompt_ids,),
    ).fetchall()

    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        grouped.setdefault(str(r[0]), []).append(
            {
                "label": r[1],
                "version_id": str(r[2]),
                "version": r[3],
                "moved_at": r[4].isoformat() if r[4] else None,
            }
        )
    return grouped


def get_prompt(project_id: str, prompt_id: str) -> dict[str, Any] | None:
    """A prompt with its full version history, newest first."""
    prompt = _prompt_row(project_id, prompt_id)
    if prompt is None:
        return None

    with get_pool().connection() as conn:
        rows = conn.execute(
            f"SELECT {_VERSION_COLUMNS} FROM prompt_versions pv "
            "WHERE pv.prompt_id = %s ORDER BY pv.version DESC",
            (prompt_id,),
        ).fetchall()
        labels = _labels_by_prompt(conn, [prompt_id]).get(prompt_id, [])
        # Which versions a replay actually used. The number that tells you a
        # version is load-bearing rather than a draft nobody ran.
        usage = conn.execute(
            "SELECT prompt_version_id, COUNT(*) FROM runs "
            "WHERE prompt_version_id = ANY(%s) GROUP BY prompt_version_id",
            ([str(r[0]) for r in rows],),
        ).fetchall()

    run_counts = {str(u[0]): u[1] for u in usage}
    return {
        **prompt,
        "labels": labels,
        "versions": [
            {**_version_dict(r), "run_count": run_counts.get(str(r[0]), 0)}
            for r in rows
        ],
    }


def get_version(project_id: str, version_id: str) -> dict[str, Any] | None:
    with get_pool().connection() as conn:
        row = conn.execute(
            f"SELECT {_VERSION_COLUMNS}, p.name, p.kind FROM prompt_versions pv "
            "JOIN prompts p ON p.id = pv.prompt_id "
            "WHERE pv.id = %s AND p.project_id = %s",
            (version_id, project_id),
        ).fetchone()
    if row is None:
        return None
    return {**_version_dict(row), "prompt_name": row[7], "kind": row[8]}


def resolve_version(
    project_id: str,
    *,
    version_id: str | None = None,
    prompt_id: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    """Turn whatever the caller referenced into one concrete version.

    Three ways in, one thing out. A run stores the resolved version, never the
    reference: `production` moving next week must not silently rewrite what
    last week's run says it sent.
    """
    if version_id:
        version = get_version(project_id, version_id)
        if version is None:
            raise PromptError("Prompt version not found")
        return version

    if not prompt_id:
        raise PromptError("Give either a prompt version id, or a prompt id")

    prompt = _prompt_row(project_id, prompt_id)
    if prompt is None:
        raise PromptError("Prompt not found")

    with get_pool().connection() as conn:
        if label and label != "latest":
            row = conn.execute(
                f"SELECT {_VERSION_COLUMNS} FROM prompt_versions pv "
                "JOIN prompt_labels pl ON pl.version_id = pv.id "
                "WHERE pl.prompt_id = %s AND pl.label = %s",
                (prompt_id, label.strip().lower()),
            ).fetchone()
            if row is None:
                raise PromptError(
                    f"{prompt['name']!r} has no version labelled {label!r}"
                )
        else:
            row = conn.execute(
                f"SELECT {_VERSION_COLUMNS} FROM prompt_versions pv "
                "WHERE pv.prompt_id = %s ORDER BY pv.version DESC LIMIT 1",
                (prompt_id,),
            ).fetchone()
            if row is None:
                raise PromptError(f"{prompt['name']!r} has no versions")

    return {**_version_dict(row), "prompt_name": prompt["name"], "kind": prompt["kind"]}


# --------------------------------------------------------------------------
# Diff
# --------------------------------------------------------------------------


def diff_versions(project_id: str, from_id: str, to_id: str) -> dict[str, Any]:
    """Line diff between two versions of the same prompt, plus config changes.

    Both halves matter. Reading only the text diff of a scorer whose scale went
    from 1-5 to 1-10 shows nothing at all, while every score it produced before
    and after means something different.
    """
    left = get_version(project_id, from_id)
    right = get_version(project_id, to_id)
    if left is None or right is None:
        raise PromptError("Prompt version not found")
    if left["prompt_id"] != right["prompt_id"]:
        raise PromptError("Those versions belong to different prompts")

    lines = diff_lines(left["template"], right["template"])
    return {
        "from": {"id": left["id"], "version": left["version"], "note": left["note"]},
        "to": {"id": right["id"], "version": right["version"], "note": right["note"]},
        "lines": lines,
        "added": sum(1 for line in lines if line["op"] == "insert"),
        "removed": sum(1 for line in lines if line["op"] == "delete"),
        "config_changes": config_changes(left["config"], right["config"]),
    }


def diff_lines(before: str, after: str) -> list[dict[str, Any]]:
    """A unified line diff as flat ops, ready to render.

    splitlines() rather than split("\\n"): a template that gains a trailing
    newline would otherwise show a phantom empty line as a real change.
    """
    left = before.splitlines()
    right = after.splitlines()
    matcher = difflib.SequenceMatcher(a=left, b=right, autojunk=False)

    out: list[dict[str, Any]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset, text in enumerate(left[i1:i2]):
                out.append(
                    {
                        "op": "equal",
                        "left_no": i1 + offset + 1,
                        "right_no": j1 + offset + 1,
                        "text": text,
                    }
                )
            continue
        # Deletions before insertions within a replace, so a changed line reads
        # as "was this, now that" top to bottom.
        if tag in ("replace", "delete"):
            for offset, text in enumerate(left[i1:i2]):
                out.append(
                    {"op": "delete", "left_no": i1 + offset + 1, "right_no": None, "text": text}
                )
        if tag in ("replace", "insert"):
            for offset, text in enumerate(right[j1:j2]):
                out.append(
                    {"op": "insert", "left_no": None, "right_no": j1 + offset + 1, "text": text}
                )
    return out


def config_changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Every config key that differs, including ones added or dropped."""
    keys = sorted(set(before) | set(after))
    return [
        {"key": key, "from": before.get(key), "to": after.get(key)}
        for key in keys
        if before.get(key) != after.get(key)
    ]


# --------------------------------------------------------------------------
# Scorer backfill
# --------------------------------------------------------------------------


def scorer_config(scorer: Any) -> dict[str, Any]:
    """The half of a scorer's definition that isn't its prompt text.

    Takes anything with the scorer attributes rather than importing the Scorer
    dataclass, which would make scoring.py and this module import each other.
    """
    return {
        "model": scorer.model,
        "max_tokens": scorer.max_tokens,
        "output_type": scorer.output_type,
        "score_min": scorer.score_min,
        "score_max": scorer.score_max,
        "categories": list(scorer.categories),
        "pass_threshold": scorer.pass_threshold,
    }


def backfill_scorer_prompts() -> int:
    """Give pre-step-5 scorers a prompt and a v1. Returns how many were linked.

    Runs at startup and is a no-op once every scorer has one. The v1 it writes
    is the scorer's *current* definition, which is the only honest thing
    available — the text of any earlier edit was overwritten in place before
    this table existed and is not recoverable. Scores from before the backfill
    keep a null prompt_version_id rather than being pointed at a v1 that may
    not be what judged them.
    """
    linked = 0
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT id, project_id, name, description, prompt_template, model, "
            "max_tokens, output_type, score_min, score_max, categories, pass_threshold "
            "FROM scorers WHERE prompt_id IS NULL"
        ).fetchall()

        for r in rows:
            config = {
                "model": r[5],
                "max_tokens": r[6],
                "output_type": r[7],
                "score_min": float(r[8]) if r[8] is not None else None,
                "score_max": float(r[9]) if r[9] is not None else None,
                "categories": json.loads(r[10] or "[]"),
                "pass_threshold": float(r[11]) if r[11] is not None else None,
            }
            # A scorer archived under a name a live scorer now uses would trip
            # the unique index, so the name is disambiguated rather than
            # aborting a startup task over a naming collision.
            name = r[2]
            if conn.execute(
                "SELECT id FROM prompts WHERE project_id = %s AND kind = 'scorer' "
                "AND name = %s AND archived_at IS NULL",
                (r[1], name),
            ).fetchone():
                name = f"{name} ({str(r[0])[:8]})"

            prompt_id, _ = create_prompt_tx(
                conn,
                str(r[1]),
                name=name,
                description=r[3],
                kind="scorer",
                template=r[4],
                config=config,
                note="Imported at the step 5 upgrade — the definition as it stood then.",
            )
            conn.execute(
                "UPDATE scorers SET prompt_id = %s WHERE id = %s", (prompt_id, r[0])
            )
            linked += 1

    return linked
