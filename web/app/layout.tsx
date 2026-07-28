import type { Metadata, Viewport } from "next";
import { Inter, Geist_Mono } from "next/font/google";
import "./globals.css";
import { Providers } from "./providers";

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

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="en"
      className={`${inter.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="flex min-h-full flex-col bg-neutral-950 font-sans text-neutral-100">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
