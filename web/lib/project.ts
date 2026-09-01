"use client";

/**
 * Which project the browser is working in.
 *
 * Not React state and not a context: `request()` in `api.ts` needs the value
 * at fetch time, and it is called from query functions that no provider wraps.
 * A module-level read keeps one source of truth without threading a prop
 * through every call site.
 *
 * **`localStorage` is that source of truth, and `?project=` is an input to it
 * rather than a rival for it.** The first read of a page load adopts a
 * `project` query param into storage and from then on nothing consults the URL
 * again. That distinction is the whole design:
 *
 *  - A shared link still opens against the project it came from. A trace URL
 *    sent to a colleague shows that trace, not whichever project they last
 *    looked at, which is the same reason the source filter lives in the URL.
 *  - But switching project cannot race the URL. An earlier version let the
 *    param win on every read, and switching while one was present did exactly
 *    what it looks like it would: the picker wrote the new selection, the
 *    refetch fired, and the still-present param sent every one of those calls
 *    to the *old* project — a switch that silently undid itself.
 *
 * Adoption is synchronous and happens on the first read, so it lands before
 * the first fetch rather than in an effect afterwards. There is no window in
 * which the page shows one project's numbers under another's name.
 *
 * The value is an id, never a name. Names are editable; the id is what every
 * span partition and every foreign key already points at.
 */

const STORAGE_KEY = "obs.project";
export const PROJECT_PARAM = "project";

// Per page load. A fresh load of a shared link re-runs it; an in-app
// navigation, which never adds the param, does not.
let adopted = false;

/** "" when nothing has been chosen — the backend then uses its default. */
export function currentProjectId(): string {
  if (typeof window === "undefined") return "";

  if (!adopted) {
    adopted = true;
    const fromUrl = new URLSearchParams(window.location.search).get(PROJECT_PARAM);
    if (fromUrl) setCurrentProjectId(fromUrl);
  }

  try {
    return window.localStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    // Private browsing, or storage disabled. The default project is a working
    // app, so this degrades to "no selection" rather than to an error.
    return "";
  }
}

export function setCurrentProjectId(id: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, id);
  } catch {
    // See above: losing the persistence is survivable, throwing here is not.
  }
}
