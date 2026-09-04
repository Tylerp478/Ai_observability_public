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
4. **Google auth + allowlist + roles** (Item 4) — **the allowlist and roles
   are built (2026-09-01)**, on local accounts with admin-issued invites, which
   never needed the hostname. Only the "no passwords for other people" half is
   still open, and GitHub's device flow would do it without a redirect URI at
   all. CLAUDE.md updated in the same change, as this item required.
5. ~~**Projects as first-class sources**~~ — **done 2026-08-31.** A header
   picker, `X-Obs-Project` on every call, projects CRUD minus delete, and the
   15 session routes that reached past their dependencies for the module
   global. The gate ("only if `service_name` is too coarse") now describes when
   to reach for a project rather than whether to build one.
6. **Vendor-agnostic providers** (Item 5) — **Phase 1 built 2026-08-28.**
   Provider registry in `llm.py`, Anthropic moved behind it unchanged, an
   OpenAI-compatible adapter registered for xAI, routing switched from the
   model id to the credential, pricing keyed on `(provider, model)`. No schema
   migration was needed. **All four phases are now built** — cost fidelity,
   Gemini, and the UI, with every rate verified against its vendor. Copilot is
   rejected as a provider — see the item for why.

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

---

## Item 5 — Vendor-agnostic providers (planned 2026-08-28)

### The problem

One vendor is wired up. The ask is to paste a key from xAI, Google, or anyone
else and have runs, scorers, guardrails and the Playground work against it,
with cost and latency reported the same way they are for Anthropic today.

The interesting part is not the API calls. It is that **cost stops being a
lookup and becomes a model**: different vendors bill different *kinds* of
token, and a table that only knows input and output will quietly under-report
on exactly the expensive calls.

### What already exists — the audit

Better than expected, because Item 2's seam held. Verified rather than assumed:

- `llm.` is called from **four** modules only — `runner.py:458`,
  `scoring.py:691`, `playground.py:110`, and guardrails via scoring.
- `llm.tool_call` — the part flagged as hard in the README — has **exactly one
  call site**, `scoring.judge` (`scoring.py:691`).
- Cost is one pure function, `estimate_cost_usd(model, in, out)`, called at
  four sites, always keyed on the **response** model.
- The frontend has one model constant, `RUN_MODELS` (`web/lib/api.ts:763`),
  consumed by six pages.

**No database migration is needed to start.** `provider_credentials.provider`
already exists (`db.py:495`) with no CHECK constraint, and spans already carry
`gen_ai.provider.name` (`models.py:26`) because the OTel convention was
followed. The data model is already vendor-neutral; only the code filling it
is not.

The single line that blocks everything today is `credentials.py:122`:
`if provider != "anthropic": raise`.

### Decision — the credential routes the call, not the model id

`provider_for` currently prefix-matches `claude-` (`llm.py:93`). Extending that
to `grok-`, `gemini-`, `gpt-` looks obvious and is wrong: prefixes collide,
vendors rename, and a model released next week would be rejected by a lookup
table that has not been edited yet.

Every path that spends money already calls `credentials.resolve`, and that row
already names a provider. So:

- **Routing** is `credential.provider` → adapter. Total, never guesses, and a
  brand-new model id works the day it ships.
- **Pricing** is keyed on `(provider, response_model)`. Unknown model still
  returns `None`, which already means "we don't have a price for this" rather
  than "free" — that behaviour is correct and stays.
- **The pre-flight guard survives in a better form.** Today `provider_for`
  raises before spending, which is what stops a typo costing one round trip per
  test case in a replay run. Replace it with a mismatch check: if the model id
  is known to the registry and belongs to a *different* provider than the
  chosen key, refuse. That catches the actual likely error — running
  `claude-opus-5` against an xAI key — while letting unknown ids through.

`provider_label` keeps its total, never-raising contract for span builders, for
the reason given in its docstring.

### Phase 1 — Grok, via an OpenAI-compatible adapter — **BUILT 2026-08-28**

xAI speaks the OpenAI wire format, so one adapter buys Grok plus DeepSeek,
Groq, Together, Fireworks and a local vLLM. That is the highest-leverage first
move and it is why Grok goes first rather than Gemini.

- Add a `Provider` protocol in `llm.py` with the three methods that already
  exist as module functions: `complete`, `tool_call`, `validate_key`. A
  registry dict maps provider name → instance. This is the second real use
  case, so the abstraction is now earned rather than speculative.
- `AnthropicProvider` is the current code moved, not rewritten.
- `OpenAICompatProvider` takes a base URL, so xAI is a two-line registration.
- `tool_call` maps to `tools=[{type:"function", ...}]` with
  `tool_choice={"type":"function","function":{"name":…}}`. The judge's schema
  (`scoring.build_tool_schema`, `scoring.py:580`) is already plain JSON Schema,
  which is the same thing OpenAI-compatible endpoints want — so the judge port
  is closer to mechanical than the README feared.
- `validate_key` uses each provider's models-list endpoint: cheap, unbilled,
  still exercises auth. Same reasoning as `llm.validate_key` today.
- Drop the `provider != "anthropic"` gate; validate the name against the
  registry instead.
- `_anthropic`'s `lru_cache(maxsize=16)` becomes a per-provider client cache
  with the same properties — keyed on the secret so a rotated key cannot serve
  a stale client, bounded so a long-lived process cannot accumulate clients.

**Boot check.** `require_anthropic_key` (`config.py:79`) must relax to "at
least one usable key exists", counting stored credentials rather than one env
var. Keep the refuse-to-start behaviour — the reasoning in that docstring is
still right, only the definition of "a key" widens. `seed_from_env` keeps
adopting `ANTHROPIC_API_KEY` when present, so existing installs are untouched.

**Done when** a Grok key saves on the Keys page, and a Playground run against
it produces a span with `gen_ai.provider.name = "xai"`, a latency, and a cost.

### Phase 2 — cost fidelity

Deliberately before Gemini. The point of this tool is trustworthy numbers, and
Gemini's context-tiered pricing is the hardest pricing case there is — the
pricing model wants to be general *before* the provider that stresses it most
arrives, not retrofitted after.

`_Call` (`llm.py:50`) carries only `input_tokens` and `output_tokens`. Three
things break cost at that resolution:

- **Cached input** is billed differently by every vendor, often ~10× cheaper on
  read and *more* expensive on write. Ignoring it does not round off; it skews.
- **Reasoning tokens** bill as output but are reported separately. Missing them
  under-reports precisely the expensive calls.
- **Context-length tiers** — above a threshold the per-token rate steps up.
  The current table has no notion of a rate that depends on request size.

Work: widen `_Call` with `cached_input_tokens` / `cache_write_tokens` /
`reasoning_tokens`; make the pricing entry a small structure with optional
cache and tier rates instead of a `(float, float)` tuple; give
`estimate_cost_usd` the provider and the total token count.

**This needs no Parquet migration to make the numbers right.** `obs.cost_usd`
is already a promoted column computed at write time, so correct cost lands in
the existing schema. The extra token counts go into `attributes_json` — which
is exactly the case the design note in `models.py` anticipated. Promote them to
real columns only if aggregating on cache-hit rate becomes something worth
doing, and treat that as its own decision.

Pin any new `gen_ai.usage.*` attribute names against the semconv version in
use; these are still pre-stable and the names move between releases.

The Sonnet-5 intro-pricing handling (`pricing.py:41`) is the precedent for
time-boxed rates and should generalize into the same structure rather than
staying a special case keyed on one model id.

### Phase 3 — Gemini

Native SDK, its own function-calling shape, context-tiered pricing that Phase 2
has already made expressible. Self-contained once the registry exists: one new
adapter class, one registration, pricing rows.

### Phase 4 — the UI

- `RUN_MODELS` (`api.ts:763`) stops being a frontend constant and becomes a
  backend read, filtered to providers a key is actually held for. Offering a
  model you cannot pay for is a dead end the UI should not render. Six pages
  consume it and none of their call sites change.
- Keys page gains a provider dropdown (`web/app/keys/page.tsx:173`), and its
  copy stops saying "Anthropic" (`:216`, `:324`).
- `model-mix.tsx:35` hardcodes `claude-haiku|sonnet|opus|fable` as tiers.
  Tiering across vendors is a genuine design question, not a rename — group by
  provider first, then by tier within it.
- The credential picker already discloses which key will pay; it should show
  the provider too, since that is now part of the fact being disclosed.

### Rejected — Copilot as a provider

It does not fit and should not be forced. There is no general-purpose GitHub
Copilot completions endpoint that accepts a pasted key and bills per call — the
API surface is for editor and extension integrations, and the enterprise
metrics API returns seat counts and acceptance rates, not per-call token usage.

Copilot is a *seat licence with usage statistics*, not a metered inference API.
Representing it would mean a separate ingestion path answering a different
question ("what is our per-seat utilisation") against a different data shape,
sharing none of the work above. Worth doing only as its own item, and only
after deciding it is a question this tool is trying to answer.

### Rejected — a provider column on every span table

Tempting, and unnecessary. `gen_ai.provider.name` is already promoted
(`models.py:26`), and per-provider spend rolls up through the credential, which
already carries the provider. Adding a second source of the same truth invites
the two to disagree.

### Rejected — abstracting the SDK's tracing wrapper in the same pass

`sdk/src/obs_sdk/tracing.py` wraps an Anthropic client directly (`:203`) and
hardcodes the provider attribute (`:223`). That is the *observed* application's
instrumentation, not the backend's spending path — a different user, a
different release cadence, and no shared code with any of the above. It should
become vendor-agnostic eventually; doing it inside this item would double the
surface for no gain in what the backend can bill.

### Estimate

| Phase | |
|---|---|
| 1 — registry + OpenAI-compatible adapter (**Grok**) | 1–1.5 days |
| 2 — cost fidelity (cached / reasoning / tiered) | 1–2 days |
| 3 — Gemini adapter | 0.5–1 day |
| 4 — UI | 0.5–1 day |

Roughly **3–5 focused days** for Grok and Gemini done properly. Phase 1 alone,
accepting naive input/output cost as a first cut, is under a day — the adapters
really are easy, because the seam is real. Phase 2 is the part that separates
"it runs" from "the numbers can be trusted", and is the one to resist skipping
in a tool whose entire purpose is watching spend.


## Item 5, Phase 1 — what was built (2026-08-28)

`llm.py` is now a `Provider` protocol (`complete`, `tool_call`, `validate_key`)
plus a registry. `AnthropicProvider` is the old module-level code moved intact;
`OpenAICompatProvider` takes a base URL and is registered once, for xAI.

**Routing is the credential**, as planned. `provider_for`'s `claude-` prefix
match is gone. What replaced the guard it provided is `check_model_matches`,
which fires only when the pricing table already places a model with a different
vendor — so `claude-opus-5` on an xAI key is refused before any spend, while a
model id nobody has seen passes through.

Three things the build turned up that the plan had not:

**1. The boot check had to become a warning.** `Settings.require_anthropic_key`
refused to start without `ANTHROPIC_API_KEY` in .env. That was right when keys
could only come from the environment, and is circular now that they live in
Postgres and are added through the Keys page — a fresh install could never
start the UI it needs in order to be given a key. It is now
`credentials.warn_if_no_keys`, printed after seeding. The guarantee it was
protecting is intact and better placed: `resolve` still refuses on every path
that spends, before any money moves. This is a deliberate departure from
CLAUDE.md's "refuse to start if the LLM API key env var is missing".

**2. Guardrails resolve their credential *inside* the try.** So the except path
could reach `judge_span` with `credential` unbound — a resolve failure is one
of the failures that block exists to record. A `provider = ""` local seeded
before the try fixes it, and `provider_label("")` returning `""` is exactly the
case that function's never-raises contract was written for.

**3. `check_model_matches` in the runner needed wrapping in `RunError`.** The
POST endpoint catches `RunError`; a bare `ValueError` out of `llm` would have
surfaced as a 500 for what is plainly a bad request.

**Tool arguments differ in kind, not just in spelling.** Anthropic returns the
tool input as a dict; an OpenAI-compatible endpoint returns a JSON *string*
that can arrive truncated. Both now collapse onto the existing
`payload is None` contract, so `scoring.judge` reads a cut-off judge the same
way whichever vendor produced it — including the existing max_tokens hint.

Verified with mocked transports: the Anthropic wire format is unchanged
(forced `tool_choice`, `input_schema`, timeout omitted unless set), the xAI
adapter normalizes usage/finish_reason/tool arguments and degrades to 0 tokens
when an endpoint omits `usage`, the guard refuses before any client is
constructed, and a simulated Grok Playground run produces a span with
`gen_ai.provider.name = "xai"` priced at $18.00 for 1M+1M tokens.

**Not yet done for Phase 1:** a real xAI key against the live API. Everything
else is verified against the running stack — `/api/providers` serves both
providers, the Keys page ships the dropdown, and existing Anthropic data and
traces are untouched.

Two environment facts found while verifying, worth keeping:

  - `POSTGRES_PASSWORD` was absent from .env and compose refuses to start
    without it. Added 2026-08-28, hex per the .env.example note.
  - **The host dev servers shadow the compose stack.** A host uvicorn on :8000
    and a host Next on :3000 were already running; compose's backend publishes
    no host port at all, and its web binds `127.0.0.1:3000` while the host Next
    holds the wildcard. So `localhost:3000` resolves to the *host* stack and
    `127.0.0.1:3000` to the *container* — two installs, two databases, one
    plausible-looking URL each. Run one or the other, not both.

**xAI prices in `pricing.py` are the least-confident values in the table** and
are marked VERIFY. Check them against x.ai's published rates before trusting a
spend figure.

## Item 5, Phase 1b — cross-vendor grading (2026-08-28)

Found by running it: adding an xAI key and scoring a Grok completion failed,
because the scorers were being billed to the key that generated the output and
their models are Claude models. The new mismatch guard was reporting it
correctly — the bug was upstream of the guard.

**Generating and grading are separate purchases.** They were always the same
key before a second provider existed, so the Playground handed one credential
to both and `_execute_job` applied one credential to every scorer.
`playground.py` even said so in a comment: *"Same key both ways here"*. That
assumption dies the moment the interesting case arrives — and the interesting
case is most of the point of not being tied to one vendor, since a judge from
the same family as the model it grades is the weakest judge available.

**The scorer's own model decides which key pays for it.** `judge_credentials`
maps each scorer to a credential: the caller's chosen key when it can serve
that scorer (same vendor, or a model this build cannot attribute), otherwise
that vendor's default key. Resolved once up front, so a 40-call job decrypts
each secret once and a missing key fails before anything is paid for rather
than partway through.

The data model needed no change — `scores` already carried `credential_id` and
`generation_credential` as separate columns, from the multiple-Anthropic-keys
work in Item 2b. Only the code that filled them assumed they were equal.

Applied to every judge path, not just the Playground: `score_run`,
`score_span`, `try_scorer` and guardrails all resolve the same way, so a Claude
safety scorer keeps working on a guardrail pinned to an xAI key.

**Disclosure over silence.** The Playground response carries `judged_by`, and
the result footer names the judging key only when it differs from the
generating one — the same instinct as the credential picker rendering while
disabled with a single key: it is not offering a choice, it is disclosing a
fact an app about watching spend should not make you infer. The picker is
relabelled "Generate with" on the two surfaces that generate, because "API key"
overstated what it decides.

**Not done, and the honest limit:** a scorer cannot yet be pinned to a
*specific* key within its vendor — it gets that vendor's default. Guardrails
already model the better version by carrying their own `credential_id`, and a
scorer-owned key is the natural refinement if two keys for one vendor ever need
to be told apart for scoring. Left until that is a real need rather than a
hypothetical one.

Also fixed here: both new error messages phrased without an indefinite article
before a vendor name. "a Anthropic key" / "an Google key" is the bug that
writes itself the next time a provider is registered.

## Item 5, Phase 4 (partial) — model lists follow the key (2026-08-28)

Selecting the xAI key still offered Claude models. The backend refused the
pairing before spending, so nothing was ever mischarged — but a dropdown that
lets you pick a guaranteed error is a poor way to learn the rule.

**Providers now declare their own models** (`llm.py`), served through
`/api/providers`. Curated rather than derived from the pricing table: that
table also carries legacy and dated ids which exist to cost old traces, and
offering those would invite new spend on a deprecated model.

**The invariant is enforced instead of asked for.** The old frontend list
carried a comment asking whoever edited it to keep it in step with the pricing
table, because a model missing from that table runs fine and silently reports
no cost. `_check_offered_models_are_priced` runs at import and refuses to start
if an offered model has no price. In a tool whose job is watching spend, a
model you can select but cannot cost is the worst kind of bug — it looks like
it worked.

**Two questions, one hook.** `useModels` distinguishes surfaces that *spend*
from surfaces that *define*:

  - Playground and the replay run form pass the selected credential, and get
    only that vendor's models.
  - A scorer's judge model and a prompt's config pass nothing, and get every
    model from every provider a key is held for — because a scorer's model is
    what *chooses* which vendor grades with it (Phase 1b), so narrowing it to
    one key would hide exactly the cross-vendor judges worth having.

Neither ever offers a model from a provider with no key.

Two details worth keeping:

  - **Derived, not synced.** `effectiveModel` is computed from the offered list
    each render rather than pushed into state by an effect, so changing the key
    cannot leave a stale selection the backend would reject.
  - **[] while loading, not a guess.** Falling back to a Claude model during the
    fetch would show the wrong model to someone holding only an xAI key and
    then swap it under them. Callers render an empty select for that instant
    and guard submit on having a model.

In the dataset run form the key gets the last word over a saved prompt version:
a version written against a Claude model falls back to something the chosen key
can serve, visibly, rather than failing on submit.

`RUN_MODELS` is gone. `FALLBACK_MODELS` replaces it and is explicitly not a
place to add models — anything there that the backend does not offer would be
selectable and then rejected.

**Still open in Phase 4:** `model-mix.tsx` still tiers on `claude-*` prefixes,
so Grok spans fall outside its grouping. Tiering across vendors is a design
question, not a rename.

## Item 5, Phase 2 — cost fidelity (built 2026-08-28)

**The vendors disagree about what a token count means, and both readings look
plausible.** Anthropic's `usage.input_tokens` counts only tokens that were
neither read from nor written to the cache; an OpenAI-compatible
`usage.prompt_tokens` already includes cached tokens. Passing either through as
"the prompt size" is wrong for the other one, and the failure is silent — you
get a believable number and only the invoice disagrees.

`_Call` now defines the inclusive reading: `input_tokens` and `output_tokens`
are totals, with `cached_input_tokens`, `cache_write_tokens` and
`reasoning_tokens` as named *subsets*. Each adapter normalizes into it —
Anthropic's by reassembling the total, the OpenAI-compatible one by passing it
through and extracting the parts. Anthropic calls its reasoning counter
`thinking_tokens`; that is normalized too, so no caller learns which vendor
answered.

**What this actually fixes, in order of how much it mattered:**

1. **xAI cached input was billed at the full rate.** xAI caches automatically,
   with nobody opting in, so this was live from the moment a Grok key was
   added. A call with an 80% cache hit was reading **67% high**.
2. **Anthropic's total prompt size was under-reported** whenever caching was
   used — the cached part was simply missing from the span. Not live today
   (nothing here sets `cache_control`) but wrong the moment anything does, and
   wrong by more the better caching works.
3. **Reasoning tokens were invisible.** This was never a *cost* bug: they are
   already inside the output count and billed at the output rate, so the totals
   were right. It was a reporting gap — "what did thinking cost me" had no
   answer. Recorded now, still priced as output, deliberately not passed to
   `estimate_cost_usd` because that would double-count.

A price is now a `ModelPrice` — base `Rates`, optional cache read/write rates,
an optional long-context tier keyed on input size, and a promotional window.
The Sonnet 5 intro pricing was a hardcoded special case keyed on one model id;
it is now just a `promo` with a `promo_ends`, and produces the same numbers
either side of the boundary. Anthropic's cache multipliers (0.1x read, 1.25x
5-minute write) hold across the model line, so a helper encodes them once and
each row stays the two numbers that actually differ.

Precedence where a model has both a long-context tier and an active promo: the
tier wins. Introductory rates are advertised against standard context, so
applying them to a long-context call would understate the bill. No model
currently has both; the rule is written down so the first one that does is not
a surprise.

**No Parquet migration, as predicted.** `obs.cost_usd` is a promoted column
computed at write time, so correct cost lands in the existing schema. The new
counts go into `attributes_json` under `obs.*` — deliberately not `gen_ai.*`,
because the GenAI conventions are pre-stable here and have not settled on names
for cached or reasoning tokens. Squatting on a `gen_ai.usage.*` name that later
means something else is worse than a namespaced one that has to be renamed.
Zeros are omitted rather than written, since most calls cache nothing.

**Not modelled, deliberately:** Anthropic's 1-hour cache-write TTL is 2x base
against the 5-minute 1.25x, and `usage.cache_creation` reports the split — but
nothing in this app sets `cache_control` at all, so pricing the distinction
would be building for a caller that does not exist.

**Still the weakest numbers here:** the xAI rates, and among them the
cached-input multiplier (0.25x base) most of all. It is now load-bearing in a
way it was not before — it applies automatically, to every Grok call with a
cache hit.

## Item 5, Phase 3 — Gemini (built 2026-08-30)

Its own adapter rather than an OpenAI-compatible base URL. Google publishes a
compatibility endpoint, but going native buys the thing that matters:
`FunctionDeclaration.parameters_json_schema` takes the judge's plain JSON
Schema verbatim, so the scorer schema needs no translation into Gemini's own
`Schema` type and cannot drift from what the other two providers are sent.

**A third token convention, and the one that bites hardest.** The SDK documents
`total_token_count` as the sum of `prompt_token_count`,
`candidates_token_count`, `tool_use_prompt_token_count` and
`thoughts_token_count` — so those four are *disjoint*, which settles the
question the other two vendors answer differently:

  - `candidates_token_count` does **not** include thinking tokens, unlike
    Anthropic's and OpenAI's output counts, which do. Gemini 2.5 thinks by
    default and thinking bills as output, so reading it as the output total
    under-reports by however much the model thought. In the test case that is
    8,000 of 10,000 output tokens — an 80% under-report.
  - `prompt_token_count` does **not** include tool-use prompt tokens, which are
    billed as input, so they are added back.
  - `prompt_token_count` *does* include cached content, stated explicitly in
    the field docs, so cached stays a subset rather than being added twice.

Three vendors, three conventions, and every one of them produces a plausible
number under the wrong reading. That is the argument for `_Call` defining one
inclusive meaning and each adapter converting into it, rather than each caller
learning who answered.

**Two traps worth naming:**

  - **Gemini's timeout is an int of milliseconds**, where ours is float
    seconds. Passing it through would ask for a 30ms deadline and fail every
    call — with a timeout error, which reads like a slow model rather than a
    units bug.
  - **`validate_key` has to consume the listing.** `models.list()` is lazy; a
    bare call would validate nothing and report success on a bad key.

**A Phase 1 bug found while doing this.** `scoring.judge`'s "raise max_tokens"
hint tested `stop_reason == "max_tokens"`, which is Anthropic's spelling. An
OpenAI-compatible endpoint says `length` and Gemini says `MAX_TOKENS`, so the
hint never fired for a Grok judge — precisely the case where the advice was
needed. There is now a `_Call.truncated` property covering all three
spellings; `stop_reason` keeps the vendor's own word, because that is what the
span should record.

**The long-context tier built in Phase 2 has its first real user.** Gemini 2.5
Pro roughly doubles every rate above 200k input tokens. A judge fed a long
transcript crosses that line without anyone deciding to, which is exactly the
case a flat rate would silently under-report. Verified firing at the boundary:
200,000 tokens prices at base, 200,001 at the long tier.

`key_hint` moved onto the registry too, so the Keys page paste-field
placeholder comes from the same place as everything else about a provider —
the two-way ternary it replaced was already wrong for a third vendor.

**Not modelled:** Gemini's explicit context caching bills storage per hour
rather than a per-token write rate, which this table cannot express and this
app cannot trigger — nothing here creates a cached-content handle. Cache
*reads* are priced normally.

**Rates entered 2026-08-30 and unverified**, including the 200k threshold.
Check against https://ai.google.dev/gemini-api/docs/pricing.

## Item 5, Phase 4 — model mix tiering (built 2026-08-30)

The last open piece. `model-mix.tsx` ranked models with a hardcoded ladder of
`claude-*` prefixes, so six of the nine selectable models rendered
off-ladder grey the moment a second and third provider existed.

**The fix was to notice what the ramp was already encoding.** The component's
own docstring argued for a single-hue ordinal ramp on the grounds that
haiku → sonnet → opus → fable is "a real capability and price ladder". Price is
the ordering; the prefix list was a hand-maintained proxy for it. So the tier
now comes from the pricing table — banded on the output rate, which is the
number that actually separates these models, since input rates cluster far more
tightly and output is where a real workload's bill is decided.

Consequences worth having stated:

  - **A new vendor needs no edit to this component.** Price a model and it is
    ranked; the frontend keeps no ladder of its own. This is the same lesson as
    `RUN_MODELS`: a list in the UI that must be "kept in step" with the backend
    will drift, so it should not exist.
  - **The ramp no longer distinguishes vendor.** A Grok and a Claude model in
    the same price band get the same colour, which is the honest reading of a
    price scale. Vendor moved to the legend, where the model names say it
    plainly — and the legend was already load-bearing, since slices are never
    colour-alone. `shortName` therefore stopped stripping the `claude-` prefix:
    that was pure noise when every model carried it, and hides the one
    attribute the colour no longer encodes now that it does not.
  - **Bands are fixed, not derived from the models present.** Colour follows
    the model and must not move because a different model was added next to it,
    which is the same rule the old lookup followed. Verified.
  - **Anthropic models shifted one rung** (haiku 0→1, sonnet 1→2, opus 2→3),
    because the cheap tier is now occupied by Grok and Gemini models that
    genuinely are cheaper than any Claude model. A one-time recalibration, not
    a drift.

### A costing bug found on the way

The tier lookup and the cost lookup key on the same thing, which is what
surfaced it: **Gemini reports a build suffix** (`gemini-2.5-pro-002`) as its
answering model, and no pricing table will carry that. Every Gemini call would
have reported *no cost at all* — a plausible-looking blank on a dashboard whose
entire job is spend.

`estimate_cost_usd` now takes `request_model` and falls back to it when the
answering model is unpriced. Pricing still follows the answer first, because an
alias resolving to a dated id means the dated id is what was billed; the
requested model is a safe floor because it is always one this app offered, and
every offered model is priced by the boot-time invariant.

The mix chart itself was never exposed to this — the overview groups by
`gen_ai_request_model` (`query.py:522`), not the response model.

## Item 5 — pricing verified against the vendors (2026-08-30)

The rates had been entered from memory and marked VERIFY. Checking them found
more than wrong numbers.

### xAI: the models were retired, and the app never noticed

`grok-4-fast-reasoning`, `grok-3` and `grok-code-fast-1` were **retired on
2026-05-15**. Two of the three models this app offered no longer existed.

**xAI does not reject a retired id — it answers with the replacement.** A span
already in this project proves it: `gen_ai_request_model` says `grok-3-mini`
and `gen_ai_response_model` says `grok-4.3`. Because `grok-4.3` was not in the
pricing table, `obs_cost_usd` was **None** on all three Grok calls ever made
here. Not an error, not a zero anyone would question — a blank, on the one
dashboard whose job is spend.

Three separate failures had to line up, and each is worth keeping:

  - offering models nobody checked were still alive;
  - a vendor that silently substitutes rather than failing, so nothing surfaced;
  - pricing keyed on the *answering* model, which is right, but with no
    fallback when the answer is unpriced. The `request_model` fallback added
    with the tiering work would have caught this — at guessed rates, so it is a
    safety net rather than a substitute for checking.

Offered models are now `grok-4.6`, `grok-4.5`, `grok-4.3`. The retired ids stay
**priced at what they actually bill** — the replacement's rate, not their
historical one — because that is what the invoice says, and because the
dashboard groups by the *requested* model, so dropping them would grey out real
history and blind `provider_of_model`.

Every xAI text model is also tiered at 200k input, which was not modelled at
all. All the long-band rates are exactly 2x, so a helper encodes the doubling
once.

### Gemini: base rates right, cache rates 2.5x too high

The base rates and the 200k threshold on 2.5 Pro were correct as entered. The
cache rates were not: they had been derived from an assumed 0.25x multiplier
when Google's actual discount is 0.10x. Pro $0.3125 → $0.125, Flash $0.075 →
$0.03, Flash-Lite $0.025 → $0.01. An over-estimate rather than an under-one,
but wrong in the direction that makes caching look less worthwhile than it is.

Google publishes Batch, Flex and Priority tiers at 0.5x and 1.8x of Standard.
This app uses Standard; the others are noted in the table rather than modelled.

### The vendors disagree about the tier boundary by one token

Google charges the high band *above* 200k. xAI charges it *at or above* 200k.
`long_context_threshold` is therefore documented as "the largest input that
still gets base rates" — 200_000 for Gemini, 199_999 for xAI — which expresses
both with one comparison instead of a second flag. Verified at the boundary in
both directions.

### Anthropic

Not re-checked. Still as of 2026-07-27, and now the only unverified provider in
the table.

## Item 5 — Gemini 3.x added (2026-08-31)

Offered models are now `gemini-3.7-flash`, `gemini-3.6-flash`,
`gemini-3.5-flash-lite` and `gemini-2.5-pro`. The 2.5 Flash models stay priced
but stop being offered — `gemini-3.5-flash-lite` is priced identically to
`gemini-2.5-flash` and is newer, so keeping both would be two names for the
same trade.

**2.5 Pro stays as the Pro option on purpose.** The only Pro-class model in the
3.x line is `gemini-3.1-pro-preview`, and it is still preview. Offering a
preview id in a tool that reports spend is how you end up billing a model that
changed under you — which is precisely what the xAI retirement did. It is
priced, so a span carrying it still costs and `provider_of_model` can claim it
for the mismatch guard; it is just not in the dropdown.

**The two newest Flash models are on introductory pricing that doubles on
2027-01-01**, so `base` holds the post-promo list price and `promo` holds what
is actually charged until then — the same shape as the Sonnet 5 intro rate.
Entering today's price as `base` would look right today and quietly halve every
estimate in January. Verified across the boundary: $4.50 per 1M+1M through
31 December, $9.00 from 1 January, with the cached rate following the promo too.

**`price_tier` reads `base`, never the promotional rate.** A promo lapses on a
date; a model whose colour changed overnight on the dashboard — without its
capability or its place in the lineup changing — would be reporting a calendar
event as a category change.

## Item 5 — Anthropic pricing verified (2026-08-31)

The last unverified provider, checked against the `claude-api` skill's model
table rather than from memory. **All nine current models matched as entered**,
and so did the two cache multipliers the `_anthropic` helper encodes: reads at
0.1x base input, 5-minute writes at 1.25x.

Also confirmed as a deliberate absence rather than an oversight: Anthropic's 1M
context window carries **no long-context premium**, which is why no Anthropic
entry has a `long` tier where every xAI model and Gemini 2.5 Pro do.

Four legacy rows remain unverifiable — `claude-opus-4-5`,
`claude-opus-4-1-20250805`, `claude-sonnet-4-5-20250929`,
`claude-3-haiku-20240307`. They are retired or superseded, so no current source
lists them and there is nothing left to check against. They stay because this
table doubles as the historical cost table, and dropping a row would silently
un-cost any old span carrying it. Checked against the span store: no model in
this project uses one, so the exposure today is zero.

**Two rates this table structurally cannot express**, both now documented in
the module docstring rather than silently missing:

  - **Fast mode.** `speed: "fast"` on Claude Opus 5 bills $10/$50 rather than
    $5/$25 — a *different rate for the same model id*.
  - **Batch API.** 50% of standard, same shape of problem.

Pricing either would need a third key dimension beyond `(provider, model)`.
Nothing in this app sets `speed` or uses Batches, so no call can currently
produce one; the note exists so the gap is a known limit rather than a
discovered surprise.

With this, every rate in the table is either verified against its vendor or
explicitly marked as unverifiable legacy. That closes the pricing work opened
in Phase 2.

## Item 5, Phase 4 — the credential picker names its provider (2026-08-31)

The last open bullet of Phase 4, and the one the rest of the phase created.
`CredentialPicker` disclosed *which key pays* — the fact worth showing when
every key was Anthropic. Once a key could be Anthropic, xAI or Google, the
disclosure was half a fact: "prod" and "prod-eu" say nothing about which vendor
they reach, and since Phase 4's first bullet the model dropdown silently
follows the selected key. Picking a key and watching the model list change
underneath was the only way to learn what a key was for.

Options now read `Grok test account · xAI`.

**In the option text, not an `<optgroup>` header.** Grouping reads better with
the menu open and is worth nothing with it closed — a native select renders
only the chosen option's text, and closed is the state you read before pressing
Run. The provider sits before the `(default)` marker so that when the compact
select truncates at 230px, what falls off the end is the marker rather than the
vendor. The full string is on `title` either way.

**One definition of a provider's name.** The Keys page had a local
`providerLabel` over `/api/providers`; that is now `useProviderLabel` in
`use-models.ts`, which already owned the registry query, and both surfaces read
it. Two places rendering the same registry under two names was a drift waiting
to happen, not a duplication worth keeping.

Verified in the app: Playground shows `Default (from .env) · Anthropic
(default)` and `Grok test account · xAI`, selecting the xAI key swaps the model
list to `grok-4.6 / 4.5 / 4.3`, the compact picker on a scorer's Try it
truncates the marker and keeps the vendor, and the Keys page labels are
unchanged. Clean typecheck, lint and console.

With this, **Item 5 Phase 4 is complete**, and so is Item 5.

## Projects as first-class sources (built 2026-08-31)

Build order item 5, gated in Item 2 on `service_name` proving too coarse. It
was built on request rather than because that gate tripped — and the gate is
still the right description of when to *use* one, which is now in the UI copy:
a project is for an app that should have its own datasets, scorers, prompts and
bill, and telling apart apps that share those is still a source filter.

### What was already true

Almost all of it. Every Postgres table has carried `project_id` since step 2a,
the span store has always been partitioned `traces/project=<id>/dt=…`, and
`TraceQuery._spans_relation` has always taken a project. **35 of the 50 routes
already declared `project_id: Depends(require_any_auth)`** and needed no change
at all. The column was true and inert: `ensure_project("default")` at boot was
the only value it ever took.

So this was not a data-model change. It was removing one pin.

### The pin, and the 15 routes around it

`require_any_auth` returned `_admin_project_id` for any cookie request, and 15
session-only routes — keys, provider credentials, run creation, scoring, the
Playground — reached past their dependencies for the same module global. Those
now take `require_session_project`, which exists mostly so the next route added
here cannot quietly reach for a global again. The global survives under a
truer name, `_default_project_id`: what a request lands in when it names none.

### A header, not a cookie

The browser names its project in `X-Obs-Project`. A cookie would have been less
code and is the wrong shape: it is ambient, so it would ride along on ingest
and on `curl` calls that never chose a project, and *silently writing to the
wrong project* is the failure that cannot be undone.

Two rules make the seam safe, and both are tested:

  - **A key's project always wins.** A bearer key presented with a header
    naming a different project is a **403**, not a quiet fall back to the key's
    project. The two disagree about what was asked for, and guessing is how a
    script reports on the wrong app.
  - **An unknown project id is a 400**, not a fall back to the default.
    Answering with a different project's spend is exactly the lie this app
    exists not to tell. `GET /api/projects` is the single deliberate exception
    — it reports which project it resolved instead of validating, because a
    client holding a stale id needs one route that still answers to escape.

### No deleting, and no across-projects view

Both are absences on purpose, argued in `projects.py`. Postgres would cascade
where the object store cannot, so a delete leaves Parquet under
`traces/project=<id>/` that nothing can name again; rename covers what actually
comes up. And an "all projects" mode would work on the two pages backed by the
span store and silently ignore itself on the six backed by a boundary that
exists to keep things apart.

### Two bugs found in the client, both mine, both about the same thing

The UI half is small — a header picker, a `Projects` section on the Keys page,
one header in `request()` — and it went wrong twice, in the two places where
project state has more than one source.

**`queryClient.clear()` does not refetch.** The first switch wrote the
selection, emptied the cache and left the page showing the old project's
numbers, because clearing removes entries without asking any observer to fetch
again. `resetQueries` is the primitive that does both. `invalidateQueries` is
wrong here too, in the other direction: it keeps serving the old answer until
the new one lands, which is the mislabelled number, just briefly. The projects
list itself is excluded — it is the one query that is not project-scoped, and
blanking it unmounts the picker mid-switch.

**Two sources of truth for the selection raced each other.** `?project=` in the
URL began as a peer of `localStorage`, read fresh on every request. Switching
while a param was present then did something worse than nothing: the picker
wrote the new project, the refetch fired, and the still-present param sent
every one of those calls back to the old one. The param is now an *input* to
storage rather than a rival — adopted synchronously on the first read of a page
load, never consulted again — so a shared link still opens in its own project
and a switch cannot be undone by the URL it happened on.

Their common shape is worth keeping: **the second source of truth was always
the stale one.** The same mistake appeared a third time in the recovery effect,
which adopted the backend's `current` on any mismatch. Stripping `?project=`
remounts the picker, the effect re-ran against a cached `current` that was
still the old project, and the switch reverted. It now fires only when the
stored id names no project the backend knows — recovery from a reset database,
which is all it was ever for.

### Verified

End to end against the real install, with a second project created for it:

  - the default project reads 35 prompts and $0.084 over 30 days; the second
    reads 0 with the same window selected, and its Datasets, Sources, ingest
    keys and provider keys are all empty;
  - creating a dataset under the second project makes it visible there and not
    in the default, and fetching it by id from the default is a 404;
  - an ingest key reads its own project, is refused with 403 against another,
    and cannot enumerate projects at all;
  - an unknown project id is a 400 everywhere except `/api/projects`;
  - `?project=<id>` opens a link in that project, and switching away from it
    holds through navigation.

The second project ("Staging env") was removed afterwards with a direct
`DELETE FROM projects`, which is the only way there is one — checked first
against all nine `project_id` tables (0 rows) and against the span store (no
`traces/project=…` partition), so nothing cascaded and nothing was orphaned.
That check is exactly the work the absent route would have had to do, and the
reason it is absent: it can only be done honestly by looking at both stores.

A production `next build` passes with all 12 pages prerendered, which is what
confirms the shell's Suspense boundary — the picker calls `useSearchParams`
from the chrome every page renders inside, so without it every page would have
failed to prerender rather than just one.

### Still open

Provider keys are per-project, which means a new project cannot spend until a
key is pasted into it. That is the honest reading of the schema and of what a
project is for, and the empty state says so — but it is the one place where the
boundary costs real setup, and sharing a credential across projects is the
alternative if that becomes annoying.

## Item 4, without Google — an allowlist and two roles (built 2026-09-01)

Item 4 bundled three things and deferred all of them behind one blocker. Only
one of the three actually needed a third party: **not storing other people's
passwords**. The allowlist and the roles never did, and they are the part that
protects anything, so they were built on the local-accounts path that already
existed.

Worth recording, because the deferral note is misleading as written: local dev
was never blocked. Google accepts `http://localhost:3000/...` as a registered
redirect URI — loopback is exempt from the HTTPS rule. The rotating hostname is
a problem for a *deployed* instance, not for running this.

### What did not change, exactly as the plan promised

The session layer. Server-side rows in Postgres, hashed cookie value, real
revocation, 30-day sliding expiry — untouched. Both new ways in mint the same
`obs_session`. "Google replaces the credential check, not the session" turned
out to describe an invite just as well.

### The role lives on the allowlist, not on the user

One decision everything else follows from. `allowed_emails` is checked on
**every request** anyway — that is what makes a revocation take effect on the
next click rather than up to 30 days later — so reading the role from the same
row costs nothing and cannot drift from it. A `users.role` column would be a
second copy of the answer, and the two would disagree in exactly the window
that matters: between a demotion and that person's next login.

Verified: promoting a signed-in viewer to admin, then demoting them, changed
what their **existing session** could do on the very next request, with no
re-login either way.

### Enforcement is one middleware check, not thirty decorators

`default_deny` already establishes that a route added without the right
dependency fails closed. The role gate extends it rather than competing with
it: a cookie-authenticated request with a non-GET method and a viewer's role is
refused before it reaches any route. Thirty-odd write routes each remembering
`Depends(require_admin)` is thirty chances to forget.

It runs **only on writes** — a viewer reads everything, so gating GETs would
buy nothing and would put a second session lookup in front of every poll on a
live dashboard. Bearer requests are untouched: an ingest key has no role and
POSTing spans is its whole purpose. Exactly one write is exempt, `logout`; a
read-only user trapped in a session they cannot end would be a worse outcome
than anything this prevents.

`require_admin` still exists, for the handful of **reads** that are admin-only
— ingest keys, the allowlist, and provider credentials. That last one is a
judgement: key names, their last four and what each has cost are purely about
spending, and a viewer cannot spend. Both controls built on it (the credential
picker, the Overview "which key paid" filter) already render nothing when the
list is empty, so they degrade into the honest answer rather than breaking.

### The invite link is built from `window.location.origin`

The piece that makes this work with no fixed hostname, and the reason it suits
this app better than OAuth would. No hostname is configured anywhere; the URL
the admin is already looking at is the one URL known to reach the backend, so
the client builds the link from it. Correct on localhost, on a tunnel, and on a
deployed box without any of them being written down.

There is no email delivery, deliberately — inventing SMTP config to transport a
string would be more machinery than the string. The token is shown once and
stored as a SHA-256 hash, the same as an ingest key and a session cookie.

The accept page confirms **who the invite is for** before asking for anything.
An opaque link that immediately demands a password is indistinguishable from a
phishing page; showing the address lets the recipient check it is theirs. That
reveals one email to whoever already holds that email's single-use token, which
is not a disclosure — it is what they were sent.

### The lockout escape hatch

Because the allowlist is now consulted on every request, a bad edit to it could
lock everyone out — including out of the page that fixes it. `seed_admin`
therefore re-asserts the `.env` admin as an accepted, un-revoked **admin** on
every boot. That is also why the API refuses to revoke or demote that address:
the change would not survive a restart, and silently undoing an action is worse
than declining it. The error says what is actually true — that account is
defined by `.env`, so change it there.

### Verified end to end

  - invite → accept → signed in, with the token refused on replay and a
    password under 12 characters refused before an account exists;
  - `Ada@Example.TEST` invited, `ada@example.test` stored and
    matched at login — the allowlist join is on a normalized address, so a
    capitalised invite does not lock out the person it was sent to;
  - a viewer reads Overview, Traces, Datasets, Scorers, Prompts, Guardrails and
    Projects (all 200) and is refused ingest keys, the allowlist and provider
    credentials (403);
  - every write refused for a viewer — datasets, playground, invites, project
    rename — while logout still works;
  - revoking killed a **live** session immediately and blocked re-login;
  - the `.env` admin cannot be revoked or demoted (400, with the reason);
  - in the browser: the Keys nav item disappears, typing `/keys` gives "This
    page is for admins" instead of a shell full of dead buttons, the Playground
    Run button is disabled with a reason even once a prompt is typed, and a
    read-only banner explains the shape of it in one sentence.

Production build passes with all 13 routes prerendered.

### Still open

  - **`member`** — spend, but no admin surfaces — is still not built. The two
    roles here are read-only and everything, which is the honest cut while one
    person holds the keys. The moment someone needs to run an eval without
    being able to revoke people, the write gate splits into "spends" and
    "administers", and that is the middleware's one `is_admin` check becoming
    two.
  - **Whether a viewer should see trace payloads** — the actual prompts and
    completions — is still open, and still defaulting to yes. Item 4 raised it;
    nothing here answers it, and it is worth deciding before anyone outside
    your own projects gets an invite.
  - **No password reset.** A viewer who forgets theirs is re-invited by an
    admin, which works because re-inviting keeps the account and its note. That
    is fine for a handful of people and would not be for more.

## The .env admin becomes a bootstrap, not a fixture (2026-09-01)

`ADMIN_EMAIL`/`ADMIN_PASSWORD` were authoritative forever: `seed_admin`
re-hashed the password from `.env` on every boot, so that account's plaintext
password necessarily lived in a file on disk — the one account in the system
whose password was not hash-only.

They are now a **bootstrap**. While set, they behave exactly as before,
including re-asserting that address as an un-revoked admin, which is the escape
hatch if the allowlist is ever edited into a state nobody can sign in from.
Once someone has accepted an admin invite and chosen their own password, they
can be removed — and the app still boots, because `has_active_admin()` says
there is another way in. Put them back and restart to recover; that path runs
every boot and always works.

`_guard_seeded_admin` no-ops when the env vars are absent, so the account stops
being un-manageable at the same moment it stops being env-defined. The two
facts were always the same fact.

### Password reset, which the invite flow nearly had already

`issue_reset` mints a single-use token against an existing account, redeemed
through the same accept page. Deliberately **not** `invite()` with the guard
removed: an invite creates access, a reset replaces a credential on access that
already exists, and `invite` refuses accepted accounts precisely so it can
never be used as an unlogged password reset. Same machinery, different intent,
separate name.

`accept_invite` stopped requiring `accepted_at IS NULL`, since a reset is
issued against a very much accepted row. The token is the credential either
way — single-use, expiring, stored only as a hash.

**Issuing one cannot lock anybody out.** The account keeps working on the old
password until the link is used, which matters most in the case that motivated
this: the only admin resetting themselves. The People list shows `RESET SENT`
rather than `INVITED` for exactly that reason — labelling it "invited" would
claim they have no access when they still do.

Refused for the `.env` admin while those vars are set, because `seed_admin`
would re-hash from `.env` at the next boot and silently revert the reset.

## The login rate limiter was keyed on a constant (found 2026-09-01)

Diagnosing a failed phone login turned up something worse than the login: every
UI login on the box was recorded from `127.0.0.1`. `next.config.ts` proxies
`/api` to the backend, so the TCP peer is the Next server, not the browser.

The schema note says the limiter is keyed by IP rather than by email so nobody
can lock out a known account. Keyed on a constant it does the *worse* version
of that: five fumbled passwords from anyone locks out everyone, and an attacker
shares one allowance with all legitimate users.

**The fix is not application code.** A `_client_ip` helper reading
`X-Forwarded-For` was written, and then deleted on discovering that uvicorn
already does this — `--proxy-headers` is on by default with
`forwarded_allow_ips=127.0.0.1`, so `request.client.host` had *already* been
rewritten from the header before any of our code ran. The helper was not just
redundant, it was reading a value the server had already substituted.

So the correct change is one flag in `backend/Dockerfile`:
`--forwarded-allow-ips "*"`. Safe in that image specifically because the
backend service publishes no ports at all — only the frontend can reach it, so
the only `X-Forwarded-For` it can see is the one the proxy in front wrote.
Documented in the Dockerfile as not to be copied to a directly reachable
backend, where it would let any caller forge its own address.

In local dev, uvicorn's loopback default means a process on the box can forge
the header. That is unchanged, is uvicorn's documented behaviour, and matters
only where anything local could already reach the backend anyway.

## The phone login failure was not the password (2026-09-01)

Recorded because the wrong diagnosis was convincing and nearly stuck.

Reaching `next dev` on the machine's LAN address instead of `localhost` served
the page but **never hydrated it** — Next 16 refuses cross-origin dev requests,
so the client bundle never loaded. The login form was server-rendered HTML with
no JavaScript behind it: tapping Sign in submitted nothing.

Every downstream signal agreed with "wrong password". The login-attempts table
had no entry from the phone, which reads as "they mistyped it and gave up"
rather than "the button was dead" — an empty table cannot distinguish a
failure from an absence. The fix is `allowedDevOrigins` in `next.config.ts`,
computed from the machine's own interfaces so it cannot go stale silently.

What made it visible was the *second* report — a page stuck on "Checking your
invite…" — because that state is only reachable if client code never runs. A
form that does nothing is ambiguous; a spinner that never resolves is not.

**Two real findings came out of chasing the wrong one**, both worth keeping:
the login rate limiter was keyed on a constant address (see above), and the
`.env` admin's plaintext password was the only credential in the system not
stored solely as a hash.
