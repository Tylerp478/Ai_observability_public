# Backend image. Build context is the REPO ROOT, not backend/ — pyproject
# declares obs-sdk as an editable path dependency on ../sdk, so a context of
# backend/ alone cannot resolve it.
#
#   docker build -f backend/Dockerfile .
#
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# The SDK lands first: uv resolves the editable path dependency during sync,
# so the directory has to exist before that runs.
COPY sdk/ ./sdk/
COPY backend/ ./backend/

WORKDIR /app/backend
RUN uv sync --locked

ENV PATH="/app/backend/.venv/bin:$PATH"

# Written by the local StorageBackend: compacted Parquet plus the WAL. A
# named volume mounts here — without one, every trace dies with the container.
ENV OBS_DATA_DIR=/data
RUN mkdir -p /data

EXPOSE 8000
# --forwarded-allow-ips: who may set X-Forwarded-For. Uvicorn's proxy-header
# handling is on by default but only trusts 127.0.0.1, and in compose the
# caller is the frontend container on the bridge network — a different address
# every time it is recreated. Without this the header is ignored and
# `request.client.host` is the frontend's own address, one constant for every
# user, which silently turns the login rate limiter into a global one: five
# fumbled passwords from anyone locks out everyone.
#
# "*" is safe *here specifically* because this service publishes no ports (see
# compose.yaml), so nothing but the frontend can reach it and the only header
# it can see is the one the proxy in front wrote. Do not copy this flag to a
# backend that is directly reachable — there it would let any caller forge its
# own address and mint itself a fresh rate-limit allowance.
CMD ["uvicorn", "obs_backend.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--forwarded-allow-ips", "*"]
