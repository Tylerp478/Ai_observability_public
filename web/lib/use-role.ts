"use client";

import { useQuery } from "@tanstack/react-query";
import { api, type Role } from "@/lib/api";

/**
 * What the signed-in person may do.
 *
 * Reads the same `["me"]` query the shell already runs, so this costs nothing
 * extra and cannot disagree with the identity in the header.
 *
 * **This hides controls; it does not enforce anything.** Every write is
 * refused server-side by the middleware in `main.py` regardless of what the
 * client rendered — a hidden button is a courtesy, not a permission. The point
 * is that a viewer should not be offered a Run button that spends money and
 * then be told no, which teaches them the tool is broken rather than that they
 * are read-only.
 *
 * Defaults to `viewer` while loading and on error. The safe direction: a brief
 * under-offer corrects itself on the next render, where a brief over-offer is
 * a button that fails.
 */
export function useRole(): { role: Role; isAdmin: boolean; isLoading: boolean } {
  const { data, isLoading } = useQuery({
    queryKey: ["me"],
    queryFn: api.me,
    retry: false,
    staleTime: 30_000,
  });

  const role: Role = data?.role ?? "viewer";
  return { role, isAdmin: role === "admin", isLoading };
}
