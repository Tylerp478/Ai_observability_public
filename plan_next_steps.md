# Plan — next steps (post Step 6)

Four problems raised on 2026-07-28, with the decisions made on each. Steps 1–6
are built (SDK, ingest + traces UI, datasets + replay, scorers, prompt
versioning, guardrails). This is what comes after.

Build order is at the bottom and is **not** the order of the sections — the
provider seam is small and comes first because it stops the Playground from
becoming a third hardcoded call site.

> **Status, 2026-07-28.** Items 1, 2 and 3 are built and verified end to end.
> Item 4 (Google auth) is deferred until there is a stable hostname to register
> redirect URIs against — nothing else depended on it. See "Build order" for
> what landed.

---

## Item 1 — Score a prompt's response directly, with no test case

### The problem

There is no way to send one prompt and score what comes back. Today you must
create a dataset, add a test case to it, and run that. Two hard requirements
enforce it:

- `runner.create_run` rejects any template without `{{input}}`
  (`runner.py:149`) — deliberately, because a template without the placeholder
  sends byte-identical requests for every item.
- It also requires a dataset with at least one item (`runner.py:158`).

### What already exists

Both halves of the feature are built; nothing bridges them.

- `scoring.try_scorer` (`scoring.py:1139`) judges output text against a scorer,
  synchronously, without a dataset — but *you* supply the output. It does not
  generate anything.
- `scoring.score_span` (`scoring.py:1084`) scores an arbitrary span from any
  trace, `target_kind='span'`, no dataset involved. It already handles the
  pending-row creation, the background judge job, and the cost cap.

So the missing piece is: generate a completion, write it as a span, hand that
span to `score_span`.

### Decision — a Playground tab

New nav item. Type a prompt, pick a model, tick scorers, run.

- `POST /api/playground` with `{prompt, model, max_tokens, scorer_ids[]}`.
- One LLM call through the new `llm.py` seam (Item 2), span built the way
  `runner._run_one` builds one (`runner.py:421`), written through `SpanWriter`.
- The resulting `(trace_id, span_id)` goes straight to `score_span`. Scores
  land asynchronously; the UI polls, exactly as span-scoring does today.
- **No `{{input}}` requirement.** For a direct run the prompt *is* the whole
  request. An optional input box substitutes if the placeholder happens to be
  present.
- Cost cap already covered by `check_run_scoring_budget(1, n_scorers)`.

Because the output is a real span, it appears in Traces and in the Overview
cost tiles with no extra work — no parallel results viewer to drift from the
real one. That is the same reasoning that put replay runs in the span store
(`runner.py` module docstring).

Two follow-on buttons close the loop back into the existing primitives:
**Save as test case** (into a dataset) and **Save as prompt version**.

### Rejected

Auto-creating a hidden one-item dataset per playground run. It reuses more
code, but it junks up the dataset list and makes "what is a run" mean two
different things.

---

## Item 2 — What the Keys tab is, and what it becomes

### What it is today

Ingest credentials, one direction only: the SDK holds an `obsk_…` bearer token
and *pushes* spans to `/v1/traces`. The key is what tells the backend which
project the spans belong to (`auth.py:102`). It is not API forwarding and not a
place to add KPIs. The page never says this, which is why it reads as "somewhere
credentials go".

### Decision — build for Anthropic, shape it for more

Multiple own apps reporting in (now) and other LLM providers (later, not yet).
The prep for the second is small if done before the Playground exists.

**The provider seam.** Exactly two places construct an Anthropic client today —
`runner.py:332` and `scoring.py:154` — and both hardcode
`gen_ai_provider_name="anthropic"` into the spans they emit. One new module,
Anthropic as the only implementation:

- `complete(model, prompt, max_tokens, timeout) -> Completion` returning a
  normalized result (text, tokens, response model/id, stop reason). Runner and
  Playground call this.
- `judge_call(...)` for the forced-tool-call path.
- Provider derived from the model id, so `gen_ai_provider_name` stops being a
  string literal in two files.
- `estimate_cost_usd` needs nothing — it already returns `None` rather than a
  misleading `0.0` for unknown models (`pricing.py:59`).

**Where the difficulty actually is, for whenever a second provider lands:** the
completion path ports trivially; the judge path does not. Scoring depends on a
forced tool call (`tool_choice={"type":"tool"}`) to guarantee schema-valid
output — the reasoning is in the `scoring.py` module docstring. Every provider
spells that differently and some do it worse. Adding OpenAI later is one easy
file and one careful one; the seam is what keeps it contained.

**The page** splits into two sections: *Ingest keys* (what exists, plus a
sentence saying a key identifies a source and is not a login) and *Sources*
(what is reporting in). Provider credentials get a third section when a second
provider is actually added — not an empty shell before then.

### Sources: `service_name` now, projects only if needed

Spans already carry `service_name`, set per app via `OBS_SERVICE_NAME`
(`sdk/src/obs_sdk/tracing.py:92`) and carried from the OTLP resource down onto
every span (`otlp.py:88`). **Distinguishing sources needs no schema change.**

Making projects first-class is the heavier alternative and is deferred. The
whole app is currently nailed to one project (`_admin_project_id =
ensure_project("default")`, `main.py:76`; every read returns it, `main.py:171`).
Unpicking that means a project selector in `require_any_auth`, a picker in the
header, and cross-project globs in the query layer. Every Postgres table
already carries `project_id`, so the metadata side would come along free —
but only do this if `service_name` proves too coarse in real use.

---

## Item 3 — Filter the Overview by source, total by default

Small once Item 2 picks the model, and the same work either way.

- `GET /api/sources` — distinct sources with span counts. A `SELECT DISTINCT`
  in DuckDB, cheap.
- `GET /api/overview?hours=24&source=…` — one added `WHERE` clause in
  `query.overview` (`query.py:183`). All three tiles and the series honour it,
  so the "$0.42 across 31 prompts averaging 800ms" arithmetic promised in that
  docstring stays true.
- UI: picker above the tiles on `web/app/page.tsx`, default **All**, selection
  mirrored into the URL query param so refreshes and shared links both survive.
  The same control goes on Traces, so the filter means one thing app-wide.
- **Chart stays a single line** for the selected source. Four overlapping
  series is unreadable at 375px, and the existing "View as table" twin already
  carries the detail. A per-source breakdown table under the chart is the
  mobile-safe way to show all sources at once.

---

## Item 4 — Google sign-in, an admin-managed allowlist, and roles

### Scope note

CLAUDE.md line 139 defers RBAC and line 150 rules out OAuth entirely. Both were
scope calls for a single-user prototype, not technical objections, and both are
now being overridden deliberately. **CLAUDE.md must be edited in the same
change** or a future session will helpfully undo this.

### What does not change

The session layer is already the right shape and stays exactly as it is:
server-side sessions in Postgres, hashed cookie value, real revocation, 30-day
sliding expiry. **Google replaces the credential check, not the session.** That
is the smallest correct change and it is what keeps "log me out everywhere"
working.

### The auth split

- **Admin (you)** — password login, seeded from `ADMIN_EMAIL` /
  `ADMIN_PASSWORD`, argon2id, never through Google.
- **Everyone else** — Google only, no password, ever.

This satisfies "don't store user passwords locally" literally: the only
password hash in the database is the admin's. It also means a Google
misconfiguration or an expired OAuth client cannot lock you out of your own
tool. Keep the password form reachable behind a "sign in another way" link
rather than removing it.

### The OAuth flow

Authorization Code + PKCE, exchange server-side so the client secret never
reaches the browser.

- `GET /api/auth/google/start` — redirect to Google with state + nonce held in
  a short-lived HttpOnly cookie.
- `GET /api/auth/google/callback` — exchange the code, verify the `id_token`
  against Google's JWKS, require `email_verified: true`, check the allowlist,
  then mint the **existing** `obs_session` cookie.
- Deps: `httpx` (already present via the anthropic SDK) plus `google-auth` for
  token verification. Roughly 80 readable lines. Authlib does it in fewer but
  hides the part worth reading.

### Schema

Two tables, two lifecycles. `allowed_emails` is a pre-authorization — the row
exists before the person has ever touched the app. `users` is created on their
first successful callback. Anything Google tells us can only live on `users`;
anything you type has to live on `allowed_emails`, or the pending-invite list
is a column of blank cells.

```
allowed_emails
  email        -- the join key
  name         -- what YOU type. "Sarah (work)". Available immediately,
                  before they have ever logged in.
  role         -- admin | viewer. Assigned at invite time.
  note         -- free text, for context you'd write in a sentence
  added_by     -- user id
  added_at
  revoked_at   -- soft delete, so revocation is auditable and re-adding
                  someone does not lose the note

users            (created on first Google login)
  google_sub     -- the stable identity. NOT email, NOT name.
  name           -- from Google's id_token
  email
  last_login_at
  auth_provider  -- google | password
```

Existing `users` rows need `password_hash` made nullable — a Google user has no
password to hash.

**Why two names.** Google's `name` claim is user-controlled text they can change
at will, so it is a display convenience, not an identity. Yours is the label
you will actually recognise them by in a list, and it is the only one that
exists for a pending invite. Show yours, fall back to Google's, and never join
on either — `google_sub` is the identity, `email` is the allowlist join key.

**Why last-seen, not just last-login.** Sessions are 30-day with sliding
renewal, so someone who authenticated once in January and used the tool daily
through March would show a January `last_login_at`. That is a misleading number
on exactly the screen where you decide whether to revoke someone. The session
table already tracks the better signal: `sessions.last_seen_at` (`db.py:76`) is
updated on activity, so `MAX(last_seen_at)` per user is "last actually used the
tool". Show **last seen** as the primary column on the admin page; keep
`last_login_at` on the user row for the audit trail.

Caveat: `purge_expired_sessions()` runs at startup (`main.py:72`), so last-seen
goes null once a user's sessions age out. That reads correctly as "not here
recently", but it is why last-seen cannot be the only record.

### Check the allowlist on every request, not just at login

`require_session` joins the user's email against `allowed_emails` on each call.
Removing someone — or demoting them from admin to viewer — then takes effect on
their next click.

The alternative is that a revoked user keeps their session for up to 30 days
after you have cut them off, which is a bad property for the one screen whose
entire job is controlling access. Sessions are server-side specifically so
revocation is possible (`db.py:66` says as much), so this uses the design as
intended rather than working around it. It costs one indexed lookup per request
on a table with a handful of rows.

Also delete their sessions on revoke — one statement, belt and braces.

### Roles

The sharp edge here is not which pages someone sees. **Almost every interesting
page spends money**: a run, a scorer try, a playground call, a guardrail check
all bill the single `ANTHROPIC_API_KEY`. So the cut is about spend, not
visibility.

| Role | Sees | Can spend | Admin surfaces |
|---|---|---|---|
| **admin** | everything | yes | Keys, allowlist, delete/archive |
| **viewer** | Overview, Traces, Datasets, Prompts, Scorers, Guardrails — read-only | **no** | hidden |
| *member* (later) | same as viewer | yes, with a per-user cap | hidden |

Ship **admin + viewer**. Add `member` only when someone actually needs to run
something.

Enforcement is server-side: a `require_admin` dependency alongside
`require_session`, and every write route checked. A hidden nav item is not
security. The default-deny middleware (`main.py:101`) already establishes the
fail-closed pattern; this extends it rather than fighting it.

### Prerequisites that are yours, not mine

To be added to `steps_for_user.md` when this step starts:

- A Google Cloud Console project with an OAuth 2.0 Web client; client ID and
  secret into `.env`.
- **A stable hostname decided first.** Google requires exact registered
  redirect URIs, so a rotating tunnel hostname means re-registering in Cloud
  Console every single time.
- The OAuth consent screen configured (external, with your email as a test
  user, unless you verify the app).

### Open question

Should a viewer see **trace payloads** — the actual prompts and completions in
the waterfall? That is the most sensitive data in the system, and "can read the
dashboard" and "can read every prompt anyone sent" are quite different grants.
Defaulting to yes, since it is your own traffic. Revisit before showing this to
anyone outside your own projects.

---

## Build order

1. ~~**The provider seam**~~ (`llm.py`, Item 2) — **done.** `complete` and
   `tool_call` with normalized returns; runner and scoring refactored onto it;
   `gen_ai.provider.name` derived rather than hardcoded. `provider_for` raises
   on an unknown model, `provider_label` does not — span builders run on error
   paths, and a raising lookup there would turn one failed test case into a
   failed run.
2. ~~**Playground tab**~~ (Item 1) — **done.** `playground.py` +
   `POST /api/playground` + `/playground`. One completion, written as a real
   span, handed to `score_span`. Save-as-test-case carries the trace and span
   backlink.
3. ~~**Source filter on Overview + Traces**~~ (Item 3) — **done.**
   `GET /api/sources`, `?source=` on both reads, a shared picker holding its
   value in the URL, and a Sources section on the Keys page. Verified on real
   data: per-source prompt counts sum exactly to the unfiltered total.
4. **Google auth + allowlist + roles** (Item 4) — **deferred**, waiting on a
   stable hostname. Touches schema, routes, nav, and CLAUDE.md.
5. **Projects as first-class sources** — only if `service_name` turns out to be
   too coarse once it is in real use.

## Item 2b — multiple Anthropic keys (built 2026-07-29)

Chosen over making projects first-class: a key is a *spending* choice, and
spending is an event rather than a workspace. So the credential is recorded on
the things that spend — runs, scores, guardrail checks, and the spans
themselves — and picked at the point of spending (Playground, run form, scorer
Try-it). Guardrails carry a configured key instead, since they fire with nobody
at the keyboard.

What this gives: billing separation, per-key spend, and a credential filter on
Overview and Traces that composes with the source filter. What it does not
give: isolation. Datasets, prompts and scorers stay in one shared pool, so when
a second person signs in (Item 4) they see all of it. Projects remain the
answer if that ever matters; nothing here makes that harder.

**Secrets are encrypted, not hashed** — the one design point worth
remembering. A password is argon2id and an ingest key is SHA-256 because both
are only ever *compared*; an Anthropic key has to be *sent*, so it must come
back out. The mitigation is that the key material (OBS_SECRET_KEY) lives in
.env, not the database, so a Postgres dump alone decrypts nothing.

  - Lose OBS_SECRET_KEY and every stored key must be re-entered. Back it up.
  - Rotating it means re-encrypting every row; write a script before doing it.
  - Keys are validated against Anthropic on save, so a typo is an edit rather
    than a mid-run failure.

Per-key spend is read from the **span store**, not from `runs.cost_usd +
scores.cost_usd`. Those two know only about eval spend, so a Playground or
guardrail call is invisible to them; every billable call writes a span carrying
`obs.credential`, which is the only place that sees all of it.

The picker renders **even when there is only one key**, disabled. With one key
it is not offering a choice, it is disclosing a fact — *this will bill to X* —
which is the fact an app about watching spend should not make you go and look
up. It lives in `components/credential-picker.tsx` and is used by all five
surfaces, because the first cut of this wired two of them and forgot three,
which is exactly what a shared control prevents.

Guardrails have no picker: each guardrail carries its own configured key, since
it fires server-side with nobody at the keyboard.

Still open: RBAC over which users may spend on which key. That arrives with the
roles in Item 4 — until then anyone signed in can use any key, which is fine
while that is one person.

## Bugs found and fixed while building this

A WAL file in which some column is null in *every* row made
`read_json_auto` infer that column as JSON. Unioning it with the Parquet
branch, where the same column is VARCHAR, then cast VARCHAR to JSON and failed
on the first real span id — `829e6b15c07335a3` is not a JSON document — taking
down the whole traces query until the next compaction folded the file away.

The Playground is what surfaced it: a run writes exactly one parentless span,
so a WAL file whose `parent_span_id` is null in every row became an ordinary
state for the first time. The eval runner and judge write parentless roots too,
but always alongside children in the same batch, so their WAL files never had
an all-null column.

Fixed by reading the WAL with explicit column types (`DUCKDB_WAL_COLUMNS` in
wal.py, derived from `ARROW_SCHEMA` so the two cannot drift) instead of letting
DuckDB infer them. We wrote the file; there was never a reason to guess at its
schema.

**2. One DuckDB connection shared across concurrent requests.** `TraceQuery` is
a module-level singleton and FastAPI runs sync endpoints on a threadpool.
`_rows` called `self.conn.execute(...)`, and DuckDB's `execute` returns the
*connection* — so the result set, `description` and `fetchall` were all
connection state that two overlapping requests interleaved on. One request
would read the other's columns, surfacing as `KeyError: 'hour_start'` on a
query that ran perfectly in isolation.

Pre-existing, but this work made it easy to hit: the Overview page now fires
overview, sources and credentials together. Fixed by giving each query its own
`self.conn.cursor()`. Verified with 120 concurrent calls across 12 threads —
zero errors, every query type returning one consistent value.

**3. The browser served stale cached reads.** The backend sets no cache
headers, which leaves the browser free to apply heuristic freshness to plain
GETs — and it did. The symptom was genuinely misleading: a page rendering an
empty state while the identical URL fetched with a cache-buster returned data,
and a 200 in the network panel for a response the server never sent that time.
This is what was behind the intermittent empty Traces and Sources lists. Fixed
with `cache: "no-store"` in the api.ts fetch wrapper — every read here is live
observability data, where a stale answer is worse than a slow one.
