"""Storage interface — one of the two abstractions CLAUDE.md asks for up front.

Two implementations behind one protocol, toggled by OBS_STORAGE_BACKEND:
local filesystem (default, free, no credentials) and S3. Everything above this
layer works in URI-ish key terms and never touches a path or a bucket directly.

The S3 implementation is deliberately a stub in 2a — the interface exists so
that filling it in later is additive, which is the whole point of having the
abstraction now.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Protocol

from obs_backend.config import Settings


class StorageBackend(Protocol):
    """Keys are POSIX-style relative paths, e.g. 'traces/project=x/dt=y/f.parquet'."""

    def write_bytes(self, key: str, data: bytes) -> None: ...

    def append_text(self, key: str, text: str) -> None: ...

    def read_bytes(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...

    def list_keys(self, prefix: str) -> list[str]: ...

    def duckdb_glob(self, pattern: str) -> str:
        """Translate a key glob into something DuckDB's read_parquet accepts."""
        ...

    def local_path(self, key: str) -> Path | None:
        """Local filesystem path for a key, or None if the backend isn't local.

        The WAL writer uses this to append without a read-modify-write cycle.
        Object stores can't append, which is exactly why the WAL is local-only
        and only compacted Parquet goes to S3.
        """
        ...


class LocalStorage:
    """Filesystem-backed storage rooted at a base directory."""

    def __init__(self, base_dir: Path) -> None:
        self.base = base_dir
        self.base.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Guard against a key escaping the base dir via '..'. Keys are
        # internally generated today, but this is one line and the failure
        # mode (writing outside the data dir) is bad.
        path = (self.base / key).resolve()
        if not path.is_relative_to(self.base.resolve()):
            raise ValueError(f"key escapes storage root: {key!r}")
        return path

    def write_bytes(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file then rename. Rename is atomic on POSIX, so a
        # crash mid-write can't leave a half-written Parquet file that DuckDB
        # would later choke on.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def append_text(self, key: str, text: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text)

    def read_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def list_keys(self, prefix: str) -> list[str]:
        root = self._path(prefix)
        if not root.exists():
            return []
        return sorted(
            str(p.relative_to(self.base)) for p in root.rglob("*") if p.is_file()
        )

    def duckdb_glob(self, pattern: str) -> str:
        return str(self.base / pattern)

    def local_path(self, key: str) -> Path | None:
        return self._path(key)

    def reset(self) -> None:
        """Delete everything. Test helper — not called by the app."""
        shutil.rmtree(self.base, ignore_errors=True)
        self.base.mkdir(parents=True, exist_ok=True)


class S3Storage:
    """S3-backed storage. Not implemented in 2a.

    Filling this in means: boto3 for read/write/list, and pointing
    duckdb_glob at 's3://bucket/...' with the httpfs extension loaded and
    credentials configured. local_path returns None, which is why the WAL
    stays on local disk regardless of this setting.
    """

    def __init__(self, bucket: str) -> None:
        if not bucket:
            raise ValueError("OBS_S3_BUCKET must be set when OBS_STORAGE_BACKEND=s3")
        self.bucket = bucket
        raise NotImplementedError(
            "S3 storage is not implemented yet — 2a runs on OBS_STORAGE_BACKEND=local. "
            "The interface is here so adding it later is additive."
        )

    def local_path(self, key: str) -> Path | None:
        return None


def build_storage(settings: Settings) -> StorageBackend:
    backend = settings.storage_backend.lower()
    if backend == "local":
        return LocalStorage(settings.data_dir)
    if backend == "s3":
        return S3Storage(settings.s3_bucket)
    raise ValueError(f"OBS_STORAGE_BACKEND must be 'local' or 's3' — got {backend!r}")
