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

const nextConfig: NextConfig = {
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
