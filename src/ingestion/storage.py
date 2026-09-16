from __future__ import annotations

import abc
import contextlib
import os
import tempfile
from pathlib import Path


class StorageError(Exception):
    """Base exception for Storage backend failures."""


class StorageNotFoundError(StorageError):
    """retrieve()/delete() was asked for a storage_ref that doesn't exist."""


class Storage(abc.ABC):
    """Abstraction over where original bytes physically live.

    Ingestion and everything above it depends on this interface only - never
    on filesystem paths. Swapping the backend (VPS filesystem, object
    storage) means providing a new Storage implementation, not touching
    Resource, ResourceVersion, or Ingestion.
    """

    @abc.abstractmethod
    def store(self, data: bytes, content_hash: str) -> str:
        """Persist data, addressed by its content hash. Returns an opaque storage_ref.

        Keyed by content_hash rather than a ResourceVersion id: Ingestion's
        required sequence is download -> hash -> store -> persist
        ResourceVersion, so no ResourceVersion row (and therefore no id)
        exists yet at the point store() must be called. Using the hash
        instead is available at the right time AND gives free deduplication
        of identical content. This is a deliberate precision of the Phase 1
        Storage contract, not a schema or architecture change.

        Idempotent: storing the same content_hash twice must not duplicate
        anything or fail - it should return the same storage_ref.
        """

    @abc.abstractmethod
    def retrieve(self, storage_ref: str) -> bytes:
        """Return the exact bytes previously stored. Raises StorageNotFoundError if missing."""

    @abc.abstractmethod
    def exists(self, storage_ref: str) -> bool:
        """Return whether storage_ref currently points to stored data."""

    @abc.abstractmethod
    def delete(self, storage_ref: str) -> None:
        """Administrative removal. The Ingestion pipeline itself never calls this
        for a successfully committed ResourceVersion - originals are treated as
        immutable. This exists for operational cleanup only (e.g. discarding a
        partial write that never became a ResourceVersion)."""


class FilesystemStorage(Storage):
    """Local filesystem Storage backend - the only implementation for this phase.

    Content-addressed: the storage_ref is the content hash itself, used as the
    filename. This makes store() naturally idempotent (identical content maps
    to the same file) without needing a lookup.
    """

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, storage_ref: str) -> Path:
        # storage_ref is a hex hash produced by us - safe as a bare filename,
        # but guard against path traversal regardless of how it was obtained.
        if "/" in storage_ref or "\\" in storage_ref or ".." in storage_ref:
            raise StorageError(f"Invalid storage_ref: {storage_ref!r}")
        return self._root / storage_ref

    def store(self, data: bytes, content_hash: str) -> str:
        target = self._path_for(content_hash)
        if target.exists():
            return content_hash  # identical content already stored - no-op

        fd, tmp_path = tempfile.mkstemp(dir=self._root, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(tmp_path)
            raise
        return content_hash

    def retrieve(self, storage_ref: str) -> bytes:
        path = self._path_for(storage_ref)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise StorageNotFoundError(storage_ref) from exc

    def exists(self, storage_ref: str) -> bool:
        return self._path_for(storage_ref).exists()

    def delete(self, storage_ref: str) -> None:
        path = self._path_for(storage_ref)
        try:
            path.unlink()
        except FileNotFoundError as exc:
            raise StorageNotFoundError(storage_ref) from exc
