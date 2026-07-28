"use client";

/**
 * Version history and diff for one prompt — step 5.
 *
 * Shared by the prompt detail page and the scorer list, because a scorer's
 * definition is a prompt of kind 'scorer' and its history is the same object.
 * Two renderings of the same chain would start disagreeing about it the first
 * time either one changed.
 *
 * Labels are hidden for scorers (`showLabels`). A label is a pointer a run
 * resolves at start; nothing resolves a scorer that way — it always judges with
 * its current definition — so a promote button there would be a control that
 * quietly does nothing.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import {
  api,
  ApiError,
  type DiffLine,
  type PromptVersion,
  relativeTime,
} from "@/lib/api";

export function VersionHistory({
  promptId,
  showLabels = true,
}: {
  promptId: string;
  showLabels?: boolean;
}) {
  const { data, isLoading } = useQuery({
    queryKey: ["prompt", promptId],
    queryFn: () => api.prompt(promptId),
  });

  // Which two versions the diff compares. Null means "not chosen yet", which
  // resolves below to the newest pair — the comparison you almost always want
  // and would otherwise have to make by hand on every visit.
  const [fromId, setFromId] = useState<string | null>(null);
  const [toId, setToId] = useState<string | null>(null);

  if (isLoading) {
    return <p className="py-4 text-center text-[11px] text-neutral-500">Loading history…</p>;
  }
  if (!data) return null;

  const versions = data.versions;
  const to = toId ?? versions[0]?.id ?? null;
  const from = fromId ?? versions[1]?.id ?? null;

  return (
    <div className="space-y-3">
      {showLabels && (
        <Labels promptId={promptId} labels={data.labels} versions={versions} />
      )}

      {versions.length > 1 && from && to && (
        <DiffPane
          promptId={promptId}
          versions={versions}
          fromId={from}
          toId={to}
          onFrom={setFromId}
          onTo={setToId}
        />
      )}

      <ul className="space-y-1.5">
        {versions.map((v) => (
          <VersionRow
            key={v.id}
            version={v}
            labels={data.labels
              .filter((l) => l.version_id === v.id)
              .map((l) => l.label)}
          />
        ))}
      </ul>
    </div>
  );
}

// --------------------------------------------------------------------------
// Labels
// --------------------------------------------------------------------------

function Labels({
  promptId,
  labels,
  versions,
}: {
  promptId: string;
  labels: { label: string; version_id: string; version: number }[];
  versions: PromptVersion[];
}) {
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("production");
  const [versionId, setVersionId] = useState(versions[0]?.id ?? "");

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["prompt", promptId] });
    queryClient.invalidateQueries({ queryKey: ["prompts"] });
  };

  const promote = useMutation({
    mutationFn: () => api.setPromptLabel(promptId, name.trim().toLowerCase(), versionId),
    onSuccess: () => {
      setAdding(false);
      invalidate();
    },
  });

  const remove = useMutation({
    mutationFn: (label: string) => api.removePromptLabel(promptId, label),
    onSuccess: invalidate,
  });

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-1.5">
        {labels.map((l) => (
          <span
            key={l.label}
            className="flex items-center gap-1 rounded bg-sky-950 px-1.5 py-0.5 text-[10px] text-sky-300"
          >
            {l.label} → v{l.version}
            <button
              onClick={() => remove.mutate(l.label)}
              className="text-sky-500 hover:text-red-400"
              aria-label={`Remove label ${l.label}`}
            >
              ✕
            </button>
          </span>
        ))}
        <button
          onClick={() => setAdding((v) => !v)}
          className="text-[11px] text-neutral-500 hover:text-neutral-300"
        >
          {adding ? "Cancel" : labels.length ? "+ Move a label" : "+ Add a label"}
        </button>
      </div>

      {adding && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            promote.mutate();
          }}
          className="flex flex-wrap items-end gap-2 rounded-lg border border-neutral-800 bg-neutral-950 p-2.5"
        >
          <label className="block">
            <span className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
              Label
            </span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="production"
              className="w-32 rounded-lg border border-neutral-700 bg-neutral-950 px-2 py-1 text-xs outline-none focus:border-neutral-500"
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
              Points at
            </span>
            <select
              value={versionId}
              onChange={(e) => setVersionId(e.target.value)}
              className="rounded-lg border border-neutral-700 bg-neutral-950 px-2 py-1 text-xs outline-none focus:border-neutral-500"
            >
              {versions.map((v) => (
                <option key={v.id} value={v.id}>
                  v{v.version}
                </option>
              ))}
            </select>
          </label>
          <button
            type="submit"
            disabled={!name.trim() || !versionId || promote.isPending}
            className="rounded-lg border border-neutral-700 px-2.5 py-1 text-[11px] text-neutral-200 disabled:opacity-40"
          >
            {promote.isPending ? "Moving…" : "Set"}
          </button>
          {promote.isError && (
            <p className="w-full text-[11px] text-red-400">
              {promote.error instanceof ApiError
                ? promote.error.message
                : "Could not set the label."}
            </p>
          )}
        </form>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// Diff
// --------------------------------------------------------------------------

function DiffPane({
  promptId,
  versions,
  fromId,
  toId,
  onFrom,
  onTo,
}: {
  promptId: string;
  versions: PromptVersion[];
  fromId: string;
  toId: string;
  onFrom: (id: string) => void;
  onTo: (id: string) => void;
}) {
  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["prompt-diff", promptId, fromId, toId],
    queryFn: () => api.promptDiff(promptId, fromId, toId),
  });

  const picker = (value: string, onChange: (id: string) => void, label: string) => (
    <label className="flex items-center gap-1.5">
      <span className="text-[10px] uppercase tracking-wide text-neutral-500">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="rounded-lg border border-neutral-700 bg-neutral-950 px-2 py-1 text-[11px] outline-none focus:border-neutral-500"
      >
        {versions.map((v) => (
          <option key={v.id} value={v.id}>
            v{v.version}
          </option>
        ))}
      </select>
    </label>
  );

  return (
    <div className="rounded-xl border border-neutral-800 bg-neutral-950">
      <div className="flex flex-wrap items-center gap-3 border-b border-neutral-850 px-3 py-2">
        {picker(fromId, onFrom, "from")}
        {picker(toId, onTo, "to")}
        {data && (
          <span className="text-[11px] tabular-nums text-neutral-500">
            <span className="text-emerald-400">+{data.added}</span>{" "}
            <span className="text-red-400">−{data.removed}</span>
          </span>
        )}
      </div>

      {isLoading && (
        <p className="px-3 py-3 text-[11px] text-neutral-500">Comparing…</p>
      )}
      {isError && (
        <p className="px-3 py-3 text-[11px] text-red-400">
          {error instanceof ApiError ? error.message : "Could not build the diff."}
        </p>
      )}

      {data && (
        <>
          {/* Config first. A scorer whose scale went 1-5 to 1-10 has an empty
              text diff and every score before and after means something
              different — reading only the words would miss it entirely. */}
          {data.config_changes.length > 0 && (
            <ul className="space-y-0.5 border-b border-neutral-850 px-3 py-2">
              {data.config_changes.map((c) => (
                <li key={c.key} className="font-mono text-[11px]">
                  <span className="text-neutral-500">{c.key}</span>{" "}
                  <span className="text-red-400">{display(c.from)}</span>
                  <span className="text-neutral-600"> → </span>
                  <span className="text-emerald-400">{display(c.to)}</span>
                </li>
              ))}
            </ul>
          )}

          {data.lines.every((l) => l.op === "equal") ? (
            <p className="px-3 py-3 text-[11px] text-neutral-500">
              {data.config_changes.length > 0
                ? "The prompt text is identical — only settings changed."
                : "These versions are identical."}
            </p>
          ) : (
            <div className="max-h-96 overflow-auto">
              {data.lines.map((line, i) => (
                <DiffRow key={i} line={line} />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

/** JSON for anything that isn't already a bare scalar, so `[]` reads as `[]`. */
function display(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

function DiffRow({ line }: { line: DiffLine }) {
  const tone =
    line.op === "insert"
      ? "bg-emerald-950/40 text-emerald-200"
      : line.op === "delete"
        ? "bg-red-950/40 text-red-200"
        : "text-neutral-400";
  const marker = line.op === "insert" ? "+" : line.op === "delete" ? "−" : " ";

  return (
    <div className={`flex gap-2 px-3 py-px font-mono text-[11px] ${tone}`}>
      <span className="w-8 shrink-0 select-none text-right tabular-nums text-neutral-600">
        {line.right_no ?? line.left_no ?? ""}
      </span>
      <span className="w-2 shrink-0 select-none">{marker}</span>
      {/* An empty line still needs to occupy a row, hence the zero-width
          fallback — otherwise a blank insertion collapses and the line
          numbers on either side of it stop lining up with the text. */}
      <span className="whitespace-pre-wrap break-words">{line.text || "​"}</span>
    </div>
  );
}

// --------------------------------------------------------------------------
// History rows
// --------------------------------------------------------------------------

function VersionRow({
  version,
  labels,
}: {
  version: PromptVersion;
  labels: string[];
}) {
  const [open, setOpen] = useState(false);

  return (
    <li className="rounded-lg border border-neutral-800 bg-neutral-950">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-start gap-2 px-2.5 py-2 text-left"
      >
        <span className="shrink-0 rounded bg-neutral-800 px-1.5 py-0.5 font-mono text-[10px] text-neutral-300">
          v{version.version}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[11px] text-neutral-400">
            {version.note || "No note"}
          </span>
          <span className="mt-0.5 flex flex-wrap gap-x-2 text-[10px] text-neutral-600">
            {version.created_at && (
              <span>{relativeTime(Date.parse(version.created_at) * 1e6)}</span>
            )}
            {/* Whether anything actually ran on it. The difference between a
                version that shaped results and a draft nobody used. */}
            {version.run_count != null && version.run_count > 0 && (
              <span>
                {version.run_count} {version.run_count === 1 ? "run" : "runs"}
              </span>
            )}
            {labels.map((l) => (
              <span key={l} className="text-sky-400">
                {l}
              </span>
            ))}
          </span>
        </span>
      </button>

      {open && (
        <div className="space-y-2 border-t border-neutral-850 px-2.5 py-2">
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-neutral-900 px-2 py-1.5 font-mono text-[11px] text-neutral-400">
            {version.template}
          </pre>
          {Object.keys(version.config).length > 0 && (
            <div className="flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-[10px] text-neutral-500">
              {Object.entries(version.config).map(([key, value]) => (
                <span key={key}>
                  {key}={display(value)}
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </li>
  );
}
