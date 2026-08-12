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
        className="max-w-[190px] truncate rounded-lg border border-neutral-700 bg-neutral-950 px-2.5 py-1.5 text-xs text-neutral-300 outline-none focus:border-neutral-500"
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
        className="max-w-[190px] truncate rounded-lg border border-neutral-700 bg-neutral-950 px-2.5 py-1.5 text-xs text-neutral-300 outline-none focus:border-neutral-500"
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
