"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { Shell } from "../../shell";
import { CredentialPicker } from "@/components/credential-picker";
import { useRole } from "@/lib/use-role";
import {
  api,
  ApiError,
  formatCost,
  formatDuration,
  formatScore,
  scoreTone,
  type RunItem,
  type Score,
  type ScoreSummary,
} from "@/lib/api";

export default function RunDetailPage() {
  return (
    <Shell>
      <RunDetail />
    </Shell>
  );
}

function RunDetail() {
  const params = useParams<{ id: string }>();
  const queryClient = useQueryClient();

  const { data, isLoading, isError } = useQuery({
    queryKey: ["run", params.id],
    queryFn: () => api.run(params.id),
    // Poll while in flight. Results stream in as items finish rather than
    // appearing all at once at the end, which is the difference between
    // watching a run and waiting for one.
    //
    // Scoring is polled the same way and outlives the replay: a run with
    // scorers attached finishes 'succeeded' and *then* spends another minute
    // judging, so stopping at the run's own status would freeze the page
    // halfway through the thing it was asked to show.
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data) return false;
      if (data.status === "running" || data.status === "pending") return 1500;
      return data.score_summary.some((s) => s.pending > 0) ? 2000 : false;
    },
  });

  const cancel = useMutation({
    mutationFn: () => api.cancelRun(params.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["run", params.id] }),
  });

  if (isLoading) {
    return <p className="py-10 text-center text-sm text-neutral-500">Loading run…</p>;
  }
  if (isError || !data) {
    return (
      <div className="py-10 text-center">
        <p className="text-sm text-red-400">Run not found.</p>
        <Link href="/datasets" className="mt-3 inline-block text-xs text-neutral-400 underline">
          Back to datasets
        </Link>
      </div>
    );
  }

  const inFlight = data.status === "running" || data.status === "pending";
  const progress =
    data.item_count > 0 ? (data.completed_count / data.item_count) * 100 : 0;

  return (
    <div className="space-y-5">
      <div>
        <Link
          href={`/datasets/${data.dataset_id}`}
          className="text-xs text-neutral-500 hover:text-neutral-300"
        >
          ← Dataset
        </Link>
        <div className="mt-2 flex items-center gap-2">
          <h1 className="text-base font-semibold">{data.name || "Replay run"}</h1>
          <span className="rounded bg-neutral-800 px-1.5 py-0.5 text-[10px] text-neutral-300">
            {data.status}
          </span>
        </div>
        <p className="mt-1 font-mono text-[11px] text-neutral-500">{data.model}</p>
      </div>

      {data.error && (
        <p className="rounded-lg bg-amber-950/50 px-3 py-2 text-[11px] text-amber-300">
          {data.error}
        </p>
      )}

      {inFlight && (
        <div className="space-y-2">
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-neutral-850">
            <div
              className="h-full rounded-full bg-sky-500 transition-all"
              style={{ width: `${progress}%` }}
            />
          </div>
          <div className="flex items-center justify-between text-[11px] text-neutral-500">
            <span>
              {data.completed_count} of {data.item_count} done
            </span>
            <button
              onClick={() => cancel.mutate()}
              disabled={cancel.isPending}
              className="text-amber-400 hover:text-amber-300 disabled:opacity-50"
            >
              Cancel run
            </button>
          </div>
        </div>
      )}

      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat label="Cases" value={String(data.item_count)} />
        <Stat label="Failed" value={String(data.error_count)} />
        <Stat label="Cost" value={formatCost(data.cost_usd)} />
        <Stat
          label="Avg latency"
          value={avgLatency(data.items)}
        />
      </dl>

      {data.trace_id && (
        <Link
          href={`/traces/${data.trace_id}`}
          className="inline-block text-xs text-sky-400 hover:underline"
        >
          View this run as a trace →
        </Link>
      )}

      <Scores
        runId={params.id}
        summary={data.score_summary}
        scorable={data.items.filter((i) => i.status === "succeeded" && i.output).length}
        onScored={() => queryClient.invalidateQueries({ queryKey: ["run", params.id] })}
      />

      <div>
        <h2 className="mb-2 text-xs font-medium uppercase tracking-wide text-neutral-500">
          Results
        </h2>
        <ul className="space-y-2">
          {data.items.map((item) => (
            <ResultRow key={item.id} item={item} />
          ))}
        </ul>
      </div>

      <details className="rounded-xl border border-neutral-800 bg-neutral-900">
        <summary className="cursor-pointer px-3.5 py-2.5 text-xs text-neutral-400">
          Prompt used
          {data.prompt_name && (
            <span className="ml-1.5 font-mono text-[11px] text-neutral-500">
              {data.prompt_name} v{data.prompt_version}
            </span>
          )}
        </summary>
        <div className="space-y-2 border-t border-neutral-800 px-3.5 py-3">
          {data.prompt_id ? (
            <p className="text-[11px] text-neutral-500">
              From{" "}
              <Link
                href={`/prompts/${data.prompt_id}`}
                className="text-sky-400 hover:underline"
              >
                {data.prompt_name}
              </Link>{" "}
              v{data.prompt_version}
              {data.prompt_label && (
                <>
                  , resolved through{" "}
                  <span className="text-sky-400">{data.prompt_label}</span> when
                  this run started — that label may point somewhere else now
                </>
              )}
              .
            </p>
          ) : (
            <p className="text-[11px] text-neutral-500">
              Typed into the run form, not a saved prompt — so it has no history
              to compare against.
            </p>
          )}
          {/* Read off the run row, not the prompt's current version. Editing a
              prompt after a run must not rewrite what that run actually sent,
              and this text is the copy that guarantees it. */}
          <pre className="overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] text-neutral-400">
            {data.prompt_template}
          </pre>
        </div>
      </details>
    </div>
  );
}

function avgLatency(items: RunItem[]): string {
  const done = items.filter((i) => i.latency_ms != null);
  if (done.length === 0) return "—";
  return formatDuration(
    done.reduce((sum, i) => sum + (i.latency_ms ?? 0), 0) / done.length,
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900 px-3 py-2">
      <dt className="text-[10px] uppercase tracking-wide text-neutral-500">{label}</dt>
      <dd className="mt-0.5 text-sm tabular-nums">{value}</dd>
    </div>
  );
}

// --------------------------------------------------------------------------
// Scores (step 4)
// --------------------------------------------------------------------------

/**
 * Per-scorer rollup, plus the control that starts a scoring pass.
 *
 * Scoring an already-finished run is a first-class path, not a fallback: the
 * scorer you want usually doesn't exist yet when the run happens, and having to
 * re-run the whole replay to apply a new judge would mean paying twice to
 * evaluate the same outputs.
 */
function Scores({
  runId,
  summary,
  scorable,
  onScored,
}: {
  runId: string;
  summary: ScoreSummary[];
  scorable: number;
  onScored: () => void;
}) {
  const [picking, setPicking] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [credentialId, setCredentialId] = useState("");
  const { isAdmin } = useRole();

  const { data } = useQuery({
    queryKey: ["scorers"],
    queryFn: api.scorers,
    enabled: picking,
  });

  const score = useMutation({
    mutationFn: () => api.scoreRun(runId, selected, credentialId || null),
    onSuccess: () => {
      setPicking(false);
      setSelected([]);
      onScored();
    },
  });

  const scorers = data?.scorers ?? [];
  const toggle = (id: string) =>
    setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <h2 className="text-xs font-medium uppercase tracking-wide text-neutral-500">
          Scores
        </h2>
        {scorable > 0 && (
          <button
            onClick={() => setPicking((v) => !v)}
            className="text-xs text-neutral-400 hover:text-neutral-200"
          >
            {picking ? "Cancel" : summary.length ? "+ Score again" : "+ Score this run"}
          </button>
        )}
      </div>

      {picking && (
        <div className="space-y-2 rounded-xl border border-neutral-800 bg-neutral-900 p-3">
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
              <ul className="space-y-1.5">
                {scorers.map((s) => (
                  <li key={s.id}>
                    <label className="flex items-start gap-2">
                      <input
                        type="checkbox"
                        checked={selected.includes(s.id)}
                        onChange={() => toggle(s.id)}
                        className="mt-0.5"
                      />
                      <span className="min-w-0">
                        <span className="text-[12px] text-neutral-200">{s.name}</span>
                        <span className="ml-1.5 text-[10px] text-neutral-500">
                          {s.output_type} · {s.model}
                        </span>
                      </span>
                    </label>
                  </li>
                ))}
              </ul>

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
                  disabled={!isAdmin || selected.length === 0 || score.isPending}
                  title={isAdmin ? undefined : "Read-only access — scoring spends money"}
                  className="rounded-lg btn-primary px-3 py-1.5 text-xs font-medium disabled:opacity-40"
                >
                  {score.isPending ? "Starting…" : "Score"}
                </button>
                {/* The bill, stated before the click. Only successful items get
                    judged, so this is the real call count, not the item count. */}
                <span className="text-[11px] text-neutral-500">
                  {scorable * selected.length} judge{" "}
                  {scorable * selected.length === 1 ? "call" : "calls"}
                </span>
              </div>
            </>
          )}
        </div>
      )}

      {summary.length === 0 ? (
        !picking && (
          <div className="rounded-xl border border-dashed border-neutral-800 px-4 py-5 text-center text-xs text-neutral-500">
            Not scored yet.
          </div>
        )
      ) : (
        <ul className="grid gap-2 sm:grid-cols-2">
          {summary.map((s) => (
            <li
              key={s.scorer_id}
              className="rounded-xl border border-neutral-800 bg-neutral-900 px-3 py-2.5"
            >
              <div className="flex items-baseline justify-between gap-2">
                <span className="truncate text-[12px] text-neutral-300">
                  {s.scorer_name}
                </span>
                <span className="shrink-0 text-sm tabular-nums">
                  {s.output_type === "categorical"
                    ? "—"
                    : s.mean != null
                      ? s.output_type === "boolean"
                        ? `${Math.round((s.pass_rate ?? 0) * 100)}%`
                        : `${s.mean.toFixed(2)}/${s.score_max}`
                      : "—"}
                </span>
              </div>

              {s.distribution && (
                <div className="mt-1 flex flex-wrap gap-x-2 text-[11px] text-neutral-400">
                  {Object.entries(s.distribution).map(([label, n]) => (
                    <span key={label}>
                      {label} {n}
                    </span>
                  ))}
                </div>
              )}

              {/* Two scorer versions behind one mean is not a detail. It says
                  the judge changed mid-experiment, so the number averages
                  verdicts from two different judges. */}
              {s.versions.length > 1 && (
                <p className="mt-1 text-[10px] text-amber-400">
                  Mixes scorer v{s.versions.join(" and v")} — the judge was
                  edited between passes.
                </p>
              )}

              <div className="mt-1 flex flex-wrap gap-x-2.5 text-[10px] text-neutral-500">
                <span>
                  {s.scored}/{s.total} scored
                </span>
                {s.versions.length === 1 && (
                  <span>
                    {s.prompt_id ? (
                      <Link
                        href={`/prompts/${s.prompt_id}`}
                        className="hover:text-neutral-300"
                      >
                        scorer v{s.versions[0]}
                      </Link>
                    ) : (
                      `scorer v${s.versions[0]}`
                    )}
                  </span>
                )}
                {s.output_type === "numeric" && s.pass_rate != null && (
                  <span>{Math.round(s.pass_rate * 100)}% pass</span>
                )}
                {s.pending > 0 && <span className="text-sky-400">{s.pending} running</span>}
                {s.failed > 0 && <span className="text-red-400">{s.failed} failed</span>}
                {s.cost_usd > 0 && <span>{formatCost(s.cost_usd)}</span>}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ScoreChips({ scores }: { scores: Score[] }) {
  if (scores.length === 0) return null;
  return (
    <div className="mt-1.5 flex flex-wrap gap-1">
      {scores.map((s) => (
        <span
          key={s.id}
          title={s.reasoning || s.error}
          className={`rounded px-1.5 py-0.5 text-[10px] ${scoreTone(s)}`}
        >
          {s.scorer_name} {formatScore(s)}
        </span>
      ))}
    </div>
  );
}

function ResultRow({ item }: { item: RunItem }) {
  const [open, setOpen] = useState(false);

  const tone =
    item.status === "failed"
      ? "border-red-900"
      : item.status === "succeeded"
        ? "border-neutral-800"
        : "border-neutral-850";

  return (
    <li className={`rounded-xl border bg-neutral-900 ${tone}`}>
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full px-3 py-2.5 text-left"
      >
        <div className="flex items-start justify-between gap-2">
          <span className="shrink-0 font-mono text-[11px] text-neutral-600">
            {item.ordinal + 1}
          </span>
          <div className="min-w-0 flex-1">
            <p className="truncate text-[12px] text-neutral-400">{item.input}</p>
            <p
              className={`mt-1 truncate text-[12px] ${
                item.status === "failed" ? "text-red-400" : "text-neutral-200"
              }`}
            >
              {item.status === "failed"
                ? item.error
                : item.output || (item.status === "succeeded" ? "(empty)" : "…")}
            </p>
            <ScoreChips scores={item.scores} />
          </div>
          {item.latency_ms != null && (
            <span className="shrink-0 font-mono text-[11px] tabular-nums text-neutral-600">
              {formatDuration(item.latency_ms)}
            </span>
          )}
        </div>
      </button>

      {open && (
        <div className="space-y-3 border-t border-neutral-850 px-3 py-3">
          <Block label="Input" body={item.input} />
          {item.expected_output && (
            <Block label="Expected" body={item.expected_output} />
          )}
          {item.output && <Block label="Output" body={item.output} />}
          {item.error && <Block label="Error" body={item.error} tone="error" />}

          {/* The judge's reasoning, not just its verdict. A score without the
              argument behind it is an oracle, and an unauditable oracle is not
              much use for deciding whether the judge itself is any good. */}
          {item.scores.map((score) => (
            <div key={score.id}>
              <p className="mb-1 flex items-center gap-1.5 text-[10px] uppercase tracking-wide text-neutral-500">
                {score.scorer_name}
                <span className={`rounded px-1 py-0.5 normal-case ${scoreTone(score)}`}>
                  {formatScore(score)}
                </span>
              </p>
              <p className="whitespace-pre-wrap break-words rounded-lg bg-neutral-950 px-2.5 py-2 text-[11px] text-neutral-400">
                {score.reasoning || score.error || "…"}
              </p>
              {score.judge_trace_id && (
                <Link
                  href={`/traces/${score.judge_trace_id}`}
                  className="mt-1 inline-block text-[11px] text-sky-400 hover:underline"
                >
                  judge trace
                </Link>
              )}
            </div>
          ))}

          <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-neutral-500">
            {item.input_tokens != null && (
              <span>
                {item.input_tokens} in / {item.output_tokens} out
              </span>
            )}
            {item.cost_usd != null && <span>{formatCost(item.cost_usd)}</span>}
            {item.finish_reason && <span>{item.finish_reason}</span>}
            {item.source_trace_id && (
              <Link
                href={`/traces/${item.source_trace_id}`}
                className="text-sky-400 hover:underline"
              >
                source trace
              </Link>
            )}
          </div>
        </div>
      )}
    </li>
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
      <pre
        className={`max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg px-2.5 py-2 font-mono text-[11px] ${
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
