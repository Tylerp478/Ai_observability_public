"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

export function Providers({ children }: { children: React.ReactNode }) {
  // Created in state, not at module scope: a module-level client would be
  // shared across requests during SSR and leak one user's cache into another.
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // Traces are append-only, so briefly stale data is never wrong,
            // just late. Refetching on window focus is what makes the list
            // feel live when you come back to the tab.
            staleTime: 5_000,
            refetchOnWindowFocus: true,
            retry: (failureCount, error) => {
              // Never retry auth failures — the redirect to /login handles
              // those, and retrying just delays it.
              if (error instanceof Error && "status" in error) {
                const status = (error as { status?: number }).status;
                if (status === 401 || status === 403) return false;
              }
              return failureCount < 2;
            },
          },
        },
      }),
  );

  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
