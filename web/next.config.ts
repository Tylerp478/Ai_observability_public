import { networkInterfaces } from "node:os";
import type { NextConfig } from "next";

/**
 * Proxy API calls through the Next.js origin.
 *
 * Without this the browser holds a session cookie issued by the backend's
 * origin (127.0.0.1:8000) while the page runs on localhost:3000. Those are
 * different sites, so a SameSite=Lax cookie is never sent back: login appears
 * to succeed, then every subsequent call 401s and bounces to the login page.
 *
 * Proxying makes the cookie first-party. It also means exposing this through a
 * tunnel is one origin rather than two, which avoids needing
 * SameSite=None + Secure + a CORS allowlist just to sign in.
 *
 * The backend keeps its CORS config for the case where the frontend and
 * backend really are deployed on separate domains.
 */
// Read when this config is evaluated, which for a production build means
// `next build` bakes the resulting destination into the routes manifest.
// Setting BACKEND_ORIGIN on the running container has no effect — see the
// build arg in web/Dockerfile.
const BACKEND = process.env.BACKEND_ORIGIN ?? "http://127.0.0.1:8000";

/**
 * Origins `next dev` will serve its client assets to.
 *
 * Next 16 blocks cross-origin dev requests: reach the dev server on anything
 * other than localhost and the page still server-renders, but the HMR socket
 * and the client bundle are refused, so React never hydrates. The failure is
 * silent and looks nothing like a permissions problem — the page paints, and
 * then nothing that needs JavaScript works. A login form posts nowhere, and a
 * page that fetches on mount sits on its loading state forever.
 *
 * That is exactly what testing from a phone looks like, so the machine's own
 * LAN addresses are computed rather than written down: a hardcoded IP goes
 * stale the next time DHCP moves, and it goes stale silently, in the same
 * shape as the bug it was meant to fix.
 *
 * Dev only — `next build` and `next start` ignore this entirely.
 */
function lanOrigins(): string[] {
  const nets = networkInterfaces();
  return Object.values(nets)
    .flatMap((addrs) => addrs ?? [])
    .filter((a) => a.family === "IPv4" && !a.internal)
    .map((a) => a.address);
}

const nextConfig: NextConfig = {
  allowedDevOrigins: lanOrigins(),

  // Traces the server's actual imports into .next/standalone, so the runtime
  // image ships that instead of the whole node_modules tree. On a 2 GB box
  // that is the difference between a ~200 MB image and a ~1 GB one. No effect
  // on `next dev`.
  output: "standalone",

  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${BACKEND}/api/:path*` },
      // OTLP ingest, so an SDK can point at the same origin as the UI.
      { source: "/v1/:path*", destination: `${BACKEND}/v1/:path*` },
    ];
  },
};

export default nextConfig;
