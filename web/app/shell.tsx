"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import { api, ApiError } from "@/lib/api";

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

  const nav = [
    { href: "/", label: "Overview" },
    { href: "/traces", label: "Traces" },
    { href: "/datasets", label: "Datasets" },
    { href: "/prompts", label: "Prompts" },
    { href: "/scorers", label: "Scorers" },
    { href: "/guardrails", label: "Guardrails" },
    // "Keys" rather than "API keys", and the page itself says what kind.
    { href: "/keys", label: "Keys" },
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

          <span className="rounded-md bg-neutral-800 px-2.5 py-[3px] text-[11px] text-neutral-100">
            Local
          </span>

          <span
            title={data?.email}
            className="flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-full bg-sky-800 text-[12px] font-semibold text-sky-100"
          >
            {initials}
          </span>

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

      <main className="mx-auto w-full max-w-5xl flex-1 px-4 py-5">{children}</main>

      <footer className="px-4 pb-6 text-center text-[11px] text-neutral-600">
        {data?.email}
      </footer>
    </div>
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
