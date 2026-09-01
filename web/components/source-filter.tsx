"use client";

import { useQuery } from "@tanstack/react-query";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback } from "react";
import { api } from "@/lib/api";

/**
 * The selected source, held in the URL rather than in component state.
 *
 * Three things fall out of that for free: a refresh keeps the filter, a link
 * carries it to whoever you send it to, and the Overview and Traces pages read
 * the same key so the filter means one thing across the app.
 *
 * `replace`, not `push`. Changing a filter is not navigation, and stacking a
 * history entry per click makes the back button walk through every dropdown
 * change instead of leaving the page.
 */
function useFilterParam(key: string): [string, (next: string) => void] {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();

  const value = params.get(key) ?? "";

  const setValue = useCallback(
    (next: string) => {
      const query = new URLSearchParams(params.toString());
      if (next) {
        query.set(key, next);
      } else {
        // Deleted rather than set empty, so the clean state has a clean URL.
        query.delete(key);
      }
      const qs = query.toString();
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [key, params, pathname, router],
  );

  return [value, setValue];
}

/**
 * Clear several filters in one navigation.
 *
 * Not a loop over the individual setters. Each of those builds its next URL
 * from the `params` it captured at render, so calling three in a row starts all
 * three from the same snapshot and only the last one survives — which reads as
 * "Clear filters cleared one filter and put the others back". One
 * URLSearchParams, one replace, no interleaving.
 */
export function useClearParams(): (keys: string[]) => void {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();

  return useCallback(
    (keys: string[]) => {
      const query = new URLSearchParams(params.toString());
      for (const key of keys) query.delete(key);
      const qs = query.toString();
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    },
    [params, pathname, router],
  );
}

export function useSourceParam(): [string, (next: string) => void] {
  return useFilterParam("source");
}

/**
 * Which provider key's spend to show.
 *
 * A separate axis from source, not an alternative to it: source is *who sent
 * the span*, credential is *whose money paid for it*. An eval run and a
 * guardrail check can share a key while coming from different services, and
 * two apps can share a service name while billing to different accounts. Both
 * filters compose, and the URL carries both.
 */
export function useCredentialParam(): [string, (next: string) => void] {
  return useFilterParam("credential");
}

/** Whether to show only errored traces, only clean ones, or both. */
export function useStatusParam(): [string, (next: string) => void] {
  return useFilterParam("status");
}

/**
 * Which order the trace list comes back in.
 *
 * A URL param like the filters, and for the same reasons — but it is not a
 * filter, and the UI keeps it visually apart so nobody reads "highest cost" as
 * "only expensive ones".
 */
export function useSortParam(): [string, (next: string) => void] {
  return useFilterParam("sort");
}

// --- time range -----------------------------------------------------------

/**
 * The windows the dashboard offers.
 *
 * `hours` is the API's parameter; the backend picks the series bucket width
 * from it and reports that back, so nothing here needs to know that 30 days
 * means daily points.
 *
 * `refetchMs` scales with the window on purpose. Polling a 30-day rollup every
 * 30 seconds spends a full Parquet scan to move a line by less than a pixel —
 * the newest bucket on that range is a day wide and will not visibly change
 * within one. Short windows stay fast because that is where a live dashboard
 * earns its keep.
 */
export const RANGES = [
  { id: "1h", short: "1h", label: "1 hour", hours: 1, refetchMs: 15_000 },
  { id: "6h", short: "6h", label: "6 hours", hours: 6, refetchMs: 30_000 },
  { id: "24h", short: "24h", label: "24 hours", hours: 24, refetchMs: 30_000 },
  { id: "7d", short: "7d", label: "7 days", hours: 168, refetchMs: 300_000 },
  { id: "30d", short: "30d", label: "30 days", hours: 720, refetchMs: 900_000 },
] as const;

export type Range = (typeof RANGES)[number];

/** 24 hours: long enough to hold a full daily cycle, short enough that the
 *  newest bucket still moves while you watch it. */
export const DEFAULT_RANGE: Range = RANGES[2];

/**
 * What a window is called, given its length in hours.
 *
 * Takes hours rather than a Range so callers can label the window the data on
 * screen actually describes — `data.window_hours` — instead of the one
 * currently selected. Those differ for one render while a longer range is
 * still loading, and a heading that changes before its numbers do is a
 * heading that has lied at least once.
 */
export function rangeLabel(hours: number): string {
  return RANGES.find((r) => r.hours === hours)?.label ?? `${hours} hours`;
}

/**
 * How far back the dashboard looks.
 *
 * In the URL alongside the filters, so a link carries the window as well as
 * what was filtered inside it. An unrecognised value falls back to the default
 * rather than erroring, which is how every other param here treats a URL
 * somebody typed by hand.
 */
export function useRangeParam(): [Range, (next: string) => void] {
  const [value, setValue] = useFilterParam("range");

  const setRange = useCallback(
    // The default clears the param instead of writing it, matching the
    // filters: the clean state has a clean URL.
    (next: string) => setValue(next === DEFAULT_RANGE.id ? "" : next),
    [setValue],
  );

  return [RANGES.find((r) => r.id === value) ?? DEFAULT_RANGE, setRange];
}

/**
 * Range picker — a segmented control, not a select like the filters.
 *
 * Five short options that never grow, on the control people reach for most,
 * is exactly the case a segmented control is for: the choices are visible
 * without opening anything and switching costs one tap. The whole row measures
 * ~190px, so it fits a 375px phone beside the key picker.
 *
 * `aria-pressed` rather than a radiogroup: these are buttons that re-scope the
 * page, and each one reads as "24h, pressed" on its own.
 */
export function RangePicker({
  value,
  onChange,
}: {
  value: Range;
  onChange: (next: string) => void;
}) {
  return (
    <div
      role="group"
      aria-label="Time range"
      className="flex shrink-0 items-center gap-0.5 rounded-lg border border-neutral-700 bg-neutral-950 p-0.5"
    >
      {RANGES.map((r) => {
        const active = r.id === value.id;
        return (
          <button
            key={r.id}
            type="button"
            onClick={() => onChange(r.id)}
            aria-pressed={active}
            // The abbreviation is the label; the title spells it out for
            // anyone who reads "7d" as ambiguous.
            title={`Last ${r.label}`}
            className={`rounded-[6px] px-2 py-1 text-[11px] font-medium tabular-nums ${
              active
                ? "bg-sky-900 text-sky-400"
                : "text-neutral-400 hover:text-neutral-100"
            }`}
          >
            {r.short}
          </button>
        );
      })}
    </div>
  );
}

// Shared by both selects below and by the source and credential pickers above.
const SELECT =
  "max-w-[190px] truncate rounded-lg border border-neutral-700 bg-neutral-950 px-2.5 py-1.5 text-xs text-neutral-300 outline-none focus:border-neutral-500";

/**
 * Error filter.
 *
 * Three states rather than a checkbox. "Only errors" is the common one, but
 * "no errors" earns its place next to a cost sort: the most expensive trace in
 * a window is often an expensive failure, and hiding those is how you find the
 * most expensive thing that actually worked.
 */
export function StatusFilter({
  value,
  onChange,
}: {
  value: string;
  onChange: (next: string) => void;
}) {
  return (
    <label className="flex items-center gap-2">
      <span className="sr-only">Filter by status</span>
      <select value={value} onChange={(e) => onChange(e.target.value)} className={SELECT}>
        <option value="">All statuses</option>
        <option value="error">Errors only</option>
        <option value="ok">No errors</option>
      </select>
    </label>
  );
}

/**
 * Sort order. The backend owns the ordering — see TRACE_SORTS in query.py —
 * so these values are the contract and not just labels.
 */
export function SortPicker({
  value,
  onChange,
}: {
  value: string;
  onChange: (next: string) => void;
}) {
  return (
    <label className="flex items-center gap-1.5">
      {/* Visible label, unlike the filters. Without it a select reading
          "Most recent" is indistinguishable from a filter that shows only
          recent traces. */}
      <span className="text-[11px] text-neutral-500">Sort</span>
      <select
        value={value || "recent"}
        onChange={(e) => onChange(e.target.value === "recent" ? "" : e.target.value)}
        className={SELECT}
      >
        <option value="recent">Most recent</option>
        <option value="duration">Longest run time</option>
        <option value="cost">Highest cost</option>
      </select>
    </label>
  );
}

/**
 * Source picker. Renders nothing until there is a choice to make.
 *
 * A native <select> rather than a segmented control: there are already five
 * sources on a system with one app instrumented (the app itself, plus
 * obs-runner, obs-judge, obs-guardrail and obs-playground), and five segments
 * do not fit a 375px phone. A select also gets the platform's own picker on
 * mobile, which is a better control than anything worth hand-rolling here.
 */
export function SourceFilter({
  value,
  onChange,
}: {
  value: string;
  onChange: (next: string) => void;
}) {
  const { data } = useQuery({
    queryKey: ["sources"],
    queryFn: api.sources,
    staleTime: 30_000,
  });

  const sources = data?.sources ?? [];

  // One source is not a choice, and a dropdown that can only be set to what it
  // already says is chrome that does nothing.
  if (sources.length < 2) return null;

  // A source named in the URL that no longer has spans still gets an option,
  // or the select would silently show "All sources" while the page below it
  // renders a filtered — and empty — view.
  const missing = value && !sources.some((s) => s.name === value);

  return (
    <label className="flex items-center gap-2">
      <span className="sr-only">Filter by source</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={SELECT}
      >
        <option value="">All sources</option>
        {sources.map((s) => (
          <option key={s.name} value={s.name}>
            {s.name} ({s.span_count.toLocaleString()})
          </option>
        ))}
        {missing && <option value={value}>{value} (no spans)</option>}
      </select>
    </label>
  );
}

/**
 * Provider-key picker — the primary filter on this dashboard.
 *
 * Unlike SourceFilter this renders whenever there is at least one key, even
 * though a single key makes "All keys" and that key the same set. It was
 * previously hidden below two keys on the "don't show a choice of one" rule,
 * and that was the wrong call here: it made the whole per-key view invisible
 * until you had already gone and added a second key, so the feature could not
 * be found by the person most likely to want it.
 *
 * "All keys" is the default and the combined view — no key clause at all,
 * rather than a union over the keys that exist, so spend and scores recorded
 * before keys were a concept still appear.
 */
export function CredentialFilter({
  value,
  onChange,
}: {
  value: string;
  onChange: (next: string) => void;
}) {
  const { data } = useQuery({
    queryKey: ["credentials"],
    queryFn: api.credentials,
    staleTime: 30_000,
  });

  const credentials = data?.credentials ?? [];
  if (credentials.length === 0) return null;

  // Filtering is by name, not id: the name is what gets written onto the span
  // as obs.credential, because a span should stay readable without a join back
  // to a table row that may since have been archived.
  const missing = value && !credentials.some((c) => c.name === value);

  return (
    <label className="flex items-center gap-2">
      <span className="sr-only">Filter by API key</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={SELECT}
      >
        <option value="">All keys</option>
        {credentials.map((c) => (
          <option key={c.id} value={c.name}>
            {c.name}
          </option>
        ))}
        {missing && <option value={value}>{value} (removed)</option>}
      </select>
    </label>
  );
}
