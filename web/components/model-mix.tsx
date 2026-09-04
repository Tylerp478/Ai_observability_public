"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Overview } from "@/lib/api";

/**
 * Share of prompts by model, as a donut.
 *
 * **Why a ramp and not a categorical palette.** This app reserves red, amber
 * and emerald for pass/warn/fail (globals.css says so explicitly), which leaves
 * exactly one non-status hue — not enough for four distinct identities. It
 * turns out not to matter, because these categories are *ordered*, and an
 * ordinal ramp on one hue is the correct encoding for an ordered scale. The
 * four steps are validated (single hue, monotone lightness, adjacent ΔL ≥ 0.06,
 * light end clearing the card surface at 3.41:1).
 *
 * **What the ramp encodes is price, and it is read from the pricing table.**
 * It used to be a hardcoded ladder of `claude-*` prefixes, which greyed out
 * every model the moment a second provider existed. The ordering it was really
 * expressing was cost, so the tier now comes from `/api/providers`, banded on
 * the output rate the billing code already uses. A model cannot be coloured as
 * a tier it is not priced as, and a new vendor needs no edit here.
 *
 * The consequence worth stating: **the ramp does not encode vendor.** A Grok
 * and a Claude model of the same price band are the same colour, which is the
 * honest reading of a price scale. Vendor is carried by the legend, where the
 * model names say it plainly — and the legend is already load-bearing, since
 * the slices are never colour-alone.
 *
 * **Colour follows the model, never its share.** modelColor is a lookup on the
 * model name, so filtering the dashboard cannot repaint the survivors — a
 * reader who learned "sonnet is the mid purple" keeps that. The tier bands are
 * fixed rather than derived from whichever models happen to be present, for
 * the same reason.
 *
 * **The legend names the models; the numbers live on hover.** That is a
 * deliberate exception to "never gate a value behind a tooltip", made because
 * this card is meant to read as a shape at a glance rather than a table. The
 * counts and shares are still in the SVG's aria-label, so a screen reader gets
 * them without hovering, and the same numbers are exact on the Traces page.
 */

// Light → dark, matching the tier ladder. Referenced as the ramp's variables,
// not as the hex values they currently hold: these started life as literals
// copied out of the sky ramp, which was accurate and became wrong the moment
// the accent could change per user — the donut kept its purple while every
// other accent on the page followed the theme. A copied colour is a colour
// that stops agreeing with its source.
const TIER_RAMP = ["var(--color-sky-300)", "var(--color-sky-400)", "var(--color-sky-500)", "var(--color-sky-600)"];

// Anything the pricing table cannot rank — a model retired before it was
// priced, or one newer than the table. Neutral rather than a fifth rung,
// because it has no place in the order.
const OFF_LADDER = "var(--color-neutral-700)";

// Past this many slices a donut stops being readable, so the tail folds into
// one "Other" segment rather than growing more colours.
const MAX_SLICES = 6;

function modelColor(model: string, tiers: Record<string, number>): string {
  const tier = tiers[model];
  return tier === undefined ? OFF_LADDER : (TIER_RAMP[tier] ?? OFF_LADDER);
}

/** Short label for the legend: the model, without the dated build suffix.
 *
 *  The vendor prefix is deliberately kept. Stripping `claude-` made sense when
 *  every model carried it and it was pure noise; with three vendors it would
 *  leave "sonnet-5" sitting next to "grok-4" and "gemini-2.5-flash", hiding
 *  the one attribute the colour no longer encodes. */
function shortName(model: string): string {
  return model.replace(/-\d{8}$/, "");
}

const SIZE = 168;
const R_OUTER = 78;
const R_INNER = 52;
const CENTER = SIZE / 2;

/** Polar → cartesian, with 0° at 12 o'clock so the first slice starts at the top. */
function point(angle: number, radius: number) {
  const rad = ((angle - 90) * Math.PI) / 180;
  return [CENTER + radius * Math.cos(rad), CENTER + radius * Math.sin(rad)];
}

function arc(start: number, end: number): string {
  const large = end - start > 180 ? 1 : 0;
  const [x1, y1] = point(start, R_OUTER);
  const [x2, y2] = point(end, R_OUTER);
  const [x3, y3] = point(end, R_INNER);
  const [x4, y4] = point(start, R_INNER);
  return [
    `M ${x1} ${y1}`,
    `A ${R_OUTER} ${R_OUTER} 0 ${large} 1 ${x2} ${y2}`,
    `L ${x3} ${y3}`,
    `A ${R_INNER} ${R_INNER} 0 ${large} 0 ${x4} ${y4}`,
    "Z",
  ].join(" ");
}

interface Slice {
  model: string;
  prompts: number;
  cost_usd: number;
  share: number;
  color: string;
  start: number;
  end: number;
}

function buildSlices(
  models: Overview["models"],
  total: number,
  tiers: Record<string, number>,
): Slice[] {
  const sorted = [...models].sort((a, b) => b.prompts - a.prompts);

  // Fold the tail rather than adding colours for it.
  const head = sorted.slice(0, MAX_SLICES - 1);
  const tail = sorted.slice(MAX_SLICES - 1);
  const shown =
    tail.length > 1
      ? [
          ...head,
          {
            model: "Other",
            prompts: tail.reduce((s, m) => s + m.prompts, 0),
            cost_usd: tail.reduce((s, m) => s + m.cost_usd, 0),
          },
        ]
      : sorted;

  let cursor = 0;
  return shown.map((m) => {
    const share = total > 0 ? m.prompts / total : 0;
    const start = cursor;
    const end = cursor + share * 360;
    cursor = end;
    return {
      ...m,
      share,
      color: m.model === "Other" ? OFF_LADDER : modelColor(m.model, tiers),
      start,
      end,
    };
  });
}

export function ModelMix({ data }: { data: Overview }) {
  const [hover, setHover] = useState<number | null>(null);

  // Same query key as the model pickers, so this shares their cache rather
  // than costing a request. Undefined until it lands, which colours every
  // slice off-ladder for that instant — a neutral donut is a better first
  // paint than one that recolours itself once prices arrive.
  const { data: registry } = useQuery({
    queryKey: ["providers"],
    queryFn: api.providers,
    staleTime: 5 * 60_000,
  });
  const tiers = registry?.model_tiers ?? {};

  const total = data.models.reduce((s, m) => s + m.prompts, 0);
  const slices = buildSlices(data.models, total, tiers);
  const active = hover === null ? null : slices[hover];

  if (total === 0) {
    return (
      <div className="rounded-xl border border-neutral-800 bg-neutral-900 p-3.5">
        <h2 className="text-sm font-medium">Models used</h2>
        <p className="mt-2 text-[11px] text-neutral-500">
          No prompts in this window, so there is no model mix to show.
        </p>
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-neutral-800 bg-neutral-900 p-3.5">
      <h2 className="text-sm font-medium">Models used</h2>

      <div className="mt-2 flex flex-wrap items-center gap-x-5 gap-y-3">
        <div className="relative shrink-0">
          <svg
            viewBox={`0 0 ${SIZE} ${SIZE}`}
            width={SIZE}
            height={SIZE}
            className="block"
            role="img"
            aria-label={`Share of prompts by model. ${slices
              .map((s) => `${shortName(s.model)} ${Math.round(s.share * 100)}%`)
              .join(", ")}.`}
            onMouseLeave={() => setHover(null)}
          >
            {slices.length === 1 ? (
              // A single model is a ring, not an arc: start and end angles are
              // equal at 360°, which collapses an arc path to nothing.
              <circle
                cx={CENTER}
                cy={CENTER}
                r={(R_OUTER + R_INNER) / 2}
                fill="none"
                stroke={slices[0].color}
                strokeWidth={R_OUTER - R_INNER}
              />
            ) : (
              slices.map((s, i) => (
                <path
                  key={s.model}
                  d={arc(s.start, s.end)}
                  fill={s.color}
                  // The gap between segments is the card surface showing
                  // through, not a border drawn around each slice.
                  stroke="var(--color-neutral-900)"
                  strokeWidth="2"
                  className="cursor-default transition-opacity"
                  opacity={hover === null || hover === i ? 1 : 0.45}
                  onMouseEnter={() => setHover(i)}
                />
              ))
            )}
          </svg>

          {/* The hole earns its place by holding the total. Rendered as HTML so
              the type keeps its exact size instead of scaling with the
              viewBox. Proportional figures, not tabular — this is a display
              number that aligns with nothing. */}
          <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
            <span className="text-xl font-medium">
              {active ? `${Math.round(active.share * 100)}%` : total.toLocaleString()}
            </span>
            <span className="max-w-[92px] truncate text-[10px] text-neutral-500">
              {active ? shortName(active.model) : total === 1 ? "prompt" : "prompts"}
            </span>
          </div>
        </div>

        {/* Legend. Names only — identity, so the slices are never
            colour-alone. Hovering a row lights its slice and puts that model's
            share in the hole. */}
        <ul className="w-full min-w-0 max-w-[280px] space-y-1.5">
          {slices.map((s, i) => (
            <li
              key={s.model}
              className="flex items-center gap-2 text-[11px]"
              onMouseEnter={() => setHover(i)}
              onMouseLeave={() => setHover(null)}
            >
              <span
                aria-hidden="true"
                className="h-2.5 w-2.5 shrink-0 rounded-[3px]"
                style={{ background: s.color }}
              />
              <span className="min-w-0 flex-1 truncate text-neutral-300">
                {shortName(s.model)}
              </span>
            </li>
          ))}
        </ul>
      </div>

    </div>
  );
}
