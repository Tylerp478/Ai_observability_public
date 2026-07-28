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
export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
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
 * Dashboard rollup. All three totals describe the same population — gen_ai
 * `chat` spans, i.e. prompts actually sent to a model — so they divide into
 * each other correctly. `series` is always `window_hours` buckets ending at
 * the current hour, zero-filled by the backend.
 */
export interface Overview {
  window_hours: number;
  prompts: number;
  duration_ms: number;
  cost_usd: number;
  series: { hour_start: number; prompts: number }[];
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
// form neither sets them nor should be able to.
export type ScorerDraft = Omit<
  Scorer,
  "id" | "created_at" | "score_count" | "prompt_id" | "version"
>;

// Declared here rather than beside RUN_MODELS below: SCORER_PRESETS
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

/** Models offered in the run form. Kept in step with the SDK pricing table —
 *  a model missing from that table runs fine but reports no cost. */
export const RUN_MODELS = [
  "claude-sonnet-5",
  "claude-opus-5",
  "claude-haiku-4-5",
] as const;

// --- calls ---------------------------------------------------------------

export const api = {
  login: (email: string, password: string) =>
    request<{ email: string }>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),

  logout: () => request<{ status: string }>("/api/auth/logout", { method: "POST" }),

  me: () => request<{ email: string; user_id: string }>("/api/auth/me"),

  overview: (hours = 24) => request<Overview>(`/api/overview?hours=${hours}`),

  traces: (limit = 50) =>
    request<{ traces: TraceSummary[]; count: number }>(`/api/traces?limit=${limit}`),

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

  archiveScorer: (id: string) =>
    request<{ status: string }>(`/api/scorers/${id}`, { method: "DELETE" }),

  tryScorer: (
    id: string,
    body: { output: string; input?: string; expected?: string | null },
  ) =>
    request<TryScorerResult>(`/api/scorers/${id}/try`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  scoreRun: (runId: string, scorerIds: string[]) =>
    request<{ status: string; judge_calls: number }>(`/api/runs/${runId}/score`, {
      method: "POST",
      body: JSON.stringify({ scorer_ids: scorerIds }),
    }),

  scoreSpan: (traceId: string, spanId: string, scorerIds: string[]) =>
    request<{ status: string; score_ids: string[] }>(
      `/api/traces/${traceId}/spans/${spanId}/score`,
      { method: "POST", body: JSON.stringify({ scorer_ids: scorerIds }) },
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

export function formatCost(usd: number): string {
  if (usd === 0) return "$0";
  // Sub-cent costs are the norm for a single call, so 2dp would render
  // everything as "$0.00" and hide the number that matters.
  if (usd < 0.01) return `$${usd.toFixed(5)}`;
  return `$${usd.toFixed(4)}`;
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
