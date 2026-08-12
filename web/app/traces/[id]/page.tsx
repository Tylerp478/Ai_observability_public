"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useMemo, useState } from "react";
import { Shell } from "../../shell";
import { CredentialPicker } from "@/components/credential-picker";
import {
  api,
  ApiError,
  formatCost,
  formatDuration,
  formatScore,
  formatTime,
  scoreTone,
  type Score,
  type Span,
} from "@/lib/api";

export default function TraceDetailPage() {
  return (
    <Shell>
      <TraceDetail />
    </Shell>
  );
}

/** Span colour by operation — one glance should say "LLM call" vs "tool". */
function barColor(span: Span): string {
  if (span.status_code === "ERROR") return "bg-red-500";
  switch (span.gen_ai_operation_name) {
    case "chat":
      return "bg-violet-500";
    case "execute_tool":
      return "bg-sky-500";
    case "invoke_agent":
      return "bg-neutral-600";
    default:
      return "bg-neutral-600";
  }
}

/**
 * Wall-clock time the trace spent inside LLM calls.
 *
 * Merges overlapping intervals rather than summing durations. Summing is only
 * correct when calls are sequential — the moment any are concurrent (a replay
 * run fans out four at a time; so does any parallel agent) the total exceeds
 * the trace's own duration and the share renders as something like 193%, which
 * is visibly nonsense and quietly wrong long before it gets that far.
 */
function llmWallClockMs(spans: Span[]): number {
  const intervals = spans
    .filter((s) => s.gen_ai_operation_name === "chat")
    .map((s) => [s.start_time_unix_nano, s.end_time_unix_nano] as const)
    .sort((a, b) => a[0] - b[0]);

  let covered = 0;
  let cursor = -Infinity;
  for (const [start, end] of intervals) {
    const from = Math.max(start, cursor);
    if (end > from) {
      covered += end - from;
      cursor = end;
    }
  }
  return covered / 1e6;
}

function TraceDetail() {
  const params = useParams<{ id: string }>();
  const [openSpan, setOpenSpan] = useState<string | null>(null);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["trace", params.id],
    queryFn: () => api.trace(params.id),
    // Judge calls are background work like a replay, so the page polls until
    // the last one settles rather than leaving a score stuck on "…".
    refetchInterval: (query) =>
      query.state.data?.scores.some(
        (s) => s.status === "pending" || s.status === "running",
      )
        ? 2000
        : false,
  });

  // Depth by walking the parent chain rather than assuming a fixed shape, so
  // deeper nesting (agent -> agent -> tool) indents correctly.
  const layout = useMemo(() => {
    if (!data?.spans.length) return null;
    const spans = data.spans;
    const byId = new Map(spans.map((s) => [s.span_id, s]));

    const depthOf = (span: Span): number => {
      let depth = 0;
      let cursor = span;
      // Bounded: a malformed parent cycle would otherwise hang the render.
      while (cursor.parent_span_id && byId.has(cursor.parent_span_id) && depth < 20) {
        cursor = byId.get(cursor.parent_span_id)!;
        depth += 1;
      }
      return depth;
    };

    const t0 = Math.min(...spans.map((s) => s.start_time_unix_nano));
    const t1 = Math.max(...spans.map((s) => s.end_time_unix_nano));
    const total = Math.max(1, t1 - t0);

    return {
      t0,
      totalMs: total / 1e6,
      rows: spans.map((s) => ({
        span: s,
        depth: depthOf(s),
        // Percentages, so the bar scales with the container instead of needing
        // a fixed pixel width that would overflow a phone.
        leftPct: ((s.start_time_unix_nano - t0) / total) * 100,
        widthPct: Math.max(
          0.5, // sub-millisecond spans still need to be visible
          ((s.end_time_unix_nano - s.start_time_unix_nano) / total) * 100,
        ),
      })),
    };
  }, [data]);

  if (isLoading) {
    return <p className="py-10 text-center text-sm text-neutral-500">Loading trace…</p>;
  }
  if (isError || !data || !layout) {
    return (
      <div className="py-10 text-center">
        <p className="text-sm text-red-400">Trace not found.</p>
        <Link href="/traces" className="mt-3 inline-block text-xs text-neutral-400 underline">
          Back to traces
        </Link>
      </div>
    );
  }

  const llmMs = llmWallClockMs(data.spans);
  const llmShare = layout.totalMs > 0 ? (llmMs / layout.totalMs) * 100 : 0;

  return (
    <div className="space-y-5">
      <div>
        <Link href="/traces" className="text-xs text-neutral-500 hover:text-neutral-300">
          ← Traces
        </Link>
        <h1 className="mt-2 text-base font-semibold">
          {data.spans.find((s) => !s.parent_span_id)?.name ?? "Trace"}
        </h1>
        <p className="mt-1 break-all font-mono text-[11px] text-neutral-500">
          {data.trace_id}
        </p>
      </div>

      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat label="Duration" value={formatDuration(layout.totalMs)} />
        <Stat label="Spans" value={String(data.span_count)} />
        <Stat label="Cost" value={formatCost(data.cost_usd)} />
        <Stat
          label="LLM time"
          value={`${llmShare.toFixed(0)}%`}
          hint={formatDuration(llmMs)}
        />
      </dl>

      <div>
        <h2 className="mb-2 text-xs font-medium uppercase tracking-wide text-neutral-500">
          Waterfall
        </h2>

        {/* Label above, full-width bar below. A side-by-side label/bar layout
            needs ~500px before the bar is readable, which forces horizontal
            scrolling on a phone. Stacking keeps the time axis full-width at
            every size. */}
        <ul className="space-y-1">
          {layout.rows.map(({ span, depth, leftPct, widthPct }) => {
            const open = openSpan === span.span_id;
            return (
              <li key={span.span_id} className="rounded-lg border border-neutral-850">
                <button
                  onClick={() => setOpenSpan(open ? null : span.span_id)}
                  className="w-full rounded-lg px-2.5 py-2 text-left hover:bg-neutral-900"
                >
                  <div
                    className="flex items-center justify-between gap-2"
                    style={{ paddingLeft: `${depth * 12}px` }}
                  >
                    <span className="truncate text-[13px]">
                      {span.status_code === "ERROR" && (
                        <span className="mr-1 text-red-400">!</span>
                      )}
                      {span.name}
                    </span>
                    <span className="shrink-0 font-mono text-[11px] tabular-nums text-neutral-500">
                      {formatDuration(span.duration_ms)}
                    </span>
                  </div>

                  <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-neutral-850">
                    <div
                      className={`h-full rounded-full ${barColor(span)}`}
                      style={{
                        marginLeft: `${leftPct}%`,
                        width: `${widthPct}%`,
                      }}
                    />
                  </div>
                </button>

                {open && (
                  <SpanDetail
                    span={span}
                    scores={data.scores.filter((s) => s.span_id === span.span_id)}
                  />
                )}
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900 px-3 py-2">
      <dt className="kicker">{label}</dt>
      <dd className="mt-0.5 text-sm tabular-nums">
        {value}
        {hint && <span className="ml-1 text-[11px] text-neutral-500">{hint}</span>}
      </dd>
    </div>
  );
}

function SpanDetail({ span, scores }: { span: Span; scores: Score[] }) {
  const rows: [string, string][] = [];

  if (span.gen_ai_response_model) rows.push(["Model", span.gen_ai_response_model]);
  if (span.gen_ai_usage_input_tokens != null)
    rows.push([
      "Tokens",
      `${span.gen_ai_usage_input_tokens} in / ${span.gen_ai_usage_output_tokens} out`,
    ]);
  if (span.obs_cost_usd != null) rows.push(["Cost", formatCost(span.obs_cost_usd)]);
  if (span.gen_ai_finish_reasons)
    rows.push(["Finish", span.gen_ai_finish_reasons.replace(/[[\]"]/g, "")]);
  if (span.gen_ai_tool_name) rows.push(["Tool", span.gen_ai_tool_name]);
  rows.push(["Started", formatTime(span.start_time_unix_nano)]);
  rows.push(["Span ID", span.span_id]);

  return (
    <div className="space-y-3 border-t border-neutral-850 px-2.5 py-3">
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 text-[11px]">
        {rows.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-neutral-500">{k}</dt>
            <dd className="break-all font-mono text-neutral-300">{v}</dd>
          </div>
        ))}
      </dl>

      {span.status_message && (
        <Block label="Error" body={span.status_message} tone="error" />
      )}
      {span.gen_ai_input_messages && (
        <Block label="Input" body={span.gen_ai_input_messages} />
      )}
      {span.gen_ai_output_messages && (
        <Block label="Output" body={span.gen_ai_output_messages} />
      )}

      {scores.length > 0 && (
        <div className="space-y-2">
          <p className="text-[10px] uppercase tracking-wide text-neutral-500">Scores</p>
          {scores.map((score) => (
            <div key={score.id} className="rounded-lg bg-neutral-950 px-2.5 py-2">
              <div className="flex items-center gap-1.5">
                <span className={`rounded px-1.5 py-0.5 text-[10px] ${scoreTone(score)}`}>
                  {formatScore(score)}
                </span>
                <span className="text-[11px] text-neutral-400">{score.scorer_name}</span>
                {score.judge_trace_id && (
                  <Link
                    href={`/traces/${score.judge_trace_id}`}
                    className="ml-auto text-[10px] text-sky-400 hover:underline"
                  >
                    judge trace
                  </Link>
                )}
              </div>
              {(score.reasoning || score.error) && (
                <p className="mt-1 whitespace-pre-wrap break-words text-[11px] text-neutral-500">
                  {score.reasoning || score.error}
                </p>
              )}
            </div>
          ))}
        </div>
      )}

      <div className="flex flex-wrap gap-2">
        {span.gen_ai_input_messages && <SaveAsTestCase span={span} />}
        {span.gen_ai_output_messages && <ScoreSpan span={span} />}
      </div>
    </div>
  );
}

/**
 * Run a judge against this span's output — the production-traffic half of
 * step 4.
 *
 * Offered only where there is an output to judge. This is the path that makes
 * scoring an observability feature rather than an eval one: a span that looked
 * wrong in the waterfall can be handed to a scorer directly, without first
 * being turned into a dataset and replayed.
 */
function ScoreSpan({ span }: { span: Span }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [credentialId, setCredentialId] = useState("");

  const { data } = useQuery({
    queryKey: ["scorers"],
    queryFn: api.scorers,
    enabled: open,
  });

  const score = useMutation({
    mutationFn: () =>
      api.scoreSpan(span.trace_id, span.span_id, selected, credentialId || null),
    onSuccess: () => {
      setOpen(false);
      setSelected([]);
      queryClient.invalidateQueries({ queryKey: ["trace", span.trace_id] });
    },
  });

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="rounded-lg border border-neutral-700 px-2.5 py-1.5 text-[11px] text-neutral-300 hover:border-neutral-600"
      >
        Score this span
      </button>
    );
  }

  const scorers = data?.scorers ?? [];

  return (
    <div className="w-full space-y-2 rounded-lg border border-neutral-700 p-2.5">
      <div className="flex items-center justify-between">
        <p className="text-[10px] uppercase tracking-wide text-neutral-500">
          Score this span
        </p>
        <button
          onClick={() => setOpen(false)}
          className="text-[11px] text-neutral-500 hover:text-neutral-300"
        >
          Cancel
        </button>
      </div>

      {scorers.length === 0 ? (
        <p className="text-[11px] text-neutral-500">
          No scorers defined yet.{" "}
          <Link href="/scorers" className="text-sky-400 hover:underline">
            Create one
          </Link>
          .
        </p>
      ) : (
        <>
          <div className="flex flex-wrap gap-x-4 gap-y-1.5">
            {scorers.map((s) => (
              <label key={s.id} className="flex items-center gap-1.5">
                <input
                  type="checkbox"
                  checked={selected.includes(s.id)}
                  onChange={() =>
                    setSelected((ids) =>
                      ids.includes(s.id) ? ids.filter((x) => x !== s.id) : [...ids, s.id],
                    )
                  }
                />
                <span className="text-[11px] text-neutral-300">{s.name}</span>
              </label>
            ))}
          </div>

          {score.isError && (
            <p className="text-[11px] text-red-400">
              {score.error instanceof ApiError
                ? score.error.message
                : "Could not start scoring."}
            </p>
          )}

          <div className="flex flex-wrap items-center gap-3">
            <CredentialPicker
              value={credentialId}
              onChange={setCredentialId}
              compact
            />
            <button
              onClick={() => score.mutate()}
              disabled={selected.length === 0 || score.isPending}
              className="rounded-lg btn-primary px-2.5 py-1.5 text-[11px] font-medium disabled:opacity-40"
            >
              {score.isPending ? "Starting…" : "Score"}
            </button>
            <span className="text-[11px] text-neutral-500">
              {selected.length} judge {selected.length === 1 ? "call" : "calls"}
            </span>
          </div>
        </>
      )}
    </div>
  );
}

/**
 * Turn this span into a dataset test case — the step 3 capture path.
 *
 * Only offered on spans that carry an input, since that is the thing being
 * captured. The span's own output is prefilled as the expected output: what
 * production actually returned is the most useful baseline to diff a replay
 * against, and it is far more likely to be right than blank.
 *
 * The input is editable before saving. A captured prompt usually needs the
 * fixed scaffolding stripped out so the template can supply it instead —
 * saving it verbatim gives you a dataset that only replays one prompt.
 */
function SaveAsTestCase({ span }: { span: Span }) {
  const [open, setOpen] = useState(false);
  const [datasetId, setDatasetId] = useState("");
  const [newName, setNewName] = useState("");
  const [input, setInput] = useState(span.gen_ai_input_messages ?? "");
  const [expected, setExpected] = useState(span.gen_ai_output_messages ?? "");
  const [saved, setSaved] = useState<string | null>(null);

  const { data } = useQuery({
    queryKey: ["datasets"],
    queryFn: api.datasets,
    enabled: open,
  });

  const save = useMutation({
    mutationFn: async () => {
      // Creating the dataset inline keeps the capture to one interaction. The
      // alternative — go make a dataset, come back, find the span again — is
      // enough friction that traces stop getting captured at all.
      const target =
        datasetId ||
        (await api.createDataset(newName.trim(), "Captured from traces")).id;
      await api.addItem(target, {
        input,
        expected_output: expected || null,
        source_trace_id: span.trace_id,
        source_span_id: span.span_id,
      });
      return target;
    },
    onSuccess: (target) => setSaved(target),
  });

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="rounded-lg border border-neutral-700 px-2.5 py-1.5 text-[11px] text-neutral-300 hover:border-neutral-600"
      >
        Save as test case
      </button>
    );
  }

  if (saved) {
    return (
      <div className="flex items-center gap-3 rounded-lg bg-emerald-950/50 px-2.5 py-2 text-[11px] text-emerald-300">
        <span>Saved.</span>
        <Link href={`/datasets/${saved}`} className="underline">
          Open dataset
        </Link>
      </div>
    );
  }

  const datasets = data?.datasets ?? [];
  const canSave = input.trim() && (datasetId || newName.trim());

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
      className="space-y-2 rounded-lg border border-neutral-700 p-2.5"
    >
      <div className="flex items-center justify-between">
        <p className="text-[10px] uppercase tracking-wide text-neutral-500">
          Save as test case
        </p>
        <button
          type="button"
          onClick={() => setOpen(false)}
          className="text-[11px] text-neutral-500 hover:text-neutral-300"
        >
          Cancel
        </button>
      </div>

      <select
        value={datasetId}
        onChange={(e) => setDatasetId(e.target.value)}
        className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-2 py-1.5 text-[11px] outline-none focus:border-neutral-500"
      >
        <option value="">＋ New dataset…</option>
        {datasets.map((d) => (
          <option key={d.id} value={d.id}>
            {d.name} ({d.item_count})
          </option>
        ))}
      </select>

      {!datasetId && (
        <input
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          placeholder="New dataset name"
          className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-2 py-1.5 text-[11px] outline-none focus:border-neutral-500"
        />
      )}

      <textarea
        value={input}
        onChange={(e) => setInput(e.target.value)}
        rows={3}
        className="w-full resize-y rounded-lg border border-neutral-700 bg-neutral-950 px-2 py-1.5 font-mono text-[11px] outline-none focus:border-neutral-500"
      />
      <textarea
        value={expected}
        onChange={(e) => setExpected(e.target.value)}
        rows={2}
        placeholder="Expected output (optional)"
        className="w-full resize-y rounded-lg border border-neutral-700 bg-neutral-950 px-2 py-1.5 font-mono text-[11px] outline-none focus:border-neutral-500"
      />

      {save.isError && (
        <p className="text-[11px] text-red-400">
          {save.error instanceof ApiError ? save.error.message : "Could not save."}
        </p>
      )}

      <button
        type="submit"
        disabled={!canSave || save.isPending}
        className="rounded-lg btn-primary px-2.5 py-1.5 text-[11px] font-medium disabled:opacity-40"
      >
        {save.isPending ? "Saving…" : "Save"}
      </button>
    </form>
  );
}

function Block({
  label,
  body,
  tone,
}: {
  label: string;
  body: string;
  tone?: "error";
}) {
  return (
    <div>
      <p className="mb-1 text-[10px] uppercase tracking-wide text-neutral-500">{label}</p>
      {/* Prompts and completions are unbounded; capping the height keeps one
          long span from burying the rest of the waterfall. */}
      <pre
        className={`max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-lg px-2.5 py-2 font-mono text-[11px] ${
          tone === "error"
            ? "bg-red-950/50 text-red-300"
            : "bg-neutral-950 text-neutral-400"
        }`}
      >
        {body}
      </pre>
    </div>
  );
}
