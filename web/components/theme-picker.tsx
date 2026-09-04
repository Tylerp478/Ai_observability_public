"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { THEMES, type ThemeId, applyTheme, isTheme } from "@/lib/theme";

/**
 * Pick your accent. Everyone gets this, viewers included.
 *
 * **Applied before the request, not after it.** Colour is the one setting
 * where the confirmation *is* the result — you can see whether it worked — so
 * waiting for a round trip to repaint would make the control feel broken on a
 * slow connection. If the save fails the swatch snaps back and says so, which
 * is the honest version of optimism.
 */
export function ThemePicker({ current }: { current: string }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [failed, setFailed] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  // The server is the record; `pending` is only what this tab clicked before
  // the record caught up. Derived rather than copied into state and synced by
  // an effect — a copy has to be kept in step with the thing it copied, and
  // the moment it drifts the swatch and the page disagree about the colour.
  const stored: ThemeId = isTheme(current) ? current : "purple";
  const [pending, setPending] = useState<ThemeId | null>(null);
  const shown = pending ?? stored;

  // Paint whatever is showing. Runs on the server's answer too, which is what
  // makes the colour follow you onto a device that never stored it locally.
  useEffect(() => {
    applyTheme(shown);
  }, [shown]);

  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  const save = useMutation({
    mutationFn: (theme: ThemeId) => api_setTheme(theme),
    onError: () => {
      // Drop back to what the server still believes, so the swatch never
      // claims a preference that was not stored. One assignment, because
      // `shown` is derived — there is no second copy to walk back.
      setPending(null);
      setFailed(true);
    },
    onSuccess: () => {
      setFailed(false);
      qc.invalidateQueries({ queryKey: ["me"] });
    },
  });

  const choose = (theme: ThemeId) => {
    setPending(theme);
    save.mutate(theme);
    setOpen(false);
  };

  const active = THEMES.find((t) => t.id === shown) ?? THEMES[0];

  return (
    <div ref={box} className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        title={failed ? "Could not save your theme" : `Theme: ${active.label}`}
        className="flex h-[30px] w-[30px] items-center justify-center rounded-full border border-neutral-700 hover:border-neutral-500"
      >
        <span
          className="h-[14px] w-[14px] rounded-full"
          style={{ background: "var(--color-accent)" }}
        />
        <span className="sr-only">Change theme (currently {active.label})</span>
      </button>

      {open && (
        <div
          role="menu"
          className="absolute right-0 z-20 mt-1.5 w-40 rounded-xl border border-neutral-800 bg-neutral-900 p-1 elev-md"
        >
          {THEMES.map((t) => (
            <button
              key={t.id}
              role="menuitemradio"
              aria-checked={t.id === shown}
              onClick={() => choose(t.id)}
              className={`flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs hover:bg-neutral-850 ${
                t.id === shown ? "text-neutral-100" : "text-neutral-400"
              }`}
            >
              <span
                className="h-3 w-3 shrink-0 rounded-full ring-1 ring-neutral-700"
                style={{ background: t.swatch }}
              />
              {t.label}
              {t.id === shown && <span className="ml-auto text-[10px]">✓</span>}
            </button>
          ))}
          {failed && (
            <p className="px-2 py-1 text-[10px] text-red-400">
              Could not save — showing what is stored.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

// Imported lazily through a thin wrapper so this component does not pull the
// whole api module into the header's critical path.
async function api_setTheme(theme: ThemeId) {
  const { api } = await import("@/lib/api");
  return api.setTheme(theme);
}
