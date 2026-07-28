# obs-sdk

Step 1 of the AI observability platform: wraps an Anthropic call and emits an
OpenTelemetry span with `gen_ai.*` attributes.

Spans currently go to the console. There is no ingest endpoint yet — that's
step 2, which swaps the exporter in `tracing.py:_get_tracer()`.

## Setup

```bash
cp ../.env.example ../.env   # then fill in ANTHROPIC_API_KEY
uv sync
```

## Run the example

Makes one real (paid) Anthropic call and prints the resulting span:

```bash
uv run examples/basic_call.py
```

## Captured attributes

Names verified 2026-07-27 against `opentelemetry-semantic-conventions==0.65b0`.

The GenAI conventions have **moved out of the main semconv repo** to
[semantic-conventions-genai](https://github.com/open-telemetry/semantic-conventions-genai),
so every `gen_ai.*` constant in the installed package is flagged Deprecated.
Mostly that means "governance moved, names unchanged" — but two were real
changes, noted below. No PyPI package exists for the new repo yet, so these
strings are hand-written; switch to imported constants when one ships.

| Attribute | Notes |
| --- | --- |
| `gen_ai.operation.name` | always `chat` |
| `gen_ai.provider.name` | always `anthropic`. Renamed from `gen_ai.system` |
| `gen_ai.request.model` / `gen_ai.response.model` | requested vs. actually served |
| `gen_ai.request.max_tokens` | |
| `gen_ai.response.id` | Anthropic message ID, for correlating with their side |
| `gen_ai.response.finish_reasons` | list. `refusal` here is why a span can be OK with empty output |
| `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens` | output includes thinking tokens |
| `gen_ai.input.messages` / `gen_ai.output.messages` | successors to the removed `gen_ai.prompt` / `gen_ai.completion`; flat strings for now — see note in `tracing.py` |
| `obs.latency_seconds` | custom |
| `obs.cost_usd` | custom |

Custom attributes use the `obs.` prefix rather than squatting on the
spec-governed `gen_ai.*` namespace. Latency is also the span's start/end delta;
the explicit attribute is there so it survives into Parquet without a computed
column.

## Gotchas

- **`max_tokens` covers thinking too.** Opus 5 runs adaptive thinking by
  default, and the budget caps thinking + visible text together. A budget sized
  for the answer alone can be spent on thinking, leaving output truncated or
  empty. Check `finish_reasons`.
- **A refusal is not an error.** Opus 5's safety classifiers can decline a
  request and still return HTTP 200 with empty content and
  `finish_reasons: ["refusal"]`. The span status stays OK — query
  `finish_reasons`, not span status, to find these.

Cost comes from a hardcoded table in `pricing.py` that needs manual upkeep.
Unknown models return `None` rather than `0.0` so "no price on file" is
distinguishable from "free". Sonnet 5 has introductory pricing through
2026-08-31, so `estimate_cost_usd` takes an optional `on=` date — pass a span's
own timestamp when re-costing historical traces.
