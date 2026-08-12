"use client";

import { useQuery } from "@tanstack/react-query";
import { api, type ScorerAverage } from "@/lib/api";

/**
 * Each scorer's headline number, as horizontal bars.
 *
 * **Bars, not a second donut.** The job here is comparing magnitudes across a
 * handful of named things, which is what a bar chart is for; a pie would be
 * answering "what share of the total is each", and these numbers are not parts
 * of any total.
 *
 * **One hue, because this is one series.** Every bar is the same accent — the
 * scorer names carry identity, and colouring each bar differently would spend
 * the colour channel on information the labels already give. Length is the
 * only encoding.
 *
 * **Each bar is on its own scale, and says so.** A 1-5 faithfulness mean and a
 * boolean pass rate are different quantities; the shared axis is "how far along
 * this scorer's own scale", and the label beside each bar is the real value in
 * its own units. That is the one honest way to put them in a column together —
 * scoring.py's summarize() has the long version of why a single averaged
 * number across output types would be wrong.
 *
 * **Scoped by API key.** A score records which key generated the output it
 * judged — not which key paid the judge — so "how good is what key A
 * produces" is the question this answers. There is no source dimension here
 * and no source picker on this page: a judge belongs to no application, and a
 * replay run's output was produced by this backend rather than by the app
 * being observed, so service.name could never have scoped this card honestly.
 */

// Where the bar fills to, as a fraction of this scorer's own scale.
function fraction(s: ScorerAverage): number | null {
  if (s.output_type === "numeric") {
    if (s.mean === null || s.score_min === null || s.score_max === null) return null;
    const span = s.score_max - s.score_min;
    return span > 0 ? (s.mean - s.score_min) / span : null;
  }
  if (s.output_type === "boolean") return s.pass_rate;
  // Categorical has no scalar to plot — a distribution is its honest summary,
  // so the row shows the most common label instead of an invented number.
  return null;
}

/** The value in its own units — never normalized, never a bare percentage. */
function readout(s: ScorerAverage): string {
  if (s.output_type === "numeric" && s.mean !== null) {
    return `${s.mean.toFixed(1)}/${s.score_max}`;
  }
  if (s.output_type === "boolean" && s.pass_rate !== null) {
    return `${Math.round(s.pass_rate * 100)}% pass`;
  }
  return s.top_label || "—";
}

export function ScoreAverages({
  hours,
  credential,
}: {
  hours: number;
  credential: string;
}) {
  const { data, isLoading } = useQuery({
    queryKey: ["score-summary", hours, credential],
    queryFn: () => api.scoreSummary(hours, credential),
    refetchInterval: 30_000,
  });

  const scorers = data?.scorers ?? [];

  return (
    <div className="rounded-xl border border-neutral-800 bg-neutral-900 p-3.5">
      <h2 className="text-sm font-medium">Average score</h2>

      {isLoading ? (
        <p className="mt-2 text-[11px] text-neutral-500">Loading…</p>
      ) : scorers.length === 0 ? (
        <p className="mt-2 text-[11px] text-neutral-500">
          Nothing has been scored in this window.
        </p>
      ) : (
        <ul className="mt-3 space-y-3">
          {scorers.map((s) => {
            const f = fraction(s);
            // Only for numeric scorers that define one. Drawn on the same
            // scale as the bar, so "is the average passing?" is answered by
            // looking rather than by arithmetic.
            const threshold =
              s.output_type === "numeric" &&
              s.pass_threshold !== null &&
              s.score_min !== null &&
              s.score_max !== null &&
              s.score_max > s.score_min
                ? (s.pass_threshold - s.score_min) / (s.score_max - s.score_min)
                : null;

            return (
              <li key={s.scorer_id}>
                <div className="flex items-baseline justify-between gap-2 text-[11px]">
                  <span className="min-w-0 truncate text-neutral-300">
                    {s.scorer_name}
                  </span>
                  <span className="shrink-0 tabular-nums text-neutral-400">
                    {readout(s)}
                  </span>
                </div>

                <div className="relative mt-1 h-1.5 w-full rounded-full bg-neutral-850">
                  {f !== null && (
                    <div
                      className="h-full rounded-full bg-sky-500"
                      style={{ width: `${Math.max(0, Math.min(1, f)) * 100}%` }}
                    />
                  )}
                  {threshold !== null && (
                    <span
                      aria-hidden="true"
                      title={`Passes at ${s.pass_threshold}`}
                      className="absolute top-[-2px] h-[10px] w-px bg-neutral-500"
                      style={{ left: `${Math.min(1, threshold) * 100}%` }}
                    />
                  )}
                </div>

                <p className="mt-1 text-[10px] text-neutral-500">
                  {s.scored} scored
                  {threshold !== null && ` · passes at ${s.pass_threshold}`}
                </p>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
