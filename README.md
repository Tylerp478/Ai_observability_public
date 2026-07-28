# AI Observability

A prototype AI observability platform — trace capture, evals, LLM-as-judge
scoring, prompt versioning, and real-time guardrails — built to explore what
actually differs between observing an LLM application and observing a
conventional service.

Inspired by Galileo, Arize, Braintrust, Langfuse and Fiddler. Built to learn the
space hands-on, not to compete with any of them.

## What it does

- **Trace capture** for LLM calls, tool invocations and agent steps, using
  OpenTelemetry GenAI semantic conventions (`gen_ai.*` spans)
- **Replay runs** — production traces become test cases you can re-run against
  a different prompt or model
- **LLM-as-judge scoring** for faithfulness, relevance and hallucination, with
  every score recording which version of the judge produced it
- **Prompts and datasets as versioned artifacts** — editing appends an
  immutable version, so a run from three weeks ago can still name the exact
  text it sent
- **Real-time guardrails** that screen an output and answer pass/block before
  it reaches a user

## Stack

| Layer | |
|---|---|
| Backend, SDK | Python 3.12, FastAPI, Pydantic v2, OpenTelemetry |
| Frontend | TypeScript, Next.js (App Router), Tailwind v4, TanStack Query |
| Span storage | Parquet on disk, queried in place by DuckDB |
| Metadata | Postgres |
| Judge / app model | Anthropic API |

Spans land in an NDJSON write-ahead log and a background compactor folds them
into Parquet. The query layer unions both, so a trace you just emitted is
visible immediately rather than after the next compaction.

## Running it

```bash
cp .env.example .env   # set ANTHROPIC_API_KEY, ADMIN_EMAIL, ADMIN_PASSWORD
docker compose up -d --build
```

Then open http://localhost:3000.

For a hosted deployment — an AWS Lightsail instance reachable from a phone over
Tailscale and from nowhere else — see [deploy/RUNBOOK.md](deploy/RUNBOOK.md).

`CLAUDE.md` is the design brief the project was built against, and
`steps_for_user.md` is the running build log, including the failure modes worth
remembering.

## Scope

Single-user by design. No multi-tenancy, no RBAC, no auth provider — password
login for one person, and the app is not intended to be exposed to the public
internet. The S3 storage backend is an interface with a stub behind it; the
filesystem backend is what runs today.
