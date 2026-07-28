# obs-backend

Step 2a: OTLP ingest, Parquet storage, DuckDB query layer, API-key auth.
No UI — that's 2b.

## What runs

```
SDK ──OTLP/protobuf──> POST /v1/traces ──> NDJSON WAL ──compactor──> Parquet
                            (bearer)                                    │
                       GET /api/traces <──── DuckDB ────────────────────┘
                                              (reads WAL ∪ Parquet)
```

## Setup

Postgres must be running. Once:

```bash
createdb obs
uv run scripts/bootstrap.py
```

That prints an ingest key **once**. Put it in the repo-root `.env` as
`OBS_API_KEY`.

## Run

```bash
uv run uvicorn obs_backend.main:app --reload --port 8000
```

Then emit a trace from the SDK:

```bash
cd ../sdk && OBS_EXPORTER=otlp uv run examples/agent_trace.py
```

## Endpoints

| Method | Path | Auth |
| --- | --- | --- |
| GET | `/health` | public |
| POST | `/v1/traces` | bearer (OTLP standard path) |
| GET | `/api/traces` | bearer or session |
| GET | `/api/traces/{trace_id}` | bearer or session |
| POST | `/api/admin/compact` | bearer (debug) |
| GET POST | `/api/datasets` | bearer or session |
| GET DELETE | `/api/datasets/{id}` | bearer or session |
| POST | `/api/datasets/{id}/items` | bearer or session |
| DELETE | `/api/datasets/{id}/items/{item_id}` | bearer or session |
| GET | `/api/runs`, `/api/runs/{id}` | bearer or session |
| POST | `/api/runs` | **session only** |
| POST | `/api/runs/{id}/cancel` | **session only** |

**Default-deny.** A middleware rejects any path not in `PUBLIC_PATHS` without
an `Authorization` header, so a new route added without a dependency returns
401 rather than being open. The per-route `require_api_key` dependency does
the actual validation.

**Why starting a run is session-only.** An ingest key is handed out to every
instrumented application, so it is the credential most likely to leak. Reads
are fine with one; starting a replay is not, because it spends money on the
account's Anthropic key. Reading a dataset with an ingest key still works, so
a script can push test cases without a browser.

## Design notes

**Why a WAL.** Writing one Parquet object per span is prohibited (cost and
latency), but a pure in-memory buffer loses every span accepted since the last
flush when the process dies — after ingest already returned 200. Spans append
to a local NDJSON file first, then compact.

**Why replay runs emit traces.** A run writes a real trace into the span store
— one `eval run` root span, one `chat` child per test case, `service_name` of
`obs-runner` — so replays render in the same waterfall as production traffic.
The alternative is a private results viewer, which means a second rendering of
the same data that immediately starts drifting from the first. Spans go
through `SpanWriter` in process rather than being exported over OTLP to our
own ingest endpoint: that loop works, but it would mean the backend holding an
ingest API key to talk to itself.

**Why the query layer reads both.** A span sits in the WAL until compaction.
Querying Parquet alone means a just-emitted trace doesn't exist yet, which is
indistinguishable from a bug. Reading both makes ingest look instant. Spans
appearing in both sources are deduplicated by `span_id`.

**Why SHA-256 for API keys, not argon2id.** Keys are 256 bits of CSPRNG
output — no dictionary to attack, no work factor needed. Argon2 exists for
low-entropy human passwords and is used for those in 2b. Running it on every
ingest request would put ~100ms of deliberate slowness in the hot path of
every span batch.

**Why project_id comes from the API key.** Not from the payload — otherwise a
client could write into another project's partition by lying about it.

**Fixed columns plus a JSON bag.** Known `gen_ai.*`/`obs.*` attributes get real
Parquet columns so DuckDB can filter without parsing; everything else lands in
`attributes_json`. The conventions are still pre-stable, so a fixed-only
schema would need a migration every time an attribute appears.

**Schema is applied at startup, not migrated.** Fine for one developer with no
deployed instances. Switch to Alembic once there's data you can't recreate.

## Scale limits (not built for now)

DuckDB scanning Parquet is fine for a prototype and into the millions of
spans. Sustained high-cardinality ingest wants ClickHouse or a purpose-built
trace store. The compactor is a daemon thread in one process — it does not
survive a mid-compaction restart or coordinate across replicas.

S3 storage is an unimplemented stub. The interface exists so adding it is
additive; `OBS_STORAGE_BACKEND=local` is the supported path in 2a.
