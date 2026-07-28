"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Shell } from "../shell";
import { VersionHistory } from "@/components/version-history";
import {
  api,
  ApiError,
  EXPECTED_PLACEHOLDER,
  formatCost,
  formatDuration,
  INPUT_PLACEHOLDER,
  OUTPUT_PLACEHOLDER,
  RUN_MODELS,
  SCORER_PRESETS,
  type Scorer,
  type ScorerDraft,
  type ScorerOutputType,
  type TryScorerResult,
} from "@/lib/api";

export default function ScorersPage() {
  return (
    <Shell>
      <Scorers />
    </Shell>
  );
}

const BLANK: ScorerDraft = {
  name: "",
  description: "",
  prompt_template: `Judge the answer below.

Question:
${INPUT_PLACEHOLDER}

Answer:
${OUTPUT_PLACEHOLDER}

Record passed = true if the answer is acceptable.`,
  model: RUN_MODELS[0],
  max_tokens: 1024,
  output_type: "boolean",
  score_min: null,
  score_max: null,
  categories: [],
  pass_threshold: null,
};

/** Strip the server-owned fields, leaving only what the form edits. */
function toDraft(scorer: Scorer): ScorerDraft {
  return {
    name: scorer.name,
    description: scorer.description,
    prompt_template: scorer.prompt_template,
    model: scorer.model,
    max_tokens: scorer.max_tokens,
    output_type: scorer.output_type,
    score_min: scorer.score_min,
    score_max: scorer.score_max,
    categories: scorer.categories,
    pass_threshold: scorer.pass_threshold,
  };
}

function Scorers() {
  const queryClient = useQueryClient();
  const [creating, setCreating] = useState<ScorerDraft | null>(null);

  const { data, isLoading } = useQuery({ queryKey: ["scorers"], queryFn: api.scorers });
  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["scorers"] });

  const scorers = data?.scorers ?? [];

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-base font-semibold">Scorers</h1>
        <p className="mt-1 text-xs text-neutral-500">
          A scorer is a prompt, a model, and an output schema. The judge answers
          through a forced tool call, so every score comes back typed rather than
          as prose.
        </p>
      </div>

      {!creating && (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <button
              onClick={() => setCreating(BLANK)}
              className="rounded-lg btn-primary px-3 py-1.5 text-xs font-medium"
            >
              ＋ New scorer
            </button>
            <span className="text-[11px] text-neutral-500">or start from:</span>
            {SCORER_PRESETS.map((preset) => (
              <button
                key={preset.label}
                onClick={() => setCreating(preset.draft)}
                title={preset.hint}
                className="rounded-lg border border-neutral-700 px-2.5 py-1.5 text-[11px] text-neutral-300 hover:border-neutral-600"
              >
                {preset.label}
              </button>
            ))}
          </div>
        </div>
      )}

      {creating && (
        <ScorerEditor
          initial={creating}
          onCancel={() => setCreating(null)}
          onSaved={() => {
            setCreating(null);
            invalidate();
          }}
        />
      )}

      {isLoading ? (
        <p className="py-10 text-center text-sm text-neutral-500">Loading scorers…</p>
      ) : scorers.length === 0 && !creating ? (
        <div className="rounded-xl border border-dashed border-neutral-800 px-4 py-8 text-center text-xs text-neutral-500">
          No scorers yet. Start from a preset above — you can edit it before saving.
        </div>
      ) : (
        <ul className="space-y-2">
          {scorers.map((scorer) => (
            <ScorerRow key={scorer.id} scorer={scorer} onChanged={invalidate} />
          ))}
        </ul>
      )}
    </div>
  );
}

/** One-line description of what a scorer produces. */
function outputLabel(scorer: Pick<Scorer, "output_type" | "score_min" | "score_max" | "categories" | "pass_threshold">): string {
  switch (scorer.output_type) {
    case "numeric":
      return `${scorer.score_min}–${scorer.score_max}${
        scorer.pass_threshold != null ? ` · pass ≥ ${scorer.pass_threshold}` : ""
      }`;
    case "categorical":
      return scorer.categories.join(" / ");
    default:
      return "pass / fail";
  }
}

function ScorerRow({ scorer, onChanged }: { scorer: Scorer; onChanged: () => void }) {
  const [mode, setMode] = useState<"closed" | "edit" | "try" | "history">("closed");

  const archive = useMutation({
    mutationFn: () => api.archiveScorer(scorer.id),
    onSuccess: onChanged,
  });

  return (
    <li className="rounded-xl border border-neutral-800 bg-neutral-900">
      <div className="flex items-start justify-between gap-2 px-3.5 py-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[13px] font-medium">{scorer.name}</span>
            <span className="rounded bg-neutral-800 px-1.5 py-0.5 text-[10px] text-neutral-300">
              {scorer.output_type}
            </span>
            <span className="text-[11px] text-neutral-500">{outputLabel(scorer)}</span>
          </div>
          {scorer.description && (
            <p className="mt-1 text-[11px] text-neutral-500">{scorer.description}</p>
          )}
          <p className="mt-1 font-mono text-[10px] text-neutral-600">
            {scorer.model} · {scorer.score_count ?? 0} scores
            {scorer.version != null && ` · v${scorer.version}`}
          </p>
        </div>
      </div>

      <div className="flex flex-wrap gap-2 border-t border-neutral-850 px-3.5 py-2">
        <button
          onClick={() => setMode(mode === "try" ? "closed" : "try")}
          className="text-[11px] text-sky-400 hover:text-sky-300"
        >
          {mode === "try" ? "Close" : "Try it"}
        </button>
        <button
          onClick={() => setMode(mode === "edit" ? "closed" : "edit")}
          className="text-[11px] text-neutral-400 hover:text-neutral-200"
        >
          {mode === "edit" ? "Close" : "Edit"}
        </button>
        {/* Only once there is something to look at. A scorer with one version
            has a history that says "created", which is not worth a click. */}
        {scorer.prompt_id && (scorer.version ?? 1) > 1 && (
          <button
            onClick={() => setMode(mode === "history" ? "closed" : "history")}
            className="text-[11px] text-neutral-400 hover:text-neutral-200"
          >
            {mode === "history" ? "Close" : `History (${scorer.version})`}
          </button>
        )}
        <button
          onClick={() => archive.mutate()}
          disabled={archive.isPending}
          className="ml-auto text-[11px] text-neutral-600 hover:text-red-400"
        >
          Archive
        </button>
      </div>

      {mode === "edit" && (
        <div className="border-t border-neutral-850 p-3">
          <ScorerEditor
            initial={toDraft(scorer)}
            scorerId={scorer.id}
            promptId={scorer.prompt_id}
            onCancel={() => setMode("closed")}
            onSaved={() => {
              setMode("closed");
              onChanged();
            }}
          />
        </div>
      )}

      {mode === "try" && (
        <div className="border-t border-neutral-850 p-3">
          <TryPanel scorer={scorer} />
        </div>
      )}

      {mode === "history" && scorer.prompt_id && (
        <div className="border-t border-neutral-850 p-3">
          {/* Labels are hidden: nothing resolves a scorer by label — it always
              judges with its current definition — so a promote control here
              would be a button that does nothing. */}
          <VersionHistory promptId={scorer.prompt_id} showLabels={false} />
        </div>
      )}
    </li>
  );
}

// --------------------------------------------------------------------------
// Editor
// --------------------------------------------------------------------------

const FIELD =
  "w-full rounded-lg border border-neutral-700 bg-neutral-950 px-2.5 py-1.5 text-xs outline-none focus:border-neutral-500";

function ScorerEditor({
  initial,
  scorerId,
  promptId,
  onCancel,
  onSaved,
}: {
  initial: ScorerDraft;
  scorerId?: string;
  promptId?: string | null;
  onCancel: () => void;
  onSaved: () => void;
}) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<ScorerDraft>(initial);
  // Categories are edited as free text so a half-typed list doesn't get
  // normalized out from under the cursor on every keystroke.
  const [categoryText, setCategoryText] = useState(initial.categories.join(", "));
  const [note, setNote] = useState("");

  const set = <K extends keyof ScorerDraft>(key: K, value: ScorerDraft[K]) =>
    setDraft((d) => ({ ...d, [key]: value }));

  /**
   * Switching output type carries defaults with it.
   *
   * The backend rejects a numeric scorer with no range, so leaving the fields
   * null after a switch turns a two-click change into a validation error the
   * user has to read to understand.
   */
  const setOutputType = (output_type: ScorerOutputType) =>
    setDraft((d) => ({
      ...d,
      output_type,
      score_min: output_type === "numeric" ? (d.score_min ?? 1) : null,
      score_max: output_type === "numeric" ? (d.score_max ?? 5) : null,
      pass_threshold: output_type === "numeric" ? d.pass_threshold : null,
    }));

  // Typed as void: create returns an id and update returns a status, and the
  // caller uses neither — a union return type here would only be noise.
  const save = useMutation<void>({
    mutationFn: async () => {
      const body: ScorerDraft = {
        ...draft,
        categories:
          draft.output_type === "categorical"
            ? categoryText.split(",").map((c) => c.trim()).filter(Boolean)
            : [],
      };
      if (scorerId) {
        await api.updateScorer(scorerId, body, note);
      } else {
        await api.createScorer(body);
      }
    },
    onSuccess: () => {
      // The edit may have appended a version, so the open history panel is
      // now stale. Invalidating here rather than in the panel keeps the two
      // from having to know about each other.
      if (promptId) {
        queryClient.invalidateQueries({ queryKey: ["prompt", promptId] });
      }
      onSaved();
    },
  });

  const missingOutput = !draft.prompt_template.includes(OUTPUT_PLACEHOLDER);

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
            placeholder="Hallucination"
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
            placeholder="What this judge is looking for"
            className={FIELD}
          />
        </label>
      </div>

      <div>
        <label className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
          Judge prompt
        </label>
        <textarea
          value={draft.prompt_template}
          onChange={(e) => set("prompt_template", e.target.value)}
          rows={10}
          className={`${FIELD} resize-y font-mono text-[12px]`}
        />
        <p
          className={`mt-1 text-[11px] ${
            missingOutput ? "text-amber-400" : "text-neutral-500"
          }`}
        >
          {missingOutput
            ? `Must contain ${OUTPUT_PLACEHOLDER} — that is the text being judged.`
            : `${OUTPUT_PLACEHOLDER} is the text being judged. ${INPUT_PLACEHOLDER} and ${EXPECTED_PLACEHOLDER} are optional.`}
        </p>
      </div>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        <label className="block">
          <span className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
            Judge model
          </span>
          <select
            value={draft.model}
            onChange={(e) => set("model", e.target.value)}
            className={FIELD}
          >
            {RUN_MODELS.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </label>

        <label className="block">
          <span className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
            Output type
          </span>
          <select
            value={draft.output_type}
            onChange={(e) => setOutputType(e.target.value as ScorerOutputType)}
            className={FIELD}
          >
            <option value="boolean">boolean — pass/fail</option>
            <option value="numeric">numeric — a scale</option>
            <option value="categorical">categorical — a label</option>
          </select>
        </label>

        <label className="block">
          <span className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
            Max tokens
          </span>
          <input
            type="number"
            min={1}
            max={32000}
            value={draft.max_tokens}
            onChange={(e) => set("max_tokens", Number(e.target.value))}
            className={`${FIELD} tabular-nums`}
          />
        </label>
      </div>

      {draft.output_type === "numeric" && (
        <div className="grid grid-cols-3 gap-2">
          <label className="block">
            <span className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
              Min
            </span>
            <input
              type="number"
              value={draft.score_min ?? 1}
              onChange={(e) => set("score_min", Number(e.target.value))}
              className={`${FIELD} tabular-nums`}
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
              Max
            </span>
            <input
              type="number"
              value={draft.score_max ?? 5}
              onChange={(e) => set("score_max", Number(e.target.value))}
              className={`${FIELD} tabular-nums`}
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
              Pass ≥
            </span>
            <input
              type="number"
              value={draft.pass_threshold ?? ""}
              placeholder="none"
              onChange={(e) =>
                set("pass_threshold", e.target.value === "" ? null : Number(e.target.value))
              }
              className={`${FIELD} tabular-nums`}
            />
          </label>
        </div>
      )}

      {draft.output_type === "categorical" && (
        <label className="block">
          <span className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
            Categories (comma separated)
          </span>
          <input
            value={categoryText}
            onChange={(e) => setCategoryText(e.target.value)}
            placeholder="helpful, neutral, dismissive"
            className={FIELD}
          />
          <span className="mt-1 block text-[11px] text-neutral-500">
            Becomes an enum in the judge&apos;s output schema, so it can only answer
            with one of these.
          </span>
        </label>
      )}

      {/* Only when editing. On create the version note is always "Created." —
          there is no change to describe. */}
      {scorerId && (
        <label className="block">
          <span className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
            What changed (optional)
          </span>
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Widen the scale to 1–10"
            className={FIELD}
          />
          <span className="mt-1 block text-[11px] text-neutral-500">
            Saving records a new version. Past scores keep pointing at the
            version that produced them.
          </span>
        </label>
      )}

      {save.isError && (
        <p className="text-[11px] text-red-400">
          {save.error instanceof ApiError ? save.error.message : "Could not save scorer."}
        </p>
      )}

      <div className="flex items-center gap-3">
        <button
          type="submit"
          disabled={!draft.name.trim() || missingOutput || save.isPending}
          className="rounded-lg btn-primary px-3 py-1.5 text-xs font-medium disabled:opacity-40"
        >
          {save.isPending ? "Saving…" : scorerId ? "Save changes" : "Create scorer"}
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
 * One judge call against text you paste in.
 *
 * This is the cheap feedback loop. Without it, the shortest way to find out a
 * judge prompt is wrong is a whole run — which is how you spend forty calls
 * learning that the prompt asks for 1–10 while the schema says 1–5.
 */
function TryPanel({ scorer }: { scorer: Scorer }) {
  const [input, setInput] = useState("");
  const [output, setOutput] = useState("");
  const [expected, setExpected] = useState("");

  const run = useMutation<TryScorerResult>({
    mutationFn: () =>
      api.tryScorer(scorer.id, { output, input, expected: expected || null }),
  });

  return (
    <div className="space-y-2">
      <textarea
        value={input}
        onChange={(e) => setInput(e.target.value)}
        rows={2}
        placeholder={`Input — substituted for ${INPUT_PLACEHOLDER}`}
        className={`${FIELD} resize-y`}
      />
      <textarea
        value={output}
        onChange={(e) => setOutput(e.target.value)}
        rows={4}
        placeholder={`Output to judge — substituted for ${OUTPUT_PLACEHOLDER}`}
        className={`${FIELD} resize-y`}
      />
      {scorer.prompt_template.includes(EXPECTED_PLACEHOLDER) && (
        <textarea
          value={expected}
          onChange={(e) => setExpected(e.target.value)}
          rows={2}
          placeholder={`Expected — substituted for ${EXPECTED_PLACEHOLDER}`}
          className={`${FIELD} resize-y`}
        />
      )}

      <div className="flex items-center gap-3">
        <button
          onClick={() => run.mutate()}
          disabled={!output.trim() || run.isPending}
          className="rounded-lg border border-neutral-700 px-2.5 py-1.5 text-[11px] text-neutral-200 disabled:opacity-40"
        >
          {run.isPending ? "Judging…" : "Run once"}
        </button>
        <span className="text-[11px] text-neutral-500">1 live API call</span>
      </div>

      {run.isError && (
        <p className="text-[11px] text-red-400">
          {run.error instanceof ApiError ? run.error.message : "Judge call failed."}
        </p>
      )}

      {run.data && (
        <div className="space-y-2 rounded-lg border border-neutral-800 bg-neutral-950 p-2.5">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${
                run.data.passed === true
                  ? "bg-emerald-950 text-emerald-300"
                  : run.data.passed === false
                    ? "bg-red-950 text-red-300"
                    : "bg-neutral-800 text-neutral-300"
              }`}
            >
              {scorer.output_type === "numeric" && run.data.value != null
                ? `${run.data.value}/${scorer.score_max}`
                : run.data.label}
            </span>
            <span className="text-[11px] text-neutral-500">
              {formatDuration(run.data.latency_ms)}
              {run.data.cost_usd != null && ` · ${formatCost(run.data.cost_usd)}`}
            </span>
          </div>
          <p className="whitespace-pre-wrap break-words text-[11px] text-neutral-400">
            {run.data.reasoning}
          </p>
        </div>
      )}
    </div>
  );
}
