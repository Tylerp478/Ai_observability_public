import type { Metadata, Viewport } from "next";
import { Inter, Geist_Mono } from "next/font/google";
import { cookies } from "next/headers";
import "./globals.css";
import { Providers } from "./providers";
import { DEFAULT_THEME, THEME_COOKIE, isTheme } from "@/lib/theme";

// Nocturne is an Inter system — its heading and body faces are the same
// family, separated only by weight. Mono stays Geist; the mock left monospace
// to the platform, and the waterfall reads better on a face with real digits.
const inter = Inter({ variable: "--font-inter", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "AI Observability",
  description: "Trace capture and inspection for LLM calls, tools, and agent steps",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  // The app is used on a phone; a fixed viewport would make the waterfall
  // unreadable at the default zoom.
  maximumScale: 5,
};

/**
 * The theme is read here, on the server, so the document arrives already
 * wearing it.
 *
 * The usual trick is a blocking inline script that rewrites the attribute
 * before first paint. It works, but it means shipping markup you know to be
 * wrong and fixing it a moment later, which costs a hydration mismatch on
 * every single load. The backend publishes the choice as a plain cookie
 * instead — it is a colour, not a secret — and this reads it.
 *
 * **This is what makes every route dynamic.** `cookies()` opts the whole app
 * out of static generation, which is a real cost and an easy one here: every
 * page is behind a login and fetches its data client-side anyway, so what
 * static rendering was producing was an empty shell.
 */
export default async function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  const stored = (await cookies()).get(THEME_COOKIE)?.value;
  const theme = isTheme(stored) ? stored : DEFAULT_THEME;

  return (
    <html
      lang="en"
      data-theme={theme}
      className={`${inter.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="flex min-h-full flex-col bg-neutral-950 font-sans text-neutral-100">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
