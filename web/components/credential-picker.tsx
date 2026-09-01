"use client";

import { useQuery } from "@tanstack/react-query";
import { api, type Credential } from "@/lib/api";
import { useProviderLabel } from "@/lib/use-models";

/**
 * "Which key pays for this", shown wherever a call can be started.
 *
 * One component rather than a copy per surface. There are five places in the
 * app that spend money — the Playground, the replay run form, a scorer's Try
 * it, re-scoring a run, and scoring a span — and the first version of this
 * feature wired two of them and forgot the rest. A shared control is how they
 * stop drifting apart.
 *
 * **It renders even when there is only one key.** That is deliberate, and it
 * is not the usual "hide a control with nothing to choose" rule: with one key
 * this is not offering a choice, it is disclosing a fact — *this will bill to
 * X* — which is exactly the fact an app about watching spend should not make
 * you go and look up. It renders disabled in that case, so it reads as
 * information rather than a control that does nothing.
 *
 * Nothing renders when there are no keys at all. Spending will fail server-side
 * with a message that says how to fix it, and an empty dropdown would only
 * repeat that less clearly.
 */
export function CredentialPicker({
  value,
  onChange,
  label = "API key",
  compact = false,
}: {
  /** Credential id, or "" for the project default. */
  value: string;
  onChange: (next: string) => void;
  label?: string;
  /** Tighter type, for the inline panels rather than a full form. */
  compact?: boolean;
}) {
  const { data } = useQuery({
    queryKey: ["credentials"],
    queryFn: api.credentials,
    staleTime: 30_000,
  });
  const providerLabel = useProviderLabel();

  const credentials = data?.credentials ?? [];
  if (credentials.length === 0) return null;

  const fallback = credentials.find((c) => c.is_default) ?? credentials[0];
  const single = credentials.length === 1;
  const selected = credentials.find((c) => c.id === value) ?? fallback;

  /**
   * "prod · Anthropic" — the key, and the vendor it spends at.
   *
   * Which key pays stopped being the whole fact once a key could belong to any
   * of four providers: two keys named "prod" and "prod-eu" say nothing about
   * which one can serve the model in the box next to this, and the model list
   * silently follows the key. Naming the provider makes an empty or shrinking
   * model dropdown legible instead of surprising.
   *
   * In the option text rather than an `<optgroup>` header, even though
   * grouping would read better while the menu is open: a native select shows
   * only the chosen option's text once it is closed, and the closed state is
   * the one you read before pressing Run.
   */
  const optionLabel = (c: Credential) =>
    `${c.name} · ${providerLabel(c.provider)}` +
    (c.id === fallback.id && !single ? " (default)" : "");

  return (
    <label className={compact ? "flex items-center gap-1.5" : "block"}>
      <span
        className={
          compact
            ? "text-[10px] uppercase tracking-wide text-neutral-500"
            : "kicker mb-1 block"
        }
      >
        {label}
      </span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={single}
        // Always carries the full label, because the compact select truncates
        // and the provider is the part that falls off the end.
        title={
          single
            ? `${optionLabel(fallback)} — add another key on the Keys page to choose where a call bills`
            : optionLabel(selected)
        }
        className={`rounded-lg border border-neutral-700 bg-neutral-950 px-2 py-1.5 text-xs outline-none focus:border-neutral-500 disabled:opacity-70 ${
          compact ? "max-w-[230px] truncate" : "w-full"
        }`}
      >
        {/* The default is the empty value, not its id. Sending "" keeps one
            answer to "which key pays" and keeps it server-side, so a default
            changed on the Keys page takes effect without this form knowing. */}
        <option value="">{optionLabel(fallback)}</option>
        {credentials
          .filter((c) => c.id !== fallback.id)
          .map((c) => (
            <option key={c.id} value={c.id}>
              {optionLabel(c)}
            </option>
          ))}
      </select>
    </label>
  );
}
