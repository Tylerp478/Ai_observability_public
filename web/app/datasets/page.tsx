"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { Shell } from "../shell";
import { api, ApiError, relativeTime } from "@/lib/api";

export default function DatasetsPage() {
  return (
    <Shell>
      <DatasetList />
    </Shell>
  );
}

function DatasetList() {
  const queryClient = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");

  const { data, isLoading, isError } = useQuery({
    queryKey: ["datasets"],
    queryFn: api.datasets,
  });

  const create = useMutation({
    mutationFn: () => api.createDataset(name, description),
    onSuccess: () => {
      setName("");
      setDescription("");
      setCreating(false);
      queryClient.invalidateQueries({ queryKey: ["datasets"] });
    },
  });

  if (isLoading) {
    return <p className="py-10 text-center text-sm text-neutral-500">Loading datasets…</p>;
  }
  if (isError) {
    return <p className="py-10 text-center text-sm text-red-400">Failed to load datasets.</p>;
  }

  const datasets = data?.datasets ?? [];

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <h1 className="text-base font-semibold">Datasets</h1>
        <button
          onClick={() => setCreating((v) => !v)}
          className="rounded-lg border border-neutral-700 px-2.5 py-1 text-xs text-neutral-300 hover:border-neutral-600"
        >
          {creating ? "Cancel" : "New dataset"}
        </button>
      </div>

      {creating && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate();
          }}
          className="space-y-2 rounded-xl border border-neutral-800 bg-neutral-900 p-3.5"
        >
          <input
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Name, e.g. support-questions"
            className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-2.5 py-2 text-sm outline-none focus:border-neutral-500"
          />
          <input
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What this dataset is for (optional)"
            className="w-full rounded-lg border border-neutral-700 bg-neutral-950 px-2.5 py-2 text-sm outline-none focus:border-neutral-500"
          />
          {create.isError && (
            <p className="text-xs text-red-400">
              {create.error instanceof ApiError
                ? create.error.message
                : "Could not create dataset."}
            </p>
          )}
          <button
            type="submit"
            disabled={!name.trim() || create.isPending}
            className="rounded-lg btn-primary px-3 py-1.5 text-xs font-medium disabled:opacity-40"
          >
            {create.isPending ? "Creating…" : "Create"}
          </button>
        </form>
      )}

      {datasets.length === 0 ? (
        <div className="rounded-xl border border-dashed border-neutral-800 px-6 py-12 text-center">
          <p className="text-sm text-neutral-300">No datasets yet</p>
          <p className="mt-2 text-xs text-neutral-500">
            Open a trace and use{" "}
            <span className="text-neutral-400">Save as test case</span> on an LLM
            span, or create an empty dataset above.
          </p>
        </div>
      ) : (
        <ul className="space-y-2">
          {datasets.map((d) => (
            <li key={d.id}>
              <Link
                href={`/datasets/${d.id}`}
                className="block rounded-xl border border-neutral-800 bg-neutral-900 p-3.5 transition-colors hover:border-neutral-700"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium">{d.name}</p>
                    {d.description && (
                      <p className="mt-0.5 truncate text-[11px] text-neutral-500">
                        {d.description}
                      </p>
                    )}
                  </div>
                  <p className="shrink-0 text-[11px] text-neutral-500">
                    {d.last_run_at ? `run ${relativeTime(Date.parse(d.last_run_at) * 1e6)}` : "never run"}
                  </p>
                </div>
                <div className="mt-2 flex gap-3 text-[11px] text-neutral-500">
                  <span>
                    {d.item_count} {d.item_count === 1 ? "case" : "cases"}
                  </span>
                  <span>
                    {d.run_count} {d.run_count === 1 ? "run" : "runs"}
                  </span>
                </div>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
