"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Suspense, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { Shell } from "./shell";
import { ModelMix } from "@/components/model-mix";
import { ScoreAverages } from "@/components/score-averages";
import { CredentialFilter, useCredentialParam } from "@/components/source-filter";
import { TopTraces } from "@/components/top-traces";
import {
  api,
  formatCostLong,
  formatCountLong,
  formatDurationLong,
  type Overview,
} from "@/lib/api";

// CLAUDE.md rules out a *landing* page — marketing, sign-up, the public front
// door. This is the opposite of that: an in-app overview behind the auth gate,
// which is the first thing a signed-in user wants. Root used to redirect to
// /traces; that redirect is what this replaces.
export default function HomePage() {
  return (
    <Shell>
      {/* useSearchParams needs a Suspense boundary above it or the page
          cannot be prerendered. */}
      <Suspense fallback={null}>
        <Dashboard />
      </Suspense>
    </Shell>
  );
}

function Dashboard() {
  const [credential, setCredential] = useCredentialParam();

  const { data, isLoading, isError, isFetching } = useQuery({
    // The credential is part of the key, so switching it reads from cache when
    // it can and refetches when it can't, instead of showing one key's numbers
    // under another's label.
    //
    // No source filter on this page: which API key paid is the question this
    // dashboard answers, and service.name was a second axis that only muddied
    // it. Traces still carries it, where "which app emitted this" is the
    // question actually being asked.
    queryKey: ["overview", 24, credential],
    queryFn: () => api.overview(24, "", credential),
    refetchInterval: 30_000,
    // Hold the previous render across a refetch rather than dropping back to
    // the loading state — a chart that blinks every 30s is unreadable.
    placeholderData: keepPreviousData,
  });

  if (isLoading) {
    return <p className="py-10 text-center text-sm text-neutral-500">Loading overview…</p>;
  }
  if (isError || !data) {
    return <p className="py-10 text-center text-sm text-red-400">Failed to load overview.</p>;
  }

  return (
    <div className={`space-y-4 transition-opacity ${isFetching ? "opacity-60" : ""}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-base font-semibold">Overview</h1>
        <div className="flex flex-wrap items-center gap-2.5">
          <CredentialFilter value={credential} onChange={setCredential} />
          <p className="text-xs text-neutral-500">Last {data.window_hours} hours</p>
        </div>
      </div>

      {/* Three across at every size. These are short numbers, and stacking them
          on a phone would push the chart — the thing worth scrolling to —
          below the fold. */}
      <dl className="grid grid-cols-3 gap-2 sm:gap-3">
        <StatTile label="Prompts run" value={formatCountLong(data.prompts)} />
        <StatTile label="Time in model" value={formatDurationLong(data.duration_ms)} />
        <StatTile label="Cost" value={formatCostLong(data.cost_usd)} />
      </dl>

      <PromptsChart data={data} />

      {/* Two cards, one row on anything wider than a phone. */}
      <div className="grid gap-4 sm:grid-cols-2">
        <ModelMix data={data} />
        <ScoreAverages hours={24} credential={credential} />
      </div>

      {/* Full width and last: the rows carry long names and a link each, so
          this wants the whole measure rather than half of it. */}
      <TopTraces data={data} />
    </div>
  );
}

/**
 * A stat tile, not a one-bar chart: the number is the whole story.
 *
 * Proportional figures rather than tabular-nums — equal-width digits make a
 * standalone number look loose at display sizes, and these tiles don't align
 * vertically with anything that would benefit.
 */
function StatTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-neutral-800 bg-neutral-900 px-3 py-2.5">
      <dt className="kicker">{label}</dt>
      <dd className="mt-1 truncate text-xl font-medium sm:text-2xl">{value}</dd>
    </div>
  );
}

// --------------------------------------------------------------------------
// Prompts per hour
// --------------------------------------------------------------------------

/** Round a raw step up to the nearest 1, 2 or 5 × 10ⁿ, so ticks land on
 *  numbers a reader recognises instead of 3.7 / 7.4 / 11.1. */
function niceStep(raw: number): number {
  const mag = Math.pow(10, Math.floor(Math.log10(Math.max(raw, 1))));
  for (const m of [1, 2, 5]) {
    if (raw <= m * mag) return m * mag;
  }
  return 10 * mag;
}

function hourLabel(unixSeconds: number): string {
  return new Date(unixSeconds * 1000).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  });
}

// Geometry. The viewBox includes the x-axis band, so the card never grows a
// nested scrollbar just to reach the tick labels.
const H = 240;
const PAD = { top: 14, right: 14, bottom: 30, left: 40 };
const PLOT_H = H - PAD.top - PAD.bottom;

/**
 * Container width in CSS pixels.
 *
 * The viewBox has to track it. With a fixed viewBox the whole drawing is
 * scaled to fit, and on a 315px-wide phone card a 720-unit box shrinks by
 * 0.44 — which takes 11px axis labels down to about 5px and makes them
 * unreadable. Measuring keeps one unit equal to one pixel at every width, so
 * text, strokes and markers stay the size they're specified at.
 */
function useWidth<T extends HTMLElement>(fallback: number) {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(fallback);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => {
      setWidth(Math.max(240, Math.round(entry.contentRect.width)));
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  return [ref, width] as const;
}

function PromptsChart({ data }: { data: Overview }) {
  const [hover, setHover] = useState<number | null>(null);
  const [showTable, setShowTable] = useState(false);
  const tableId = useId();
  const [plotRef, W] = useWidth<HTMLDivElement>(720);
  const PLOT_W = W - PAD.left - PAD.right;

  // Touch has no hover, and a stuck tooltip from a stray tap is worse than
  // none — the caption and the table carry the values there.
  useEffect(() => {
    if (hover === null) return;
    const clear = () => setHover(null);
    window.addEventListener("scroll", clear, { passive: true });
    return () => window.removeEventListener("scroll", clear);
  }, [hover]);

  const series = data.series;
  const peak = Math.max(...series.map((p) => p.prompts), 0);

  // Two intervals is enough grid for a card this size; more would compete with
  // the line it is supposed to sit behind.
  const step = niceStep(Math.max(peak, 1) / 2);
  const yMax = Math.max(step * 2, step * Math.ceil(peak / step));
  const ticks: number[] = [];
  for (let t = 0; t <= yMax; t += step) ticks.push(t);

  const bandW = PLOT_W / Math.max(series.length - 1, 1);
  const x = (i: number) => PAD.left + i * bandW;

  // A rendered "12:00 AM" is ~52px at 11px Inter; 70 leaves a clear gutter.
  // Candidates stay multiples of hours a reader thinks in, so the axis reads
  // 6- or 8-hourly rather than every 7 hours.
  const labelEvery = [6, 8, 12].find((s) => s * bandW >= 70) ?? 12;

  const y = (v: number) => PAD.top + PLOT_H - (v / yMax) * PLOT_H;

  const points = series.map((p, i) => `${x(i).toFixed(1)},${y(p.prompts).toFixed(1)}`);
  const last = series[series.length - 1];
  const active = hover === null ? null : series[hover];

  return (
    <div className="rounded-xl border border-neutral-800 bg-neutral-900 p-3.5">
      <div className="flex items-baseline justify-between gap-3">
        {/* One series, so no legend — the title names it. */}
        <h2 className="text-sm font-medium">Prompts per hour</h2>
        <button
          onClick={() => setShowTable((v) => !v)}
          aria-expanded={showTable}
          aria-controls={tableId}
          className="shrink-0 text-[11px] text-neutral-500 hover:text-sky-400"
        >
          {showTable ? "Hide table" : "View as table"}
        </button>
      </div>

      <div ref={plotRef} className="relative mt-2">
        <svg
          viewBox={`0 0 ${W} ${H}`}
          width={W}
          height={H}
          className="block"
          role="img"
          aria-label={`Prompts per hour over the last ${data.window_hours} hours. Peak ${peak} in one hour.`}
          onMouseLeave={() => setHover(null)}
        >
          {/* Solid hairline grid, one step off the surface. Never dashed. */}
          {ticks.map((t) => (
            <g key={t}>
              <line
                x1={PAD.left}
                x2={W - PAD.right}
                y1={y(t)}
                y2={y(t)}
                stroke="#3f424d"
                strokeWidth="1"
              />
              <text
                x={PAD.left - 8}
                y={y(t) + 4}
                textAnchor="end"
                className="fill-neutral-500 tabular-nums"
                fontSize="11"
              >
                {t.toLocaleString()}
              </text>
            </g>
          ))}

          {/* Label density follows the measured width. A fixed every-6th-hour
              axis collides on a phone, where the buckets are ~11px apart and
              "12:00 AM" is ~52px wide. All labels are centre-anchored — a
              start-anchored first label reaches further right than a centred
              one and is what collided with its neighbour. */}
          {series.map((p, i) =>
            i % labelEvery === 0 ? (
              <text
                key={p.hour_start}
                x={x(i)}
                y={H - 10}
                textAnchor="middle"
                className="fill-neutral-500 tabular-nums"
                fontSize="11"
              >
                {hourLabel(p.hour_start)}
              </text>
            ) : null,
          )}

          <polyline
            points={points.join(" ")}
            fill="none"
            stroke="var(--color-accent)"
            strokeWidth="2"
            strokeLinejoin="round"
            strokeLinecap="round"
          />

          {/* Endpoint marker. Selective labelling: the latest value is the one
              worth carrying without a hover, and the caption below states it. */}
          <circle
            cx={x(series.length - 1)}
            cy={y(last.prompts)}
            r="4"
            fill="var(--color-accent)"
            stroke="#232532"
            strokeWidth="2"
          />

          {active && hover !== null && (
            <g>
              <line
                x1={x(hover)}
                x2={x(hover)}
                y1={PAD.top}
                y2={PAD.top + PLOT_H}
                stroke="#595d6c"
                strokeWidth="1"
              />
              <circle
                cx={x(hover)}
                cy={y(active.prompts)}
                r="4.5"
                fill="var(--color-accent)"
                stroke="#232532"
                strokeWidth="2"
              />
            </g>
          )}

          {/* Hit targets: one full-height band per bucket, so the pointer never
              has to land on a 4px dot. */}
          {series.map((p, i) => (
            <rect
              key={p.hour_start}
              x={x(i) - bandW / 2}
              y={PAD.top}
              width={bandW}
              height={PLOT_H}
              fill="transparent"
              onMouseEnter={() => setHover(i)}
            />
          ))}
        </svg>

        {active && hover !== null && (
          <div
            className="pointer-events-none absolute z-10 -translate-x-1/2 -translate-y-full rounded-lg border border-neutral-700 bg-neutral-950 px-2 py-1 text-[11px] whitespace-nowrap"
            style={{
              left: `${(x(hover) / W) * 100}%`,
              top: `${(y(active.prompts) / H) * 100 - 2}%`,
            }}
          >
            <span className="tabular-nums">{active.prompts.toLocaleString()}</span>
            <span className="text-neutral-500">
              {" "}
              prompt{active.prompts === 1 ? "" : "s"} · {hourLabel(active.hour_start)}
            </span>
          </div>
        )}
      </div>

      <p className="mt-1 text-[11px] text-neutral-500">
        {peak === 0
          ? // Names the key when one is selected. "No prompts in this window"
            // over a filtered view reads as "nothing is running" when what it
            // means is "not this key".
            data.credential
            ? `No prompts on ${data.credential} in this window.`
            : "No prompts in this window."
          : `Latest hour ${last.prompts.toLocaleString()} · peak ${peak.toLocaleString()}`}
      </p>

      {/* The table twin. A tooltip must never be the only way to read a value. */}
      {showTable && (
        <div id={tableId} className="mt-3 max-h-64 overflow-y-auto">
          <table className="table-fade w-full text-left text-xs">
            <thead>
              <tr>
                <th className="py-1.5 font-normal text-neutral-500">Hour</th>
                <th className="py-1.5 text-right font-normal text-neutral-500">Prompts</th>
              </tr>
            </thead>
            <tbody>
              {series.map((p) => (
                <tr key={p.hour_start}>
                  <td className="py-1.5 tabular-nums">{hourLabel(p.hour_start)}</td>
                  <td className="py-1.5 text-right tabular-nums">
                    {p.prompts.toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
