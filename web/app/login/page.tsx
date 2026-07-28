"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { api, ApiError } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.login(email, password);
      router.push("/traces");
      router.refresh();
    } catch (err) {
      // Surface the rate-limit message specifically — "invalid password" when
      // you're actually locked out sends you hunting for the wrong problem.
      setError(
        err instanceof ApiError ? err.message : "Could not reach the backend",
      );
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-dvh items-center justify-center p-6">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm space-y-5 rounded-xl border border-neutral-800 bg-neutral-900 p-6"
      >
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
            Sign in to inspect traces
          </p>
        </div>

        <div className="space-y-3">
          <div>
            <label htmlFor="email" className="mb-1 block text-xs text-neutral-400">
              Email
            </label>
            <input
              id="email"
              type="email"
              inputMode="email"
              autoComplete="username"
              autoCapitalize="none"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2.5 text-base caret-sky-400 outline-none focus:border-sky-500"
            />
          </div>

          <div>
            <label htmlFor="password" className="mb-1 block text-xs text-neutral-400">
              Password
            </label>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
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
          disabled={busy}
          className="w-full rounded-lg btn-primary px-4 py-2.5 text-sm font-medium disabled:opacity-50"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
