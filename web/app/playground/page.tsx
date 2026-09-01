"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { Shell } from "../shell";
import { CredentialPicker } from "@/components/credential-picker";
import { useRole } from "@/lib/use-role";
import { useModels } from "@/lib/use-models";
import {
  api,
  ApiError,
  formatCost,
  formatDuration,
  scoreTone,
  type PlaygroundResult,
  type Score,
} from "@/lib/api";

/**
 * Send one prompt, see the answer, score it. No dataset, no test case.
 *
 * The gap this closes: a scorer's "Try it" panel judges output you paste in,
 * and a replay run generates output but needs a dataset first. Neither lets you
 * ask the actual question — "is what this prompt produces any good?" — in one
 * step.
 *
 * The result is a real span, so anything run here shows up in Traces and in
 * the Overview's cost figures. That is deliberate: this spends money on the
 * same key as everything else, and a tool for watching LLM spend should not
 * have a corner where its own spend is invisible.
 */
export default function PlaygroundPage() {
  return (
    <Shell>
      <Playground />
    </Shell>
  );
}

const FIELD =
  "w-full rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm outline-none focus:border-neutral-500";

const INPUT_PLACEHOLDER = "{{input}}";

function Playground() {
  const [prompt, setPrompt] = useState("");
  const [input, setInput] = useState("");
  const [model, setModel] = useState("");
  const [maxTokens, setMaxTokens] = useState(1024);
  const [scorerIds, setScorerIds] = useState<string[]>([]);
  const [credentialId, setCredentialId] = useState("");
  const { isAdmin } = useRole();
  const [result, setResult] = useState<PlaygroundResult | null>(null);

  // Only the models the selected key can actually serve. Derived rather than
  // synced into state: when the key changes, a model belonging to the old
  // vendor simply stops being the effective one, so there is no window where
  // the form holds a selection the backend would reject.
  const models = useModels(credentialId);
  const effectiveModel = models.includes(model) ? model : (models[0] ?? "");

  const { data: scorerData } = useQuery({ queryKey: ["scorers"], queryFn: api.scorers });
  // Filtered rather than showing everything: a judge that wants an expected
  // output has nothing to compare against here, and offering it would produce a
  // score that looks like a verdict on the answer. Which scorers belong is a
  // per-scorer decision, made on the Scorers page.
  const scorers = (scorerData?.scorers ?? []).filter((s) => s.show_in_playground);

  const send = useMutation<PlaygroundResult>({
    mutationFn: () =>
      api.playground({
        prompt,
        model: effectiveModel,
        max_tokens: maxTokens,
        input,
        scorer_ids: scorerIds,
        // Empty means the default, resolved server-side. Sending "" rather
        // than guessing the default id here keeps one answer to "which key
        // pays", and it lives in the backend.
        credential_id: credentialId || null,
      }),
    // Held in state rather than read from `send.data` so the previous result
    // stays on screen while the next call is in flight — otherwise the answer
    // you are reading vanishes the moment you press Run again.
    onSuccess: setResult,
  });

  // Only offered when the prompt asks for it. A standalone prompt is the whole
  // request and has no reason to carry a placeholder; showing an input box
  // that feeds nothing would imply otherwise.
  const usesInput = prompt.includes(INPUT_PLACEHOLDER);

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-base font-semibold">Playground</h1>
        <p className="mt-1 text-xs text-neutral-500">
          Run a prompt and score the response. No test case needed.
        </p>
      </div>

      <div className="space-y-2.5 rounded-xl border border-neutral-800 bg-neutral-900 p-3.5">
        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          rows={6}
          placeholder="Write the prompt to send…"
          className={`${FIELD} resize-y`}
        />

        {usesInput && (
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            rows={2}
            placeholder={`Substituted for ${INPUT_PLACEHOLDER}`}
            className={`${FIELD} resize-y`}
          />
        )}

        <div className="flex flex-wrap gap-2">
          <label className="flex-1">
            <span className="kicker mb-1 block">Model</span>
            <select
              value={effectiveModel}
              onChange={(e) => setModel(e.target.value)}
              className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-2 py-1.5 text-xs outline-none focus:border-neutral-500"
            >
              {models.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </label>
          <div className="flex-1">
            <CredentialPicker
              value={credentialId}
              onChange={setCredentialId}
              label="Generate with"
            />
          </div>
          <label className="w-28">
            <span className="kicker mb-1 block">Max tokens</span>
            <input
              type="number"
              min={1}
              max={32000}
              value={maxTokens}
              onChange={(e) => setMaxTokens(Number(e.target.value))}
              className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-2 py-1.5 text-xs tabular-nums outline-none focus:border-neutral-500"
            />
          </label>
        </div>

        {/* Only when there are scorers but none of them are offered here.
            Otherwise this reads as "you have no scorers", which is a different
            problem with a different fix. */}
        {scorers.length === 0 && (scorerData?.scorers.length ?? 0) > 0 && (
          <p className="text-[11px] text-neutral-500">
            No scorers are set to show here.{" "}
            <Link href="/scorers" className="text-sky-400 hover:underline">
              Pick some on the Scorers page
            </Link>
            .
          </p>
        )}

        {scorers.length > 0 && (
          <div>
            <span className="kicker mb-1 block">Score with (optional)</span>
            <div className="flex flex-wrap gap-x-4 gap-y-1.5">
              {scorers.map((s) => (
                <label key={s.id} className="flex items-center gap-1.5">
                  <input
                    type="checkbox"
                    checked={scorerIds.includes(s.id)}
                    onChange={() =>
                      setScorerIds((ids) =>
                        ids.includes(s.id)
                          ? ids.filter((x) => x !== s.id)
                          : [...ids, s.id],
                      )
                    }
                  />
                  <span className="text-[12px] text-neutral-300">{s.name}</span>
                </label>
              ))}
            </div>
          </div>
        )}

        {send.isError && (
          <p className="text-xs text-red-400">
            {send.error instanceof ApiError ? send.error.message : "The call failed."}
          </p>
        )}

        <div className="flex items-center gap-3">
          <button
            onClick={() => send.mutate()}
            disabled={
              !isAdmin || !prompt.trim() || !effectiveModel || send.isPending
            }
            title={isAdmin ? undefined : "Read-only access — running a prompt spends money"}
            className="rounded-lg btn-primary px-3 py-1.5 text-xs font-medium disabled:opacity-40"
          >
            {send.isPending ? "Running…" : "Run"}
          </button>
          {/* Same honesty as the run form: say what this costs before it is
              spent, not after. */}
          <span className="text-[11px] text-neutral-500">
            1 model call
            {scorerIds.length > 0 &&
              ` · ${scorerIds.length} judge ${scorerIds.length === 1 ? "call" : "calls"}`}
          </span>
        </div>
      </div>

      {result && <Result result={result} />}
    </div>
  );
}

// --------------------------------------------------------------------------
// Result
// --------------------------------------------------------------------------

function Result({ result }: { result: PlaygroundResult }) {
  const expectsScores = result.score_ids.length > 0;

  // Scores are written by judges on a background thread, so the completion
  // arrives before the verdicts do. Poll the trace — the same endpoint the
  // trace detail view uses — and stop once every expected score is terminal.
  const { data } = useQuery({
    queryKey: ["trace", result.trace_id],
    queryFn: () => api.trace(result.trace_id),
    enabled: expectsScores,
    refetchInterval: (query) => {
      const scores = query.state.data?.scores ?? [];
      const settled = scores.filter(
        (s: Score) => s.status === "succeeded" || s.status === "failed",
      );
      return settled.length >= result.score_ids.length ? false : 1500;
    },
  });

  const scores = data?.scores ?? [];
  const pending = expectsScores && scores.length < result.score_ids.length;

  return (
    <div className="space-y-3 rounded-xl border border-neutral-800 bg-neutral-900 p-3.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-neutral-500">
        <span className="text-neutral-400">{result.response_model}</span>
        <span>{formatDuration(result.latency_ms)}</span>
        <span>{result.credential_name}</span>
        {/* Only when it differs. A judge is billed to its own model's vendor,
            so grading a Grok completion with a Claude scorer spends two keys —
            and an app about watching spend should not make you infer the
            second one. Silent when both roles used the same key, which is the
            common case and not worth the noise. */}
        {result.judged_by.some((n) => n !== result.credential_name) && (
          <span className="text-neutral-400">
            judged by {result.judged_by.join(", ")}
          </span>
        )}
        {result.cost_usd != null && <span>{formatCost(result.cost_usd)}</span>}
        <span>
          {result.input_tokens} in / {result.output_tokens} out
        </span>
        {/* The completion was cut off, so the output below is a fragment.
            Worth saying — a truncated answer scored badly is a max_tokens
            problem, not a prompt problem. */}
        {result.finish_reason === "max_tokens" && (
          <span className="rounded bg-amber-950 px-1.5 py-0.5 text-amber-300">
            hit max tokens
          </span>
        )}
      </div>

      <p className="whitespace-pre-wrap break-words text-sm text-neutral-200">
        {result.output || (
          <span className="text-neutral-500">The model returned no text.</span>
        )}
      </p>

      {result.scoring_skipped && (
        <p className="text-[11px] text-amber-300">{result.scoring_skipped}</p>
      )}

      {expectsScores && (
        <div className="space-y-2 border-t border-neutral-800 pt-2.5">
          {pending && <p className="text-[11px] text-neutral-500">Judging…</p>}
          {scores.map((s) => (
            <ScoreRow key={s.id} score={s} />
          ))}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-3 border-t border-neutral-800 pt-2.5 text-[11px]">
        <Link href={`/traces/${result.trace_id}`} className="text-sky-400 hover:underline">
          View trace
        </Link>
        <SaveAsTestCase result={result} />
      </div>
    </div>
  );
}

function ScoreRow({ score }: { score: Score }) {
  return (
    <div className="space-y-1">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] text-neutral-400">{score.scorer_name}</span>
        <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${scoreTone(score)}`}>
          {score.status === "succeeded"
            ? score.output_type === "numeric" && score.value != null
              ? `${score.value}/${score.score_max}`
              : score.label
            : score.status}
        </span>
        {score.cost_usd != null && (
          <span className="text-[11px] text-neutral-500">{formatCost(score.cost_usd)}</span>
        )}
      </div>
      {(score.reasoning || score.error) && (
        <p className="whitespace-pre-wrap break-words text-[11px] text-neutral-500">
          {score.error || score.reasoning}
        </p>
      )}
    </div>
  );
}

/**
 * Promote an ad-hoc prompt into a saved test case.
 *
 * The backlink is the point. `source_trace_id` and `source_span_id` are set to
 * the span this ran as, so a case captured here is as traceable as one lifted
 * off production traffic — "where did this test case come from?" stays
 * answerable by looking rather than by remembering.
 */
function SaveAsTestCase({ result }: { result: PlaygroundResult }) {
  const [open, setOpen] = useState(false);
  const [datasetId, setDatasetId] = useState("");

  const { data } = useQuery({ queryKey: ["datasets"], queryFn: api.datasets, enabled: open });
  const datasets = data?.datasets ?? [];

  const save = useMutation({
    mutationFn: () =>
      api.addItem(datasetId, {
        input: result.prompt,
        // The completion is recorded as the expected output — it is what this
        // prompt actually produced, which is the useful starting point for a
        // golden answer even when it needs editing afterwards.
        expected_output: result.output || null,
        source_trace_id: result.trace_id,
        source_span_id: result.span_id,
      }),
  });

  if (!open) {
    return (
      <button onClick={() => setOpen(true)} className="text-sky-400 hover:underline">
        Save as test case
      </button>
    );
  }

  if (save.isSuccess) {
    return (
      <span className="text-emerald-400">
        Saved ·{" "}
        <Link href={`/datasets/${datasetId}`} className="hover:underline">
          open dataset
        </Link>
      </span>
    );
  }

  return (
    <span className="flex flex-wrap items-center gap-2">
      <select
        value={datasetId}
        onChange={(e) => setDatasetId(e.target.value)}
        className="rounded-lg border border-neutral-700 bg-neutral-950 px-2 py-1 text-[11px] outline-none focus:border-neutral-500"
      >
        <option value="">Choose a dataset…</option>
        {datasets.map((d) => (
          <option key={d.id} value={d.id}>
            {d.name}
          </option>
        ))}
      </select>
      <button
        onClick={() => save.mutate()}
        disabled={!datasetId || save.isPending}
        className="rounded-lg border border-neutral-700 px-2 py-1 text-neutral-200 disabled:opacity-40"
      >
        {save.isPending ? "Saving…" : "Save"}
      </button>
      <button onClick={() => setOpen(false)} className="text-neutral-500 hover:text-neutral-300">
        Cancel
      </button>
      {save.isError && (
        <span className="text-red-400">
          {save.error instanceof ApiError ? save.error.message : "Could not save."}
        </span>
      )}
    </span>
  );
}
