# Gotchas

Things worth remembering, found while building. Organised by where you'd hit
them rather than by which file they live in, because the page is usually what
you're looking at when one bites.

`plan_next_steps.md` carries the design reasoning; this is the short form.
The bugs found during earlier steps (the WAL null-column inference, the shared
DuckDB connection, the browser cache) are at the bottom of that file and are
not repeated here.

**Status key** — `OPEN` still true today · `FIXED` handled, kept because the
reasoning matters · `WATCH` working but on an assumption that could lapse.

---

## Overview

**`FIXED` — the model mix panel only knew Claude.**
It tiered on hardcoded `claude-*` prefixes, greying out six of nine selectable
models once Grok and Gemini existed. Tiers now come from the pricing table,
banded on the output rate. A new vendor needs no edit to the component.

**`WATCH` — the mix ramp encodes price, not vendor.**
A Grok and a Claude model in the same price band are the same colour. That is
the honest reading of a price scale, and vendor is carried by the legend — so
`shortName` deliberately keeps the `claude-` prefix it used to strip. Anything
that re-strips it hides the one attribute the colour no longer encodes.

**`WATCH` — mix tier bands are fixed, deliberately.**
Not derived from whichever models are present, because colour must follow the
model and not move when a different model is added beside it.

**`FIXED` — Gemini answers with a build suffix that no price table carries.**
`gemini-2.5-pro-002` is not a key in `PRICING`, so every Gemini call would have
reported *no cost* — a plausible-looking blank on a spend dashboard.
`estimate_cost_usd` now takes `request_model` and falls back to it. Pricing
still follows the answer first; the request is a floor, safe because every
offered model is priced by the boot-time invariant.

**`WATCH` — cost tiles are only as good as the pricing table.**
Everything on this page sums `obs.cost_usd`, which is computed at write time
from a hand-maintained table. A model with no price contributes nothing and
looks free rather than unknown. See *Cost & pricing* below.

## Traces

**`WATCH` — `gen_ai.provider.name` now varies.**
It was always `anthropic`; it is now whatever key paid. Anything filtering or
grouping spans that assumed one value will silently narrow. The column has
always existed (`models.py`, `PROMOTED_ATTRIBUTES`), so no migration was needed
— which also means nothing forced a review of the readers.

**`WATCH` — `gen_ai.usage.input_tokens` changed meaning for Anthropic spans.**
It now includes cached tokens; before Phase 2 it excluded them. Spans written
earlier keep the old meaning and cannot be corrected after the fact, since the
cached counts were never recorded. Only affects calls that actually used
caching, of which there are currently none.

**`WATCH` — new token counts are `obs.*`, not `gen_ai.*`.**
`obs.cached_input_tokens`, `obs.cache_write_tokens`, `obs.reasoning_tokens`.
The GenAI conventions are pre-stable and have not settled on names for these;
squatting on a `gen_ai.usage.*` name that later means something else is worse
than a namespaced one that has to be renamed. They are omitted entirely when
zero, so absence means "did not happen", not "not recorded".

**`FIXED` — a judge span's `obs.credential` is per-scorer, not per-job.**
One scoring job can now span two vendors, so the label moved from the job root
onto each judge span. A root-level label would have named whichever key
happened to be resolved first.

## Playground

**`FIXED` — generating and grading are separate purchases.**
Scorers used to be billed to the key that generated the output. With one vendor
that was always true; with two it breaks the moment you grade a Grok completion
with a Claude scorer. `scoring.judge_credentials` (`scoring.py:812`) now maps
each scorer to a key that can serve its model.

**`FIXED` — the model list follows the selected key.**
Picking the xAI key used to still offer Claude models. The backend refused the
pairing before spending, so nothing was mischarged — but a dropdown that lets
you pick a guaranteed error is a poor way to learn the rule.

**`WATCH` — "Generate with" only names half the spend.**
The picker chooses the generation key. Judging may bill a different key
entirely; the result footer says `judged by …` when it differs. If you read
per-key spend expecting one key per run, that assumption is gone.

## Datasets & runs

**`WATCH` — the key overrides a saved prompt version's model.**
A version written against `claude-opus-5` cannot run on an xAI key. The run form
falls back to a model the chosen key can serve, visibly, rather than failing on
submit — so the model that runs may not be the one the version records. The
run row still stores what actually ran.

**`FIXED` — a mismatched model is rejected before the run starts.**
`create_run` checks it and raises `RunError`, deliberately: the POST endpoint
catches `RunError` only, and a bare `ValueError` from `llm` would have surfaced
as a 500 for a plainly bad request. Without the check, a mismatch cost one
rejected round trip *per test case* before the run gave up.

## Prompts

**`WATCH` — a version's `config.model` is not validated against your keys.**
You can save a version naming a model you hold no key for. It saves fine and
fails at run time. The model dropdown only offers models you can pay for, so
this needs an old version or a removed key to happen.

## Scorers

**`OPEN` — a scorer picks a vendor, not a key.**
The scorer's model decides which vendor grades with it, and it gets that
vendor's *default* key. There is no way to pin a scorer to a specific key when
you hold two for one vendor. Guardrails already model the better version by
carrying their own `credential_id`; scorers should probably follow if that ever
matters.

**`WATCH` — the judge model list spans every provider you hold a key for.**
Deliberately *not* narrowed by the Try-it key, unlike the Playground: a
scorer's model is what chooses the vendor, so narrowing it would hide exactly
the cross-vendor judges worth having. If that dropdown ever gets filtered by a
selected key, cross-vendor judging quietly disappears.

**`WATCH` — a judge that hits max_tokens returns no verdict at all.**
Forced tool use means the model cannot reply in prose, so a cut-off answer is a
*missing* verdict rather than a malformed one. Surfaced with a hint pointing at
the scorer's max_tokens, because "no tool call" from a forced tool call is
otherwise baffling.

## Guardrails

**`FIXED` — the credential is resolved inside the `try`.**
Which means the error path could reach `judge_span` with `credential` never
bound — a resolve failure is one of the failures that block exists to record.
A `provider = ""` local seeded before the try (`guardrails.py:497`) keeps the
error span buildable. `llm.provider_label("")` returning `""` is precisely the
case that function's never-raises contract was written for.

**`WATCH` — guardrails fail open.**
On judge error the default is allow. That converts a provider blip into
"everything passes" rather than "everything blocks", which is the right default
for a prototype and the wrong one for anything real.

## Keys

**`FIXED` — the one-time ingest key can be collapsed away.**
The plaintext is shown once and the backend keeps only a hash. The Ingest keys
section is force-open while a fresh key is on screen; Dismiss clears it and
releases the lock. Anything that hides that banner destroys the secret.

**`OPEN` — losing `OBS_SECRET_KEY` unrecoverably orphans every provider key.**
It encrypts them at rest and lives in `.env`, not the database — that is the
point, since a Postgres dump alone then decrypts nothing. It belongs in
whatever you back up. Rotating it means re-encrypting every row and there is no
migration helper.

**`WATCH` — provider keys are validated at save, not at first spend.**
A typo caught at save is an edit; the same typo caught mid-run has already paid
for the calls before it. Validation costs one unbilled models-list call.

## Cost & pricing

**`FIXED` — Gemini cache rates were 2.5x too high.**
Derived from an assumed 0.25x multiplier; Google's actual discount is 0.10x.
Base rates and the 200k threshold were correct as entered. Verified 2026-08-30.

**`WATCH` — Gemini 3.7/3.6 Flash are on introductory pricing that doubles.**
It lapses 2027-01-01. `base` holds the post-promo list price and `promo` holds
what is charged until then. Entering the current price as `base` would look
right today and halve every estimate in January.

**`WATCH` — `price_tier` reads list price, not the promotional rate.**
So a model does not change colour on the dashboard the morning a promo lapses.
A calendar event is not a category change.

**`WATCH` — `gemini-3.1-pro-preview` is priced but deliberately not offered.**
It is the only Pro-class model in the 3.x line and is still preview. Offering a
preview id in a spend-reporting tool is how you end up billing a model that
changed under you. Pricing it anyway keeps spans costed and lets
`provider_of_model` claim it for the mismatch guard.

**`WATCH` — Gemini publishes Batch, Flex and Priority tiers too.**
0.5x and 1.8x of Standard. This app uses Standard and models only that; a
request routed through another service tier would be mispriced.

**`WATCH` — the tier boundary differs by one token between vendors.**
Google charges the high band *above* 200k, xAI *at or above* it.
`long_context_threshold` means "the largest input that still gets base rates" —
200_000 for Gemini, 199_999 for xAI. Read it as a threshold and one of them is
off by a token.

**`WATCH` — Gemini's explicit context caching is not modelled.**
It bills storage per hour rather than a per-token write rate, which this table
cannot express and this app cannot trigger — nothing creates a cached-content
handle. Cache *reads* are priced normally.

**`FIXED` — two of three offered xAI models were retired on 2026-05-15.**
And **xAI answers a retired id with its replacement rather than failing**, so
nothing surfaced: a span here requested `grok-3-mini` and got `grok-4.3` back.
`grok-4.3` was unpriced, so every Grok call in this project recorded **no cost
at all** — a blank, not an error. Offered models are now `grok-4.6`,
`grok-4.5`, `grok-4.3`; retired ids stay priced at what they actually bill.

**`WATCH` — a vendor silently substituting models is the failure mode to expect.**
Check `gen_ai_response_model` against `gen_ai_request_model` when a cost looks
wrong — a mismatch means you are being billed for something other than what the
UI offered. The `request_model` fallback stops it becoming a blank, but it
prices the model you *asked* for, which is the wrong one.

**`WATCH` — every xAI text model is tiered at 200k input.**
All long-band rates are exactly 2x base. Not modelled before 2026-08-30, so any
Grok call over 200k tokens before then was under-reported by half.

**`WATCH` — all three providers are now verified; only four legacy rows are not.**
Anthropic checked 2026-08-31 against the `claude-api` skill's model table (all
nine current models matched as entered, cache multipliers exact); xAI and
Gemini checked 2026-08-30 against vendor docs. The exceptions are
`claude-opus-4-5`, `claude-opus-4-1-20250805`, `claude-sonnet-4-5-20250929` and
`claude-3-haiku-20240307` — retired or superseded, so absent from any current
source and unverifiable. No span in this project uses one, so the exposure is
zero; they exist only so an old span would still cost.

**`WATCH` — Anthropic's 1M context carries no long-context premium.**
That is why no Anthropic entry has a `long` tier, unlike xAI (every model) and
Gemini 2.5 Pro. If a future Anthropic model adds one, the mechanism is already
there — it just has no Anthropic user today.

**`WATCH` — Anthropic fast mode and the Batch API are unpriceable here.**
`speed: "fast"` on Claude Opus 5 bills $10/$50 instead of $5/$25, and Batch is
50% of standard — both are *different rates for the same model id*, which a
table keyed on `(provider, model)` cannot express. Nothing here sets either, so
no call can produce one; pricing them would need a third key dimension.

**`FIXED` — cached input used to be billed at the full rate.**
xAI caches automatically, so this was live from the moment a Grok key existed:
a call with an 80% cache hit read **67% high**. Cache reads and writes now have
their own rates.

**`FIXED` — Anthropic's reported input count excludes cached tokens.**
An OpenAI-compatible one *includes* them. Both readings look plausible and only
the invoice disagrees, so `llm.py` normalizes both to a total before anything is
priced — `_Call.input_tokens` is always the whole prompt, with
`cached_input_tokens` / `cache_write_tokens` as subsets of it. Any new adapter
must normalize the same way.

**`WATCH` — reasoning tokens are recorded but never priced separately.**
They are already inside the output count and billed at the output rate, so
passing them to `estimate_cost_usd` would double-count. This was always a
reporting gap rather than a cost bug. Recorded as `obs.reasoning_tokens`.

**`WATCH` — Anthropic's 1-hour cache-write TTL is not modelled.**
It is 2x base against the 5-minute 1.25x, and `usage.cache_creation` reports
the split. Nothing here sets `cache_control` at all, so no call can currently
produce one — revisit alongside whatever first opts in.

**`WATCH` — long-context tier beats an active promo, by design.**
Introductory rates are advertised against standard context, so applying them to
a long-context call would understate the bill. No model currently has both;
the rule is written down so the first one that does is not a surprise.

**`FIXED` — an offered model with no price now fails at boot.**
`llm._check_offered_models_are_priced` (`llm.py:421`) runs at import. The old
frontend list carried a comment *asking* whoever edited it to keep the two in
step; a model you can select but cannot cost is the worst kind of bug, because
it looks like it worked.

**`WATCH` — cost is priced on the date of the call, not today.**
Only matters for time-boxed promotional rates (Claude Sonnet 5's intro pricing
lapses 2026-08-31). Re-costing historical traces without passing the span's own
timestamp will drift once a promo ends.

## Providers & the LLM seam

**`WATCH` — the credential routes the call, not the model id.**
There is no prefix matching. A model id nobody recognises is passed through to
the provider rather than rejected, so a model released this morning works
without an edit. The only pre-flight is a *mismatch* check, which fires when the
pricing table already places a model with another vendor.

**`WATCH` — tool arguments differ in kind between vendors.**
Anthropic and Gemini return a dict; OpenAI-compatible endpoints return a JSON
*string* that can arrive truncated. All three collapse onto `payload is None`,
so `scoring.judge` reads a cut-off judge identically either way. A new adapter
must preserve that or the judge path will surface parse errors that no longer
exist anywhere else.

**`WATCH` — three vendors, three token conventions, all plausible-looking.**
Anthropic's input count *excludes* cached tokens; OpenAI's *includes* them.
Anthropic and OpenAI output counts *include* reasoning; **Gemini's
`candidates_token_count` excludes it**, and Gemini 2.5 thinks by default — so
the naive reading under-reports output by however much the model thought (80%
in the test case). `_Call` defines one inclusive meaning and each adapter
converts into it. A fourth provider must be checked against all three
questions, not assumed to match whichever one was looked at last.

**`FIXED` — the judge's max_tokens hint only knew Anthropic's spelling.**
`stop_reason == "max_tokens"` never matched xAI's `length` or Gemini's
`MAX_TOKENS`, so the "raise the scorer's max_tokens" advice never fired for a
Grok judge — the case that most needed it. Use `_Call.truncated`, not a
`stop_reason` comparison; `stop_reason` deliberately keeps the vendor's own
word for the span.

**`WATCH` — Gemini's timeout is milliseconds, and an int.**
Ours is float seconds. Passing it straight through asks for a 30ms deadline and
fails every call, reporting a timeout — which reads like a slow model rather
than a units bug.

**`WATCH` — `validate_key` must consume Gemini's listing.**
`models.list()` is lazy. A bare call sends no request and would report a bad
key as valid.

**`WATCH` — the Gemini provider name is `gcp.gemini`, with a dot.**
That is the semantic convention's value, and `gcp.vertex_ai` is a *different*
value for the same models reached a different way — a hand-picked "gemini"
would erase that distinction. It is a database value and a UI select value, so
anything parsing provider names must tolerate the dot.

**`OPEN` — the SDK's tracing wrapper is still Anthropic-only.**
`sdk/src/obs_sdk/tracing.py:265` hardcodes `"anthropic"`. That instruments the
*observed* app rather than this backend's spending, so it shares no code with
the provider registry and moves on its own schedule — but a Grok app
instrumented with this SDK will be mispriced and mislabelled.

**`WATCH` — error text avoids indefinite articles before vendor names.**
"a Anthropic key" / "an Google key" is the bug that writes itself the next time
a provider is registered. Both messages are phrased around it.

## Running it

**`OPEN` — the host dev servers shadow the compose stack.**
A host uvicorn on :8000 and a host Next on :3000 were running alongside Docker.
Compose's backend publishes **no host port at all**, and its web binds
`127.0.0.1:3000` while the host Next holds the wildcard. So:

| URL | reaches |
|---|---|
| `localhost:3000`, `[::1]:3000` | the **host** stack — your real data |
| `127.0.0.1:3000` | the **container** — a separate, empty database |

Two installs, two databases, one plausible-looking URL each. Run one or the
other. Symptom to recognise: data that is impossibly old or impossibly absent
for a database you just created.

**`FIXED` — `POSTGRES_PASSWORD` is required and was absent from `.env`.**
Compose refuses to start without it. Must be URL-safe — it is interpolated into
`OBS_DATABASE_URL`, and base64's `/` and `+` truncate the URL, which then
presents as "failed to resolve host 'obs'" and points nowhere near the
password. Use `openssl rand -hex 32`.

**`WATCH` — `POSTGRES_PASSWORD` only takes effect on an empty `pgdata` volume.**
Postgres reads it at initialisation. Changing it later leaves the old password
in the volume and the backend unable to authenticate.

**`OPEN` — no boot-time key check any more.**
`Settings.require_anthropic_key` is gone (`credentials.warn_if_no_keys`,
`credentials.py:351`, warns instead). Keys live in Postgres and arrive through
the Keys page, so refusing to boot without one in `.env` was circular — a fresh
install could never start the UI it needs to be given a key. **This is a
deliberate departure from `CLAUDE.md`'s "refuse to start if the LLM API key env
var is missing".** The guarantee it protected is intact: `credentials.resolve`
still refuses on every path that spends, before any money moves.

**`WATCH` — deps must stay off the iCloud-synced Desktop.**
Not new, but it still holds: iCloud sync corrupts venvs and `node_modules`.

## A Secure cookie over plain HTTP: login "succeeds", then bounces

**Symptom.** `POST /api/auth/login` returns 200 and sets a cookie you can see
in the response. Every request after it returns 401 and the UI drops you back
on the login form, as though the password were wrong. Devtools shows the
Set-Cookie header, so it looks like the backend is rejecting its own session.

**Cause.** The browser never stored the cookie. `Secure` means HTTPS-only, and
the page was served over `http://` — so the cookie was discarded on arrival,
silently, and the next request carried nothing. Nothing in the login response
indicates this; the server did its job and the browser quietly declined.

**Which way round.**

| Serving over | `OBS_COOKIE_SECURE` |
|---|---|
| `http://localhost:3000` (dev) | `false` |
| `http://<lan-ip>:3000` (phone on your Wi-Fi) | `false`, plus `OBS_ALLOW_INSECURE_COOKIES=true` if the backend also binds non-loopback |
| any `https://` URL | `true` — or leave it unset, since that is the default |

**Why the default is `true` now.** It used to be `false` while the comment
above it claimed the opposite, which meant any deployment that was not
`compose.yaml` shipped cleartext session cookies and said nothing about it.
Forgetting the variable on a live host now yields a safe cookie; forgetting it
locally yields this very loud failure, which is the direction you want the
mistake to point.

**The related trap.** `SameSite=none` without `Secure` is not weaker — it is
*discarded*, producing exactly the same symptom. `check_cookie_security`
refuses to boot on that pairing on every host, loopback included, because the
browser does not care where the server is.

## `next dev` from a phone: the page paints, and nothing works

**Symptom.** Reach the dev server on the machine's LAN address instead of
`localhost` and the app looks fine — styles, layout, copy, all correct. Then
nothing that needs JavaScript happens. The login form does nothing when you
submit it. A page that fetches on mount sits on its loading state forever
(`Checking your invite…` with an empty account field). No error appears
anywhere, and the same URL on `localhost` works perfectly.

**Cause.** Next 16 blocks cross-origin dev requests. The HTML is
server-rendered and served, but the client bundle and the HMR socket are
refused, so **React never hydrates**. What you are looking at is the SSR
output, frozen in its initial state — which is exactly why a loading spinner
never resolves: the query that would end it lives in client code that never
ran. The console shows repeated
`WebSocket connection to 'ws://<lan-ip>:3000/_next/webpack-hmr' failed`, which
reads like a hot-reload annoyance and is actually the whole story.

**Fix.** `allowedDevOrigins` in `web/next.config.ts`. It computes the machine's
own non-internal IPv4 addresses at startup rather than hardcoding one, because
a written-down IP goes stale the next time DHCP moves — and it goes stale
*silently, in the same shape as this bug*.

**Why it is worth knowing.** This mimics an auth problem convincingly. The
first read of "I could see it on my phone but couldn't log in" is a wrong
password, and the login-attempts table appears to support that, because a form
that never hydrated submits nothing at all — so there is no failed attempt
recorded, and an empty table looks like "you never tried" rather than "the
button is dead".

**Still expected after the fix.** The HMR websocket keeps failing from a LAN
origin, so edits do not hot-reload on the phone; reload manually. Hydration —
the part that matters — works. None of this affects `next build` /
`next start`, which have no dev-origin restriction at all.


## Recovering a component you deleted before writing it out

A refactor that carved `People` out of `keys/page.tsx` wrote the *remainder*
back to disk and kept the carved-out half only in a Python variable, which
died with the process. The component had never been committed, so `git` had
nothing to restore.

It was recoverable from the dev server's **source maps**: Next writes
`sourcesContent` into `.next/**/*.js.map`, so the pre-edit file was sitting in
the build output.

```
python3 - <<'EOF'
import glob, json
for path in glob.glob(".next/**/*.js.map", recursive=True):
    m = json.load(open(path))
    for src, content in zip(m.get("sources", []), m.get("sourcesContent") or []):
        if content and src.endswith("app/keys/page.tsx"):
            print(path, len(content))
EOF
```

Pick the longest match — several chunks carry overlapping copies and the
largest is usually the whole file. This only works until the next build
overwrites the chunk, so do it before rebuilding.
