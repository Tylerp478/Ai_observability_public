"use client";

import { type QueryClient, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { PROJECT_PARAM, currentProjectId, setCurrentProjectId } from "@/lib/project";
import { ThemePicker } from "@/components/theme-picker";

/**
 * Auth gate plus chrome for signed-in pages.
 *
 * The check is client-side: the session cookie is HttpOnly, so a server
 * component can't read it without forwarding headers, and this is a
 * single-user prototype. The real enforcement is the backend returning 401 —
 * this only decides what to render. A bypass here reveals nothing, because
 * every data call still needs the cookie.
 */
export function Shell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ["me"],
    queryFn: api.me,
    retry: false,
  });

  useEffect(() => {
    if (isError && error instanceof ApiError && error.status === 401) {
      router.replace("/login");
    }
  }, [isError, error, router]);

  if (isLoading) {
    return (
      <div className="flex min-h-dvh items-center justify-center text-sm text-neutral-500">
        Loading…
      </div>
    );
  }

  if (isError) {
    const unreachable = !(error instanceof ApiError);
    return (
      <div className="flex min-h-dvh items-center justify-center p-6 text-center text-sm text-neutral-400">
        {unreachable ? (
          <div className="space-y-2">
            <p className="text-neutral-300">Can&apos;t reach the backend.</p>
            <p className="font-mono text-xs text-neutral-500">
              uv run uvicorn obs_backend.main:app --port 8000
            </p>
          </div>
        ) : (
          <p>Redirecting to sign in…</p>
        )}
      </div>
    );
  }

  const isAdmin = data?.role === "admin";

  const nav = [
    { href: "/", label: "Overview" },
    { href: "/traces", label: "Traces" },
    { href: "/playground", label: "Playground" },
    { href: "/datasets", label: "Datasets" },
    { href: "/prompts", label: "Prompts" },
    { href: "/scorers", label: "Scorers" },
    { href: "/guardrails", label: "Guardrails" },
    // Both admin-only, and hidden rather than disabled: the backend refuses
    // to list what either page shows for a viewer, so a nav item leading only
    // to 403s is worse than no nav item.
    //
    // "Keys" rather than "API keys", and the page itself says what kind.
    // People is separate because it is the only screen about *people* rather
    // than about credentials, and because revoking someone should not be
    // three clicks deep on a page about API keys.
    ...(isAdmin
      ? [
          { href: "/keys", label: "Keys" },
          { href: "/people", label: "People" },
        ]
      : []),
  ];

  // Initials for the header avatar. The local part is all we have — an email
  // like "ada.lovelace@…" gives "AL", a bare "ada@…" gives "AD".
  const local = (data?.email ?? "").split("@")[0];
  const parts = local.split(/[._-]+/).filter(Boolean);
  const initials = (
    parts.length > 1 ? parts[0][0] + parts[1][0] : local.slice(0, 2)
  ).toUpperCase();

  return (
    <div className="flex min-h-dvh flex-col">
      <header className="sticky top-0 z-10 bg-neutral-950/90 backdrop-blur">
        <div className="mx-auto flex max-w-5xl items-center gap-2.5 px-4 pt-3">
          {/* mr-auto lives on the wrapper, not on the byline: the byline is
              display:none below sm, and an auto margin on a hidden element
              doesn't push anything, which would collapse the whole right-hand
              group leftward on a phone. */}
          <div className="mr-auto flex min-w-0 items-baseline gap-2.5">
            <Link href="/" className="flex min-w-0 items-center gap-2.5">
              <span className="flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-[7px] border border-sky-700 bg-sky-900">
                <EyeMark />
              </span>
              {/* Truncates rather than wraps. The full row measures ~328px, so
                  it fits a 375px phone; below ~360px the wordmark gives way
                  first, which is the right thing to lose. */}
              <span className="truncate text-[18px] font-medium tracking-tight">
                AI Observability
              </span>
            </Link>

            {/* A byline, so it sits outside the link — clicking a credit
                shouldn't navigate. Nocturne's caption treatment: 11px at 55%
                of the text colour, which is what separates it from the
                wordmark. Hidden below sm; the row is already ~328px of the
                343px a 375px phone gives it. */}
            <span className="hidden shrink-0 text-[11px] text-neutral-500 sm:inline">
              made by Tyler Phillips
            </span>
          </div>

          {/* useSearchParams needs a Suspense boundary above it or the page
              cannot be prerendered — and this one sits in the shell, so every
              page would inherit the problem. */}
          <Suspense fallback={null}>
            <ProjectPicker />
          </Suspense>

          <span
            title={data?.email}
            className="flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-full bg-sky-800 text-[12px] font-semibold text-sky-100"
          >
            {initials}
          </span>

          <ThemePicker current={data?.theme ?? "purple"} />

          <button
            onClick={async () => {
              await api.logout();
              router.replace("/login");
            }}
            className="text-xs text-neutral-500 hover:text-sky-400"
          >
            Sign out
          </button>
        </div>

        {/* Scrolls rather than wraps. Step 5's fifth item pushes the row past
            375px, and a header that grows to two lines shoves the page content
            down on exactly the device this has to be usable on. */}
        <nav className="mx-auto flex max-w-5xl gap-1.5 overflow-x-auto px-4 pt-2.5 pb-3 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          {nav.map((item) => {
            // Exact match for the root: "/foo".startsWith("/") is true, so a
            // prefix test would light up Overview on every page.
            const active =
              item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`whitespace-nowrap rounded-lg px-3 py-1.5 text-[13px] font-medium ${
                  active
                    ? "bg-sky-900 text-sky-400"
                    : "text-neutral-300 hover:bg-neutral-900 hover:text-neutral-100"
                }`}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>

        {/* Nocturne's fading rule instead of a hard border — the header's
            edge dissolves at both ends rather than boxing the page in. */}
        <div className="hr-fade" />
      </header>

      <main className="mx-auto w-full max-w-5xl flex-1 px-4 py-5">
        {/* Said once, at the top, rather than repeated as a tooltip on every
            disabled control. A viewer should learn the shape of their access
            in one sentence instead of discovering it one greyed-out button at
            a time. */}
        {data && !isAdmin && (
          <p className="mb-4 rounded-xl border border-neutral-800 bg-neutral-900/40 px-3.5 py-2.5 text-xs text-neutral-400">
            <span className="font-medium text-neutral-200">Read-only access.</span>{" "}
            You can see everything here. Running, editing and anything that
            spends money are an admin&apos;s to do.
          </p>
        )}
        {children}
      </main>

      <footer className="px-4 pb-6 text-center text-[11px] text-neutral-600">
        {data?.email}
      </footer>
    </div>
  );
}

/** Drop every answer that was given for the previous project, and refetch
 *  whatever is on screen. Everything except the projects list itself, which is
 *  the same list from inside any of them. */
function resetProjectScopedQueries(qc: QueryClient) {
  qc.resetQueries({ predicate: (q) => q.queryKey[0] !== "projects" });
  qc.invalidateQueries({ queryKey: ["projects"] });
}

/**
 * Which project everything on the page belongs to.
 *
 * In the header rather than on a settings page because it scopes every number
 * below it. It sits where the "Local" chip did — that chip said the same thing
 * on every deploy of this app, and the space is better spent on the one label
 * that changes what the page means.
 *
 * **Rendered even with a single project**, for the reason the credential
 * picker is: with one project this is not a choice, it is the disclosure of
 * which project the spend on screen belongs to. It renders disabled so it
 * reads as a fact rather than a control that does nothing.
 *
 * **Switching resets the query cache.** Query keys here do not carry the
 * project, so a cached `["overview", 24]` from the old project would render
 * under the new project's name — the exact wrong number under a confident
 * label. `reset` rather than `invalidate`: invalidating refetches but keeps
 * serving the old answer until the new one lands, which is precisely the
 * mislabelled number, just for a shorter time. `clear` is wrong in the other
 * direction — it empties the cache without asking anything to refetch, so the
 * page simply keeps whatever it had.
 *
 * The projects list is the one query deliberately left out of that: it is not
 * project-scoped, it answers the same either way, and blanking it would
 * unmount this control mid-switch.
 *
 * Switching also strips `?project=` from the URL. Nothing reads it after the
 * first fetch of a page load (see `lib/project.ts`), so this is only to stop a
 * later reload of that URL from re-adopting a project the user has since
 * switched away from.
 */
function ProjectPicker() {
  const qc = useQueryClient();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();

  const { data } = useQuery({
    queryKey: ["projects"],
    queryFn: api.projects,
    staleTime: 30_000,
  });

  const projects = data?.projects ?? [];
  const current = data?.current ?? "";
  const stored = typeof window === "undefined" ? "" : currentProjectId();

  // What the select shows. Local state so the label moves on click rather
  // than a round trip later; `current` is the authority once it catches up.
  const [selected, setSelected] = useState<string | null>(null);

  // Recovery only: adopt the backend's answer when what is stored names no
  // project it knows about — a client carrying an id from before a database
  // was reset, where every other route is 400ing and this is what ends it.
  //
  // **Never when the stored id is valid**, even if it disagrees with `current`.
  // They disagree for a moment after every switch, because `current` is still
  // the previous answer until the refetch lands, and an earlier version that
  // adopted on any mismatch used that moment to undo the switch: stripping
  // `?project=` remounts this component, the effect re-ran against the stale
  // value, and the selection reverted to the project the user had just left.
  const known = projects.some((p) => p.id === stored);
  useEffect(() => {
    if (!current || known) return;
    setCurrentProjectId(current);
    resetProjectScopedQueries(qc);
  }, [current, known, qc]);

  if (projects.length === 0) return null;

  const switchTo = (id: string) => {
    setSelected(id);
    setCurrentProjectId(id);
    resetProjectScopedQueries(qc);
    if (params.get(PROJECT_PARAM)) {
      const query = new URLSearchParams(params.toString());
      query.delete(PROJECT_PARAM);
      const qs = query.toString();
      router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
    }
  };

  return (
    <select
      value={selected ?? current}
      onChange={(e) => switchTo(e.target.value)}
      disabled={projects.length === 1}
      title={
        projects.length === 1
          ? "Add a project on the Keys page to work in more than one"
          : "The project every number on this page belongs to"
      }
      aria-label="Project"
      className="max-w-[150px] truncate rounded-md bg-neutral-800 px-2 py-[3px] text-[11px] text-neutral-100 outline-none focus:ring-1 focus:ring-sky-700 disabled:opacity-70"
    >
      {projects.map((p) => (
        <option key={p.id} value={p.id}>
          {p.name}
        </option>
      ))}
    </select>
  );
}

/** Inlined rather than pulled from an icon font — the mock's Phosphor CDN tag
 *  is one script this app doesn't need for a single 15px glyph. */
function EyeMark() {
  return (
    <svg
      viewBox="0 0 256 256"
      className="h-[15px] w-[15px] fill-sky-400"
      aria-hidden="true"
    >
      <path d="M247.31 124.76c-.35-.79-8.82-19.58-27.65-38.41C194.57 61.26 162.88 48 128 48S61.43 61.26 36.34 86.35C17.51 105.18 9 124 8.69 124.76a8 8 0 0 0 0 6.5c.35.79 8.82 19.57 27.65 38.4C61.43 194.74 93.12 208 128 208s66.57-13.26 91.66-38.34c18.83-18.83 27.3-37.61 27.65-38.4a8 8 0 0 0 0-6.5M128 192c-30.78 0-57.67-11.19-79.93-33.25A133.5 133.5 0 0 1 25 128a133.3 133.3 0 0 1 23.07-30.75C70.33 75.19 97.22 64 128 64s57.67 11.19 79.93 33.25A133.5 133.5 0 0 1 231.05 128c-7.21 13.46-38.62 64-103.05 64m0-112a48 48 0 1 0 48 48 48.05 48.05 0 0 0-48-48m0 80a32 32 0 1 1 32-32 32 32 0 0 1-32 32" />
    </svg>
  );
}
