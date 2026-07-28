# Steps for User

Things only you can do — credentials, accounts, installs, and decisions I
shouldn't make unilaterally. Ordered: everything in section 1 blocks Step 1
from being fully verified; section 2 blocks Step 2.

I'll add to this file as later steps introduce new prerequisites.

---

## 1. Now — finish verifying Step 1

- [ ] **1.1 Reopen your editor at the new project path.**
  The project moved off the iCloud-synced Desktop on 2026-07-27 because iCloud
  was corrupting the Python venv. New path:
  ```
  ~/dev/AI_Observability_Project
  ```
  The old Desktop path no longer exists. Don't move it back — `node_modules` in
  Step 2 will hit the same problem much harder.

- [x] **1.2 Get an Anthropic API key.** — done 2026-07-27. Console credits also
  purchased; the first attempt failed with a 400 "credit balance is too low",
  which is separate from any claude.ai subscription.

- [x] **1.3 Create your `.env` and paste the key in.** — done 2026-07-27.
  Originally created as `claude_api.env`, which `load_dotenv()` does not look
  for and `.gitignore` did not cover; renamed to `.env` at the project root.
  `.env.example` is the checked-in template. Never commit the real key.

- [x] **1.4 Run the example — the one path never tested with a real call.**
  ```bash
  cd ~/dev/AI_Observability_Project/sdk && uv run examples/basic_call.py
  ```
  Passed 2026-07-27. Both paths are now exercised against the live API: the
  error path (400, credit balance) and the success path.

- [x] **1.5 Sanity-check the span against reality.** — passed 2026-07-27 on
  `claude-opus-5`: `finish_reasons` `["end_turn"]`, usage 23 in / 87 out,
  `obs.cost_usd` `0.00229`. Cost re-derived by hand and matched exactly, so the
  pricing-table arithmetic is right — the *rates* still need 1.6.
  For future runs, confirm in the printed span that:
  - `gen_ai.response.finish_reasons` is `["end_turn"]` — if it's
    `["max_tokens"]`, thinking ate the budget; raise `max_tokens`. If it's
    `["refusal"]`, the safety classifier declined and content will be empty.
  - `gen_ai.usage.*` token counts look plausible.
  - `obs.cost_usd` is present (absent means the model ID isn't in the pricing
    table — tell me and I'll add it).

- [ ] **1.6 Spot-check cost against real billing.**
  After a few calls, compare `obs.cost_usd` totals to
  <https://console.anthropic.com/settings/usage>. The pricing table is
  hand-maintained, so this is the only way to know it's right. Report drift.

---

## 2. Before Step 2

Step 2 in CLAUDE.md is really four projects (ingest, storage/query, auth, UI),
so we split it on 2026-07-27:

- **Step 2a** — SDK span nesting, OTLP ingest, Postgres + API-key auth,
  WAL→Parquet storage, DuckDB query layer, read API. Verified by curl, no UI.
- **Step 2b** — user sessions/login, Next.js UI, trace list, trace detail,
  span waterfall, API key management page.

Postgres moved earlier than originally written: ingest auth needs a key store,
and the alternative is a throwaway env-var key path we'd delete a day later.

### Blocking Step 2a

- [x] **2.1 Initialize git.** — done 2026-07-27. First commit `4686ab2`.
  `uv init` had left `sdk/` as its own nested repo, which would have committed
  the SDK as an empty gitlink with no source; removed. `.gitignore` expanded to
  cover `.DS_Store`, venvs, Node/Next artifacts, and local Parquet/WAL data.

  A GitHub remote exists as `origin`, with two branches:
  - `main` — the working branch. Every step commit lands here.
  - `Public` — what you share publicly. As of 2026-07-27 it sits at `91f1690`
    (Step 2a), five commits behind `main`, and is a strict ancestor of it, so
    publishing is a fast-forward with nothing to reconcile.

  **History is clean of secrets, verified 2026-07-27.** Scanned every commit
  on every branch: the only env file ever committed is `.env.example`, and no
  `sk-ant-*` or `obsk_*` material appears anywhere. That was worth checking
  rather than assuming — `.env` began life as `claude_api.env`, a name the
  `.gitignore` of the time did not cover (see 1.3). It was renamed before any
  commit, so the window closed without incident.

- [x] **2.2 Install and start Postgres.** — done 2026-07-27. PostgreSQL 16.14
  (Homebrew), running as a service, `psql` on PATH at `/opt/homebrew/bin/psql`.
  Metadata store: projects and API keys in 2a; users and sessions in 2b;
  prompts, datasets, scorers, eval runs later.

- [x] **2.3 Choose your storage backend for traces.** — decided 2026-07-27:
  **local filesystem**. Free, no AWS account needed. The `StorageBackend`
  interface keeps the S3 swap to an env var rather than a rewrite, so this is
  reversible. Traces land under `data/` (gitignored).

### Blocking Step 2b

- [x] **2.4 Install pnpm.** — done 2026-07-27, pnpm 11.17.0.

- [x] **2.5 Pick admin credentials for UI login.** — set 2026-07-27.
  `ADMIN_EMAIL` and `ADMIN_PASSWORD` are set in the gitignored `.env`. Values
  are deliberately not repeated here — this file is committed. Seeded into the
  users table on first boot and hashed with argon2id; the plaintext is never
  stored.

  Use a password manager to generate it. Nothing about the current value is
  recorded here, in the git history, or anywhere I can see — that is the
  point, and it is why I don't pick it for you.

  **Rotating is a restart, not a SQL edit.** Change `ADMIN_PASSWORD` in `.env`
  and restart the backend. `seed_admin` compares the env value against the
  stored argon2id hash and re-hashes on mismatch, so first boot and rotation
  are the same code path. It also deletes that user's existing sessions — a
  password change that left old sessions alive would defeat the purpose of
  rotating. Expect to sign in again everywhere afterwards.

  The backend refuses to bind a non-loopback address when the password is
  short or on a common-wordlist blocklist (`check_password_strength` in
  `config.py`). That guard keys off the bind address rather than a warning
  you have to remember, so a weak password stays harmless on loopback and
  becomes a startup failure the moment you expose the app.

- [x] **2.5a Rotate `ADMIN_PASSWORD`.** — done 2026-07-27. Verified three
  ways, none of which involve knowing the value: `users.updated_at` moved
  past `created_at` (the hash was rewritten), live sessions went 6 → 0, and
  `check_password_strength` now passes with a simulated non-loopback bind, so
  the 2.6 tunnel guard is satisfied on this front.

  ⚠️ **Editing `.env` is not enough on its own — you must restart the
  backend.** This bit us on the first attempt. `uvicorn --reload` watches
  `.py` files only, so a `.env` edit under a running server changes nothing:
  `seed_admin` never re-runs, Postgres keeps the old hash, and the *old*
  password goes on working while the new one is rejected. It looks exactly
  like a successful rotation from the outside — the file says what you typed.

  How to tell the difference without knowing the password:

  ```bash
  psql -t -d obs -c "SELECT (updated_at > created_at) AS rehashed, \
    (SELECT count(*) FROM sessions) AS live_sessions FROM users;"
  ```

  `t` and `0` mean it took. `f` with sessions still alive means the process
  never restarted.

- [ ] **2.6 Decide how you'll reach it from your phone.**
  Tunnel first, hosted later. **Tunnel port 3000 only** — the Next.js app
  proxies `/api` and `/v1` to the backend, so one origin covers both and the
  session cookie stays first-party. Exposing 8000 separately reintroduces the
  cross-site cookie problem that cost us a debugging cycle in 2b.

  Two things must change before you tunnel:
  - **A strong `ADMIN_PASSWORD`.** The backend refuses to bind a non-loopback
    address with a weak one (see 2.5).
  - **`OBS_COOKIE_SECURE=true`** once you're on HTTPS, which any tunnel gives
    you. A Secure cookie is dropped over plain HTTP, so don't set it before.

  Tell me when you're ready and I'll walk it through rather than let you debug
  it blind.

---

## 2.5 Step 3 — datasets and replay runs

Built 2026-07-27. Nothing here blocks you; these are things to know.

- [ ] **2.7 Replay runs spend real money.**
  Every test case in a run is one live, billable Anthropic call. A 40-case
  dataset run against Opus is 40 calls. Two guards are in place:
  - `OBS_MAX_RUN_ITEMS` (default 100) caps calls per run. A bigger dataset is
    truncated and the run records that it was.
  - The run form states the call count before you start, and a run in flight
    can be cancelled — the check happens between cases, so calls already
    in-flight still complete and still bill.

  This is the number to watch in 1.6 when you compare against console billing.
  The `eval run` traces are the ones that will move it.

- [ ] **2.8 Prompt templates must contain `{{input}}`.**
  That is where each test case's input is substituted. A template without it
  is rejected at run creation rather than accepted — otherwise every case
  sends a byte-identical request and the run looks successful while meaning
  nothing. The template is one user message; there is no separate system
  prompt field yet, because step 5 is where prompt structure becomes real.

- [ ] **2.9 Replay runs show up in your trace list.**
  A run emits a real trace: one `eval run` root span with a `chat` child per
  case, `service_name` of `obs-runner`. So the traces page now mixes
  application traffic with your own eval traffic. That is deliberate — it is
  the same data and it should cost the same way — but it does mean trace
  counts and totals include replays. Say the word if you'd rather filter them
  out by service.

---

## 2.6 Step 4 — LLM-as-judge scorers

Built 2026-07-27. Nothing here blocks you; these are things to know.

- [ ] **2.10 Scoring spends money separately from replaying.**
  A scoring pass is one live judge call per output per scorer. A 40-case run
  scored by two scorers is 80 judge calls *on top of* the 40 replay calls. The
  run form and the score button both state the call count before you commit.

  `OBS_MAX_SCORER_CALLS` (default 100) caps a single scoring job. Unlike the
  replay cap, going over it is **refused, not truncated** — scoring only the
  first N items would produce a mean silently biased by dataset order, and a
  wrong number that looks right is worse than no number.

  Judge calls run on whatever model the scorer specifies, independent of the
  model being evaluated. Haiku is a reasonable default judge and roughly 5x
  cheaper than Opus per token; the scorers I verified with used it.

- [ ] **2.11 The judge answers through a forced tool call.**
  A scorer's output schema becomes a tool `input_schema`, and the judge is
  required to call it. That is what makes a score typed data rather than a
  paragraph — numeric bounds and category enums are declared in the schema and
  re-checked on the way back, so a 6 on a 1–5 scale is recorded as a failed
  score rather than quietly dragging a mean down.

  The consequence worth knowing: **there is no hidden system prompt.**
  Everything the judge sees comes from your template. If a scorer behaves
  oddly, the prompt is the whole explanation.

- [ ] **2.12 Try a scorer before you run one.**
  Each scorer has a "Try it" panel that judges text you paste in — one call,
  synchronous, not saved. This is the cheap way to find out that a prompt asks
  for 1–10 while the schema says 1–5. It is real spend and does emit a span,
  so preview calls show up in your traces like everything else.

- [ ] **2.13 Scoring adds a third kind of trace to your trace list.**
  Following 2.9: judge calls emit their own trace (`obs-judge`), one root
  `eval scoring` span with a `judge` child per call. So the traces page now
  mixes application traffic, replay traffic, and judge traffic. Deliberate —
  the judge is billable LLM traffic and hiding it would defeat the point — but
  it does mean trace counts and cost totals include scoring. Say the word if
  you'd rather filter by service.

- [x] **2.14 Editing a scorer does not version it.** — fixed 2026-07-27 by
  step 5. A scorer's definition is now a prompt of kind `scorer`, so editing
  one appends a version and every score records which version judged it.
  Archiving is still safe and still archives rather than deletes.

  One limit worth knowing: **scores taken before step 5 have no version.**
  The backfill gave each existing scorer a v1 holding its *current*
  definition, which is the only honest thing available — the text of any
  earlier edit was overwritten in place before the history table existed and
  is not recoverable. Those older scores are left pointing at nothing rather
  than at a v1 that may not be what produced them.

---

## 2.7 Step 5 — prompt versioning

Built 2026-07-27. Nothing here blocks you; these are things to know.

- [ ] **2.15 Editing a prompt appends; it never overwrites.**
  Versions are immutable and numbered from 1. That is the whole mechanism —
  a run records the version id it sent, so "what produced this result?" stays
  answerable however much the prompt moves afterwards.

  Two consequences that surprise people:
  - **Saving without changing anything creates nothing.** Versions are keyed
    by a hash over the text *and* the settings, so a no-op save returns the
    existing version and says so, rather than minting a v7 identical to v6.
  - **Renaming is not a version.** A rename changes nothing a run would send,
    and putting it in the chain would bury the diffs that did change
    behaviour under ones that didn't.

- [ ] **2.16 Labels are movable; versions are not.**
  `production` is a pointer you promote between versions. `latest` is not a
  real label — it always means the highest version number, and is rejected if
  you try to pin it.

  **A run resolves a label once, at the moment it starts, and records the
  version it landed on.** So promoting `production` from v1 to v2 tomorrow
  does not retroactively change what today's run says it ran. That is
  deliberate and it is the property the whole step exists to protect — if you
  ever find a run whose recorded version disagrees with what it sent, that is
  a real bug and worth telling me about.

- [ ] **2.17 A version carries its settings, not just its words.**
  Model and max_tokens are frozen into each version, and for a scorer so is
  the whole output schema. Picking a version in the run form fills in the
  model and token budget it was written for; changing them by hand overrides
  it for that run only.

  This is why the diff view shows a settings block above the text diff. A
  scorer whose scale went from 1–5 to 1–10 has an *empty* text diff while
  every score before and after it means something different — reading only
  the words would miss it completely.

- [ ] **2.18 Scorer prompts live on the same mechanism, and are edited in
  one place.**
  A scorer's history is visible under "History" on the Scorers page. You
  cannot add a version to a scorer's prompt from the Prompts page — it is
  refused — because the scorers table holds the live definition and letting
  the two be edited separately would let the history claim a definition the
  judge would never actually use.

  Scorer prompts also have no labels. Nothing resolves a scorer by label; it
  always judges with its current definition, so a promote button there would
  be a control that quietly does nothing.

- [ ] **2.19 A run scored twice by an edited scorer says so.**
  The score summary on a run averages verdicts per scorer. If the scorer was
  edited between scoring passes, the summary now carries a "mixes scorer v1
  and v2" warning rather than presenting one clean number built from two
  different judges.

---

## 2.8 Step 6 — real-time guardrails

Built 2026-07-27. Two things here need a decision from you; the rest is
things to know.

- [ ] **2.20 The guardrail endpoint accepts an ingest key. That is a real
  loosening, and it's the one thing in step 6 I'd want you to sign off on.**
  Every money-spending endpoint since step 3 has been session-only, so a leaked
  ingest key could push spans but never run up a judge bill. `/v1/guardrail`
  breaks that: the thing calling it is your application deciding whether to
  show a response to a user, and an application holds a key, not a cookie.
  Session-only would make the endpoint unusable by its only caller.

  What replaces the missing boundary is a rate cap, not a trust boundary —
  `OBS_MAX_GUARDRAIL_CALLS_PER_MIN` (default 120 judge calls/min) bounds what a
  stolen key can spend per minute. That is genuinely weaker than what steps 3-5
  give you. The clean fix is a separate key scope, so a guardrail key can screen
  but not ingest and an ingest key can't screen; say the word and it's a small
  step 7. Until then, treat the ingest key as spend-capable.

- [ ] **2.21 A judge that fails lets output through, by default.**
  Each guardrail has an `on_error` setting. It defaults to `allow`: if the
  judge errors or times out, the guardrail is treated as not triggered and the
  response goes out unscreened.

  I picked that because fail-closed turns an Anthropic blip into a total outage
  of whatever is behind the guardrail, and for a prototype that is the larger
  failure. It is defensible to disagree — for an actual safety guardrail,
  fail-closed is the conventional call. Two things make it easy to flip: it is
  per-guardrail, in the editor, and every response carries `degraded: true`
  when any judge failed, so you can implement whichever policy you want in the
  caller in one line. Both paths are verified.

- [ ] **2.22 New guardrails default to flag, not block.**
  `flag` is shadow mode — the judge runs, the result is recorded and returned,
  and the decision is never block. A guardrail is one bad judge prompt away
  from rejecting every response you serve, so the intended workflow is: create
  as flag, watch the check log against real traffic, promote to block once it
  fires on the right things. The per-guardrail counter on each row ("3 fired /
  120 checks") is there for exactly that read — one that has never fired is
  either unnecessary or broken, and both are worth knowing before you promote
  it.

- [ ] **2.23 A guardrail triggers when its scorer fails.**
  One rule across all three output types, so a safety scorer reads the way you
  would write it: `passed = true` means safe. Two consequences:
  - **A numeric scorer with no pass threshold is refused as a guardrail.** A
    1-5 scale with no failing end cannot decide anything. Set the threshold on
    the scorer first; the editor says so next to the field.
  - **A categorical scorer has to name its blocking labels.** Categoricals
    deliberately report a distribution and no pass/fail (3.7), so the guardrail
    is where that judgement gets made. Marking every category as blocking is
    refused — it would block everything it ever saw.

- [ ] **2.24 Screening costs money on every call, and shows up in your traces.**
  Following 2.9 and 2.13, this is the fourth kind of traffic in the trace list:
  each check emits an `obs-guardrail` trace with one root `guardrail
  pass`/`guardrail block` span and a `judge` child per guardrail. One check
  against one Haiku guardrail measured ~1.4s and ~$0.0015 — worth multiplying
  by your real request volume before pointing production at it.

  A check runs all its guardrails concurrently, so latency is the slowest judge
  rather than the sum: two guardrails at 1.36s and 2.48s came back in 2.48s.

  The root span is **not** marked ERROR when a check blocks. Blocking is that
  span's job working, and painting every enforcement red would make the error
  column mean "a guardrail did something" instead of "something broke".

- [ ] **2.25 "Try it" on the Guardrails page is the real endpoint.**
  It posts to `/v1/guardrail` exactly as your application would — real spend,
  real trace, and the check lands in the log below it. Deliberately not a
  preview mode: a UI-only path would be the one place where what you tested
  isn't what runs.

- [ ] **2.26 Calling it from your code is three lines.**
  ```python
  from obs_sdk import guard

  verdict = guard(output=answer, input=question, source="support-bot")
  if verdict.blocked:
      answer = "I can't help with that."
  ```
  `verdict.reasons()` gives the judge's own words for why, which is usually
  what you want when turning a block into a message. It uses `urllib` from the
  standard library rather than adding a dependency for one POST.

  **It raises if the backend is unreachable**, rather than passing. A screening
  step that silently passes when it never ran is the failure this whole project
  exists to make visible. `guard(..., fail_open=True)` gets the other behaviour
  and says so at the call site. Note the difference from 2.21: `degraded` means
  a judge didn't answer, unreachable means nothing ran at all.

- [ ] **2.27 I left a working `Safety` scorer and guardrail in your project.**
  Created while verifying, on `claude-haiku-4-5`, boolean, screening for
  actionable harm / harassment / CSAM / leaked credentials. It is set to
  **block** and enabled, because that is the configuration I needed to verify.
  If you'd rather it not block anything until you've read its prompt, flip it to
  flag on the Guardrails page. The check log has a handful of my test entries in
  it; the temporary scorers I made for validation testing are archived.

---

## 3. Open decisions — my calls, reversible if you disagree

Cheap to change now, painful once Parquet schemas and dashboards depend on them.

- [ ] **3.1 Custom span attributes use an `obs.` prefix.**
  `obs.cost_usd` and `obs.latency_seconds`, rather than sitting in the
  spec-governed `gen_ai.*` namespace. I raised this and moved ahead when you
  didn't object. One-line revert.

- [ ] **3.2 Cost estimates are date-aware.**
  Sonnet 5 has introductory pricing through 2026-08-31, so a flat table would
  overstate its cost by 33% today. `estimate_cost_usd` takes an optional `on=`
  date. Complexity for accuracy — say the word and I'll simplify to flat rates.

- [ ] **3.3 Prompt/completion are flat strings, not structured.**
  Semconv replaced the removed `gen_ai.prompt`/`completion` with structured
  `input.messages`/`output.messages`. I kept flat strings because Step 2
  offloads these to S3 blobs anyway — the real shape gets decided there.

- [ ] **3.4 Dataset items and run outputs live in Postgres, not S3 blobs.**
  CLAUDE.md routes large payloads to S3. That rule exists to keep the Parquet
  columns narrow so scans stay fast; these rows are read one run at a time and
  never scanned in bulk, so the reason doesn't apply. The span copy of each
  completion still goes through the columnar path as normal.

- [x] **3.5 A run stores the prompt it sent, inline.** — landed as written on
  2026-07-27. `runs.prompt_version_id` is a nullable FK sitting *alongside*
  `prompt_template`, not replacing it. Null means the prompt was typed into
  the run form. `ON DELETE SET NULL`, so removing a prompt never removes the
  runs that used it — they keep the text and lose only the link.

- [ ] **3.6 A score targets either a run item or a span, in one table.**
  Rather than a table per target kind. Everything downstream — aggregation,
  cost, the judge's own trace — is identical between the two; only where the
  text comes from differs. A CHECK constraint enforces that exactly one target
  is populated. If a third target appears and the shapes start diverging, that
  is the signal to split it.

- [ ] **3.7 Categorical scorers have no pass/fail.**
  Boolean scorers are inherently pass/fail and numeric ones get an optional
  `pass_threshold`. For categories, which labels count as passing is a
  per-question judgement that would need its own field, so a categorical
  scorer reports a distribution and nothing else. Easy to add if you want it.

- [ ] **3.8 Prompts and scorers share one versioning table.**
  `prompts` has a `kind` column rather than there being a separate
  `scorer_versions`. History, diffing and labels are identical between the
  two; only the frozen settings differ. If a third kind appears and the
  shapes start diverging, that is the signal to split — the same test as 3.6.

- [ ] **3.9 Diffs are computed on the backend.**
  `difflib` is in the standard library, so the UI receives a list of line ops
  and renders them rather than pulling a diff package into the web bundle for
  the same result. It also means the API can answer "what changed between v2
  and v5" for a script, not just for the browser.

- [ ] **3.10 Datasets are not versioned yet.**
  CLAUDE.md's fourth primitive is "prompt *and dataset* versioning". Step 5
  as written in the build order says prompts, and that is what this is.
  Adding or removing a test case still mutates the dataset in place, so a run
  from last week can name the prompt it used but not the exact set of cases.
  Say the word and dataset snapshots are the natural next increment — the
  machinery is now built and a dataset version would reuse it.

- [ ] **3.11 Guardrail checks get their own tables, not a third `target_kind`
  on `scores`.**
  Applying the test 3.6 set for itself. Folding span scoring into the scores
  table was right while the shapes stayed identical; these diverge. A guardrail
  check is synchronous, judges text belonging to no run and no stored span,
  carries a decision and a latency budget, and is written once and read as a
  log. Reusing `scores` would mean widening its CHECK constraint, adding a
  decision column nothing else reads, and teaching every existing aggregate to
  exclude a row type it was never meant to count.

- [ ] **3.12 Guardrails are not versioned.**
  Unlike prompts and scorers, editing a guardrail overwrites its policy. The
  reason those needed history is that a past score is a number whose meaning
  lives in the definition that produced it. A guardrail's past checks already
  record the action, the verdict and the scorer version inline, so they go on
  explaining themselves with no version chain behind them. If you find yourself
  asking "what was this guardrail set to last Tuesday", that is the signal I
  got this wrong.

- [ ] **3.13 The check log stores the full screened text.**
  Same call as 3.4: read one at a time, never scanned in bulk, so the
  Parquet-narrowness argument doesn't apply. Worth flagging separately though,
  because this table grows with your production traffic rather than with your
  experiments — it is the first thing here that has no natural ceiling. There
  is no retention policy yet. Say the word and a "delete checks older than N
  days" job is a small addition.

---

## 4. Recurring upkeep

Not one-time. These go stale silently.

- [ ] **4.0 Before fast-forwarding `Public`.**
  Two things to know, because publishing is the one action here that cannot
  be taken back — a pushed commit is scrapeable within minutes and stays in
  forks and caches after a force-push.

  **This file goes public with the code.** Keep it free of anything that
  describes the *current* state of a credential. Earlier versions of 2.5 said
  in writing that `ADMIN_PASSWORD` was a weak placeholder from common
  wordlists; that text is gone from the working copy but still sits in every
  commit from `3c821b6` onward, and a fast-forward publishes those commits
  too. The fix is to defang it rather than erase it: **rotate the password
  (2.5a) before the first `Public` fast-forward**, and the old sentence
  becomes a true statement about a value that no longer exists. Rewriting
  history to chase the text would be effort spent on the wrong problem.

  The general rule: describing a *guard* is fine and useful ("the backend
  refuses to bind non-loopback with a weak password"). Describing the
  *current value or its weakness* is not, because this repo also publishes
  the exact login route, the rate-limit numbers, and the cookie config.

  **Re-scan history, don't assume.** The check in 2.1 was a point-in-time
  result. From the repo root:

  ```bash
  git rev-list --all | while read c; do git grep -lIE 'sk-ant-[A-Za-z0-9]|obsk_[A-Za-z0-9_-]{20}' $c 2>/dev/null; done | sort -u
  ```

  Silence is a pass. Any output means a key reached a commit — rotate the
  key first, then worry about rewriting history, in that order.

- [ ] **4.1 Pricing table (`sdk/src/obs_sdk/pricing.py`).**
  Hand-maintained and verified 2026-07-27. Unknown models return `None`, not
  `0.0`, so a missing entry shows as absent cost rather than a wrong one. Check
  when Anthropic ships a model or changes prices.

- [ ] **4.2 OTel GenAI attribute names.**
  The conventions moved to
  [semantic-conventions-genai](https://github.com/open-telemetry/semantic-conventions-genai)
  and are still pre-stable. No PyPI package exists for that repo yet, so the
  names in `tracing.py` are hand-written. When a package ships, switch to
  imported constants and delete the hand-written strings.

- [ ] **4.3 Consider `opentelemetry-instrumentation-anthropic`.**
  An off-the-shelf auto-instrumentation package (0.62.1 on PyPI) does roughly
  what our SDK does. I didn't adopt it — you're building this to internalize
  what's different about AI observability, and hand-rolling is the point. Worth
  a look before Step 2 in case you'd rather not maintain the span code.
