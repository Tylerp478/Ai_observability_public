"""Postgres connection pool and schema.

Schema is applied idempotently at startup rather than through a migration
tool. That's a deliberate prototype shortcut: one developer, no deployed
instances, no rollback story needed yet. It stops being adequate the moment
there's data you can't recreate — switch to Alembic then.

2a creates projects and api_keys. 2b adds users and sessions to the same
database; 3 adds datasets and runs; 4 adds scorers and scores; 5 adds prompts,
prompt_versions and prompt_labels; 6 adds guardrails, guardrail_checks and
guardrail_results.

Additive changes to an existing table go in as ALTER ... IF NOT EXISTS at the
end of SCHEMA, since CREATE TABLE IF NOT EXISTS is a no-op once the table is
there and would otherwise leave a running database without the new column.
"""

from __future__ import annotations

from psycopg_pool import ConnectionPool

from obs_backend.config import get_settings

_pool: ConnectionPool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS api_keys (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    -- SHA-256 of the plaintext key. Not argon2id, deliberately: API keys are
    -- 256 bits of CSPRNG output, so there is no dictionary to attack and no
    -- work factor needed. Argon2 is for low-entropy human passwords (it is
    -- used for those in 2b) and running it on every ingest request would put
    -- ~100ms of deliberate slowness in the hot path of every span batch.
    key_hash    TEXT NOT NULL UNIQUE,
    -- First 8 chars of the plaintext, for identifying a key in the UI without
    -- storing anything that helps an attacker.
    key_prefix  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    revoked_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS api_keys_hash_idx ON api_keys(key_hash) WHERE revoked_at IS NULL;

-- --- 2b: UI session auth ------------------------------------------------
-- Modelled as a users table even though there is one user, so adding a second
-- later is an INSERT rather than a rewrite. No roles column: CLAUDE.md defers
-- RBAC until asked for, and a column nothing reads is worse than none.
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    -- argon2id hash, including its own salt and parameters. Never plaintext.
    password_hash TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Sessions live server-side so logout can actually revoke them. A signed
-- stateless cookie (JWT) cannot be invalidated before it expires, which
-- makes "log me out everywhere" impossible.
CREATE TABLE IF NOT EXISTS sessions (
    -- SHA-256 of the cookie value. If the database leaks, the hashes in it
    -- cannot be replayed as cookies.
    token_hash  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_agent  TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id);
CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON sessions(expires_at);

-- Login attempts, for rate limiting. Keyed by IP rather than email so an
-- attacker cannot lock out a known account by failing against it repeatedly.
CREATE TABLE IF NOT EXISTS login_attempts (
    id          BIGSERIAL PRIMARY KEY,
    ip          TEXT NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    succeeded   BOOLEAN NOT NULL
);

CREATE INDEX IF NOT EXISTS login_attempts_ip_time_idx ON login_attempts(ip, attempted_at);

-- --- 3: datasets and replay runs ----------------------------------------
-- A dataset is a bag of test cases. The point of the primitive is that a case
-- can come straight off a production trace, so items carry the trace and span
-- they were lifted from — that backlink is what makes "this eval case is real
-- traffic" checkable rather than a claim.
CREATE TABLE IF NOT EXISTS datasets (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, name)
);

CREATE TABLE IF NOT EXISTS dataset_items (
    id              TEXT PRIMARY KEY,
    dataset_id      TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    -- The test case input. Flat text, matching the flat gen_ai.input.messages
    -- the SDK emits today (steps_for_user 3.3). When that becomes structured,
    -- this becomes JSON and the runner's message assembly changes with it.
    input           TEXT NOT NULL,
    -- Optional. Golden output for eyeball comparison in 3; the thing scorers
    -- are handed in step 4. Nullable because most captured traces have no
    -- known-correct answer — recording one is a human act, not a capture.
    expected_output TEXT,
    source_trace_id TEXT NOT NULL DEFAULT '',
    source_span_id  TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS dataset_items_dataset_idx
    ON dataset_items(dataset_id, created_at);

-- One row per replay. prompt_template is stored inline rather than referenced:
-- step 5 makes prompts versioned artifacts, and a run must record what it
-- actually sent regardless. When prompt_versions exists this gains a nullable
-- FK alongside the text, not instead of it — a run whose prompt was edited
-- afterwards has to still show what ran.
CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    dataset_id      TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name            TEXT NOT NULL DEFAULT '',
    prompt_template TEXT NOT NULL,
    model           TEXT NOT NULL,
    max_tokens      INTEGER NOT NULL DEFAULT 1024,
    -- pending | running | succeeded | failed | cancelled
    status          TEXT NOT NULL DEFAULT 'pending',
    error           TEXT NOT NULL DEFAULT '',
    item_count      INTEGER NOT NULL DEFAULT 0,
    completed_count INTEGER NOT NULL DEFAULT 0,
    error_count     INTEGER NOT NULL DEFAULT 0,
    cost_usd        DOUBLE PRECISION NOT NULL DEFAULT 0,
    -- The run's own trace in the span store. A replay is LLM traffic like any
    -- other, so it goes through the same waterfall rather than a parallel
    -- viewer that would drift from it.
    trace_id        TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS runs_dataset_idx ON runs(dataset_id, created_at DESC);

-- Outputs live here as TEXT, not as S3 blobs. CLAUDE.md's blob rule is about
-- keeping the Parquet columns narrow for scan performance; these rows are
-- metadata read one run at a time, never scanned in bulk. The span copy of the
-- same completion is what goes through the columnar path.
CREATE TABLE IF NOT EXISTS run_items (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    dataset_item_id TEXT NOT NULL REFERENCES dataset_items(id) ON DELETE CASCADE,
    ordinal         INTEGER NOT NULL,
    -- pending | running | succeeded | failed | skipped
    status          TEXT NOT NULL DEFAULT 'pending',
    output          TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT '',
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cost_usd        DOUBLE PRECISION,
    latency_ms      DOUBLE PRECISION,
    finish_reason   TEXT NOT NULL DEFAULT '',
    -- Span within the run's trace, so a row links to its own waterfall bar.
    span_id         TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS run_items_run_idx ON run_items(run_id, ordinal);

-- --- 4: LLM-as-judge scorers --------------------------------------------
-- A scorer is a prompt + a model + an output schema. The schema is the part
-- that makes a score a datum rather than a paragraph: the judge is forced to
-- answer through a tool call whose input_schema is generated from these
-- columns, so what comes back is already typed and range-checked instead of
-- prose someone has to parse.
--
-- Three output types, because they cover what the scorers in CLAUDE.md
-- actually need and each aggregates differently:
--   boolean     -> pass rate       (hallucination: yes/no)
--   numeric     -> mean, + pass rate when pass_threshold is set (faithfulness 1-5)
--   categorical -> a distribution  (tone: helpful/neutral/dismissive)
CREATE TABLE IF NOT EXISTS scorers (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    -- Must contain {{output}} — a judge that never sees the output is not
    -- judging the output. {{input}} and {{expected}} are optional.
    prompt_template TEXT NOT NULL,
    model           TEXT NOT NULL,
    max_tokens      INTEGER NOT NULL DEFAULT 1024,
    -- boolean | numeric | categorical
    output_type     TEXT NOT NULL,
    score_min       DOUBLE PRECISION,
    score_max       DOUBLE PRECISION,
    categories      TEXT NOT NULL DEFAULT '[]',   -- JSON array, categorical only
    -- numeric only: at or above this counts as a pass, so a 1-5 scale can also
    -- report a pass rate. Null means the scorer reports no pass/fail at all.
    pass_threshold  DOUBLE PRECISION,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Soft delete. Scores outlive the scorer that produced them: hard-deleting
    -- a scorer would silently rewrite the history of every run it ever scored.
    archived_at     TIMESTAMPTZ
);

-- Partial unique index rather than a table constraint, so a name frees up
-- again once its scorer is archived.
CREATE UNIQUE INDEX IF NOT EXISTS scorers_project_name_idx
    ON scorers(project_id, name) WHERE archived_at IS NULL;

-- One row per judge invocation. A score targets either a replay run item or a
-- span from any trace — the two things CLAUDE.md step 4 asks to attach scores
-- to. Both are kept in one table because everything downstream (aggregation,
-- cost, the judge's own trace) is identical; only where the text comes from
-- differs.
CREATE TABLE IF NOT EXISTS scores (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    scorer_id    TEXT NOT NULL REFERENCES scorers(id) ON DELETE CASCADE,
    -- run_item | span
    target_kind  TEXT NOT NULL,
    run_id       TEXT REFERENCES runs(id) ON DELETE CASCADE,
    run_item_id  TEXT REFERENCES run_items(id) ON DELETE CASCADE,
    trace_id     TEXT NOT NULL DEFAULT '',
    span_id      TEXT NOT NULL DEFAULT '',
    -- pending | running | succeeded | failed
    status       TEXT NOT NULL DEFAULT 'pending',
    -- Normalized number: 1/0 for boolean, the score for numeric, null for
    -- categorical (averaging category indices would produce a meaningless
    -- number that still renders as if it meant something).
    value        DOUBLE PRECISION,
    label        TEXT NOT NULL DEFAULT '',
    passed       BOOLEAN,
    reasoning    TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT '',
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost_usd     DOUBLE PRECISION,
    latency_ms   DOUBLE PRECISION,
    -- The judge call's own span. Scoring is billable LLM traffic, so it is
    -- traced like any other and a score links to the call that produced it.
    judge_trace_id TEXT NOT NULL DEFAULT '',
    judge_span_id  TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT scores_target_ck CHECK (
        (target_kind = 'run_item' AND run_item_id IS NOT NULL)
        OR (target_kind = 'span' AND span_id <> '')
    )
);

CREATE INDEX IF NOT EXISTS scores_run_idx ON scores(run_id);
CREATE INDEX IF NOT EXISTS scores_item_idx ON scores(run_item_id, created_at DESC);
CREATE INDEX IF NOT EXISTS scores_span_idx ON scores(trace_id, span_id, created_at DESC);
CREATE INDEX IF NOT EXISTS scores_scorer_idx ON scores(scorer_id);

-- Scorers to apply automatically when a replay finishes, as a JSON array of
-- scorer ids. On the run rather than the dataset: which judges to spend money
-- on is a property of the experiment, not of the test cases.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS scorer_ids TEXT NOT NULL DEFAULT '[]';

-- --- 5: prompt versioning -----------------------------------------------
-- A prompt is a named artifact whose text lives in immutable versions. The
-- split is the whole point: the artifact keeps moving while a run or a score
-- goes on pointing at the exact text that produced it.
--
-- `kind` says what a prompt is for. Both kinds share this table because
-- history, diffing and labels are identical between them — only the config
-- bag and who reads it differ. Scorers live here rather than in a parallel
-- scorer_versions table, which is the "second versioning scheme" step 4
-- deliberately declined to build.
CREATE TABLE IF NOT EXISTS prompts (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    -- completion | scorer
    kind        TEXT NOT NULL DEFAULT 'completion',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Soft delete, for the same reason scorers have one: versions outlive the
    -- artifact, and hard-deleting would strand every run that cites them.
    archived_at TIMESTAMPTZ
);

-- Unique on kind as well as name. A completion prompt and a scorer may both
-- reasonably be called "Relevance" and they are not the same artifact.
CREATE UNIQUE INDEX IF NOT EXISTS prompts_project_kind_name_idx
    ON prompts(project_id, kind, name) WHERE archived_at IS NULL;

-- Immutable. Nothing UPDATEs a row here — editing a prompt appends. That is
-- what makes "what actually ran?" answerable after the fact.
CREATE TABLE IF NOT EXISTS prompt_versions (
    id           TEXT PRIMARY KEY,
    prompt_id    TEXT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
    version      INTEGER NOT NULL,
    template     TEXT NOT NULL,
    -- Settings frozen alongside the text: model and max_tokens for a
    -- completion prompt, plus the output schema for a scorer. A scorer's
    -- meaning depends on its schema as much as its wording, so a version that
    -- recorded only text would claim to explain a score it cannot explain.
    config_json  TEXT NOT NULL DEFAULT '{}',
    note         TEXT NOT NULL DEFAULT '',
    -- SHA-256 over template + canonicalized config, so saving without
    -- changing anything is refused instead of minting a v7 identical to v6.
    content_hash TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (prompt_id, version)
);

CREATE INDEX IF NOT EXISTS prompt_versions_prompt_idx
    ON prompt_versions(prompt_id, version DESC);

-- Movable pointers, so a run can target "whatever production is" instead of a
-- number someone has to remember to update. `latest` is deliberately NOT
-- stored here: it is MAX(version), and a stored copy is one more thing that
-- can fall out of step with the thing it describes.
--
-- One row per (prompt, label) means promoting is an upsert and a label can
-- only ever point at one version — a label on two versions at once is not a
-- state worth being able to represent.
CREATE TABLE IF NOT EXISTS prompt_labels (
    prompt_id  TEXT NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
    label      TEXT NOT NULL,
    version_id TEXT NOT NULL REFERENCES prompt_versions(id) ON DELETE CASCADE,
    moved_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (prompt_id, label)
);

-- The FK step 3 promised, added ALONGSIDE prompt_template rather than instead
-- of it. Nullable, because a prompt typed straight into the run form is still
-- a legitimate run. SET NULL rather than CASCADE on delete: removing a prompt
-- must not remove the runs that used it.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS prompt_version_id TEXT
    REFERENCES prompt_versions(id) ON DELETE SET NULL;

-- The label the version was resolved through, if any. "Ran production" and
-- "ran v3" can be the same run, but only one of them explains why v3.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS prompt_label TEXT NOT NULL DEFAULT '';

-- A scorer's definition is a prompt of kind 'scorer'. The scorers row stays
-- the current definition, so every step-4 read keeps working untouched; the
-- history lives in prompt_versions beside it.
ALTER TABLE scorers ADD COLUMN IF NOT EXISTS prompt_id TEXT
    REFERENCES prompts(id) ON DELETE SET NULL;

-- Which version of the scorer produced this score. This is the column that
-- closes the step-4 gap: editing a scorer no longer strands its past scores
-- with a number and no way to see the prompt behind it.
ALTER TABLE scores ADD COLUMN IF NOT EXISTS prompt_version_id TEXT
    REFERENCES prompt_versions(id) ON DELETE SET NULL;

-- --- 6: real-time guardrails --------------------------------------------
-- A guardrail is a scorer plus a policy: what its verdict means and what to do
-- about it. The scorer already knows how to judge; the guardrail is the part
-- that turns a judgement into pass/block, which is the only thing an inline
-- caller can act on.
--
-- Kept separate from `scores` deliberately, applying the test 3.6 set for
-- itself: a third target kind was fine to fold in only while the shapes stayed
-- identical, and these diverge. A guardrail check is synchronous, is judged
-- against text that belongs to no run and no stored span, carries a decision
-- and a latency budget, and is written once and read as a log. Forcing it
-- through scores would mean widening the CHECK constraint, a nullable decision
-- column nothing else uses, and every existing aggregate learning to exclude it.
CREATE TABLE IF NOT EXISTS guardrails (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    -- CASCADE rather than SET NULL: a guardrail with no scorer has nothing to
    -- evaluate, so it is not a row worth keeping. Its past checks survive —
    -- they record the verdict text inline.
    scorer_id   TEXT NOT NULL REFERENCES scorers(id) ON DELETE CASCADE,
    -- block | flag. `flag` is shadow mode: the judge runs, the result is
    -- recorded and returned, and the decision is never block. That is how a
    -- new guardrail gets tuned against real traffic without a bad prompt
    -- taking the application down on its first afternoon.
    action      TEXT NOT NULL DEFAULT 'block',
    -- Categorical only: which labels count as a trigger. Boolean and numeric
    -- scorers already answer pass/fail (3.7), categoricals deliberately do
    -- not, and a guardrail is the one place that judgement has to be made.
    block_labels TEXT NOT NULL DEFAULT '[]',
    -- allow | block. What a judge that errored or timed out means. Defaults to
    -- allow: fail-closed turns an API blip into a total outage of the calling
    -- application, which is a bigger failure than one unscreened response in a
    -- prototype. The response always says `degraded`, so a caller who wants
    -- the other policy can have it without this column changing.
    on_error    TEXT NOT NULL DEFAULT 'allow',
    enabled     BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS guardrails_project_name_idx
    ON guardrails(project_id, name) WHERE archived_at IS NULL;

-- One row per call to the guardrail endpoint. This is the log that makes
-- guardrails observable rather than just enforced — what got blocked, by what,
-- and what it cost to find out.
CREATE TABLE IF NOT EXISTS guardrail_checks (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    -- The text that was screened. Stored inline for the same reason run
    -- outputs are (3.4): read one at a time, never scanned in bulk.
    input       TEXT NOT NULL DEFAULT '',
    output      TEXT NOT NULL DEFAULT '',
    -- Free-text caller label, e.g. "support-bot". Lets the log be read per
    -- call site without inventing a second project concept.
    source      TEXT NOT NULL DEFAULT '',
    -- pass | block
    decision    TEXT NOT NULL,
    -- True when at least one guardrail's judge errored or timed out, so the
    -- decision was reached with less information than it looks like.
    degraded    BOOLEAN NOT NULL DEFAULT false,
    latency_ms  DOUBLE PRECISION,
    cost_usd    DOUBLE PRECISION,
    -- The check's own trace. Screening is billable LLM traffic in the
    -- application's hot path, which is exactly the spend worth seeing.
    trace_id    TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS guardrail_checks_recent_idx
    ON guardrail_checks(project_id, created_at DESC);

-- One row per guardrail evaluated within a check. `action` and
-- `prompt_version_id` are copied in rather than joined out: a check has to keep
-- explaining itself after the guardrail is retuned or the scorer is edited,
-- which is the same reason a run stores the prompt it sent.
CREATE TABLE IF NOT EXISTS guardrail_results (
    id           TEXT PRIMARY KEY,
    check_id     TEXT NOT NULL REFERENCES guardrail_checks(id) ON DELETE CASCADE,
    guardrail_id TEXT REFERENCES guardrails(id) ON DELETE SET NULL,
    guardrail_name TEXT NOT NULL DEFAULT '',
    scorer_id    TEXT REFERENCES scorers(id) ON DELETE SET NULL,
    prompt_version_id TEXT REFERENCES prompt_versions(id) ON DELETE SET NULL,
    action       TEXT NOT NULL DEFAULT 'block',
    -- Did this rule fire? Separate from `passed`, because an errored judge
    -- under on_error='block' triggers while having no verdict at all.
    triggered    BOOLEAN NOT NULL DEFAULT false,
    -- succeeded | failed
    status       TEXT NOT NULL DEFAULT 'succeeded',
    value        DOUBLE PRECISION,
    label        TEXT NOT NULL DEFAULT '',
    passed       BOOLEAN,
    reasoning    TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT '',
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost_usd     DOUBLE PRECISION,
    latency_ms   DOUBLE PRECISION,
    span_id      TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS guardrail_results_check_idx
    ON guardrail_results(check_id);
CREATE INDEX IF NOT EXISTS guardrail_results_guardrail_idx
    ON guardrail_results(guardrail_id, created_at DESC);
"""


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(get_settings().database_url, min_size=1, max_size=10, open=True)
    return _pool


def init_schema() -> None:
    with get_pool().connection() as conn:
        conn.execute(SCHEMA)


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
