"use client";

import { useRole } from "@/lib/use-role";

/**
 * The whole page, or a sentence explaining why not.
 *
 * The nav item is already hidden for a viewer, but a hidden link is not a
 * boundary — these URLs can be typed, bookmarked, or arrived at from a stale
 * tab. Without this a page renders its shell with every section empty and
 * every admin button live but doomed, which reads as "this tool is broken"
 * rather than "this is not yours".
 *
 * The backend refuses each of these reads independently; this only decides
 * what to draw.
 */
export function AdminOnly({
  children,
  hint,
}: {
  children: React.ReactNode;
  /** What lives behind this page, so the refusal says what was missed. */
  hint: string;
}) {
  const { isAdmin, isLoading } = useRole();

  if (isLoading) {
    return <p className="text-sm text-neutral-500">Loading…</p>;
  }
  if (!isAdmin) {
    return (
      <div className="rounded-xl border border-neutral-800 bg-neutral-900/40 px-4 py-8 text-center">
        <p className="text-sm text-neutral-300">This page is for admins</p>
        <p className="mx-auto mt-1 max-w-md text-xs text-neutral-500">
          {hint}
        </p>
      </div>
    );
  }
  return <>{children}</>;
}
