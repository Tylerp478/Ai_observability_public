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
CMD ["uvicorn", "obs_backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
