"use client";

import { useModels } from "@/lib/use-models";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { Shell } from "../shell";
import {
  api,
  ApiError,
  INPUT_PLACEHOLDER,
  relativeTime,
  type PromptSummary,
} from "@/lib/api";

export default function PromptsPage() {
  return (
    <Shell>
      <Prompts />
    </Shell>
  );
}

const STARTER = `You are a helpful assistant. Answer concisely.

${INPUT_PLACEHOLDER}`;

function Prompts() {
  const [creating, setCreating] = useState(false);
  const { data, isLoading } = useQuery({
    queryKey: ["prompts"],
    queryFn: () => api.prompts("completion"),
  });

  const prompts = data?.prompts ?? [];

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-base font-semibold">Prompts</h1>
        <p className="mt-1 text-xs text-neutral-500">
          Every prompt is a chain of immutable versions. Editing appends; a run
          records the version it sent, so what a result came from stays true no
          matter what the prompt does afterwards.
        </p>
      </div>

      {creating ? (
        <NewPrompt onCancel={() => setCreating(false)} />
      ) : (
        <button
          onClick={() => setCreating(true)}
          className="rounded-lg btn-primary px-3 py-1.5 text-xs font-medium"
        >
          ＋ New prompt
        </button>
      )}

      {isLoading ? (
        <p className="py-10 text-center text-sm text-neutral-500">Loading prompts…</p>
      ) : prompts.length === 0 && !creating ? (
        <div className="rounded-xl border border-dashed border-neutral-800 px-4 py-8 text-center text-xs text-neutral-500">
          No prompts yet. Create one, then pick it when you start a replay —
          the run will cite the exact version it used.
        </div>
      ) : (
        <ul className="space-y-2">
          {prompts.map((p) => (
            <PromptRow key={p.id} prompt={p} />
          ))}
        </ul>
      )}
    </div>
  );
}

function PromptRow({ prompt }: { prompt: PromptSummary }) {
  return (
    <li>
      <Link
        href={`/prompts/${prompt.id}`}
        className="block rounded-xl border border-neutral-800 bg-neutral-900 p-3 transition-colors hover:border-neutral-700"
      >
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-[13px] font-medium">{prompt.name}</span>
              <span className="rounded bg-neutral-800 px-1.5 py-0.5 font-mono text-[10px] text-neutral-300">
                v{prompt.latest_version ?? 1}
              </span>
              {prompt.labels.map((l) => (
                <span
                  key={l.label}
                  className="rounded bg-sky-950 px-1.5 py-0.5 text-[10px] text-sky-300"
                >
                  {l.label} → v{l.version}
                </span>
              ))}
            </div>
            {prompt.description && (
              <p className="mt-1 text-[11px] text-neutral-500">{prompt.description}</p>
            )}
          </div>
        </div>
        <div className="mt-1.5 flex flex-wrap gap-x-3 text-[11px] text-neutral-500">
          <span>
            {prompt.version_count}{" "}
            {prompt.version_count === 1 ? "version" : "versions"}
          </span>
          <span>
            {prompt.run_count} {prompt.run_count === 1 ? "run" : "runs"}
          </span>
          {prompt.updated_at && (
            <span>{relativeTime(Date.parse(prompt.updated_at) * 1e6)}</span>
          )}
        </div>
      </Link>
    </li>
  );
}

// --------------------------------------------------------------------------
// Create
// --------------------------------------------------------------------------

const FIELD =
  "w-full rounded-lg border border-neutral-700 bg-neutral-950 px-2.5 py-1.5 text-xs outline-none focus:border-neutral-500";

function NewPrompt({ onCancel }: { onCancel: () => void }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [template, setTemplate] = useState(STARTER);
  const [model, setModel] = useState("");
  const [maxTokens, setMaxTokens] = useState(1024);

  const models = useModels();
  const effectiveModel = models.includes(model) ? model : (models[0] ?? "");

  const create = useMutation({
    mutationFn: () =>
      api.createPrompt({
        name,
        description,
        template,
        // Model and max_tokens ride along with the text as the version's
        // config, so picking a version in the run form fills in a whole
        // setup rather than just words.
        config: { model: effectiveModel, max_tokens: maxTokens },
        note: "First version.",
      }),
    onSuccess: (created) => {
      queryClient.invalidateQueries({ queryKey: ["prompts"] });
      router.push(`/prompts/${created.id}`);
    },
  });

  const missingPlaceholder = !template.includes(INPUT_PLACEHOLDER);

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        create.mutate();
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
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Support triage"
            className={FIELD}
          />
        </label>
        <label className="block">
          <span className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
            Description
          </span>
          <input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What this prompt is for"
            className={FIELD}
          />
        </label>
      </div>

      <div>
        <label className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
          Prompt
        </label>
        <textarea
          value={template}
          onChange={(e) => setTemplate(e.target.value)}
          rows={8}
          className={`${FIELD} resize-y font-mono text-[12px]`}
        />
        <p
          className={`mt-1 text-[11px] ${
            missingPlaceholder ? "text-amber-400" : "text-neutral-500"
          }`}
        >
          {missingPlaceholder
            ? `Must contain ${INPUT_PLACEHOLDER} — without it every case sends the same request.`
            : `${INPUT_PLACEHOLDER} is replaced with each test case's input.`}
        </p>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <label className="block">
          <span className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
            Default model
          </span>
          <select
            value={effectiveModel}
            onChange={(e) => setModel(e.target.value)}
            className={FIELD}
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
            Default max tokens
          </span>
          <input
            type="number"
            min={1}
            max={32000}
            value={maxTokens}
            onChange={(e) => setMaxTokens(Number(e.target.value))}
            className={`${FIELD} tabular-nums`}
          />
        </label>
      </div>

      {create.isError && (
        <p className="text-[11px] text-red-400">
          {create.error instanceof ApiError
            ? create.error.message
            : "Could not create the prompt."}
        </p>
      )}

      <div className="flex items-center gap-3">
        <button
          type="submit"
          disabled={!name.trim() || !effectiveModel || missingPlaceholder || create.isPending}
          className="rounded-lg btn-primary px-3 py-1.5 text-xs font-medium disabled:opacity-40"
        >
          {create.isPending ? "Creating…" : "Create prompt"}
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
