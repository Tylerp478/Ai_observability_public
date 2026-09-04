"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Suspense, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Shell } from "./shell";
import { ModelMix } from "@/components/model-mix";
import { ScoreAverages } from "@/components/score-averages";
import {
  CredentialFilter,
  RangePicker,
  rangeLabel,
  useCredentialParam,
  useRangeParam,
} from "@/components/source-filter";
import { TopTraces } from "@/components/top-traces";
import {
  api,
  change,
  formatChange,
  formatCost,
  formatCostLong,
  formatCountLong,
  formatLatency,
  formatRate,
  formatTokens,
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
  const [range, setRange] = useRangeParam();

  const { data, isLoading, isError, isFetching } = useQuery({
    // Both the window and the credential are part of the key, so switching
    // either reads from cache when it can and refetches when it can't, instead
    // of showing one scope's numbers under another's label.
    //
    // No source filter on this page: which API key paid is the question this
    // dashboard answers, and service.name was a second axis that only muddied
    // it. Traces still carries it, where "which app emitted this" is the
    // question actually being asked.
    queryKey: ["overview", range.hours, credential],
    queryFn: () => api.overview(range.hours, "", credential),
    // Polls slower on longer windows — see RANGES. A 30-day series does not
    // move within 30 seconds and the scan is not free.
    refetchInterval: range.refetchMs,
    // Hold the previous render across a refetch rather than dropping back to
    // the loading state — a chart that blinks every 30s is unreadable. This
    // also carries the old window across a range switch, which is why every
    // label below reads `data.window_hours` and not `range`.
    placeholderData: keepPreviousData,
  });

  if (isLoading) {
    return <p className="py-10 text-center text-sm text-neutral-500">Loading overview…</p>;
  }
  if (isError || !data) {
    return <p className="py-10 text-center text-sm text-red-400">Failed to load overview.</p>;
  }

  // What every delta below is measured against. Named once here so the tiles
  // and their accessible labels can't drift from the window actually queried.
  const baseline = `the previous ${rangeLabel(data.window_hours)}`;

  return (
    <div className={`space-y-4 transition-opacity ${isFetching ? "opacity-60" : ""}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-base font-semibold">Overview</h1>
        <div className="flex flex-wrap items-center gap-2.5">
          <RangePicker value={range} onChange={setRange} />
          <CredentialFilter value={credential} onChange={setCredential} />
          {/* Spells out what the picker abbreviates, and reads the window the
              numbers on screen actually describe rather than the one selected
              — those differ for one render while a longer range loads. The
              second clause names the baseline the tile deltas use, which is
              otherwise a comparison the reader has to guess at. */}
          <p className="text-xs text-neutral-500">
            Last {rangeLabel(data.window_hours)}
            <span className="hidden sm:inline"> · vs previous</span>
          </p>
        </div>
      </div>

      {/* The golden signals, in the order you triage them: how much ran, how
          much of it failed, how slow it was, what it cost, how big it was.
          Three across on a phone rather than one column — five stacked tiles
          would push the chart, the thing worth scrolling to, well below the
          fold. Five across from `sm` up, where there is room for one row. */}
      <dl className="grid grid-cols-3 gap-2 sm:grid-cols-5 sm:gap-3">
        <StatTile
          label="Prompts"
          value={formatCountLong(data.prompts)}
          hint={throughput(data.prompts, data.window_hours)}
          delta={
            <Delta
              current={data.prompts}
              previous={data.previous.prompts}
              polarity="neutral"
              window={baseline}
            />
          }
        />
        <StatTile
          label="Error rate"
          // Em dash, not "0%", when nothing ran. A rate over an empty window
          // has no value, and "0%" is the one reading that actively misleads:
          // it says everything succeeded where the truth is that nothing was
          // attempted. Same rule as p95 beside it.
          value={data.prompts ? formatRate(data.error_rate) : "—"}
          // The counts behind the rate. 4% means something different out of 25
          // calls than out of 25,000, and the tile is where that distinction is
          // cheapest to make.
          hint={data.prompts ? `${data.errors} of ${data.prompts}` : "—"}
          delta={
            <Delta
              current={data.prompts ? data.error_rate : null}
              previous={data.previous.prompts ? data.previous.error_rate : null}
              polarity="up-bad"
              // Points, not percent: this metric is already a rate.
              points
              window={baseline}
            />
          }
        />
        <StatTile
          label="p95 latency"
          value={
            data.latency_p95_ms === null ? "—" : formatLatency(data.latency_p95_ms)
          }
          // p50 beside p95 is the whole shape of the distribution in two
          // numbers: close together is uniformly slow, far apart is a tail.
          hint={
            data.latency_p50_ms === null
              ? "—"
              : `p50 ${formatLatency(data.latency_p50_ms)}`
          }
          delta={
            <Delta
              current={data.latency_p95_ms}
              previous={data.previous.latency_p95_ms}
              polarity="up-bad"
              window={baseline}
            />
          }
        />
        <StatTile
          label="Cost"
          value={formatCostLong(data.cost_usd)}
          // Unit cost, which is the figure that survives a change in traffic —
          // a total going up because you ran more is not the same event as
          // each call getting more expensive.
          hint={
            data.prompts ? `${formatCost(data.cost_usd / data.prompts)} each` : "—"
          }
          delta={
            <Delta
              current={data.cost_usd}
              previous={data.previous.cost_usd}
              polarity="up-bad"
              window={baseline}
            />
          }
        />
        <StatTile
          label="Tokens"
          value={formatTokens(data.input_tokens + data.output_tokens)}
          hint={`${formatTokens(data.input_tokens)} in · ${formatTokens(
            data.output_tokens,
          )} out`}
          delta={
            <Delta
              current={data.input_tokens + data.output_tokens}
              previous={data.previous.input_tokens + data.previous.output_tokens}
              polarity="neutral"
              window={baseline}
            />
          }
        />
      </dl>

      <PromptsChart data={data} />

      {/* Two cards, one row on anything wider than a phone. */}
      <div className="grid gap-4 sm:grid-cols-2">
        <ModelMix data={data} />
        <ScoreAverages hours={range.hours} credential={credential} />
      </div>

      {/* Full width and last: the rows carry long names and a link each, so
          this wants the whole measure rather than half of it. */}
      <TopTraces data={data} />
    </div>
  );
}

/**
 * Whether a rise in this metric is good news, bad news, or neither.
 *
 * Traffic and token volume are `neutral` on purpose. More prompts is not an
 * improvement or a regression — it is a fact about demand — and colouring it
 * green would quietly congratulate you for a runaway retry loop. Only the
 * metrics with a right direction get one.
 */
type Polarity = "up-bad" | "neutral";

/**
 * A metric's movement against the window before it.
 *
 * Renders nothing at all when there is no honest comparison to draw, rather
 * than a placeholder: an empty baseline is the normal state on a new install,
 * and five tiles each carrying a grey "—" is noise that teaches the reader to
 * stop looking at this line.
 *
 * The arrow never carries meaning alone — the sign is in the number and the
 * direction is spelled out in the accessible label, so this survives both
 * colour blindness and a screen reader.
 */
function Delta({
  current,
  previous,
  polarity,
  points = false,
  window,
}: {
  current: number | null;
  previous: number | null;
  polarity: Polarity;
  points?: boolean;
  /** What the baseline is, for the label: "the previous 24 hours". */
  window: string;
}) {
  const moved = change(current, previous, points);
  if (moved === null) return null;

  if (moved.kind === "new") {
    return (
      <span className="text-[10px] text-neutral-500" title={`No prompts in ${window}`}>
        new
      </span>
    );
  }
  if (moved.kind === "flat") {
    return (
      <span className="text-[10px] text-neutral-500" title={`Unchanged from ${window}`}>
        flat
      </span>
    );
  }

  const up = moved.value > 0;
  const tone =
    polarity === "neutral"
      ? "text-neutral-500"
      : up
        ? "text-red-400"
        : "text-emerald-400";

  return (
    <span
      className={`text-[10px] tabular-nums ${tone}`}
      // "up 12%" rather than "↑ 12%" — the glyph is decoration and is hidden
      // from the accessibility tree below.
      aria-label={`${up ? "up" : "down"} ${formatChange(moved.value, points)} from ${window}`}
    >
      <span aria-hidden="true">{up ? "↑" : "↓"}</span>
      {formatChange(moved.value, points)}
    </span>
  );
}

/**
 * Rate of prompts, for the count tile's second line.
 *
 * Switches unit rather than always reporting per hour: 93 prompts over 30 days
 * is "0.1/hr", which is true, unreadable, and identical to what 0.14/hr and
 * 0.05/hr both render as. Per day is the unit that still has digits in it.
 */
function throughput(prompts: number, hours: number): string {
  if (prompts === 0 || hours <= 0) return "—";
  const perHour = prompts / hours;
  const rate = perHour >= 1 ? perHour : perHour * 24;
  const unit = perHour >= 1 ? "hr" : "day";
  return `${rate < 10 ? rate.toFixed(1) : Math.round(rate).toLocaleString()}/${unit}`;
}

/**
 * A stat tile, not a one-bar chart: the number is the whole story.
 *
 * Proportional figures rather than tabular-nums — equal-width digits make a
 * standalone number look loose at display sizes, and these tiles don't align
 * vertically with anything that would benefit.
 *
 * `hint` is the qualifier the headline number can't carry on its own — the
 * counts behind a rate, the p50 behind a p95, the unit cost behind a total. It
 * lives inside the <dd> rather than beside it: a <dl> row may only hold <dt>
 * and <dd>, so a third element in the wrapper would be invalid markup.
 *
 * `delta` sits on the value's baseline and is allowed to wrap beneath it. At
 * three tiles across a 375px phone there are about 85px of text, and "$0.234"
 * plus a delta chip does not fit in that; wrapping costs a line on the tiles
 * that need it and keeps every other tile on one.
 */
function StatTile({
  label,
  value,
  hint,
  delta,
}: {
  label: string;
  value: string;
  hint?: string;
  delta?: React.ReactNode;
}) {
  return (
    <div className="rounded-xl border border-neutral-800 bg-neutral-900 px-3 py-2.5">
      <dt className="kicker">{label}</dt>
      <dd className="mt-1">
        <span className="flex flex-wrap items-baseline gap-x-1.5">
          <span className="max-w-full truncate text-xl font-medium sm:text-2xl">
            {value}
          </span>
          {delta}
        </span>
        {hint && (
          <span className="mt-0.5 block truncate text-[10px] text-neutral-500">
            {hint}
          </span>
        )}
      </dd>
    </div>
  );
}

// --------------------------------------------------------------------------
// Prompts per bucket
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

/**
 * What one point of the series is, in words.
 *
 * The title, the caption and the aria-label all name the interval, and on
 * every window but the 24-hour one "hour" is simply false — a 30-day series is
 * one point per day. Keyed on `bucket_seconds` from the backend so there is
 * one source of truth for the width and this only has to name it.
 */
const BUCKETS: Record<number, { long: string; short: string }> = {
  300: { long: "5 minutes", short: "5m" },
  900: { long: "15 minutes", short: "15m" },
  3_600: { long: "hour", short: "hour" },
  21_600: { long: "6 hours", short: "6h" },
  86_400: { long: "day", short: "day" },
};

function bucketName(seconds: number): { long: string; short: string } {
  const minutes = Math.max(1, Math.round(seconds / 60));
  return BUCKETS[seconds] ?? { long: `${minutes} minutes`, short: `${minutes}m` };
}

/**
 * Axis tick.
 *
 * A clock time stops distinguishing anything once the buckets are a day
 * apart — thirty ticks all reading "12:00 AM" — and at six hours apart the
 * time alone can't say which day it belongs to. So the format follows the
 * width: date for daily, weekday plus hour for 6-hourly, clock time below.
 */
function tickLabel(unixSeconds: number, bucket: number): string {
  const d = new Date(unixSeconds * 1000);
  if (bucket >= 86_400) {
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  if (bucket >= 21_600) {
    return d.toLocaleString(undefined, { weekday: "short", hour: "numeric" });
  }
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

/**
 * The same instant where there is room to be unambiguous — the tooltip. A
 * bucket that could be any of thirty days needs its date spelled out even
 * though the axis had to abbreviate.
 */
function fullLabel(unixSeconds: number, bucket: number): string {
  const d = new Date(unixSeconds * 1000);
  if (bucket >= 86_400) {
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  if (bucket >= 21_600) {
    return d.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "numeric",
    });
  }
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

/**
 * Tick spacing candidates, in buckets, by bucket width.
 *
 * Per width rather than one shared list so ticks land on intervals a reader
 * thinks in — every 4th six-hour bucket is a day, every 4th fifteen-minute
 * bucket is an hour — instead of on whatever happens to divide the point count.
 */
const TICK_STEPS: Record<number, number[]> = {
  300: [3, 6, 12], // 15m, 30m, 1h
  900: [4, 8, 12], // 1h, 2h, 3h
  3_600: [6, 8, 12], // 6h, 8h, 12h
  21_600: [4, 8], // 1d, 2d
  86_400: [5, 7, 10], // 5d, 1w, 10d
};

// Widest tick this has to clear at 11px Inter: "Thu 12 AM" is ~58px, "04:00 PM"
// ~52px, "Aug 8" ~34px. 70 leaves a gutter at the widest of them.
const TICK_GUTTER = 70;

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
  const [plotRef, W] = useWidth<HTMLDivElement>(720);
  const PLOT_W = W - PAD.left - PAD.right;

  // Touch has no hover, and a stuck tooltip from a stray tap is worse than
  // none — the caption carries the headline values there.
  useEffect(() => {
    if (hover === null) return;
    const clear = () => setHover(null);
    window.addEventListener("scroll", clear, { passive: true });
    return () => window.removeEventListener("scroll", clear);
  }, [hover]);

  const series = data.series;
  const bucket = data.bucket_seconds;
  const name = bucketName(bucket);
  const peak = Math.max(...series.map((p) => p.prompts), 0);

  // Two intervals is enough grid for a card this size; more would compete with
  // the line it is supposed to sit behind.
  const step = niceStep(Math.max(peak, 1) / 2);
  const yMax = Math.max(step * 2, step * Math.ceil(peak / step));
  const ticks: number[] = [];
  for (let t = 0; t <= yMax; t += step) ticks.push(t);

  const bandW = PLOT_W / Math.max(series.length - 1, 1);
  const x = (i: number) => PAD.left + i * bandW;

  // Label density follows the measured width and the bucket. Falls back to the
  // sparsest candidate rather than a fixed number: on a phone even the widest
  // spacing can fail the gutter test, and the sparsest is the best available
  // answer, not a reason to start colliding.
  const steps = TICK_STEPS[bucket] ?? [6, 8, 12];
  const labelEvery =
    steps.find((s) => s * bandW >= TICK_GUTTER) ?? steps[steps.length - 1];

  const y = (v: number) => PAD.top + PLOT_H - (v / yMax) * PLOT_H;

  const points = series.map((p, i) => `${x(i).toFixed(1)},${y(p.prompts).toFixed(1)}`);
  const last = series[series.length - 1];
  const active = hover === null ? null : series[hover];

  return (
    <div className="rounded-xl border border-neutral-800 bg-neutral-900 p-3.5">
      {/* One series, so no legend — the title names it, including the
          interval, which is the only thing that says a point on the 30-day
          view is a day rather than an hour. */}
      <h2 className="text-sm font-medium">Prompts per {name.long}</h2>

      <div ref={plotRef} className="relative mt-2">
        <svg
          viewBox={`0 0 ${W} ${H}`}
          width={W}
          height={H}
          className="block"
          role="img"
          aria-label={`Prompts per ${name.long} over the last ${rangeLabel(
            data.window_hours,
          )}. Peak ${peak.toLocaleString()} in one ${name.short}.`}
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
                stroke="var(--color-neutral-800)"
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

          {/* Label density follows the measured width. A fixed every-6th-bucket
              axis collides on a phone, where the buckets are ~11px apart and
              "12:00 AM" is ~52px wide. All labels are centre-anchored — a
              start-anchored first label reaches further right than a centred
              one and is what collided with its neighbour. */}
          {series.map((p, i) =>
            i % labelEvery === 0 ? (
              <text
                key={p.bucket_start}
                x={x(i)}
                y={H - 10}
                textAnchor="middle"
                className="fill-neutral-500 tabular-nums"
                fontSize="11"
              >
                {tickLabel(p.bucket_start, bucket)}
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
            stroke="var(--color-neutral-900)"
            strokeWidth="2"
          />

          {active && hover !== null && (
            <g>
              <line
                x1={x(hover)}
                x2={x(hover)}
                y1={PAD.top}
                y2={PAD.top + PLOT_H}
                stroke="var(--color-neutral-700)"
                strokeWidth="1"
              />
              <circle
                cx={x(hover)}
                cy={y(active.prompts)}
                r="4.5"
                fill="var(--color-accent)"
                stroke="var(--color-neutral-900)"
                strokeWidth="2"
              />
            </g>
          )}

          {/* Hit targets: one full-height band per bucket, so the pointer never
              has to land on a 4px dot. */}
          {series.map((p, i) => (
            <rect
              key={p.bucket_start}
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
              prompt{active.prompts === 1 ? "" : "s"} ·{" "}
              {fullLabel(active.bucket_start, bucket)}
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
          : `Latest ${name.short} ${last.prompts.toLocaleString()} · peak ${peak.toLocaleString()}`}
      </p>
    </div>
  );
}
