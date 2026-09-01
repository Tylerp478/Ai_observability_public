"use client";

import { useQuery } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { api, ApiError } from "@/lib/api";

/**
 * Redeeming an invite: choose a password, get an account, land signed in.
 *
 * The token arrives in the URL because that is the only thing an admin can
 * hand someone over any channel they already use. It is single-use and
 * short-lived server-side, so a link left in a chat log stops working rather
 * than staying a standing key to the tool.
 *
 * The page confirms *who the invite is for* before asking for anything. An
 * opaque link that immediately demands a password is indistinguishable from a
 * phishing page; showing the address it was issued to lets the person check it
 * is theirs. That lookup discloses one email to whoever already holds that
 * email's token, which is not a disclosure — it is what they were sent.
 */
export default function AcceptPage() {
  return (
    <Suspense fallback={null}>
      <Accept />
    </Suspense>
  );
}

function Accept() {
  const router = useRouter();
  const token = useSearchParams().get("token") ?? "";

  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const { data, isLoading, isError, error: lookupError } = useQuery({
    queryKey: ["invite", token],
    queryFn: () => api.checkInvite(token),
    enabled: token !== "",
    retry: false,
  });

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    // Checked here as well as server-side, because the mismatch is the one
    // error the server genuinely cannot see: it receives one password.
    if (password !== confirm) {
      setError("Those passwords do not match");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await api.acceptInvite(token, password);
      router.push("/");
      router.refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the backend");
      setBusy(false);
    }
  }

  const dead = token === "" || isError;

  return (
    <main className="flex min-h-dvh items-center justify-center p-6">
      <div className="w-full max-w-sm space-y-5 rounded-xl border border-neutral-800 bg-neutral-900 p-6">
        <div>
          <div className="flex items-center gap-2.5">
            <span className="flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-[7px] border border-sky-700 bg-sky-900">
              <svg viewBox="0 0 256 256" className="h-[15px] w-[15px] fill-sky-400" aria-hidden="true">
                <path d="M247.31 124.76c-.35-.79-8.82-19.58-27.65-38.41C194.57 61.26 162.88 48 128 48S61.43 61.26 36.34 86.35C17.51 105.18 9 124 8.69 124.76a8 8 0 0 0 0 6.5c.35.79 8.82 19.57 27.65 38.4C61.43 194.74 93.12 208 128 208s66.57-13.26 91.66-38.34c18.83-18.83 27.3-37.61 27.65-38.4a8 8 0 0 0 0-6.5M128 192c-30.78 0-57.67-11.19-79.93-33.25A133.5 133.5 0 0 1 25 128a133.3 133.3 0 0 1 23.07-30.75C70.33 75.19 97.22 64 128 64s57.67 11.19 79.93 33.25A133.5 133.5 0 0 1 231.05 128c-7.21 13.46-38.62 64-103.05 64m0-112a48 48 0 1 0 48 48 48.05 48.05 0 0 0-48-48m0 80a32 32 0 1 1 32-32 32 32 0 0 1-32 32" />
              </svg>
            </span>
            <h1 className="text-lg font-medium">AI Observability</h1>
          </div>
          <p className="mt-2 text-sm text-neutral-400">
            {dead
              ? "This invite cannot be used"
              : isLoading
                ? "Checking your invite…"
                : "Set a password to finish setting up your account"}
          </p>
        </div>

        {dead ? (
          <>
            <p
              role="alert"
              className="rounded-lg border border-red-900 bg-red-950 px-3 py-2 text-sm text-red-300"
            >
              {token === ""
                ? "This link is missing its invite token."
                : lookupError instanceof ApiError
                  ? lookupError.message
                  : "Could not reach the backend"}
            </p>
            <p className="text-xs text-neutral-500">
              Invites last seven days and can be used once. Ask whoever invited
              you to send a new one.
            </p>
          </>
        ) : (
          <form onSubmit={onSubmit} className="space-y-5">
            <div className="rounded-lg border border-neutral-800 bg-neutral-950 px-3 py-2.5">
              <p className="text-[11px] uppercase tracking-wide text-neutral-500">
                Account
              </p>
              <p className="truncate text-sm text-neutral-200">{data?.email ?? "…"}</p>
            </div>

            <div className="space-y-3">
              <div>
                <label htmlFor="password" className="mb-1 block text-xs text-neutral-400">
                  Password
                </label>
                <input
                  id="password"
                  type="password"
                  autoComplete="new-password"
                  required
                  minLength={12}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2.5 text-base caret-sky-400 outline-none focus:border-sky-500"
                />
                <p className="mt-1 text-[11px] text-neutral-500">
                  At least 12 characters.
                </p>
              </div>

              <div>
                <label htmlFor="confirm" className="mb-1 block text-xs text-neutral-400">
                  Confirm password
                </label>
                <input
                  id="confirm"
                  type="password"
                  autoComplete="new-password"
                  required
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                  className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2.5 text-base caret-sky-400 outline-none focus:border-sky-500"
                />
              </div>
            </div>

            {error && (
              <p
                role="alert"
                className="rounded-lg border border-red-900 bg-red-950 px-3 py-2 text-sm text-red-300"
              >
                {error}
              </p>
            )}

            <button
              type="submit"
              disabled={busy || isLoading}
              className="w-full rounded-lg btn-primary px-4 py-2.5 text-sm font-medium disabled:opacity-50"
            >
              {busy ? "Setting up…" : "Create account"}
            </button>
          </form>
        )}
      </div>
    </main>
  );
}
