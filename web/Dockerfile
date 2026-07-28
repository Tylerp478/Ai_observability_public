# Frontend image. Build context is the REPO ROOT so one compose file can point
# both builds at the same context:
#
#   docker build -f web/Dockerfile .
#
FROM node:22-bookworm-slim AS deps
# pnpm is pinned rather than taken from corepack's default: package.json has no
# packageManager field, so an unpinned corepack would drift between the version
# that wrote pnpm-lock.yaml (v9 format) and the one installing from it.
RUN npm install -g pnpm@11.17.0
WORKDIR /app
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

FROM node:22-bookworm-slim AS build
RUN npm install -g pnpm@11.17.0
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY web/ ./

# Build-time, NOT runtime. next.config.ts reads BACKEND_ORIGIN when the config
# is evaluated, and `next build` serialises the resulting rewrite destination
# into .next/standalone's routes manifest. Setting it in the container's
# environment does nothing — the value here is the one that ships. Change the
# compose service name and this has to be rebuilt, not just restarted.
ARG BACKEND_ORIGIN=http://127.0.0.1:8000
ENV BACKEND_ORIGIN=$BACKEND_ORIGIN

ENV NEXT_TELEMETRY_DISABLED=1
RUN pnpm build

FROM node:22-bookworm-slim AS run
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# Runs unprivileged. `node` (uid 1000) ships with the base image.
USER node

COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
COPY --from=build --chown=node:node /app/public ./public

EXPOSE 3000
CMD ["node", "server.js"]
