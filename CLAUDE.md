# AI Observability Platform — Vibe Coding Prompt

## Role
You are a senior full-stack engineer helping me prototype an AI 
observability platform. Prioritize working code over architectural 
perfection. Ship iteratively. When something is ambiguous, make a 
reasonable choice, note it inline as a comment, and keep moving.

## Context
I'm building a prototype AI observability tool inspired by platforms 
like Galileo, Arize, Braintrust, Langfuse, and Fiddler. The goal is 
to explore the space hands-on, not to compete commercially. I have 
a background in traditional observability (Splunk/Cisco world) and 
want to internalize what's genuinely different about AI observability.

I will access this from mobile and desktop browsers, and will expose 
it beyond localhost (via tunnel first, hosted later), so it must not 
be open to the public internet.

## Core primitives to build
The five capabilities that separate AI observability from traditional APM:
1. Trace capture for LLM calls, tool invocations, and agent steps 
   (using OpenTelemetry GenAI semantic conventions — gen_ai.* spans)
2. An eval loop where production traces can be replayed as test cases
3. LLM-as-judge scoring for output quality (faithfulness, relevance, 
   hallucination)
4. Prompt and dataset versioning as first-class artifacts
5. Real-time guardrails that can score or block outputs before they 
   reach the user

## Stack
- Backend + SDK: Python 3.12, FastAPI, Pydantic v2
- Frontend: TypeScript, Next.js (App Router), Tailwind, shadcn/ui, 
  TanStack Query
- Package management: uv for Python, pnpm for JS
- Instrumentation: OpenTelemetry Python SDK, gen_ai.* semantic conventions
  (pin the convention version — these are still pre-stable and names 
  change between releases)
- LLM provider: Anthropic API (used both for the app being observed 
  and for the LLM-as-judge scorers)

## Storage architecture
Object storage is the durable layer, with a query engine on top.

- Span/trace data → S3 as Parquet. Partition by project and date:
  s3://{bucket}/traces/project={id}/dt={YYYY-MM-DD}/*.parquet
  Buffer and batch writes. Do NOT write one object per span — that 
  will make both cost and latency terrible.
- Query engine → DuckDB, reading Parquet directly from S3 via the 
  httpfs extension. No separate warehouse for a prototype.
- Large payloads (prompts, completions, tool outputs) → S3 as raw 
  blobs, referenced by key from the Parquet rows. Keeps the columnar 
  files narrow.
- Metadata → Postgres. Prompts, prompt versions, datasets, scorers, 
  eval runs, users, sessions, API keys.
- Put storage behind a thin interface with two implementations: local 
  filesystem and S3, toggled by env var. This is one of only two 
  abstractions I want up front, because it makes local development free.

Note where this design would need to change at real scale (ClickHouse 
or a purpose-built trace store instead of DuckDB), but do not build 
for that now.

## Auth
Two separate paths. Do not try to unify them.

UI auth — session-based login:
- Single user to start, but model it as a users table so adding more 
  later isn't a rewrite
- Password hashed with argon2id (use argon2-cffi). Never store plaintext.
- Seed the initial user from env vars (ADMIN_EMAIL, ADMIN_PASSWORD) on 
  first boot; do not commit credentials
- Session token in an HTTP-only, Secure, SameSite cookie. Sessions 
  stored server-side in Postgres so they can be revoked.
- Long session expiry (30 days) with sliding renewal — I'm on mobile 
  and do not want to retype a password constantly
- Rate limit the login endpoint (e.g. 5 attempts per 15 min per IP) 
  and use a constant-time comparison
- A logout endpoint that actually deletes the server-side session

Ingest auth — static API key:
- The SDK cannot hold a cookie. The /ingest endpoint authenticates with 
  a bearer token instead.
- Keys live in Postgres: store a hash, show the plaintext exactly once 
  at creation, support multiple keys and revocation
- A simple UI page to create and revoke keys

Everything except the login endpoint and a health check requires one 
of the two. Default-deny: adding a new route should fail closed, not 
open. Make this a middleware/dependency, not a per-route decorator I 
can forget — this is the second of the two abstractions I want up front.

Deployment note: if the frontend and backend end up on different 
domains, session cookies need SameSite=None; Secure and the backend 
needs CORS with credentials allowed and an explicit origin allowlist 
(never a wildcard with credentials). Flag this when we get to step 2 
so I don't debug it blind.

## Cost controls
There's a paid LLM API key behind this and scorer endpoints cost money 
per invocation.
- Hard cap on scorer invocations per eval run, configurable, with a 
  sane default (e.g. 100)
- Refuse to start if the LLM API key env var is missing rather than 
  failing at first call
- Log estimated spend per eval run in the UI

## Build order
Do NOT try to build everything at once. Build in this order and stop 
at each step for me to review:

Step 1: A minimal Python SDK that wraps an LLM call and emits an 
OTel span with gen_ai.* attributes (model, prompt, completion, tokens, 
latency, cost).

Step 2: A backend that ingests those spans (API-key auth), writes them 
to Parquet on S3, and a Next.js UI behind session login to list and 
inspect traces. Include a span waterfall view. Build auth in this step 
— not retrofitted later.

Step 3: A dataset primitive — take any trace, save its input as a 
test case. Then a runner that replays a dataset against a prompt 
and captures new outputs.

Step 4: LLM-as-judge scorers. Let me define a scorer as a prompt + 
model + output schema. Attach scores to traces and dataset runs.

Step 5: Prompt versioning. Every prompt is a versioned artifact with 
history and diff view.

Step 6: A guardrails endpoint that scores an output against safety 
scorers and returns pass/block.

## Working style
- After each step, show me what runs and what doesn't
- Use type hints everywhere; I want to read the code and understand it
- The UI must be usable on a phone — I'll be demoing from mobile. 
  Trace lists and detail views especially.
- ~~Skip multi-tenancy and RBAC until I ask for them~~ — **both asked for
  and built (2026-09-01).** Projects are first-class and every read and
  write is scoped to one; access is an admin-managed allowlist with two
  roles, admin and viewer. Do not "simplify" either back out.
- No Docker/K8s until we have something worth deploying
- Keep dependencies minimal; explain any library you pull in
- Beyond the storage interface and the auth middleware, don't add 
  abstraction "for future flexibility" — we'll refactor when we have 
  a second real use case
- All secrets from env vars, with a .env.example checked in and .env 
  gitignored

## What I do NOT want
- A polished landing page
- OAuth or a third-party auth provider. Still true, and now deliberate
  rather than incidental: an OAuth redirect URI has to be registered
  against a hostname this app does not have. Login is a password;
  additional people are invited by an admin and set their own. The
  invite link is a one-time token, not a magic link — it creates an
  account, it does not sign anyone in without a password.
- Feature parity with any existing platform
- Multi-agent orchestration (that's a different tool)
- ML model training or fine-tuning capabilities
- Compliance/audit features