"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import { Shell } from "../../shell";
import { CredentialPicker } from "@/components/credential-picker";
import { useModels } from "@/lib/use-models";
import {
  api,
  ApiError,
  formatCost,
  INPUT_PLACEHOLDER,
  relativeTime,
  type Prompt,
  type PromptVersion,
  type Run,
} from "@/lib/api";

export default function DatasetDetailPage() {
  return (
    <Shell>
      <DatasetDetail />
    </Shell>
  );
}

const DEFAULT_TEMPLATE = `You are a helpful assistant. Answer concisely.

${INPUT_PLACEHOLDER}`;

/**
 * How the version dropdown encodes a choice: "latest", "label:production", or
 * "version:<id>". One string keeps it a plain <select>, which is the control
 * that behaves best on a phone — the alternative is three coupled inputs.
 */
function resolveVersion(
  prompt: Prompt | undefined,
  pick: string,
): PromptVersion | undefined {
  if (!prompt) return undefined;
  if (pick.startsWith("version:")) {
    return prompt.versions.find((v) => v.id === pick.slice(8));
  }
  if (pick.startsWith("label:")) {
    const label = prompt.labels.find((l) => l.label === pick.slice(6));
    return prompt.versions.find((v) => v.id === label?.version_id);
  }
  return prompt.versions[0];
}

/** The same choice as request fields. "latest" sends no version and no label,
 *  which is what the backend already treats as "newest version of this
 *  prompt" — resolved there, at creation, not here. */
function promptRef(promptId: string, pick: string) {
  if (!promptId) return {};
  if (pick.startsWith("version:")) return { prompt_version_id: pick.slice(8) };
  if (pick.startsWith("label:")) {
    return { prompt_id: promptId, prompt_label: pick.slice(6) };
  }
  return { prompt_id: promptId };
}

function DatasetDetail() {
  const params = useParams<{ id: string }>();
  const queryClient = useQueryClient();

  const { data, isLoading, isError } = useQuery({
    queryKey: ["dataset", params.id],
    queryFn: () => api.dataset(params.id),
    // Polls while a run is in flight so progress counters move without a
    // manual refresh; idles back to no polling once everything is settled.
    refetchInterval: (query) =>
      query.state.data?.runs.some((r) => r.status === "running" || r.status === "pending")
        ? 2000
        : false,
  });

  if (isLoading) {
    return <p className="py-10 text-center text-sm text-neutral-500">Loading dataset…</p>;
  }
  if (isError || !data) {
    return (
      <div className="py-10 text-center">
        <p className="text-sm text-red-400">Dataset not found.</p>
        <Link href="/datasets" className="mt-3 inline-block text-xs text-neutral-400 underline">
          Back to datasets
        </Link>
      </div>
    );
  }

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["dataset", params.id] });

  return (
    <div className="space-y-5">
      <div>
        <Link href="/datasets" className="text-xs text-neutral-500 hover:text-neutral-300">
          ← Datasets
        </Link>
        <h1 className="mt-2 text-base font-semibold">{data.name}</h1>
        {data.description && (
          <p className="mt-1 text-xs text-neutral-500">{data.description}</p>
        )}
      </div>

      <RunForm datasetId={params.id} itemCount={data.items.length} onStarted={invalidate} />

      <RunHistory runs={data.runs} />

      <TestCases
        datasetId={params.id}
        items={data.items}
        onChanged={invalidate}
      />
    </div>
  );
}

// --------------------------------------------------------------------------
// Run form
// --------------------------------------------------------------------------

function RunForm({
  datasetId,
  itemCount,
  onStarted,
}: {
  datasetId: string;
  itemCount: number;
  onStarted: () => void;
}) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [template, setTemplate] = useState(DEFAULT_TEMPLATE);
  // Null means "whatever the selected version says". Stored as an override
  // rather than copied into state when a version is picked: the copy has to be
  // re-copied on every change to the thing it came from, and the version's
  // config is not loaded yet at the moment the prompt is selected.
  const [modelOverride, setModelOverride] = useState<string | null>(null);
  const [maxTokensOverride, setMaxTokensOverride] = useState<number | null>(null);
  const [name, setName] = useState("");
  const [scorerIds, setScorerIds] = useState<string[]>([]);
  const [credentialId, setCredentialId] = useState("");
  // "" means an ad-hoc prompt typed below. Otherwise a saved prompt id, with
  // `pick` naming which of its versions — see PROMPT_PICK encoding.
  const [promptId, setPromptId] = useState("");
  const [pick, setPick] = useState("latest");

  // Only fetched once the form is open — a dataset page that never starts a run
  // has no reason to ask for the scorer or prompt lists.
  const { data: scorerData } = useQuery({
    queryKey: ["scorers"],
    queryFn: api.scorers,
    enabled: open,
  });
  const scorers = scorerData?.scorers ?? [];

  const { data: promptData } = useQuery({
    queryKey: ["prompts"],
    queryFn: () => api.prompts("completion"),
    enabled: open,
  });
  const savedPrompts = promptData?.prompts ?? [];


  const { data: selectedPrompt } = useQuery({
    queryKey: ["prompt", promptId],
    queryFn: () => api.prompt(promptId),
    enabled: open && promptId !== "",
  });

  // The version this run would actually send, resolved in the browser purely
  // so the form can show it. The backend resolves independently at creation —
  // this preview is never what gets recorded.
  const resolved = resolveVersion(selectedPrompt, pick);

  // Only what the selected key can serve.
  const models = useModels(credentialId);

  // Derived, not stored. A version carries the model and budget it was written
  // for; an explicit choice in the form wins over it until the selection moves
  // to a different version, which drops the override with it.
  //
  // The key has the last word. A version written against a Claude model cannot
  // run on an xAI key, so when the two disagree the form falls back to a model
  // the chosen key can actually serve — and shows it, rather than letting the
  // run fail on submit for a mismatch the form could see.
  const preferredModel =
    modelOverride ??
    (typeof resolved?.config.model === "string" ? resolved.config.model : models[0]);
  const model = models.includes(preferredModel) ? preferredModel : (models[0] ?? "");
  const maxTokens =
    maxTokensOverride ??
    (typeof resolved?.config.max_tokens === "number" ? resolved.config.max_tokens : 1024);

  const start = useMutation({
    mutationFn: () =>
      api.createRun({
        dataset_id: datasetId,
        // Only one of these is meaningful. When a saved prompt is referenced
        // the backend takes the version's own text and ignores this, because
        // "v3 with edits" would be recorded as v3 and would not be v3.
        prompt_template: promptId ? "" : template,
        model,
        max_tokens: maxTokens,
        name,
        scorer_ids: scorerIds,
        // Empty means the project default, resolved server-side.
        credential_id: credentialId || null,
        ...promptRef(promptId, pick),
      }),
    onSuccess: (run) => {
      onStarted();
      router.push(`/runs/${run.id}`);
    },
  });

  const choosePrompt = (id: string) => {
    setPromptId(id);
    setPick("latest");
    setModelOverride(null);
    setMaxTokensOverride(null);
  };
  const chooseVersion = (value: string) => {
    setPick(value);
    setModelOverride(null);
    setMaxTokensOverride(null);
  };

  // Checked in the browser as well as the backend. The backend rejection is
  // the one that counts, but catching it here means the user finds out while
  // still looking at the field rather than after a round trip. A saved version
  // was validated when it was written, so this only applies to ad-hoc text.
  const missingPlaceholder = !promptId && !template.includes(INPUT_PLACEHOLDER);

  if (itemCount === 0) {
    return (
      <div className="rounded-xl border border-dashed border-neutral-800 px-4 py-6 text-center text-xs text-neutral-500">
        Add a test case before running.
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-neutral-800 bg-neutral-900">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-3.5 py-3 text-left"
      >
        <span className="text-sm font-medium">Replay against a prompt</span>
        <span className="text-xs text-neutral-500">{open ? "−" : "+"}</span>
      </button>

      {open && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            start.mutate();
          }}
          className="space-y-3 border-t border-neutral-800 px-3.5 py-3"
        >
          <div className="grid grid-cols-2 gap-2">
            <label className="block">
              <span className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
                Prompt
              </span>
              <select
                value={promptId}
                onChange={(e) => choosePrompt(e.target.value)}
                className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-2 py-1.5 text-xs outline-none focus:border-neutral-500"
              >
                <option value="">Ad-hoc (type it below)</option>
                {savedPrompts.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>

            {promptId && selectedPrompt && (
              <label className="block">
                <span className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
                  Version
                </span>
                <select
                  value={pick}
                  onChange={(e) => chooseVersion(e.target.value)}
                  className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-2 py-1.5 text-xs outline-none focus:border-neutral-500"
                >
                  <option value="latest">
                    latest (v{selectedPrompt.versions[0]?.version ?? 1})
                  </option>
                  {selectedPrompt.labels.map((l) => (
                    <option key={l.label} value={`label:${l.label}`}>
                      {l.label} (v{l.version})
                    </option>
                  ))}
                  {selectedPrompt.versions.map((v) => (
                    <option key={v.id} value={`version:${v.id}`}>
                      v{v.version}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </div>

          {promptId ? (
            <div>
              {/* Read-only. Editing here would produce a run whose recorded
                  version does not match what it sent, which is the one thing
                  the version link exists to rule out. */}
              <div className="mb-1 flex items-center justify-between gap-2">
                <span className="text-[10px] uppercase tracking-wide text-neutral-500">
                  {resolved ? `v${resolved.version}` : "Prompt"} — read-only
                </span>
                <Link
                  href={`/prompts/${promptId}`}
                  className="text-[11px] text-sky-400 hover:underline"
                >
                  Edit on the prompt page →
                </Link>
              </div>
              <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-neutral-800 bg-neutral-950 px-2.5 py-2 font-mono text-[11px] text-neutral-400">
                {resolved?.template ?? "…"}
              </pre>
              {pick.startsWith("label:") && (
                <p className="mt-1 text-[11px] text-neutral-500">
                  Resolved now. The run records v{resolved?.version}, so moving
                  this label later won&apos;t change what this run says it sent.
                </p>
              )}
            </div>
          ) : (
            <div>
              <label className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
                Prompt template
              </label>
              <textarea
                value={template}
                onChange={(e) => setTemplate(e.target.value)}
                rows={6}
                className="w-full resize-y rounded-lg border border-neutral-700 bg-neutral-950 px-2.5 py-2 font-mono text-[12px] outline-none focus:border-neutral-500"
              />
              <p
                className={`mt-1 text-[11px] ${
                  missingPlaceholder ? "text-amber-400" : "text-neutral-500"
                }`}
              >
                {missingPlaceholder
                  ? `Must contain ${INPUT_PLACEHOLDER} — without it every case sends the same request.`
                  : `${INPUT_PLACEHOLDER} is replaced with each test case's input. Save it as a prompt to get history and a diff.`}
              </p>
            </div>
          )}

          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            <label className="block">
              <span className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
                Model
              </span>
              <select
                value={model}
                onChange={(e) => setModelOverride(e.target.value)}
                className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-2 py-1.5 text-xs outline-none focus:border-neutral-500"
              >
                {models.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
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
                value={maxTokens}
                onChange={(e) => setMaxTokensOverride(Number(e.target.value))}
                className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-2 py-1.5 text-xs tabular-nums outline-none focus:border-neutral-500"
              />
            </label>
            <label className="col-span-2 block sm:col-span-1">
              <span className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
                Label (optional)
              </span>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="baseline"
                className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-2 py-1.5 text-xs outline-none focus:border-neutral-500"
              />
            </label>
          </div>

          {/* Scorers run automatically when the replay finishes. Optional on
              purpose: you often want to see the outputs before deciding what
              question to ask of them, and scoring can be applied to the
              finished run later without replaying it. */}
          {scorers.length > 0 && (
            <div>
              <span className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
                Score with (optional)
              </span>
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

          <CredentialPicker
            value={credentialId}
            onChange={setCredentialId}
            label="Generate with"
          />

          {start.isError && (
            <p className="text-xs text-red-400">
              {start.error instanceof ApiError ? start.error.message : "Could not start run."}
            </p>
          )}

          <div className="flex items-center gap-3">
            <button
              type="submit"
              disabled={missingPlaceholder || !model || start.isPending}
              className="rounded-lg btn-primary px-3 py-1.5 text-xs font-medium disabled:opacity-40"
            >
              {start.isPending ? "Starting…" : `Run ${itemCount} cases`}
            </button>
            {/* Spending is the thing to be honest about up front — this makes
                real API calls, one per case, on the user's paid key, plus one
                judge call per case per scorer. */}
            <span className="text-[11px] text-neutral-500">
              {itemCount} replay {itemCount === 1 ? "call" : "calls"}
              {scorerIds.length > 0 &&
                ` + ${itemCount * scorerIds.length} judge calls`}
            </span>
          </div>
        </form>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// Runs
// --------------------------------------------------------------------------

function statusTone(status: Run["status"]): string {
  switch (status) {
    case "succeeded":
      return "bg-emerald-950 text-emerald-300";
    case "running":
    case "pending":
      return "bg-sky-950 text-sky-300";
    case "cancelled":
      return "bg-amber-950 text-amber-300";
    default:
      return "bg-red-950 text-red-300";
  }
}

function RunHistory({ runs }: { runs: Run[] }) {
  if (runs.length === 0) return null;

  return (
    <div>
      <h2 className="mb-2 text-xs font-medium uppercase tracking-wide text-neutral-500">
        Runs
      </h2>
      <ul className="space-y-2">
        {runs.map((r) => (
          <li key={r.id}>
            <Link
              href={`/runs/${r.id}`}
              className="block rounded-xl border border-neutral-800 bg-neutral-900 p-3 transition-colors hover:border-neutral-700"
            >
              <div className="flex items-center justify-between gap-2">
                <div className="flex min-w-0 items-center gap-2">
                  <span
                    className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ${statusTone(
                      r.status,
                    )}`}
                  >
                    {r.status}
                  </span>
                  <span className="truncate text-[13px]">{r.name || r.model}</span>
                </div>
                <span className="shrink-0 text-[11px] tabular-nums text-neutral-500">
                  {r.completed_count}/{r.item_count}
                </span>
              </div>
              <div className="mt-1.5 flex flex-wrap gap-x-3 text-[11px] text-neutral-500">
                <span>{r.model}</span>
                {r.prompt_name && (
                  <span className="text-neutral-400">
                    {r.prompt_name} v{r.prompt_version}
                  </span>
                )}
                {r.cost_usd > 0 && (
                  <span className="text-neutral-400">{formatCost(r.cost_usd)}</span>
                )}
                {r.error_count > 0 && (
                  <span className="text-red-400">{r.error_count} failed</span>
                )}
                {r.created_at && <span>{relativeTime(Date.parse(r.created_at) * 1e6)}</span>}
              </div>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

// --------------------------------------------------------------------------
// Test cases
// --------------------------------------------------------------------------

function TestCases({
  datasetId,
  items,
  onChanged,
}: {
  datasetId: string;
  items: Awaited<ReturnType<typeof api.dataset>>["items"];
  onChanged: () => void;
}) {
  const [adding, setAdding] = useState(false);
  const [input, setInput] = useState("");
  const [expected, setExpected] = useState("");

  const add = useMutation({
    mutationFn: () =>
      api.addItem(datasetId, { input, expected_output: expected || null }),
    onSuccess: () => {
      setInput("");
      setExpected("");
      setAdding(false);
      onChanged();
    },
  });

  const remove = useMutation({
    mutationFn: (itemId: string) => api.deleteItem(datasetId, itemId),
    onSuccess: onChanged,
  });

  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-xs font-medium uppercase tracking-wide text-neutral-500">
          Test cases ({items.length})
        </h2>
        <button
          onClick={() => setAdding((v) => !v)}
          className="text-xs text-neutral-400 hover:text-neutral-200"
        >
          {adding ? "Cancel" : "+ Add"}
        </button>
      </div>

      {adding && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            add.mutate();
          }}
          className="mb-2 space-y-2 rounded-xl border border-neutral-800 bg-neutral-900 p-3"
        >
          <textarea
            autoFocus
            value={input}
            onChange={(e) => setInput(e.target.value)}
            rows={3}
            placeholder="Input"
            className="w-full resize-y rounded-lg border border-neutral-700 bg-neutral-950 px-2.5 py-2 text-[12px] outline-none focus:border-neutral-500"
          />
          <textarea
            value={expected}
            onChange={(e) => setExpected(e.target.value)}
            rows={2}
            placeholder="Expected output (optional)"
            className="w-full resize-y rounded-lg border border-neutral-700 bg-neutral-950 px-2.5 py-2 text-[12px] outline-none focus:border-neutral-500"
          />
          <button
            type="submit"
            disabled={!input.trim() || add.isPending}
            className="rounded-lg btn-primary px-3 py-1.5 text-xs font-medium disabled:opacity-40"
          >
            Add case
          </button>
        </form>
      )}

      {items.length === 0 ? (
        <div className="rounded-xl border border-dashed border-neutral-800 px-4 py-8 text-center text-xs text-neutral-500">
          No test cases. Add one above, or save one from a trace.
        </div>
      ) : (
        <ul className="space-y-2">
          {items.map((item, i) => (
            <li
              key={item.id}
              className="rounded-xl border border-neutral-800 bg-neutral-900 p-3"
            >
              <div className="flex items-start justify-between gap-2">
                <span className="shrink-0 font-mono text-[11px] text-neutral-600">
                  {i + 1}
                </span>
                <div className="min-w-0 flex-1 space-y-1.5">
                  <p className="line-clamp-3 whitespace-pre-wrap break-words text-[12px] text-neutral-300">
                    {item.input}
                  </p>
                  {item.expected_output && (
                    <p className="line-clamp-2 whitespace-pre-wrap break-words text-[11px] text-neutral-500">
                      <span className="text-neutral-600">expected: </span>
                      {item.expected_output}
                    </p>
                  )}
                  {item.source_trace_id && (
                    <Link
                      href={`/traces/${item.source_trace_id}`}
                      className="inline-block text-[11px] text-sky-400 hover:underline"
                    >
                      from trace {item.source_trace_id.slice(0, 12)}…
                    </Link>
                  )}
                </div>
                <button
                  onClick={() => remove.mutate(item.id)}
                  className="shrink-0 text-[11px] text-neutral-600 hover:text-red-400"
                  aria-label="Delete test case"
                >
                  ✕
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
