"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { Suspense } from "react";
import { Shell } from "../shell";
import {
  CredentialFilter,
  SortPicker,
  SourceFilter,
  StatusFilter,
  useClearParams,
  useCredentialParam,
  useSortParam,
  useSourceParam,
  useStatusParam,
} from "@/components/source-filter";
import { api, categoryLabel, formatCost, formatDuration, relativeTime } from "@/lib/api";

export default function TracesPage() {
  return (
    <Shell>
      {/* useSearchParams needs a Suspense boundary above it or the page
          cannot be prerendered. */}
      <Suspense fallback={null}>
        <TraceList />
      </Suspense>
    </Shell>
  );
}

function TraceList() {
  const [source, setSource] = useSourceParam();
  const [credential, setCredential] = useCredentialParam();
  const [status, setStatus] = useStatusParam();
  const [sort, setSort] = useSortParam();
  const clearParams = useClearParams();

  const { data, isLoading, isError } = useQuery({
    queryKey: ["traces", source, credential, status, sort],
    queryFn: () => api.traces(50, source, credential, status, sort),
    refetchInterval: 10_000,
  });

  const traces = data?.traces ?? [];
  const totalCost = traces.reduce((sum, t) => sum + t.cost_usd, 0);
  const filtered = Boolean(source || credential || status);

  // One call, not three setters — see useClearParams. Sort is deliberately
  // absent: it hides nothing, so clearing it would undo a choice the user did
  // not complain about.
  const clearFilters = () => clearParams(["source", "credential", "status"]);

  // The header renders in every state, including empty. An early return for
  // "no traces" would take the source picker off the page at exactly the
  // moment the filter is the reason the page is empty, leaving no way back
  // except editing the URL.
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-base font-semibold">Traces</h1>
        {!isLoading && !isError && (
          <p className="text-xs text-neutral-500">
            {traces.length} · {formatCost(totalCost)} total
          </p>
        )}
      </div>

      {/* Their own row rather than crowding the title. Four controls plus a
          count do not fit beside a heading on a 375px phone, and wrapping them
          around it interleaves the count between two selects. */}
      <div className="flex flex-wrap items-center gap-2">
        <CredentialFilter value={credential} onChange={setCredential} />
        <SourceFilter value={source} onChange={setSource} />
        <StatusFilter value={status} onChange={setStatus} />
        {/* Pushed to the far end on a wide screen: sort is not a filter, and
            the gap is what says so. Falls back into the flow when the row
            wraps. */}
        <div className="sm:ml-auto">
          <SortPicker value={sort} onChange={setSort} />
        </div>
      </div>

      {isLoading && (
        <p className="py-10 text-center text-sm text-neutral-500">Loading traces…</p>
      )}
      {isError && (
        <p className="py-10 text-center text-sm text-red-400">Failed to load traces.</p>
      )}

      {!isLoading && !isError && traces.length === 0 && (
        <div className="rounded-xl border border-dashed border-neutral-800 px-6 py-12 text-center">
          {filtered ? (
            <>
              <p className="text-sm text-neutral-300">
                No traces matching{" "}
                {[
                  source,
                  credential,
                  status === "error"
                    ? "errors only"
                    : status === "ok"
                      ? "no errors"
                      : "",
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </p>
              <button
                onClick={clearFilters}
                className="mt-3 rounded-lg border border-neutral-700 px-2.5 py-1 text-xs text-neutral-400 hover:border-neutral-500 hover:text-neutral-200"
              >
                Clear filters
              </button>
            </>
          ) : (
            <>
              <p className="text-sm text-neutral-300">No traces yet</p>
              <p className="mt-2 text-xs text-neutral-500">Emit one from the SDK:</p>
              <code className="mt-3 block overflow-x-auto rounded-lg bg-neutral-900 px-3 py-2 text-left font-mono text-[11px] text-neutral-400">
                cd sdk &amp;&amp; OBS_EXPORTER=otlp uv run examples/agent_trace.py
              </code>
            </>
          )}
        </div>
      )}

      {/* Cards rather than a table. A table with six columns forces horizontal
          scrolling on a phone, and CLAUDE.md calls out the trace list as
          needing to work there specifically. */}
      <ul className="space-y-2">
        {traces.map((t) => (
          <li key={t.trace_id}>
            <Link
              href={`/traces/${t.trace_id}`}
              className="block rounded-xl border border-neutral-800 bg-neutral-900 p-3.5 transition-colors hover:border-neutral-700 active:bg-neutral-850"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    {t.has_error ? (
                      <span className="shrink-0 rounded bg-red-950 px-1.5 py-0.5 text-[10px] font-medium text-red-300">
                        ERROR
                      </span>
                    ) : null}
                    {/* Promoted out of the metadata line below, where it was
                        plain grey text in fourth position. Which subsystem ran
                        a trace is the first thing you want to know about it —
                        a judge call and a playground call are different kinds
                        of thing, not different values of one. */}
                    {t.service_name && (
                      <span className="shrink-0 rounded bg-neutral-800 px-1.5 py-0.5 text-[10px] text-neutral-300">
                        {categoryLabel(t.service_name)}
                      </span>
                    )}
                    <span className="truncate text-sm font-medium">{t.root_name}</span>
                  </div>
                  <p className="mt-1 truncate font-mono text-[11px] text-neutral-500">
                    {t.trace_id.slice(0, 16)}…
                  </p>
                </div>
                <div className="shrink-0 text-right">
                  <p className="text-sm tabular-nums">{formatDuration(t.duration_ms)}</p>
                  <p className="text-[11px] text-neutral-500">
                    {relativeTime(t.start_time_unix_nano)}
                  </p>
                </div>
              </div>

              <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-neutral-500">
                <span>{t.span_count} spans</span>
                {t.cost_usd > 0 && (
                  <span className="text-neutral-400">{formatCost(t.cost_usd)}</span>
                )}
                {t.input_tokens + t.output_tokens > 0 && (
                  <span>
                    {t.input_tokens} in / {t.output_tokens} out
                  </span>
                )}
              </div>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
