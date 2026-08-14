"use client";

import Link from "next/link";
import {
  categoryLabel,
  formatCost,
  relativeTime,
  type Overview,
  type TopTrace,
} from "@/lib/api";

/**
 * Where the money went, as a ranked list rather than a chart.
 *
 * **Why a list.** Ten values spanning two orders of magnitude, each identified
 * by a name long enough to need truncating, is a table's job. A bar chart of
 * the same data spends its width on the bars and has nowhere left to put the
 * labels, and the reader's next action — open the expensive one — is a link,
 * which a bar is a poor target for. The bar here is a hairline under each row
 * instead: enough to see the shape of the distribution, not enough to compete
 * with the number it sits beneath.
 *
 * **Why the category is neutral and not colour-coded.** Categories are
 * nominal — obs-judge is not more or less than obs-runner — so encoding them
 * would need a categorical palette. This app doesn't have one to spend:
 * globals.css reserves red, amber and emerald for pass/warn/fail, and
 * ModelMix already took the single remaining hue as an *ordered* ramp, which
 * is a thing nominal categories cannot borrow. The name is the encoding.
 */

export function TopTraces({ data }: { data: Overview }) {
  const traces = data.top_traces;

  if (traces.length === 0) {
    return (
      <div className="rounded-xl border border-neutral-800 bg-neutral-900 p-3.5">
        <h2 className="text-sm font-medium">Most expensive traces</h2>
        <p className="mt-2 text-[11px] text-neutral-500">
          {/* Names the key for the same reason the chart caption does: a
              filtered zero reads as "nothing ran" when it means "not this
              key". */}
          {data.credential
            ? `Nothing on ${data.credential} cost anything in this window.`
            : "Nothing cost anything in this window."}
        </p>
      </div>
    );
  }

  // Bars are scaled against the most expensive row, not against the window
  // total. The question this answers is "how do these ten compare to each
  // other" — against the total, a window with one dominant trace would render
  // nine invisible slivers.
  const peak = Math.max(...traces.map((t) => t.cost_usd));
  const shown = traces.reduce((s, t) => s + t.cost_usd, 0);
  const share = data.cost_usd > 0 ? shown / data.cost_usd : 0;

  return (
    <div className="rounded-xl border border-neutral-800 bg-neutral-900 p-3.5">
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="text-sm font-medium">Most expensive traces</h2>
        <Link href="/traces" className="shrink-0 text-[11px] text-neutral-500 hover:text-sky-400">
          All traces
        </Link>
      </div>

      <ol className="mt-2 divide-y divide-neutral-850">
        {traces.map((t) => (
          <Row key={t.trace_id} trace={t} peak={peak} />
        ))}
      </ol>

      {/* What the list leaves out. Ten rows with no denominator invite the
          reader to assume they are the whole bill. */}
      <p className="mt-2.5 text-[11px] text-neutral-500">
        {traces.length === 1 ? "This trace is" : `These ${traces.length} traces are`}{" "}
        <span className="tabular-nums">{Math.round(share * 100)}%</span> of the{" "}
        <span className="tabular-nums">{formatCost(data.cost_usd)}</span> spent in this
        window.
      </p>
    </div>
  );
}

function Row({ trace, peak }: { trace: TopTrace; peak: number }) {
  const width = peak > 0 ? (trace.cost_usd / peak) * 100 : 0;

  return (
    <li>
      <Link
        href={`/traces/${trace.trace_id}`}
        className="group flex items-start gap-3 py-2"
      >
        <div className="min-w-0 flex-1">
          <span className="block truncate text-[13px] text-neutral-200 group-hover:text-sky-400">
            {trace.root_name}
          </span>
          <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="rounded bg-neutral-800 px-1.5 py-0.5 text-[10px] text-neutral-300">
              {categoryLabel(trace.category)}
            </span>
            <span className="text-[10px] text-neutral-500">
              {trace.span_count} span{trace.span_count === 1 ? "" : "s"} ·{" "}
              {relativeTime(trace.start_time_unix_nano)}
            </span>
          </div>
        </div>

        {/* Right-aligned and tabular so the costs form a column the eye can
            scan down — the ordering is only readable if the digits line up. */}
        <div className="shrink-0 text-right">
          <span className="block text-[13px] tabular-nums text-neutral-200">
            {formatCost(trace.cost_usd)}
          </span>
          <span className="mt-1 block text-[10px] tabular-nums text-neutral-500">
            {trace.input_tokens.toLocaleString()} in /{" "}
            {trace.output_tokens.toLocaleString()} out
          </span>
        </div>
      </Link>

      {/* Under the row rather than behind it. A tinted row background would
          read as a selection state, and this is a magnitude. */}
      <div className="mb-2 h-0.5 w-full overflow-hidden rounded-full bg-neutral-850">
        <div
          className="h-full rounded-full"
          style={{ width: `${width}%`, background: "var(--color-accent)" }}
        />
      </div>
    </li>
  );
}
