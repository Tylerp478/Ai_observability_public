"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { Shell } from "../shell";
import {
  api,
  ApiError,
  formatCost,
  formatDuration,
  type Guardrail,
  type GuardrailAction,
  type GuardrailCheck,
  type GuardrailDecision,
  type GuardrailDraft,
  type GuardrailOnError,
  type GuardrailResult,
  type GuardrailStats,
  type Scorer,
} from "@/lib/api";

export default function GuardrailsPage() {
  return (
    <Shell>
      <Guardrails />
    </Shell>
  );
}

const FIELD =
  "w-full rounded-lg border border-neutral-700 bg-neutral-950 px-2.5 py-1.5 text-xs outline-none focus:border-neutral-500";

function Guardrails() {
  const queryClient = useQueryClient();
  const [creating, setCreating] = useState(false);

  const { data, isLoading } = useQuery({
    queryKey: ["guardrails"],
    queryFn: api.guardrails,
  });
  const { data: scorerData } = useQuery({ queryKey: ["scorers"], queryFn: api.scorers });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["guardrails"] });
    queryClient.invalidateQueries({ queryKey: ["guardrail-checks"] });
  };

  const guardrails = data?.guardrails ?? [];
  const scorers = scorerData?.scorers ?? [];

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-base font-semibold">Guardrails</h1>
        <p className="mt-1 text-xs text-neutral-500">
          A guardrail is a scorer plus a policy. The endpoint screens an output
          and answers pass/block before it reaches a user — a guardrail triggers
          when its scorer fails.
        </p>
      </div>

      {data?.stats && <Stats stats={data.stats} />}

      {scorers.length === 0 ? (
        <div className="rounded-xl border border-dashed border-neutral-800 px-4 py-8 text-center text-xs text-neutral-500">
          Guardrails are built from scorers, and there aren&apos;t any yet.{" "}
          <Link href="/scorers" className="text-sky-400 hover:text-sky-300">
            Create a scorer
          </Link>{" "}
          first.
        </div>
      ) : (
        !creating && (
          <button
            onClick={() => setCreating(true)}
            className="rounded-lg btn-primary px-3 py-1.5 text-xs font-medium"
          >
            ＋ New guardrail
          </button>
        )
      )}

      {creating && (
        <GuardrailEditor
          scorers={scorers}
          onCancel={() => setCreating(false)}
          onSaved={() => {
            setCreating(false);
            invalidate();
          }}
        />
      )}

      {isLoading ? (
        <p className="py-10 text-center text-sm text-neutral-500">Loading guardrails…</p>
      ) : guardrails.length === 0 && !creating ? (
        scorers.length > 0 && (
          <div className="rounded-xl border border-dashed border-neutral-800 px-4 py-8 text-center text-xs text-neutral-500">
            No guardrails yet. A new one is safest as{" "}
            <span className="text-neutral-300">flag</span> — it runs and records
            without blocking, so you can watch it against real traffic first.
          </div>
        )
      ) : (
        <ul className="space-y-2">
          {guardrails.map((guardrail) => (
            <GuardrailRow
              key={guardrail.id}
              guardrail={guardrail}
              scorers={scorers}
              onChanged={invalidate}
            />
          ))}
        </ul>
      )}

      {guardrails.length > 0 && <TryPanel onChecked={invalidate} />}
      <CheckLog />
    </div>
  );
}

// --------------------------------------------------------------------------
// Headline numbers
// --------------------------------------------------------------------------

function Stats({ stats }: { stats: GuardrailStats }) {
  const cells: { label: string; value: string; tone?: string }[] = [
    { label: "Checks", value: String(stats.checks) },
    {
      label: "Blocked",
      value:
        stats.block_rate == null
          ? "—"
          : `${stats.blocked} · ${(stats.block_rate * 100).toFixed(0)}%`,
      tone: stats.blocked > 0 ? "text-red-300" : undefined,
    },
    {
      label: "Avg latency",
      value: stats.avg_latency_ms == null ? "—" : formatDuration(stats.avg_latency_ms),
    },
    { label: "Spend", value: formatCost(stats.cost_usd) },
  ];

  return (
    <div>
      {/* Four across even on a phone: these are short numbers and stacking
          them would push the guardrail list below the fold on mobile. */}
      <dl className="grid grid-cols-4 gap-2">
        {cells.map((cell) => (
          <div
            key={cell.label}
            className="rounded-xl border border-neutral-800 bg-neutral-900 px-2.5 py-2"
          >
            <dt className="kicker">{cell.label}</dt>
            <dd className={`mt-0.5 text-sm tabular-nums ${cell.tone ?? ""}`}>
              {cell.value}
            </dd>
          </div>
        ))}
      </dl>
      <p className="mt-1.5 text-[11px] text-neutral-600">
        Last {stats.window_hours}h.
        {stats.degraded > 0 && (
          <span className="text-amber-400">
            {" "}
            {stats.degraded} check{stats.degraded === 1 ? "" : "s"} ran with a judge
            that never answered.
          </span>
        )}
      </p>
    </div>
  );
}

// --------------------------------------------------------------------------
// One guardrail
// --------------------------------------------------------------------------

/** What this guardrail's scorer has to say for the rule to fire. */
function triggerLabel(g: Guardrail): string {
  switch (g.output_type) {
    case "numeric":
      return `scores below ${g.pass_threshold}`;
    case "categorical":
      return `answers ${g.block_labels.join(" or ")}`;
    default:
      return "fails";
  }
}

function GuardrailRow({
  guardrail,
  scorers,
  onChanged,
}: {
  guardrail: Guardrail;
  scorers: Scorer[];
  onChanged: () => void;
}) {
  const [editing, setEditing] = useState(false);

  const archive = useMutation({
    mutationFn: () => api.archiveGuardrail(guardrail.id),
    onSuccess: onChanged,
  });

  // Enable/disable is the one control worth a single click rather than a trip
  // through the editor — it is what you reach for when a guardrail is blocking
  // things it shouldn't and the fix can wait until morning.
  const toggle = useMutation({
    mutationFn: () =>
      api.updateGuardrail(guardrail.id, {
        ...toDraft(guardrail),
        enabled: !guardrail.enabled,
      }),
    onSuccess: onChanged,
  });

  return (
    <li
      className={`rounded-xl border bg-neutral-900 ${
        guardrail.enabled ? "border-neutral-800" : "border-neutral-850 opacity-60"
      }`}
    >
      <div className="px-3.5 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[13px] font-medium">{guardrail.name}</span>
          <span
            className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
              guardrail.action === "block"
                ? "bg-red-950 text-red-300"
                : "bg-amber-950 text-amber-300"
            }`}
          >
            {guardrail.action === "block" ? "blocks" : "flags only"}
          </span>
          {!guardrail.enabled && (
            <span className="rounded bg-neutral-800 px-1.5 py-0.5 text-[10px] text-neutral-400">
              disabled
            </span>
          )}
        </div>

        {guardrail.description && (
          <p className="mt-1 text-[11px] text-neutral-500">{guardrail.description}</p>
        )}

        <p className="mt-1 text-[11px] text-neutral-400">
          Fires when{" "}
          <Link href="/scorers" className="text-neutral-300 hover:text-neutral-100">
            {guardrail.scorer_name}
          </Link>{" "}
          {triggerLabel(guardrail)}.
        </p>

        <p className="mt-1 font-mono text-[10px] text-neutral-600">
          {guardrail.model} · {guardrail.trigger_count ?? 0} fired /{" "}
          {guardrail.check_count ?? 0} checks · judge error ={" "}
          {guardrail.on_error}
        </p>
      </div>

      <div className="flex flex-wrap gap-2 border-t border-neutral-850 px-3.5 py-2">
        <button
          onClick={() => toggle.mutate()}
          disabled={toggle.isPending}
          className="text-[11px] text-neutral-400 hover:text-neutral-200"
        >
          {guardrail.enabled ? "Disable" : "Enable"}
        </button>
        <button
          onClick={() => setEditing(!editing)}
          className="text-[11px] text-neutral-400 hover:text-neutral-200"
        >
          {editing ? "Close" : "Edit"}
        </button>
        <button
          onClick={() => archive.mutate()}
          disabled={archive.isPending}
          className="ml-auto text-[11px] text-neutral-600 hover:text-red-400"
        >
          Archive
        </button>
      </div>

      {editing && (
        <div className="border-t border-neutral-850 p-3">
          <GuardrailEditor
            scorers={scorers}
            guardrail={guardrail}
            onCancel={() => setEditing(false)}
            onSaved={() => {
              setEditing(false);
              onChanged();
            }}
          />
        </div>
      )}
    </li>
  );
}

// --------------------------------------------------------------------------
// Editor
// --------------------------------------------------------------------------

function toDraft(g: Guardrail): GuardrailDraft {
  return {
    name: g.name,
    description: g.description,
    scorer_id: g.scorer_id,
    action: g.action,
    block_labels: g.block_labels,
    on_error: g.on_error,
    enabled: g.enabled,
  };
}

function GuardrailEditor({
  scorers,
  guardrail,
  onCancel,
  onSaved,
}: {
  scorers: Scorer[];
  guardrail?: Guardrail;
  onCancel: () => void;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState<GuardrailDraft>(
    guardrail
      ? toDraft(guardrail)
      : {
          name: "",
          description: "",
          scorer_id: scorers[0]?.id ?? "",
          // New guardrails default to shadow mode. A guardrail is one bad
          // judge prompt away from rejecting every response, and the cost of
          // finding that out in production is much higher than the cost of
          // one extra click to promote it.
          action: "flag",
          block_labels: [],
          on_error: "allow",
          enabled: true,
        },
  );

  const set = <K extends keyof GuardrailDraft>(key: K, value: GuardrailDraft[K]) =>
    setDraft((d) => ({ ...d, [key]: value }));

  const scorer = scorers.find((s) => s.id === draft.scorer_id);

  const save = useMutation<void>({
    mutationFn: async () => {
      if (guardrail) await api.updateGuardrail(guardrail.id, draft);
      else await api.createGuardrail(draft);
    },
    onSuccess: onSaved,
  });

  // The two ways a scorer can't decide anything. Caught here as well as on the
  // backend so the reason shows next to the field that causes it.
  const needsThreshold =
    scorer?.output_type === "numeric" && scorer.pass_threshold == null;
  const needsLabels =
    scorer?.output_type === "categorical" && draft.block_labels.length === 0;

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
      className="space-y-3 rounded-xl border border-neutral-800 bg-neutral-900 p-3.5"
    >
      <div className="grid gap-2 sm:grid-cols-2">
        <label className="block">
          <span className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
            Name
          </span>
          <input
            autoFocus
            value={draft.name}
            onChange={(e) => set("name", e.target.value)}
            placeholder="Safety"
            className={FIELD}
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
            Description
          </span>
          <input
            value={draft.description}
            onChange={(e) => set("description", e.target.value)}
            placeholder="What this guardrail is protecting against"
            className={FIELD}
          />
        </label>
      </div>

      <label className="block">
        <span className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
          Scorer
        </span>
        <select
          value={draft.scorer_id}
          onChange={(e) => {
            set("scorer_id", e.target.value);
            // Labels belong to the scorer that defines them, so carrying them
            // across a scorer change would send categories the new scorer
            // cannot return.
            set("block_labels", []);
          }}
          className={FIELD}
        >
          {scorers.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name} — {s.output_type}
            </option>
          ))}
        </select>
        {needsThreshold && (
          <span className="mt-1 block text-[11px] text-amber-400">
            {scorer?.name} scores {scorer?.score_min}–{scorer?.score_max} with no
            pass threshold, so nothing about its answer says &ldquo;block&rdquo;. Set
            one on the{" "}
            <Link href="/scorers" className="underline">
              scorer
            </Link>{" "}
            first.
          </span>
        )}
      </label>

      {scorer?.output_type === "categorical" && (
        <div>
          <span className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
            Trigger on
          </span>
          <div className="flex flex-wrap gap-1.5">
            {scorer.categories.map((category) => {
              const on = draft.block_labels.includes(category);
              return (
                <button
                  key={category}
                  type="button"
                  onClick={() =>
                    set(
                      "block_labels",
                      on
                        ? draft.block_labels.filter((c) => c !== category)
                        : [...draft.block_labels, category],
                    )
                  }
                  className={`rounded-lg border px-2.5 py-1 text-[11px] ${
                    on
                      ? "border-red-800 bg-red-950 text-red-300"
                      : "border-neutral-700 text-neutral-400"
                  }`}
                >
                  {category}
                </button>
              );
            })}
          </div>
          <span className="mt-1 block text-[11px] text-neutral-500">
            A categorical scorer reports a distribution and no pass/fail, so this
            is where which labels count as a trigger gets decided.
          </span>
        </div>
      )}

      <div className="grid gap-2 sm:grid-cols-2">
        <label className="block">
          <span className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
            When it fires
          </span>
          <select
            value={draft.action}
            onChange={(e) => set("action", e.target.value as GuardrailAction)}
            className={FIELD}
          >
            <option value="flag">flag — record it, never block</option>
            <option value="block">block — refuse the output</option>
          </select>
          <span className="mt-1 block text-[11px] text-neutral-500">
            {draft.action === "flag"
              ? "Shadow mode. The judge runs and the result is reported, so you can watch it against real traffic before it can reject anything."
              : "The endpoint answers block, and the calling application is expected to withhold the response."}
          </span>
        </label>

        <label className="block">
          <span className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
            If the judge fails
          </span>
          <select
            value={draft.on_error}
            onChange={(e) => set("on_error", e.target.value as GuardrailOnError)}
            className={FIELD}
          >
            <option value="allow">allow — treat as not triggered</option>
            <option value="block">block — treat as triggered</option>
          </select>
          <span className="mt-1 block text-[11px] text-neutral-500">
            {draft.on_error === "allow"
              ? "An API blip lets output through unscreened rather than taking your application down. The check is still marked degraded."
              : "Fail closed. Safer output, but an Anthropic outage becomes an outage of everything behind this guardrail."}
          </span>
        </label>
      </div>

      {save.isError && (
        <p className="text-[11px] text-red-400">
          {save.error instanceof ApiError
            ? save.error.message
            : "Could not save guardrail."}
        </p>
      )}

      <div className="flex items-center gap-3">
        <button
          type="submit"
          disabled={
            !draft.name.trim() ||
            !draft.scorer_id ||
            needsThreshold ||
            needsLabels ||
            save.isPending
          }
          className="rounded-lg btn-primary px-3 py-1.5 text-xs font-medium disabled:opacity-40"
        >
          {save.isPending ? "Saving…" : guardrail ? "Save changes" : "Create guardrail"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="text-xs text-neutral-500 hover:text-neutral-300"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}

// --------------------------------------------------------------------------
// Try it
// --------------------------------------------------------------------------

/**
 * Screen text through the real endpoint.
 *
 * Deliberately not a preview mode: this posts to /v1/guardrail exactly as an
 * instrumented application would, so the decision, the spend and the log entry
 * are the same ones production produces. A UI-only path that skipped the log
 * would be the one place where what you tested isn't what runs.
 */
function TryPanel({ onChecked }: { onChecked: () => void }) {
  const [output, setOutput] = useState("");
  const [input, setInput] = useState("");

  const run = useMutation<GuardrailDecision>({
    mutationFn: () => api.checkGuardrails({ output, input, source: "ui" }),
    onSuccess: onChecked,
  });

  return (
    <section className="space-y-2 rounded-xl border border-neutral-800 bg-neutral-900 p-3.5">
      <h2 className="text-[13px] font-medium">Try it</h2>
      <p className="text-[11px] text-neutral-500">
        Runs every enabled guardrail through the same endpoint your application
        calls. Real spend, and it lands in the log below.
      </p>

      <textarea
        value={input}
        onChange={(e) => setInput(e.target.value)}
        rows={2}
        placeholder="The question behind it (optional)"
        className={`${FIELD} resize-y`}
      />
      <textarea
        value={output}
        onChange={(e) => setOutput(e.target.value)}
        rows={4}
        placeholder="The output a user would see"
        className={`${FIELD} resize-y`}
      />

      <button
        onClick={() => run.mutate()}
        disabled={!output.trim() || run.isPending}
        className="rounded-lg border border-neutral-700 px-2.5 py-1.5 text-[11px] text-neutral-200 disabled:opacity-40"
      >
        {run.isPending ? "Screening…" : "Screen it"}
      </button>

      {run.isError && (
        <p className="text-[11px] text-red-400">
          {run.error instanceof ApiError ? run.error.message : "Check failed."}
        </p>
      )}

      {run.data && (
        <div className="space-y-2 rounded-lg border border-neutral-800 bg-neutral-950 p-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <Decision decision={run.data.decision} />
            <span className="text-[11px] text-neutral-500">
              {formatDuration(run.data.latency_ms)} · {formatCost(run.data.cost_usd)}
            </span>
            {run.data.degraded && (
              <span className="text-[11px] text-amber-400">
                degraded — a judge never answered
              </span>
            )}
            <Link
              href={`/traces/${run.data.trace_id}`}
              className="ml-auto text-[11px] text-sky-400 hover:text-sky-300"
            >
              Trace →
            </Link>
          </div>
          {run.data.results.map((result) => (
            <ResultRow key={result.span_id || result.guardrail_name} result={result} />
          ))}
        </div>
      )}
    </section>
  );
}

// --------------------------------------------------------------------------
// Log
// --------------------------------------------------------------------------

function Decision({ decision }: { decision: "pass" | "block" }) {
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${
        decision === "block"
          ? "bg-red-950 text-red-300"
          : "bg-emerald-950 text-emerald-300"
      }`}
    >
      {decision}
    </span>
  );
}

function ResultRow({ result }: { result: GuardrailResult }) {
  const verdict =
    result.status === "failed"
      ? "error"
      : result.value != null && result.output_type === "numeric"
        ? String(+result.value.toFixed(2))
        : result.label;

  return (
    <div className="border-t border-neutral-850 pt-2 first:border-t-0 first:pt-0">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] text-neutral-300">{result.guardrail_name}</span>
        <span
          className={`rounded px-1.5 py-0.5 text-[10px] ${
            result.triggered
              ? result.action === "block"
                ? "bg-red-950 text-red-300"
                : "bg-amber-950 text-amber-300"
              : "bg-neutral-800 text-neutral-400"
          }`}
        >
          {result.triggered ? `fired · ${result.action}` : "clear"}
        </span>
        <span className="font-mono text-[10px] text-neutral-600">{verdict}</span>
      </div>
      {result.reasoning && (
        <p className="mt-1 whitespace-pre-wrap break-words text-[11px] text-neutral-500">
          {result.reasoning}
        </p>
      )}
      {result.error && (
        <p className="mt-1 break-words text-[11px] text-amber-400">{result.error}</p>
      )}
    </div>
  );
}

function CheckLog() {
  const [decision, setDecision] = useState<"all" | "pass" | "block">("all");

  const { data, isLoading } = useQuery({
    queryKey: ["guardrail-checks", decision],
    queryFn: () => api.guardrailChecks(25, decision === "all" ? undefined : decision),
  });

  const checks = data?.checks ?? [];

  return (
    <section className="space-y-2">
      <div className="flex items-center gap-2">
        <h2 className="text-[13px] font-medium">Recent checks</h2>
        <div className="ml-auto flex gap-1">
          {(["all", "block", "pass"] as const).map((option) => (
            <button
              key={option}
              onClick={() => setDecision(option)}
              className={`rounded-lg px-2 py-1 text-[11px] ${
                decision === option
                  ? "bg-neutral-800 text-neutral-100"
                  : "text-neutral-500 hover:text-neutral-300"
              }`}
            >
              {option}
            </button>
          ))}
        </div>
      </div>

      {isLoading ? (
        <p className="py-6 text-center text-xs text-neutral-500">Loading…</p>
      ) : checks.length === 0 ? (
        <div className="rounded-xl border border-dashed border-neutral-800 px-4 py-6 text-center text-xs text-neutral-500">
          Nothing screened yet.
        </div>
      ) : (
        <ul className="space-y-2">
          {checks.map((check) => (
            <CheckRow key={check.id} check={check} />
          ))}
        </ul>
      )}
    </section>
  );
}

function CheckRow({ check }: { check: GuardrailCheck }) {
  const [open, setOpen] = useState(false);

  return (
    <li className="rounded-xl border border-neutral-800 bg-neutral-900">
      <button
        onClick={() => setOpen(!open)}
        className="flex w-full items-start gap-2 px-3.5 py-2.5 text-left"
      >
        <Decision decision={check.decision} />
        <div className="min-w-0 flex-1">
          {/* One line collapsed. A screened completion can be pages long and a
              log that renders them in full is a log you scroll past. */}
          <p className="truncate text-[11px] text-neutral-300">{check.output}</p>
          <p className="mt-0.5 font-mono text-[10px] text-neutral-600">
            {check.source || "—"}
            {check.latency_ms != null && ` · ${formatDuration(check.latency_ms)}`}
            {check.cost_usd != null && ` · ${formatCost(check.cost_usd)}`}
            {check.degraded && <span className="text-amber-500"> · degraded</span>}
          </p>
        </div>
        <span className="text-[11px] text-neutral-600">{open ? "−" : "+"}</span>
      </button>

      {open && (
        <div className="space-y-2 border-t border-neutral-850 px-3.5 py-2.5">
          {check.input && (
            <div>
              <p className="text-[10px] uppercase tracking-wide text-neutral-500">
                Input
              </p>
              <p className="whitespace-pre-wrap break-words text-[11px] text-neutral-400">
                {check.input}
              </p>
            </div>
          )}
          <div>
            <p className="text-[10px] uppercase tracking-wide text-neutral-500">
              Screened output
            </p>
            <p className="whitespace-pre-wrap break-words text-[11px] text-neutral-400">
              {check.output}
            </p>
          </div>
          <div className="rounded-lg border border-neutral-800 bg-neutral-950 p-2.5">
            {check.results.map((result, i) => (
              <ResultRow key={result.span_id || i} result={result} />
            ))}
          </div>
          {check.trace_id && (
            <Link
              href={`/traces/${check.trace_id}`}
              className="inline-block text-[11px] text-sky-400 hover:text-sky-300"
            >
              Trace →
            </Link>
          )}
        </div>
      )}
    </li>
  );
}
