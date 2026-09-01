"use client";

import { useModels } from "@/lib/use-models";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";
import { Shell } from "../../shell";
import { VersionHistory } from "@/components/version-history";
import {
  api,
  ApiError,
  INPUT_PLACEHOLDER,
  type PromptVersion,
} from "@/lib/api";

export default function PromptDetailPage() {
  return (
    <Shell>
      <PromptDetail />
    </Shell>
  );
}

const FIELD =
  "w-full rounded-lg border border-neutral-700 bg-neutral-950 px-2.5 py-1.5 text-xs outline-none focus:border-neutral-500";

function PromptDetail() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [renaming, setRenaming] = useState(false);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["prompt", params.id],
    queryFn: () => api.prompt(params.id),
  });

  const archive = useMutation({
    mutationFn: () => api.archivePrompt(params.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["prompts"] });
      router.push("/prompts");
    },
  });

  if (isLoading) {
    return <p className="py-10 text-center text-sm text-neutral-500">Loading prompt…</p>;
  }
  if (isError || !data) {
    return (
      <div className="py-10 text-center">
        <p className="text-sm text-red-400">Prompt not found.</p>
        <Link href="/prompts" className="mt-3 inline-block text-xs text-neutral-400 underline">
          Back to prompts
        </Link>
      </div>
    );
  }

  const latest = data.versions[0];

  return (
    <div className="space-y-5">
      <div>
        <Link href="/prompts" className="text-xs text-neutral-500 hover:text-neutral-300">
          ← Prompts
        </Link>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <h1 className="text-base font-semibold">{data.name}</h1>
          <span className="rounded bg-neutral-800 px-1.5 py-0.5 font-mono text-[10px] text-neutral-300">
            v{latest?.version ?? 1}
          </span>
          {data.archived && (
            <span className="rounded bg-amber-950 px-1.5 py-0.5 text-[10px] text-amber-300">
              archived
            </span>
          )}
        </div>
        {data.description && (
          <p className="mt-1 text-xs text-neutral-500">{data.description}</p>
        )}
      </div>

      {renaming ? (
        <RenameForm
          promptId={params.id}
          name={data.name}
          description={data.description}
          onDone={() => setRenaming(false)}
        />
      ) : (
        <div className="flex flex-wrap gap-3">
          {!editing && (
            <button
              onClick={() => setEditing(true)}
              className="rounded-lg btn-primary px-3 py-1.5 text-xs font-medium"
            >
              Edit → new version
            </button>
          )}
          <button
            onClick={() => setRenaming(true)}
            className="text-xs text-neutral-500 hover:text-neutral-300"
          >
            Rename
          </button>
          <button
            onClick={() => archive.mutate()}
            disabled={archive.isPending}
            className="ml-auto text-xs text-neutral-600 hover:text-red-400"
          >
            Archive
          </button>
        </div>
      )}

      {editing && latest && (
        <VersionEditor
          promptId={params.id}
          latest={latest}
          onDone={() => setEditing(false)}
        />
      )}

      {!editing && latest && (
        <div>
          <h2 className="mb-2 text-xs font-medium uppercase tracking-wide text-neutral-500">
            Current — v{latest.version}
          </h2>
          <pre className="overflow-auto whitespace-pre-wrap break-words rounded-xl border border-neutral-800 bg-neutral-900 px-3 py-2.5 font-mono text-[11px] text-neutral-300">
            {latest.template}
          </pre>
        </div>
      )}

      <div>
        <h2 className="mb-2 text-xs font-medium uppercase tracking-wide text-neutral-500">
          History ({data.versions.length})
        </h2>
        <VersionHistory promptId={params.id} />
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Editing
// --------------------------------------------------------------------------

/**
 * Editing writes a new version rather than changing the current one.
 *
 * Prefilled with the latest version so an edit is a tweak, not a retype, and
 * so the diff the history shows is the change you actually made.
 */
function VersionEditor({
  promptId,
  latest,
  onDone,
}: {
  promptId: string;
  latest: PromptVersion;
  onDone: () => void;
}) {
  const queryClient = useQueryClient();
  const [template, setTemplate] = useState(latest.template);
  const [note, setNote] = useState("");
  const [model, setModel] = useState(String(latest.config.model ?? ""));
  const [maxTokens, setMaxTokens] = useState(Number(latest.config.max_tokens ?? 1024));

  const models = useModels();
  const effectiveModel = models.includes(model) ? model : (models[0] ?? "");

  const save = useMutation({
    mutationFn: () =>
      api.addPromptVersion(promptId, {
        template,
        config: { model: effectiveModel, max_tokens: maxTokens },
        note,
      }),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ["prompt", promptId] });
      queryClient.invalidateQueries({ queryKey: ["prompts"] });
      // A save that changed nothing returns the existing version instead of
      // minting a duplicate. Say so rather than closing silently, which would
      // look identical to a successful edit.
      if (result.created) onDone();
    },
  });

  const missingPlaceholder = !template.includes(INPUT_PLACEHOLDER);
  const unchanged = save.data && !save.data.created;

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
      className="space-y-3 rounded-xl border border-neutral-800 bg-neutral-900 p-3.5"
    >
      <div>
        <label className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
          Prompt — saving creates v{latest.version + 1}
        </label>
        <textarea
          autoFocus
          value={template}
          onChange={(e) => setTemplate(e.target.value)}
          rows={12}
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

      <label className="block">
        <span className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
          What changed (optional)
        </span>
        <input
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="Warmer tone, force a next step"
          className={FIELD}
        />
      </label>

      {unchanged && (
        <p className="text-[11px] text-amber-400">
          Nothing changed, so no version was created — v{save.data?.version} is
          still the latest.
        </p>
      )}
      {save.isError && (
        <p className="text-[11px] text-red-400">
          {save.error instanceof ApiError
            ? save.error.message
            : "Could not save the version."}
        </p>
      )}

      <div className="flex items-center gap-3">
        <button
          type="submit"
          disabled={missingPlaceholder || !effectiveModel || save.isPending}
          className="rounded-lg btn-primary px-3 py-1.5 text-xs font-medium disabled:opacity-40"
        >
          {save.isPending ? "Saving…" : `Save as v${latest.version + 1}`}
        </button>
        <button
          type="button"
          onClick={onDone}
          className="text-xs text-neutral-500 hover:text-neutral-300"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}

function RenameForm({
  promptId,
  name,
  description,
  onDone,
}: {
  promptId: string;
  name: string;
  description: string;
  onDone: () => void;
}) {
  const queryClient = useQueryClient();
  const [draftName, setDraftName] = useState(name);
  const [draftDescription, setDraftDescription] = useState(description);

  const save = useMutation({
    mutationFn: () =>
      api.updatePrompt(promptId, { name: draftName, description: draftDescription }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["prompt", promptId] });
      queryClient.invalidateQueries({ queryKey: ["prompts"] });
      onDone();
    },
  });

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
      className="space-y-2 rounded-xl border border-neutral-800 bg-neutral-900 p-3.5"
    >
      <div className="grid gap-2 sm:grid-cols-2">
        <input
          autoFocus
          value={draftName}
          onChange={(e) => setDraftName(e.target.value)}
          className={FIELD}
        />
        <input
          value={draftDescription}
          onChange={(e) => setDraftDescription(e.target.value)}
          placeholder="Description"
          className={FIELD}
        />
      </div>
      {/* Renaming is not a version. Nothing about a past result changes because
          the artifact was renamed, and filling the history with diffs that
          changed no behaviour would bury the ones that did. */}
      <p className="text-[11px] text-neutral-500">
        Renaming does not create a version — it changes nothing a run would send.
      </p>
      {save.isError && (
        <p className="text-[11px] text-red-400">
          {save.error instanceof ApiError ? save.error.message : "Could not rename."}
        </p>
      )}
      <div className="flex items-center gap-3">
        <button
          type="submit"
          disabled={!draftName.trim() || save.isPending}
          className="rounded-lg btn-primary px-3 py-1.5 text-xs font-medium disabled:opacity-40"
        >
          Save
        </button>
        <button
          type="button"
          onClick={onDone}
          className="text-xs text-neutral-500 hover:text-neutral-300"
        >
          Cancel
        </button>
      </div>
    </form>
  );
}
