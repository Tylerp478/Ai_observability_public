"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Shell } from "../shell";
import { AdminOnly } from "@/components/admin-only";
import { api, ApiError, type Role } from "@/lib/api";

export default function PeoplePage() {
  return (
    <Shell>
      <AdminOnly hint="Who can sign in, their roles, and invites live here. Ask an admin if you need access changed.">
        <People />
      </AdminOnly>
    </Shell>
  );
}

/**
 * Who can sign in, and what they can do once they have.
 *
 * Its own page, not a section on Keys. Everything on that page is a
 * *credential or a boundary* — what may talk to this backend and what it is
 * scoped to. This is the only screen about **people**, it is the one an admin
 * comes to deliberately rather than while doing something else, and burying a
 * revoke behind a collapsed section on a page about API keys makes the fastest
 * thing you might ever need to do the hardest to find.
 *
 * **The invite link is built from `window.location.origin`.** No hostname is
 * configured anywhere in this app, and the URL you are reading this on is the
 * one URL known to reach it — so the link is correct on localhost, on a
 * tunnel, and on a deployed box, without any of them being written down. That
 * is also why there is no email: delivering the link is your job, over
 * whatever channel you already use.
 */
function People() {
  const qc = useQueryClient();
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [note, setNote] = useState("");
  const [fresh, setFresh] = useState<{ email: string; link: string } | null>(null);
  const [copied, setCopied] = useState(false);

  const { data, isLoading } = useQuery({ queryKey: ["people"], queryFn: api.people });

  const invite = useMutation({
    mutationFn: () => api.invitePerson({ email, name, role, note }),
    onSuccess: (res) => {
      setFresh({
        email: res.email,
        link: `${window.location.origin}/accept?token=${encodeURIComponent(res.token)}`,
      });
      setEmail("");
      setName("");
      setNote("");
      qc.invalidateQueries({ queryKey: ["people"] });
    },
  });

  const changeRole = useMutation({
    mutationFn: ({ e, r }: { e: string; r: Role }) => api.setPersonRole(e, r),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["people"] }),
  });

  const revoke = useMutation({
    mutationFn: (e: string) => api.revokePerson(e),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["people"] }),
  });

  const reset = useMutation({
    mutationFn: (e: string) => api.resetPersonPassword(e),
    onSuccess: (res) => {
      setFresh({
        email: res.email,
        link: `${window.location.origin}/accept?token=${encodeURIComponent(res.token)}`,
      });
      qc.invalidateQueries({ queryKey: ["people"] });
    },
  });

  const people = data?.people ?? [];
  const active = people.filter((p) => !p.revoked_at);

  const status = (p: (typeof people)[number]) => {
    if (p.revoked_at) return { label: "REVOKED", tone: "bg-neutral-800 text-neutral-400" };
    if (p.invite_pending) return { label: "INVITED", tone: "bg-amber-900 text-amber-300" };
    if (p.reset_pending) return { label: "RESET SENT", tone: "bg-amber-900 text-amber-300" };
    return { label: p.role.toUpperCase(), tone: "bg-sky-900 text-sky-300" };
  };

  return (
    <div className="space-y-3">
      <div>
        <h1 className="text-base font-semibold">
          People
          <span className="ml-2 rounded bg-neutral-800 px-1.5 py-0.5 align-middle text-[10px] tabular-nums text-neutral-400">
            {active.length}
          </span>
        </h1>
        <p className="mt-1 text-xs text-neutral-500">
          An admin can do everything. A viewer sees every page and changes
          nothing — no runs, no edits, nothing that spends. Roles and
          revocations take effect on that person&apos;s next click, not their
          next sign-in.
        </p>
      </div>

      {fresh && (
        <div className="rounded-xl border border-amber-800 bg-amber-950/40 p-3.5">
          <p className="text-xs font-medium text-amber-200">
            Send this to {fresh.email} — it will not be shown again
          </p>
          <code className="mt-2 block overflow-x-auto rounded-lg bg-neutral-950 px-3 py-2 font-mono text-[11px] break-all text-amber-100">
            {fresh.link}
          </code>
          <div className="mt-2 flex gap-2">
            <button
              onClick={() => {
                navigator.clipboard.writeText(fresh.link);
                setCopied(true);
                setTimeout(() => setCopied(false), 1500);
              }}
              className="rounded-lg bg-amber-200 px-2.5 py-1 text-xs font-medium text-amber-950"
            >
              {copied ? "Copied" : "Copy link"}
            </button>
            <button
              onClick={() => setFresh(null)}
              className="rounded-lg px-2.5 py-1 text-xs text-amber-300"
            >
              Dismiss
            </button>
          </div>
          <p className="mt-2 text-[11px] text-amber-300/70">
            Usable once, expires in 7 days.
          </p>
        </div>
      )}

      {isLoading ? (
        <p className="text-xs text-neutral-500">Loading…</p>
      ) : (
        <ul className="space-y-2">
          {people.map((p) => {
            const s = status(p);
            return (
              <li
                key={p.email}
                className="rounded-xl border border-neutral-800 bg-neutral-950/40 px-3.5 py-3"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="truncate text-sm font-medium">
                    {p.name || p.email}
                  </span>
                  <span
                    className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${s.tone}`}
                  >
                    {s.label}
                  </span>
                  {!p.revoked_at && (
                    <div className="ml-auto flex shrink-0 gap-2">
                      <select
                        value={p.role}
                        onChange={(e) =>
                          changeRole.mutate({ e: p.email, r: e.target.value as Role })
                        }
                        aria-label={`Role for ${p.email}`}
                        className="rounded-lg border border-neutral-700 bg-neutral-950 px-2 py-1 text-[11px] outline-none focus:border-neutral-500"
                      >
                        {(data?.roles ?? []).map((r) => (
                          <option key={r} value={r}>
                            {r}
                          </option>
                        ))}
                      </select>
                      {p.accepted_at && (
                        <button
                          onClick={() => reset.mutate(p.email)}
                          disabled={reset.isPending}
                          className="rounded-lg border border-neutral-700 px-2.5 py-1 text-[11px] text-neutral-300 disabled:opacity-40"
                        >
                          Reset password
                        </button>
                      )}
                      <button
                        onClick={() => revoke.mutate(p.email)}
                        className="rounded-lg border border-neutral-700 px-2.5 py-1 text-[11px] text-neutral-300"
                      >
                        Revoke
                      </button>
                    </div>
                  )}
                </div>
                <p className="mt-1 font-mono text-[11px] text-neutral-500">
                  {p.email}
                  {" · "}
                  {p.last_seen_at
                    ? `last seen ${new Date(p.last_seen_at).toLocaleDateString()}`
                    : p.accepted_at
                      ? "not seen recently"
                      : "never signed in"}
                </p>
                {p.note && (
                  <p className="mt-1 text-[11px] text-neutral-500">{p.note}</p>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {(changeRole.isError || revoke.isError || reset.isError) && (
        <p className="text-xs text-red-400">
          {(changeRole.error ?? revoke.error ?? reset.error) instanceof ApiError
            ? ((changeRole.error ?? revoke.error ?? reset.error) as ApiError).message
            : "That change did not go through."}
        </p>
      )}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (email.trim()) invite.mutate();
        }}
        className="space-y-2"
      >
        <div className="flex flex-wrap gap-2">
          <input
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            type="email"
            autoCapitalize="none"
            placeholder="Email"
            className="min-w-0 flex-1 rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm outline-none focus:border-neutral-500"
          />
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Name (what you'll recognise)"
            className="min-w-0 flex-1 rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm outline-none focus:border-neutral-500"
          />
        </div>
        <div className="flex flex-wrap gap-2">
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Note (optional)"
            className="min-w-0 flex-1 rounded-lg border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm outline-none focus:border-neutral-500"
          />
          <select
            value={role}
            onChange={(e) => setRole(e.target.value as Role)}
            aria-label="Role for the new invite"
            className="shrink-0 rounded-lg border border-neutral-700 bg-neutral-950 px-2 py-2 text-sm outline-none focus:border-neutral-500"
          >
            {(data?.roles ?? ["viewer", "admin"]).map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
          <button
            type="submit"
            disabled={invite.isPending || !email.trim()}
            className="shrink-0 rounded-lg btn-primary px-3 py-2 text-sm font-medium disabled:opacity-40"
          >
            Invite
          </button>
        </div>
      </form>

      {invite.isError && (
        <p className="text-xs text-red-400">
          {invite.error instanceof ApiError
            ? invite.error.message
            : "Could not create the invite."}
        </p>
      )}
    </div>
  );
}
