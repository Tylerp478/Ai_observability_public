"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Shell } from "../shell";
import { api } from "@/lib/api";

export default function KeysPage() {
  return (
    <Shell>
      <Keys />
    </Shell>
  );
}

function Keys() {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  // Held in component state only. Once this unmounts the plaintext is gone —
  // the backend stores a hash and cannot show it again.
  const [freshKey, setFreshKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const { data, isLoading } = useQuery({ queryKey: ["keys"], queryFn: api.keys });

  const create = useMutation({
    mutationFn: (n: string) => api.createKey(n),
    onSuccess: (res) => {
      setFreshKey(res.key);
      setName("");
      qc.invalidateQueries({ queryKey: ["keys"] });
    },
  });

  const revoke = useMutation({
    mutationFn: (id: string) => api.revokeKey(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["keys"] }),
  });

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-base font-semibold">API keys</h1>
        <p className="mt-1 text-xs text-neutral-500">
          Used by the SDK to send traces. Not for signing in.
        </p>
      </div>

      {freshKey && (
        <div className="rounded-xl border border-amber-800 bg-amber-950/40 p-3.5">
          <p className="text-xs font-medium text-amber-200">
            Copy this now — it will not be shown again
          </p>
          <code className="mt-2 block overflow-x-auto rounded-lg bg-neutral-950 px-3 py-2 font-mono text-[11px] text-amber-100">
            {freshKey}
          </code>
          <div className="mt-2 flex gap-2">
            <button
              onClick={() => {
                navigator.clipboard.writeText(freshKey);
                setCopied(true);
                setTimeout(() => setCopied(false), 1500);
              }}
              className="rounded-lg bg-amber-200 px-2.5 py-1 text-xs font-medium text-amber-950"
            >
              {copied ? "Copied" : "Copy"}
            </button>
            <button
              onClick={() => setFreshKey(null)}
              className="rounded-lg px-2.5 py-1 text-xs text-amber-300"
            >
              Dismiss
            </button>
          </div>
        </div>
      )}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (name.trim()) create.mutate(name.trim());
        }}
        className="flex gap-2"
      >
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Key name (e.g. laptop)"
          className="min-w-0 flex-1 rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm outline-none focus:border-neutral-500"
        />
        <button
          type="submit"
          disabled={create.isPending || !name.trim()}
          className="shrink-0 rounded-lg btn-primary px-3 py-2 text-sm font-medium disabled:opacity-40"
        >
          Create
        </button>
      </form>

      {isLoading ? (
        <p className="text-sm text-neutral-500">Loading…</p>
      ) : (
        <ul className="space-y-2">
          {(data?.keys ?? []).map((k) => (
            <li
              key={k.id}
              className={`flex items-center justify-between gap-3 rounded-xl border p-3 ${
                k.revoked
                  ? "border-neutral-850 bg-neutral-900/40 opacity-50"
                  : "border-neutral-800 bg-neutral-900"
              }`}
            >
              <div className="min-w-0">
                <p className="truncate text-sm">
                  {k.name}
                  {k.revoked && (
                    <span className="ml-2 text-[10px] uppercase text-neutral-500">
                      revoked
                    </span>
                  )}
                </p>
                <p className="mt-0.5 font-mono text-[11px] text-neutral-500">
                  {k.prefix}…{" "}
                  {k.last_used_at
                    ? `· used ${new Date(k.last_used_at).toLocaleDateString()}`
                    : "· never used"}
                </p>
              </div>
              {!k.revoked && (
                <button
                  onClick={() => revoke.mutate(k.id)}
                  className="shrink-0 rounded-lg border border-neutral-700 px-2.5 py-1 text-xs text-neutral-400 hover:border-red-800 hover:text-red-300"
                >
                  Revoke
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
