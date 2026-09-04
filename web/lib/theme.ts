/**
 * The accent each person sees.
 *
 * Stored twice, deliberately, and neither copy is the record.
 *
 * The **record** is the user row in Postgres, which is why the colour follows
 * you to a new device. But the server renderer cannot read Postgres — that
 * needs an authenticated request — so the backend publishes the answer as an
 * ordinary cookie, and the root layout reads that to render the right theme
 * with the document. It is a colour, so it is not HttpOnly and not a secret.
 *
 * The client writes the same cookie when you pick a theme, before the save
 * request finishes. That is what makes a reload immediately after picking show
 * the new colour rather than the old one.
 */
export const THEMES = [
  { id: "purple", label: "Purple", swatch: "#9184d9" },
  { id: "blue", label: "Blue", swatch: "#56a3ef" },
  { id: "green", label: "Green", swatch: "#64c37c" },
  { id: "red", label: "Red", swatch: "#e76d67" },
  { id: "orange", label: "Orange", swatch: "#eda15b" },
  { id: "yellow", label: "Yellow", swatch: "#edd962" },
  { id: "black", label: "Black", swatch: "#d1d1d1" },
] as const;

export type ThemeId = (typeof THEMES)[number]["id"];

export const DEFAULT_THEME: ThemeId = "purple";
/** Must match THEME_COOKIE in the backend. */
export const THEME_COOKIE = "obs_theme";

export function isTheme(value: string | null | undefined): value is ThemeId {
  return !!value && THEMES.some((t) => t.id === value);
}

/** Paint it, and remember it.
 *
 * Writing the attribute is the whole visual mechanism — every colour in
 * globals.css hangs off `html[data-theme=…]`. The cookie is so the next
 * document arrives already wearing it instead of correcting itself.
 */
export function applyTheme(theme: ThemeId): void {
  document.documentElement.dataset.theme = theme;
  try {
    const year = 60 * 60 * 24 * 365;
    const secure = window.location.protocol === "https:" ? "; Secure" : "";
    document.cookie = `${THEME_COOKIE}=${theme}; Path=/; Max-Age=${year}; SameSite=Lax${secure}`;
  } catch {
    // Cookies disabled. The theme still applies to this page; it just will not
    // survive a reload, which is cosmetic and not worth failing a render over.
  }
}
