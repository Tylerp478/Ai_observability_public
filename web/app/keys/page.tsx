"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { Shell } from "../shell";
import { api, ApiError, formatCost, relativeTime } from "@/lib/api";

/**
 * Two things that both look like "connections", kept visibly separate.
 *
 * A key is a credential the SDK uses to *push* spans in. A source is what has
 * actually pushed. They are related — a key identifies which project a source's
 * spans land in — but the page used to show only the first, which made it read
 * as a general credential store and left no way to see what was reporting.
 */
export default function KeysPage() {
  return (
    <Shell>
      <div className="space-y-6">
        <ProviderKeys />
        <Keys />
        <Sources />
      </div>
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
        <h1 className="text-base font-semibold">Ingest keys</h1>
        <p className="mt-1 text-xs text-neutral-500">
          A key lets the SDK send traces in. It is not a login, and it is not a
          credential for anything this app reads from — traffic is pushed here,
          never pulled.
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

// --------------------------------------------------------------------------
// Provider keys
// --------------------------------------------------------------------------

/**
 * The Anthropic keys this app spends on — the opposite direction from an
 * ingest key.
 *
 * An ingest key lets something else write *to* us; a provider key lets us call
 * *out*, on your money. They share a page because "keys" is where you'd look
 * for either, and they are labelled apart because confusing them is expensive
 * in one direction only.
 */
function ProviderKeys() {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [secret, setSecret] = useState("");
  const [adding, setAdding] = useState(false);

  const { data, isLoading } = useQuery({
    queryKey: ["credentials"],
    queryFn: api.credentials,
  });
  const credentials = data?.credentials ?? [];

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["credentials"] });
  };

  const create = useMutation({
    mutationFn: () => api.createCredential({ name: name.trim(), secret: secret.trim() }),
    onSuccess: () => {
      setName("");
      setSecret("");
      setAdding(false);
      invalidate();
    },
  });

  const promote = useMutation({
    mutationFn: (id: string) => api.setDefaultCredential(id),
    onSuccess: invalidate,
  });

  const archive = useMutation({
    mutationFn: (id: string) => api.archiveCredential(id),
    onSuccess: invalidate,
  });

  const mutationError = create.error ?? promote.error ?? archive.error;

  return (
    <div className="space-y-3">
      <div>
        <h2 className="text-base font-semibold">Provider keys</h2>
        <p className="mt-1 text-xs text-neutral-500">
          The Anthropic keys this app spends on. Runs, scorers, guardrails and the
          Playground bill to the default unless you pick another. Stored encrypted;
          the key itself is never shown again after you save it.
        </p>
      </div>

      {isLoading ? (
        <p className="text-sm text-neutral-500">Loading…</p>
      ) : (
        <ul className="space-y-2">
          {credentials.map((c) => (
            <li
              key={c.id}
              className="flex items-center justify-between gap-3 rounded-xl border border-neutral-800 bg-neutral-900 p-3"
            >
              <div className="min-w-0">
                <p className="truncate text-sm">
                  {c.name}
                  {c.is_default && (
                    <span className="ml-2 rounded bg-sky-900 px-1.5 py-0.5 text-[10px] uppercase text-sky-400">
                      default
                    </span>
                  )}
                </p>
                <p className="mt-0.5 font-mono text-[11px] text-neutral-500">
                  ···{c.last4}
                  {" · "}
                  {formatCost(c.spend_usd)} spent
                  {c.last_used_at
                    ? ` · used ${new Date(c.last_used_at).toLocaleDateString()}`
                    : " · never used"}
                </p>
              </div>
              <div className="flex shrink-0 gap-2">
                {!c.is_default && (
                  <button
                    onClick={() => promote.mutate(c.id)}
                    className="rounded-lg border border-neutral-700 px-2.5 py-1 text-xs text-neutral-400 hover:border-neutral-500 hover:text-neutral-200"
                  >
                    Make default
                  </button>
                )}
                <button
                  onClick={() => archive.mutate(c.id)}
                  className="rounded-lg border border-neutral-700 px-2.5 py-1 text-xs text-neutral-400 hover:border-red-800 hover:text-red-300"
                >
                  Remove
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {mutationError && (
        <p className="text-xs text-red-400">
          {mutationError instanceof ApiError
            ? mutationError.message
            : "Something went wrong."}
        </p>
      )}

      {adding ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (name.trim() && secret.trim()) create.mutate();
          }}
          className="space-y-2 rounded-xl border border-neutral-800 bg-neutral-900 p-3"
        >
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Name (e.g. Work account)"
            className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm outline-none focus:border-neutral-500"
          />
          {/* type=password so a key does not sit in plain view on screen, and
              autoComplete off so the browser never offers to remember it. */}
          <input
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
            type="password"
            autoComplete="off"
            placeholder="sk-ant-…"
            className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2 font-mono text-sm outline-none focus:border-neutral-500"
          />
          <div className="flex items-center gap-3">
            <button
              type="submit"
              disabled={create.isPending || !name.trim() || !secret.trim()}
              className="rounded-lg btn-primary px-3 py-1.5 text-xs font-medium disabled:opacity-40"
            >
              {create.isPending ? "Checking…" : "Save key"}
            </button>
            <button
              type="button"
              onClick={() => {
                setAdding(false);
                setSecret("");
                create.reset();
              }}
              className="text-xs text-neutral-500 hover:text-neutral-300"
            >
              Cancel
            </button>
            {/* Says what "Save" does before it does it: a rejected key is never
                stored, so this is a check, not a write-then-discover. */}
            <span className="text-[11px] text-neutral-500">
              Verified with Anthropic before saving
            </span>
          </div>
        </form>
      ) : (
        <button
          onClick={() => setAdding(true)}
          className="rounded-lg border border-neutral-700 px-2.5 py-1.5 text-xs text-neutral-300 hover:border-neutral-500"
        >
          Add a key
        </button>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------
// Sources
// --------------------------------------------------------------------------

/**
 * What is actually reporting in.
 *
 * Derived from span data rather than a table you register things in: a source
 * is the OTLP resource's `service.name`, so a new app appears here the moment
 * it sends its first span and nothing has to be configured twice. The internal
 * ones (obs-runner, obs-judge, obs-guardrail, obs-playground) are the
 * backend's own LLM traffic, and they are listed rather than hidden because
 * they cost real money on the same key.
 */
function Sources() {
  const { data, isLoading } = useQuery({ queryKey: ["sources"], queryFn: api.sources });
  const sources = data?.sources ?? [];

  return (
    <div className="space-y-3">
      <div>
        <h2 className="text-base font-semibold">Sources</h2>
        <p className="mt-1 text-xs text-neutral-500">
          Everything sending spans to this project. Set{" "}
          <code className="font-mono text-[11px] text-neutral-400">OBS_SERVICE_NAME</code>{" "}
          in an instrumented app to name it here, then filter the{" "}
          <Link href="/" className="text-sky-400 hover:underline">
            Overview
          </Link>{" "}
          by it.
        </p>
      </div>

      {isLoading ? (
        <p className="text-sm text-neutral-500">Loading…</p>
      ) : sources.length === 0 ? (
        <div className="rounded-xl border border-dashed border-neutral-800 px-6 py-8 text-center">
          <p className="text-sm text-neutral-300">Nothing has reported in yet</p>
          <p className="mt-2 text-xs text-neutral-500">
            Create a key above, then point the SDK at this backend.
          </p>
        </div>
      ) : (
        <ul className="space-y-2">
          {sources.map((s) => (
            <li
              key={s.name}
              className="flex items-center justify-between gap-3 rounded-xl border border-neutral-800 bg-neutral-900 p-3"
            >
              <div className="min-w-0">
                <p className="truncate text-sm">{s.name}</p>
                <p className="mt-0.5 text-[11px] text-neutral-500">
                  {s.span_count.toLocaleString()} span{s.span_count === 1 ? "" : "s"}
                  {s.last_span_unix_nano > 0 &&
                    ` · last ${relativeTime(s.last_span_unix_nano)}`}
                </p>
              </div>
              <Link
                href={`/traces?source=${encodeURIComponent(s.name)}`}
                className="shrink-0 rounded-lg border border-neutral-700 px-2.5 py-1 text-xs text-neutral-400 hover:border-neutral-500 hover:text-neutral-200"
              >
                Traces
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
