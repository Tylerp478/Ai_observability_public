"use client";

import { useQuery } from "@tanstack/react-query";
import { api, FALLBACK_MODELS } from "@/lib/api";

/**
 * Which models a picker should offer, given the key that will pay.
 *
 * Two shapes of caller, because there are two questions being asked:
 *
 *  - **A surface that generates** (the Playground, the replay run form) passes
 *    the selected credential id. Only that vendor's models are offered, since
 *    a Claude model on an xAI key is refused server-side before it spends —
 *    and a dropdown that lets you pick a guaranteed error is a worse way to
 *    learn that than not offering it.
 *  - **A surface that defines** (a scorer's judge model, a prompt's model)
 *    passes nothing. It offers every model from every provider you hold a key
 *    for, because a scorer's model is what *chooses* the vendor rather than
 *    being constrained by one — judging routes to whichever key can serve it.
 *
 * Never offers a model from a provider with no key: the only thing selecting
 * it could produce is a failure at spend time.
 *
 * **Returns [] while loading, rather than a guess.** Falling back to a Claude
 * model during the fetch would show the wrong model to someone holding only an
 * xAI key and then swap it under them — a flash of a value that was never
 * true. Callers render an empty select for that instant and guard submit on
 * having a model, which cannot mislead. FALLBACK_MODELS is only for the case
 * where the fetch succeeded and genuinely produced nothing.
 */
export function useModels(credentialId?: string): string[] {
  const { data: providerData, isPending: providersPending } = useQuery({
    queryKey: ["providers"],
    queryFn: api.providers,
    // The registry changes when the app is redeployed, not while it is open.
    staleTime: 5 * 60_000,
  });
  const { data: credentialData, isPending: credentialsPending } = useQuery({
    queryKey: ["credentials"],
    queryFn: api.credentials,
    staleTime: 30_000,
  });

  if (providersPending || credentialsPending) return [];

  const providers = providerData?.providers ?? [];
  const credentials = credentialData?.credentials ?? [];
  if (providers.length === 0 || credentials.length === 0) {
    return [...FALLBACK_MODELS];
  }

  if (credentialId !== undefined) {
    // "" means the project default, which is what the picker sends and what
    // the backend resolves. Mirrored here so the offered models match the key
    // that will actually be billed.
    const fallback = credentials.find((c) => c.is_default) ?? credentials[0];
    const chosen = credentials.find((c) => c.id === credentialId) ?? fallback;
    const models = providers.find((p) => p.name === chosen.provider)?.models ?? [];
    return models.length > 0 ? models : [...FALLBACK_MODELS];
  }

  const held = new Set(credentials.map((c) => c.provider));
  const models = providers.filter((p) => held.has(p.name)).flatMap((p) => p.models);
  return models.length > 0 ? models : [...FALLBACK_MODELS];
}

/**
 * What a provider is called, for display.
 *
 * A credential stores the registry's `name` — "anthropic", "xai", "google" —
 * which is a routing key, not a label. Anywhere a key's provider is shown to a
 * person it should read the way the vendor writes it, and from one definition,
 * so the Keys page and the credential picker cannot end up calling the same
 * provider two different things.
 *
 * Falls back to the raw name until `/api/providers` answers. That is
 * unprettified rather than wrong — the same string, lowercased — so a slow
 * fetch never briefly attributes a key to the wrong vendor.
 */
export function useProviderLabel(): (name: string) => string {
  const { data } = useQuery({
    queryKey: ["providers"],
    queryFn: api.providers,
    staleTime: 5 * 60_000,
  });

  const providers = data?.providers ?? [];
  return (name: string) => providers.find((p) => p.name === name)?.label ?? name;
}
