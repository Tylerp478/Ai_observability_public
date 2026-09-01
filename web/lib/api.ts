/**
 * Backend client.
 *
 * Every call sends `credentials: "include"` so the session cookie travels.
 * Without it fetch omits cookies on cross-origin requests and every call
 * comes back 401 while the browser devtools clearly show a valid cookie —
 * a genuinely confusing failure to debug.
 */

// Empty by default: calls go to the Next.js origin and are proxied to the
// backend by the rewrites in next.config.ts, which keeps the session cookie
// first-party. Set NEXT_PUBLIC_API_BASE only when the backend really is on a
// different origin — that also needs SameSite=None, Secure, and the backend's
// CORS allowlist.
import { currentProjectId } from "@/lib/project";

export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

/** Names the project every call is scoped to. See `lib/project.ts` for why
 *  this is a header the client sets rather than a cookie the browser sends:
 *  a cookie would ride along on requests that never chose a project. */
const PROJECT_HEADER = "X-Obs-Project";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const project = currentProjectId();
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: "include",
    // Never read from the HTTP cache. The backend sets no cache headers, which
    // leaves the browser free to use its heuristic freshness rules on plain
    // GETs — and it does. The symptom is ugly: a page renders an empty state
    // while the identical URL, fetched with a cache-buster, returns data, and
    // the network panel shows a 200 for a response the server never sent this
    // time. Every read here is live observability data where a stale answer is
    // worse than a slow one, so there is nothing to trade away.
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      // Every call, not just the reads: creating a dataset or a key has to
      // land in the project on screen, and a write that ignored the selector
      // would silently populate whichever project the backend defaults to.
      ...(project ? { [PROJECT_HEADER]: project } : {}),
      ...(init?.headers ?? {}),
    },
  });

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      // Non-JSON error body; the status text is all we have.
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

// --- types mirroring the backend read API --------------------------------

export interface TraceSummary {
  trace_id: string;
  root_name: string;
  root_status: string;
  start_time_unix_nano: number;
  duration_ms: number;
  span_count: number;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  has_error: number;
  service_name: string;
}

/**
 * Every headline number for one period.
 *
 * Its own type because the dashboard carries two of these — the window on
 * screen and the window before it — and a baseline built to a different shape
 * than the number it qualifies is a bug waiting to happen. The backend shapes
 * both through one function for the same reason.
 */
export interface OverviewTotals {
  prompts: number;
  duration_ms: number;
  cost_usd: number;
  /** Chat spans that came back with status ERROR. Per prompt, not per trace. */
  errors: number;
  /** `errors / prompts`, 0 when nothing ran. Computed server-side so the rate
   *  and the counts behind it always describe the same window. */
  error_rate: number;
  /**
   * Latency percentiles over the period's chat spans, in milliseconds.
   *
   * Null — not zero — when nothing ran, or when nothing recorded a duration. A
   * p95 of 0ms would be a claim about speed that no call ever supported.
   */
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  input_tokens: number;
  output_tokens: number;
}

/**
 * Dashboard rollup. All the totals describe the same population — gen_ai
 * `chat` spans, i.e. prompts actually sent to a model — so they divide into
 * each other correctly. `series` covers exactly `window_hours`, ending at the
 * current bucket and zero-filled by the backend.
 */
export interface Overview extends OverviewTotals {
  window_hours: number;
  /**
   * Seconds per `series` point. Not always an hour: the backend widens the
   * bucket with the window so the series stays 12-30 points at every range
   * (5 minutes at 1h, a day at 30d). Everything that labels the time axis has
   * to read this rather than assume.
   */
  bucket_seconds: number;
  /** The source these numbers describe; "" means every source. Echoed back by
   *  the backend so a filtered zero is distinguishable from an empty one. */
  source: string;
  /** The provider key these numbers describe; "" means every key. */
  credential: string;
  /**
   * The same totals over the window of equal length immediately before this
   * one, so every tile can say which way its number is moving.
   *
   * Both periods are measured from the current clock rather than from the
   * bucket grid, and are exactly the same length — the newest bucket is always
   * partial, and comparing it against a complete one would print a fall in
   * traffic that is only the time of day.
   */
  previous: OverviewTotals;
  series: { bucket_start: number; prompts: number }[];
  /**
   * Share of prompts by model, same window and filters as the totals above,
   * so these counts sum to `prompts` exactly.
   *
   * Keyed on the model that was *requested*, not the one that answered: a
   * failed call has no response model, and would otherwise drop out of a
   * breakdown whose whole job is "what did I run".
   */
  models: { model: string; prompts: number; cost_usd: number }[];
  /**
   * The ten most expensive traces in the same window, filters and currency as
   * the totals above. Free traces are omitted, so this can be shorter than ten
   * — including $0.00 rows to reach a round number would imply the tail costs
   * something.
   */
  top_traces: TopTrace[];
}

export interface TopTrace {
  trace_id: string;
  /** The root span's name — what the trace was. */
  root_name: string;
  /** service.name: which subsystem or application did the work. */
  category: string;
  start_time_unix_nano: number;
  span_count: number;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
}

/**
 * One service reporting spans in — an instrumented app, or one of the
 * backend's own internal services (obs-runner, obs-judge, obs-guardrail,
 * obs-playground). Derived from span data, not registered anywhere, so a new
 * app shows up the moment it sends its first span.
 */
/**
 * One scorer's headline number over a window.
 *
 * Which field carries the answer depends on output_type — `mean` for numeric,
 * `pass_rate` for boolean, `top_label` for categorical. There is deliberately
 * no single "score" field: averaging a 1-5 scale together with a pass rate
 * produces a number that renders convincingly and means nothing.
 */
export interface ScorerAverage {
  scorer_id: string;
  scorer_name: string;
  output_type: ScorerOutputType;
  score_min: number | null;
  score_max: number | null;
  pass_threshold: number | null;
  scored: number;
  mean: number | null;
  pass_rate: number | null;
  top_label: string;
}

export interface Source {
  name: string;
  span_count: number;
  last_span_unix_nano: number;
}

/**
 * A stored provider API key — which Anthropic account a call bills to.
 *
 * The secret is never in this object and is never returned by the API. A key
 * is identified by its name and last four characters; unlike an ingest key
 * there is no reason to show it again, because nothing outside the backend
 * ever needs it.
 */
/** What someone may do. `viewer` reads everything and writes nothing; `admin`
 *  does everything. A third role — spend, but no admin surfaces — is deferred
 *  until someone needs it. */
export type Role = "admin" | "viewer";

/**
 * A row on the allowlist, which is not the same thing as a user.
 *
 * The row exists from the moment they are invited, so a pending invite is
 * something you can see and revoke rather than an email you have to remember
 * sending. `accepted_at` is what separates "invited" from "has an account".
 */
export interface Person {
  email: string;
  /** What the admin typed, available before they have ever signed in. */
  name: string;
  role: Role;
  note: string;
  added_at: string | null;
  accepted_at: string | null;
  revoked_at: string | null;
  invite_expires_at: string | null;
  invite_pending: boolean;
  /** An outstanding reset link. They can still sign in with the old password
   *  until it is used, which is why this is not the same as "invited". */
  reset_pending: boolean;
  /** Last activity, not last login — sessions renew for 30 days, so a login
   *  date would badly understate how current someone is. Null once their
   *  sessions age out. */
  last_seen_at: string | null;
}

/**
 * A project: the boundary datasets, scorers, prompts, keys and spans all sit
 * inside. Switching changes what exists, not what is shown — see the backend's
 * `projects.py` for why there is no across-projects view.
 */
export interface Project {
  id: string;
  name: string;
  created_at: string | null;
  /** Counts, so a picker can say which projects are actually in use rather
   *  than making you switch into each one to find out. */
  ingest_keys: number;
  provider_keys: number;
  datasets: number;
}

export interface Credential {
  id: string;
  name: string;
  provider: string;
  last4: string;
  is_default: boolean;
  created_at: string | null;
  last_used_at: string | null;
  run_cost_usd: number;
  score_cost_usd: number;
  /**
   * Everything this key has cost, from the span store.
   *
   * Not run_cost_usd + score_cost_usd: those come from Postgres and know only
   * about eval spend, so a Playground or guardrail call is invisible to them.
   * Every billable call writes a span, so this is the figure that is true.
   */
  spend_usd: number;
}

export interface Span {
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  name: string;
  kind: string;
  start_time_unix_nano: number;
  end_time_unix_nano: number;
  duration_ms: number;
  status_code: string;
  status_message: string;
  service_name: string;
  gen_ai_operation_name: string | null;
  gen_ai_request_model: string | null;
  gen_ai_response_model: string | null;
  gen_ai_usage_input_tokens: number | null;
  gen_ai_usage_output_tokens: number | null;
  gen_ai_finish_reasons: string | null;
  gen_ai_input_messages: string | null;
  gen_ai_output_messages: string | null;
  gen_ai_tool_name: string | null;
  gen_ai_agent_name: string | null;
  obs_cost_usd: number | null;
  obs_latency_seconds: number | null;
  attributes_json: string;
  events_json: string;
}

export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  created_at: string | null;
  last_used_at: string | null;
  revoked: boolean;
}

// --- step 3: datasets and replay runs -------------------------------------

export interface DatasetSummary {
  id: string;
  name: string;
  description: string;
  created_at: string | null;
  item_count: number;
  run_count: number;
  last_run_at: string | null;
}

export interface DatasetItem {
  id: string;
  input: string;
  expected_output: string | null;
  source_trace_id: string;
  source_span_id: string;
  created_at: string | null;
}

export type RunStatus = "pending" | "running" | "succeeded" | "failed" | "cancelled";

export interface Run {
  id: string;
  dataset_id: string;
  name: string;
  prompt_template: string;
  model: string;
  max_tokens: number;
  status: RunStatus;
  error: string;
  item_count: number;
  completed_count: number;
  error_count: number;
  cost_usd: number;
  trace_id: string;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  /** Scorers that fire automatically when this run finishes (step 4). */
  scorer_ids: string[];
  // --- step 5 provenance ---
  // Null on an ad-hoc run: the prompt was typed into the form and belongs to
  // nothing. prompt_template above is still the text that actually went out,
  // in both cases and forever.
  prompt_version_id: string | null;
  prompt_version: number | null;
  prompt_id: string | null;
  prompt_name: string | null;
  /** The label this version was resolved through, if one was used. */
  prompt_label: string;
}

export interface RunItem {
  id: string;
  ordinal: number;
  status: "pending" | "running" | "succeeded" | "failed" | "skipped";
  output: string;
  error: string;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
  latency_ms: number | null;
  finish_reason: string;
  span_id: string;
  dataset_item_id: string;
  input: string;
  expected_output: string | null;
  source_trace_id: string;
  scores: Score[];
}

// --- step 4: LLM-as-judge scorers -----------------------------------------

export type ScorerOutputType = "boolean" | "numeric" | "categorical";

export interface Scorer {
  id: string;
  name: string;
  description: string;
  prompt_template: string;
  model: string;
  max_tokens: number;
  output_type: ScorerOutputType;
  score_min: number | null;
  score_max: number | null;
  categories: string[];
  pass_threshold: number | null;
  /** Whether the Playground offers this scorer. Not part of the definition —
   *  it changes where the scorer is presented, not how it judges. */
  show_in_playground: boolean;
  created_at?: string | null;
  score_count?: number;
  /** The versioned artifact behind this definition (step 5). */
  prompt_id: string | null;
  version: number | null;
}

export interface Score {
  id: string;
  scorer_id: string;
  scorer_name: string;
  output_type: ScorerOutputType;
  score_min: number | null;
  score_max: number | null;
  pass_threshold: number | null;
  status: "pending" | "running" | "succeeded" | "failed";
  value: number | null;
  label: string;
  passed: boolean | null;
  reasoning: string;
  error: string;
  cost_usd: number | null;
  latency_ms: number | null;
  judge_trace_id: string;
  judge_span_id: string;
  run_item_id: string | null;
  trace_id: string;
  span_id: string;
  created_at: string | null;
  // Which version of the scorer judged this (step 5). Null for scores taken
  // before step 5 — those genuinely do not know, and saying so beats guessing.
  prompt_id: string | null;
  prompt_version_id: string | null;
  scorer_version: number | null;
}

export interface ScoreSummary {
  scorer_id: string;
  scorer_name: string;
  output_type: ScorerOutputType;
  score_min: number | null;
  score_max: number | null;
  total: number;
  scored: number;
  pending: number;
  failed: number;
  cost_usd: number;
  mean: number | null;
  pass_rate: number | null;
  distribution: Record<string, number> | null;
  prompt_id: string | null;
  /** Scorer versions behind this number. More than one means the mean mixes
   *  verdicts from two different judges, which the UI says out loud. */
  versions: number[];
}

// --- step 5: prompts as versioned artifacts -------------------------------

export type PromptKind = "completion" | "scorer";

export interface PromptLabel {
  label: string;
  version_id: string;
  version: number;
  moved_at: string | null;
}

export interface PromptVersion {
  id: string;
  prompt_id: string;
  version: number;
  template: string;
  /** Settings frozen with the text: model and max_tokens, plus a scorer's
   *  output schema. Loose because a scorer's config and a completion
   *  prompt's are different shapes stored in one column. */
  config: Record<string, unknown>;
  note: string;
  created_at: string | null;
  run_count?: number;
}

export interface PromptSummary {
  id: string;
  name: string;
  description: string;
  kind: PromptKind;
  created_at: string | null;
  updated_at: string | null;
  latest_version: number | null;
  version_count: number;
  run_count: number;
  labels: PromptLabel[];
}

export interface Prompt {
  id: string;
  name: string;
  description: string;
  kind: PromptKind;
  created_at: string | null;
  updated_at: string | null;
  archived: boolean;
  labels: PromptLabel[];
  versions: PromptVersion[];
}

export interface DiffLine {
  op: "equal" | "insert" | "delete";
  left_no: number | null;
  right_no: number | null;
  text: string;
}

export interface PromptDiff {
  from: { id: string; version: number; note: string };
  to: { id: string; version: number; note: string };
  lines: DiffLine[];
  added: number;
  removed: number;
  config_changes: { key: string; from: unknown; to: unknown }[];
}

// --- step 6: real-time guardrails -----------------------------------------

/** What a triggered guardrail does. `flag` is shadow mode: it records and
 *  reports, and never blocks. */
export type GuardrailAction = "block" | "flag";

/** What a judge that errored or timed out means for this guardrail. */
export type GuardrailOnError = "allow" | "block";

export interface Guardrail {
  id: string;
  name: string;
  description: string;
  scorer_id: string;
  action: GuardrailAction;
  /** Categorical scorers only — which labels count as a trigger. */
  block_labels: string[];
  on_error: GuardrailOnError;
  enabled: boolean;
  created_at: string | null;
  // Denormalized from the scorer, so a row renders without a second fetch.
  scorer_name: string;
  output_type: ScorerOutputType;
  score_min: number | null;
  score_max: number | null;
  pass_threshold: number | null;
  categories: string[];
  model: string;
  check_count?: number;
  trigger_count?: number;
}

export type GuardrailDraft = Omit<
  Guardrail,
  | "id"
  | "created_at"
  | "scorer_name"
  | "output_type"
  | "score_min"
  | "score_max"
  | "pass_threshold"
  | "categories"
  | "model"
  | "check_count"
  | "trigger_count"
>;

export interface GuardrailResult {
  guardrail_id: string | null;
  guardrail_name: string;
  scorer_id?: string;
  scorer_name?: string;
  output_type?: ScorerOutputType;
  action: GuardrailAction;
  /** Did this rule fire? Separate from `passed` — an errored judge under
   *  on_error='block' triggers while having no verdict at all. */
  triggered: boolean;
  status: "succeeded" | "failed";
  value: number | null;
  label: string;
  passed: boolean | null;
  reasoning: string;
  error: string;
  cost_usd: number | null;
  latency_ms: number | null;
  span_id: string;
}

export interface GuardrailCheck {
  id: string;
  input: string;
  output: string;
  source: string;
  decision: "pass" | "block";
  degraded: boolean;
  latency_ms: number | null;
  cost_usd: number | null;
  trace_id: string;
  created_at: string | null;
  results: GuardrailResult[];
}

/** What POST /v1/guardrail returns — the same shape the SDK client parses. */
export interface GuardrailDecision {
  check_id: string;
  decision: "pass" | "block";
  blocked: boolean;
  triggered: string[];
  flagged: string[];
  degraded: boolean;
  latency_ms: number;
  cost_usd: number;
  trace_id: string;
  results: GuardrailResult[];
}

export interface GuardrailStats {
  window_hours: number;
  checks: number;
  blocked: number;
  degraded: number;
  block_rate: number | null;
  avg_latency_ms: number | null;
  cost_usd: number;
}

export interface TryScorerResult {
  value: number | null;
  label: string;
  passed: boolean | null;
  reasoning: string;
  cost_usd: number | null;
  latency_ms: number;
  input_tokens: number;
  output_tokens: number;
  prompt: string;
  trace_id: string;
}

// The editable definition. prompt_id and version are excluded along with the
// other server-owned fields: they describe where the history lives, and the
// form neither sets them nor should be able to. show_in_playground is excluded
// for a different reason — it has its own endpoint, because changing it must
// not append a version to the scorer's history.
export type ScorerDraft = Omit<
  Scorer,
  "id" | "created_at" | "score_count" | "prompt_id" | "version" | "show_in_playground"
>;

// Declared here rather than beside the model helpers below: SCORER_PRESETS
// interpolates all three at module evaluation, and a const declared after its
// use is a temporal-dead-zone ReferenceError rather than a hoisted undefined.
export const INPUT_PLACEHOLDER = "{{input}}";
export const OUTPUT_PLACEHOLDER = "{{output}}";
export const EXPECTED_PLACEHOLDER = "{{expected}}";

/**
 * Starter scorers, covering the three qualities CLAUDE.md names.
 *
 * Form prefills rather than rows seeded into the database: a scorer you did
 * not write, silently spending money on your key, is not a good default. These
 * appear as templates you can edit before saving.
 */
export const SCORER_PRESETS: { label: string; hint: string; draft: ScorerDraft }[] = [
  {
    label: "Hallucination",
    hint: "boolean · did the answer invent anything?",
    draft: {
      name: "Hallucination",
      description: "Fails when the output asserts specifics the input does not support.",
      output_type: "boolean",
      model: "claude-sonnet-5",
      max_tokens: 1024,
      score_min: null,
      score_max: null,
      categories: [],
      pass_threshold: null,
      prompt_template: `You are checking an AI assistant's answer for unsupported claims.

The user asked:
${INPUT_PLACEHOLDER}

The assistant answered:
${OUTPUT_PLACEHOLDER}

A claim is unsupported if it states a specific fact — a name, number, date,
citation, quote, or capability — that is not present in the question and is
not general knowledge you are confident is correct.

Record passed = true if the answer makes no unsupported claims.
Record passed = false if it makes even one.
Vagueness and refusal are not hallucination; confident invention is.`,
    },
  },
  {
    label: "Relevance",
    hint: "1–5 · did it answer the question asked?",
    draft: {
      name: "Relevance",
      description: "How directly the output addresses what was actually asked.",
      output_type: "numeric",
      model: "claude-sonnet-5",
      max_tokens: 1024,
      score_min: 1,
      score_max: 5,
      categories: [],
      pass_threshold: 4,
      prompt_template: `Rate how well the answer addresses the question that was asked.

Question:
${INPUT_PLACEHOLDER}

Answer:
${OUTPUT_PLACEHOLDER}

5 — answers exactly what was asked, nothing missing, nothing padded
4 — answers the question, with some detour or omission
3 — partially answers it, or answers a nearby question
2 — mostly off-target but touches the topic
1 — does not address the question at all

Judge relevance only. A wrong answer to the right question still scores high
here; correctness is a different scorer's job.`,
    },
  },
  {
    label: "Faithfulness",
    hint: "1–5 · does it agree with the expected answer?",
    draft: {
      name: "Faithfulness",
      description: "Agreement with the recorded expected output. Needs expected outputs.",
      output_type: "numeric",
      model: "claude-sonnet-5",
      max_tokens: 1024,
      score_min: 1,
      score_max: 5,
      categories: [],
      pass_threshold: 4,
      prompt_template: `Compare an answer against a known-good reference answer.

Question:
${INPUT_PLACEHOLDER}

Reference answer:
${EXPECTED_PLACEHOLDER}

Answer to judge:
${OUTPUT_PLACEHOLDER}

5 — same substance as the reference; wording may differ freely
4 — same substance, minor detail missing or added
3 — partly agrees, but omits or contradicts something that matters
2 — largely disagrees with the reference
1 — contradicts the reference outright

Judge substance, not style or length. If no reference answer was recorded for
this case, score 3 and say so in your reasoning.`,
    },
  },
];

/** Formats one score for a chip: "4/5", "pass", or a category label. */
export function formatScore(score: Score): string {
  if (score.status !== "succeeded") return score.status === "failed" ? "error" : "…";
  if (score.output_type === "numeric" && score.value != null) {
    return `${+score.value.toFixed(2)}/${score.score_max}`;
  }
  return score.label;
}

/** Green for good, red for bad, neutral where there is no pass/fail concept. */
export function scoreTone(score: Score): string {
  if (score.status === "failed") return "bg-red-950 text-red-300";
  if (score.status !== "succeeded") return "bg-neutral-800 text-neutral-400";
  if (score.passed === true) return "bg-emerald-950 text-emerald-300";
  if (score.passed === false) return "bg-red-950 text-red-300";
  return "bg-neutral-800 text-neutral-300";
}

/**
 * One ad-hoc prompt sent from the Playground, with the span it was recorded
 * as. The scores are NOT here — `score_ids` names the rows that judges are
 * filling in on a background thread, and the caller polls `api.trace` for the
 * verdicts, which is how every other scoring path in the app behaves.
 */
export interface PlaygroundResult {
  trace_id: string;
  span_id: string;
  prompt: string;
  output: string;
  model: string;
  response_model: string;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number | null;
  latency_ms: number;
  finish_reason: string;
  score_ids: string[];
  /** Which key paid for the *generation*. */
  credential_id: string;
  credential_name: string;
  /**
   * Which key(s) paid for the judging, which need not be the one above.
   *
   * A scorer grades with its own model, so a Claude scorer is billed to an
   * Anthropic key even when the completion was generated on Grok. Empty when
   * no scorers ran.
   */
  judged_by: string[];
  /** Non-empty when scorers were picked but there was nothing to judge. */
  scoring_skipped: string;
}

/** A provider a key can be created for. Served from the llm.py registry so
 *  adding a provider is one backend change, not two. */
export interface Provider {
  name: string;
  label: string;
  /** Models this provider offers. The backend guarantees each one is priced. */
  models: string[];
  /** What this vendor's keys look like, for the paste field's placeholder. */
  key_hint: string;
}

/** Last-resort model list, used only before /api/providers has answered.
 *
 *  The real list is served per provider by the backend — see `useModels`. This
 *  exists so a select is never rendered empty on first paint, and is not a
 *  place to add models: a model here that the backend does not offer would be
 *  selectable and then rejected. */
export const FALLBACK_MODELS = ["claude-sonnet-5"] as const;

// --- calls ---------------------------------------------------------------

export const api = {
  login: (email: string, password: string) =>
    request<{ email: string }>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),

  logout: () => request<{ status: string }>("/api/auth/logout", { method: "POST" }),

  me: () =>
    request<{ email: string; user_id: string; role: Role }>("/api/auth/me"),

  sources: () => request<{ sources: Source[] }>("/api/sources"),

  // No `source` argument, unlike the span-backed reads: a score belongs to the
  // judge that produced it, not to the app whose output was judged.
  scoreSummary: (hours = 24, credential = "") =>
    request<{ scorers: ScorerAverage[]; window_hours: number }>(
      `/api/scores/summary?hours=${hours}` +
        (credential ? `&credential=${encodeURIComponent(credential)}` : ""),
    ),

  // --- provider keys ---
  /** Also reports which project the backend resolved this call to, so a
   *  client holding an id that no longer exists can notice and adopt the
   *  answer instead of 400ing on every other route. */
  people: () =>
    request<{ people: Person[]; roles: Role[] }>("/api/people"),

  /** Returns the invite token exactly once — there is no way to read it back,
   *  by design, the same as an ingest key. */
  invitePerson: (body: { email: string; name: string; role: Role; note: string }) =>
    request<{ email: string; token: string }>("/api/people", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  setPersonRole: (email: string, role: Role) =>
    request<{ email: string; role: Role }>(
      `/api/people/${encodeURIComponent(email)}`,
      { method: "PATCH", body: JSON.stringify({ role }) },
    ),

  /** A single-use link to set a new password. Returned once, like an invite —
   *  the backend stores only its hash. */
  resetPersonPassword: (email: string) =>
    request<{ email: string; token: string }>(
      `/api/people/${encodeURIComponent(email)}/reset`,
      { method: "POST" },
    ),

  revokePerson: (email: string) =>
    request<{ status: string }>(`/api/people/${encodeURIComponent(email)}`, {
      method: "DELETE",
    }),

  /** Public: who a pending invite is for. Gated on holding the token. */
  checkInvite: (token: string) =>
    request<{ email: string }>(`/api/auth/invite?token=${encodeURIComponent(token)}`),

  acceptInvite: (token: string, password: string) =>
    request<{ email: string; role: Role }>("/api/auth/accept", {
      method: "POST",
      body: JSON.stringify({ token, password }),
    }),

  projects: () =>
    request<{ projects: Project[]; current: string; default: string }>("/api/projects"),

  createProject: (name: string) =>
    request<{ id: string; name: string }>("/api/projects", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),

  renameProject: (id: string, name: string) =>
    request<{ id: string; name: string }>(`/api/projects/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ name }),
    }),

  credentials: () => request<{ credentials: Credential[] }>("/api/credentials"),

  providers: () =>
    request<{ providers: Provider[]; model_tiers: Record<string, number> }>(
      "/api/providers",
    ),

  createCredential: (body: {
    name: string;
    secret: string;
    provider?: string;
    make_default?: boolean;
  }) =>
    request<{ id: string }>("/api/credentials", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  setDefaultCredential: (id: string) =>
    request<{ status: string }>(`/api/credentials/${id}/default`, { method: "POST" }),

  archiveCredential: (id: string) =>
    request<{ status: string }>(`/api/credentials/${id}`, { method: "DELETE" }),

  // `source` is encoded rather than interpolated raw: a service.name is
  // whatever the instrumented app set it to, and nothing stops it containing
  // an ampersand.
  overview: (hours = 24, source = "", credential = "") =>
    request<Overview>(
      `/api/overview?hours=${hours}` +
        (source ? `&source=${encodeURIComponent(source)}` : "") +
        (credential ? `&credential=${encodeURIComponent(credential)}` : ""),
    ),

  // `status` and `sort` go to the server rather than being applied to the
  // response: the result is capped at `limit`, so reordering or filtering here
  // would describe one page of recent traces while claiming to describe all of
  // them.
  traces: (limit = 50, source = "", credential = "", status = "", sort = "recent") =>
    request<{ traces: TraceSummary[]; count: number }>(
      `/api/traces?limit=${limit}` +
        (source ? `&source=${encodeURIComponent(source)}` : "") +
        (credential ? `&credential=${encodeURIComponent(credential)}` : "") +
        (status ? `&status=${encodeURIComponent(status)}` : "") +
        (sort && sort !== "recent" ? `&sort=${encodeURIComponent(sort)}` : ""),
    ),

  trace: (id: string) =>
    request<{
      trace_id: string;
      spans: Span[];
      span_count: number;
      cost_usd: number;
      scores: Score[];
    }>(`/api/traces/${id}`),

  keys: () => request<{ keys: ApiKey[] }>("/api/keys"),

  createKey: (name: string) =>
    request<{ key: string; name: string }>("/api/keys", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),

  revokeKey: (id: string) =>
    request<{ status: string }>(`/api/keys/${id}`, { method: "DELETE" }),

  // --- datasets ---
  datasets: () => request<{ datasets: DatasetSummary[] }>("/api/datasets"),

  createDataset: (name: string, description: string) =>
    request<{ id: string; name: string }>("/api/datasets", {
      method: "POST",
      body: JSON.stringify({ name, description }),
    }),

  dataset: (id: string) =>
    request<
      DatasetSummary & { items: DatasetItem[]; runs: Run[] }
    >(`/api/datasets/${id}`),

  deleteDataset: (id: string) =>
    request<{ status: string }>(`/api/datasets/${id}`, { method: "DELETE" }),

  addItem: (
    datasetId: string,
    body: {
      input: string;
      expected_output?: string | null;
      source_trace_id?: string;
      source_span_id?: string;
    },
  ) =>
    request<{ id: string }>(`/api/datasets/${datasetId}/items`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  deleteItem: (datasetId: string, itemId: string) =>
    request<{ status: string }>(`/api/datasets/${datasetId}/items/${itemId}`, {
      method: "DELETE",
    }),

  // --- runs ---
  createRun: (body: {
    dataset_id: string;
    prompt_template: string;
    model: string;
    max_tokens: number;
    name?: string;
    scorer_ids?: string[];
    credential_id?: string | null;
  }) => request<Run>("/api/runs", { method: "POST", body: JSON.stringify(body) }),

  run: (id: string) =>
    request<Run & { items: RunItem[]; score_summary: ScoreSummary[] }>(
      `/api/runs/${id}`,
    ),

  cancelRun: (id: string) =>
    request<{ status: string }>(`/api/runs/${id}/cancel`, { method: "POST" }),

  // --- scorers ---
  scorers: () => request<{ scorers: Scorer[] }>("/api/scorers"),

  createScorer: (draft: ScorerDraft) =>
    request<{ id: string }>("/api/scorers", {
      method: "POST",
      body: JSON.stringify(draft),
    }),

  // `note` lands on the version this edit creates (step 5), not on the scorer.
  updateScorer: (id: string, draft: ScorerDraft, note = "") =>
    request<{ status: string }>(`/api/scorers/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ ...draft, note }),
    }),

  // Separate from updateScorer so a visibility toggle doesn't append a version.
  setScorerPlayground: (id: string, show: boolean) =>
    request<{ status: string }>(`/api/scorers/${id}/playground`, {
      method: "PATCH",
      body: JSON.stringify({ show }),
    }),

  archiveScorer: (id: string) =>
    request<{ status: string }>(`/api/scorers/${id}`, { method: "DELETE" }),

  tryScorer: (
    id: string,
    body: {
      output: string;
      input?: string;
      expected?: string | null;
      credential_id?: string | null;
    },
  ) =>
    request<TryScorerResult>(`/api/scorers/${id}/try`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // --- playground ---
  playground: (body: {
    prompt: string;
    model: string;
    max_tokens: number;
    input?: string;
    scorer_ids?: string[];
    credential_id?: string | null;
  }) =>
    request<PlaygroundResult>("/api/playground", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  scoreRun: (runId: string, scorerIds: string[], credentialId?: string | null) =>
    request<{ status: string; judge_calls: number }>(`/api/runs/${runId}/score`, {
      method: "POST",
      body: JSON.stringify({ scorer_ids: scorerIds, credential_id: credentialId }),
    }),

  scoreSpan: (
    traceId: string,
    spanId: string,
    scorerIds: string[],
    credentialId?: string | null,
  ) =>
    request<{ status: string; score_ids: string[] }>(
      `/api/traces/${traceId}/spans/${spanId}/score`,
      {
        method: "POST",
        body: JSON.stringify({
          scorer_ids: scorerIds,
          credential_id: credentialId,
        }),
      },
    ),

  // --- prompts (step 5) ---
  prompts: (kind: PromptKind = "completion") =>
    request<{ prompts: PromptSummary[]; suggested_labels: string[] }>(
      `/api/prompts?kind=${kind}`,
    ),

  prompt: (id: string) => request<Prompt>(`/api/prompts/${id}`),

  createPrompt: (body: {
    name: string;
    template: string;
    description?: string;
    config?: Record<string, unknown>;
    note?: string;
  }) =>
    request<{ id: string; version_id: string }>("/api/prompts", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  updatePrompt: (id: string, body: { name: string; description: string }) =>
    request<{ status: string }>(`/api/prompts/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  archivePrompt: (id: string) =>
    request<{ status: string }>(`/api/prompts/${id}`, { method: "DELETE" }),

  addPromptVersion: (
    id: string,
    body: { template: string; config?: Record<string, unknown>; note?: string },
  ) =>
    request<{ id: string; version: number; created: boolean }>(
      `/api/prompts/${id}/versions`,
      { method: "POST", body: JSON.stringify(body) },
    ),

  promptDiff: (id: string, fromVersion: string, toVersion: string) =>
    request<PromptDiff>(
      `/api/prompts/${id}/diff?from_version=${fromVersion}&to_version=${toVersion}`,
    ),

  setPromptLabel: (id: string, label: string, versionId: string) =>
    request<{ status: string; label: string }>(
      `/api/prompts/${id}/labels/${encodeURIComponent(label)}`,
      { method: "PUT", body: JSON.stringify({ version_id: versionId }) },
    ),

  removePromptLabel: (id: string, label: string) =>
    request<{ status: string }>(
      `/api/prompts/${id}/labels/${encodeURIComponent(label)}`,
      { method: "DELETE" },
    ),

  // --- guardrails (step 6) ---
  guardrails: () =>
    request<{ guardrails: Guardrail[]; stats: GuardrailStats }>("/api/guardrails"),

  createGuardrail: (draft: GuardrailDraft) =>
    request<{ id: string }>("/api/guardrails", {
      method: "POST",
      body: JSON.stringify(draft),
    }),

  updateGuardrail: (id: string, draft: GuardrailDraft) =>
    request<{ status: string }>(`/api/guardrails/${id}`, {
      method: "PATCH",
      body: JSON.stringify(draft),
    }),

  archiveGuardrail: (id: string) =>
    request<{ status: string }>(`/api/guardrails/${id}`, { method: "DELETE" }),

  guardrailChecks: (limit = 25, decision?: "pass" | "block") =>
    request<{ checks: GuardrailCheck[] }>(
      `/api/guardrails/checks?limit=${limit}${decision ? `&decision=${decision}` : ""}`,
    ),

  // The same endpoint an instrumented application calls. Screening from the UI
  // goes through it rather than a UI-only variant, so what you try here is
  // exactly what production gets — including the check landing in the log.
  checkGuardrails: (body: { output: string; input?: string; source?: string }) =>
    request<GuardrailDecision>("/v1/guardrail", {
      method: "POST",
      body: JSON.stringify(body),
    }),
};

// --- formatting helpers, shared across views -----------------------------

export function formatDuration(ms: number): string {
  if (ms < 1) return `${(ms * 1000).toFixed(0)}µs`;
  if (ms < 1000) return `${ms.toFixed(0)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

/**
 * One call's latency, for a headline tile.
 *
 * Between the other two on purpose. formatDuration spends 2dp on a single
 * span, which is right in a waterfall and noise at display size;
 * formatDurationLong rounds to whole seconds, which collapses the entire range
 * an LLM p95 actually lands in (1-30s) onto a handful of values and makes a
 * real regression invisible. This keeps one decimal where the number moves and
 * drops it once the magnitude carries the story.
 */
export function formatLatency(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 10_000) return `${(ms / 1000).toFixed(1)}s`;
  if (ms < 60_000) return `${Math.round(ms / 1000)}s`;
  return formatDurationLong(ms);
}

/**
 * A rate as a percentage, for the error tile.
 *
 * Keeps a decimal below 10% because that is where the interesting range is: a
 * dashboard that rounds 0.4% and 1.4% both to "1%" has thrown away the
 * difference between a nuisance and an incident. Above 10% the integer is
 * plenty — nobody needs the decimal to know it is bad.
 */
export function formatRate(fraction: number): string {
  const pct = fraction * 100;
  if (pct === 0) return "0%";
  // Not "0.0%", which reads as clean when it is not.
  if (pct < 0.1) return "<0.1%";
  if (pct < 10) return `${pct.toFixed(1)}%`;
  return `${Math.round(pct)}%`;
}

/**
 * Coarse duration for aggregate totals.
 *
 * formatDuration is built for one span and tops out at seconds — a day's
 * worth of LLM time renders as "4210.00s", which nobody can read. This drops
 * to two units and stops.
 */
export function formatDurationLong(ms: number): string {
  if (ms < 1000) return `${ms.toFixed(0)}ms`;
  const total = Math.round(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return m > 0 ? `${h}h ${m}m` : `${h}h`;
  if (m > 0) return s > 0 ? `${m}m ${s}s` : `${m}m`;
  return `${s}s`;
}

/**
 * Display form of a trace's category — its service.name.
 *
 * Strips this app's own `obs-` prefix, which is carried by every internal
 * subsystem and so distinguishes none of them from each other. An instrumented
 * application's name passes through untouched.
 *
 * Shared by the Overview's cost list and the Traces list so the same trace is
 * labelled the same way in both. The source dropdown deliberately keeps the raw
 * name: it is picking a service, and the raw name is what the backend filters
 * on and what OBS_SERVICE_NAME was set to.
 */
export function categoryLabel(serviceName: string): string {
  return serviceName.replace(/^obs-/, "");
}

export function formatCost(usd: number): string {
  if (usd === 0) return "$0";
  // Sub-cent costs are the norm for a single call, so 2dp would render
  // everything as "$0.00" and hide the number that matters.
  if (usd < 0.01) return `$${usd.toFixed(5)}`;
  return `$${usd.toFixed(4)}`;
}

/**
 * Coarse cost for the dashboard stat tiles.
 *
 * Same split as formatDuration / formatDurationLong, for the same reason.
 * formatCost is built for one call and spends up to five decimals on it, which
 * is right in a trace list and too wide for a tile: three tiles across a 375px
 * phone leaves 83px of text, and "$0.00043" renders at 95px in 20px Inter. It
 * truncated to "$0.00…" — the worst possible failure for a cost figure, since
 * the digits it drops are the entire number.
 *
 * Below a cent this switches to cents rather than rounding to "<$0.01". During
 * development nearly every window total is sub-cent, and a tile that reads
 * "<$0.01" all day answers no question at all. Changing unit by magnitude is
 * what the duration tile beside it already does (ms → s → m → h); this is the
 * same move applied to money.
 *
 * Every branch is measured to fit 83px.
 */
export function formatCostLong(usd: number): string {
  if (usd === 0) return "$0";
  if (usd >= 1000) return `$${(usd / 1000).toFixed(1)}k`;
  if (usd >= 100) return `$${usd.toFixed(0)}`;
  if (usd >= 1) return `$${usd.toFixed(2)}`;
  if (usd >= 0.01) return `$${usd.toFixed(3)}`;

  const cents = usd * 100;
  // toPrecision(2) is safe from exponent notation here: cents is in
  // [0.01, 1), which formats as "0.010" through "0.99".
  if (cents >= 0.01) return `${cents.toPrecision(2)}¢`;
  return "<0.01¢";
}

/**
 * How a metric moved against its baseline.
 *
 * `null` means there is nothing honest to show: a period that measured nothing
 * (a null p95), or two zeros, which is not a 0% change but an absence of any
 * change to report. `"new"` is the other special case — a baseline of zero
 * makes a percentage undefined, and "up from nothing" is the real story.
 */
export type Change =
  | { kind: "new" }
  | { kind: "flat" }
  | { kind: "move"; /** Signed. Percent, or points if `points` was set. */ value: number };

/**
 * Compare a number against the same number one window ago.
 *
 * `points` switches from relative change to absolute difference in percentage
 * points, which is the only correct reading when the metric is itself a rate.
 * An error rate going 20% → 12% is eight points down; calling that "-40%"
 * is arithmetically true about the ratio and reliably misread as a fall of
 * forty points.
 *
 * Anything that rounds away to nothing reports "flat" rather than "0%", which
 * would imply a precision the rounding just threw out.
 */
export function change(
  current: number | null,
  previous: number | null,
  points = false,
): Change | null {
  if (current === null || previous === null) return null;
  if (previous === 0) return current === 0 ? null : { kind: "new" };

  const value = points
    ? (current - previous) * 100
    : ((current - previous) / previous) * 100;

  // Matches the display rounding below, so nothing ever renders as "0%".
  const shown = Math.abs(value) < 10 ? +value.toFixed(1) : Math.round(value);
  return shown === 0 ? { kind: "flat" } : { kind: "move", value };
}

/** A change, rendered. Same rounding rule as formatRate: a decimal where the
 *  number still moves, an integer once the magnitude carries it. */
export function formatChange(value: number, points = false): string {
  const magnitude = Math.abs(value);
  const digits = magnitude < 10 ? magnitude.toFixed(1) : String(Math.round(magnitude));
  return `${digits}${points ? "pp" : "%"}`;
}

/**
 * Token counts for a tile.
 *
 * Compact from a thousand up, unlike formatCountLong which holds out to a
 * hundred thousand. A prompt count is a figure someone might reconcile against
 * the traces list, so it stays exact for as long as it fits; a token total is
 * a magnitude nobody counts, and it reaches seven digits in an afternoon.
 */
export function formatTokens(n: number): string {
  if (n < 1000) return n.toLocaleString();
  return new Intl.NumberFormat(undefined, {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(n);
}

/**
 * Coarse count for the same tiles. Plain toLocaleString is fine up to five
 * digits; "1,000,000" is 88px and would truncate exactly like the cost did.
 */
export function formatCountLong(n: number): string {
  if (n < 100_000) return n.toLocaleString();
  return new Intl.NumberFormat(undefined, {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(n);
}

export function formatTime(unixNano: number): string {
  return new Date(unixNano / 1e6).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function relativeTime(unixNano: number): string {
  const seconds = (Date.now() - unixNano / 1e6) / 1000;
  if (seconds < 60) return `${Math.max(0, Math.floor(seconds))}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}
