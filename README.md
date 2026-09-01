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
- **A playground** for running one prompt and scoring the answer without first
  building a dataset — the result is a real span, so it shows up in traces and
  in the cost figures like anything else
- **Multiple provider keys**, chosen at the point of spending. Every run, score
  and span records which key paid for it, so spend and output quality can both
  be read per key

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
cp .env.example .env
docker compose up -d --build
```

`.env` needs four values before it will start — it refuses to boot without
them rather than failing at the first call:

| | |
|---|---|
| `ANTHROPIC_API_KEY` | the model and judge calls |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | seeds the single UI login |
| `OBS_SECRET_KEY` | encrypts stored provider keys at rest — generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and **keep a backup**, since losing it means re-entering every stored key |

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

Anthropic, xAI (Grok) and Google Gemini are wired up.
`backend/src/obs_backend/llm.py` holds a small provider registry: an adapter
implements `complete`, `tool_call` and `validate_key`, and the credential
chosen at the point of spending is what selects one — the model id does not
route, so a model released today works without an edit here. The xAI adapter
speaks the OpenAI chat-completions wire format, so registering another base URL
would also cover DeepSeek, Groq, Together or a local vLLM; Gemini has its own
adapter because going native lets the judge's JSON Schema pass through
untranslated.

Generating and grading are separate purchases. A scorer's own model decides
which vendor grades with it, so a Grok completion judged by a Claude scorer
bills two different keys — recorded separately, and surfaced when they differ.

Cost is keyed on `(provider, model)` in `sdk/src/obs_sdk/pricing.py` and is
still a hand-maintained table, but it prices four kinds of token rather than
two: uncached input, cache reads, cache writes and output. Vendors disagree
about whether their reported input count already includes cached tokens —
Anthropic's excludes them, an OpenAI-compatible one includes them — so `llm.py`
normalizes both to a total before anything is priced. Reasoning tokens are
recorded but not priced separately; they are already inside the output count
and billed at the output rate.

Long-context tiers and promotional windows are expressible in the same
structure. What is *not* modelled: Anthropic's 1-hour cache-write TTL, which is
priced differently from the 5-minute one — nothing here sets `cache_control`,
so no call can currently produce one.

`plan_next_steps.md` carries what is planned next and the reasoning behind the
decisions already made, including the bugs found along the way.
`gotchas.md` is the short form of the same thing — the traps worth knowing
before touching a page, organised by where you'd hit them.
