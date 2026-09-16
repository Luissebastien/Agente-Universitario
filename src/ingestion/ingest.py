from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone

from database import ingestion_repository as repo
from ingestion.models import ResourceDescriptor, ResourceVersion
from ingestion.storage import Storage, StorageError
from moodle.client import MoodleClient
from moodle.exceptions import MoodleError

_URL_MIMETYPE = "text/uri-list"


class IngestionError(Exception):
    """An ingestion attempt failed before producing a valid ResourceVersion.

    No partial/fake ResourceVersion is ever created when this is raised - the
    resource simply remains at whatever version (possibly none) it already
    had, and ingest() can be safely retried later.
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Ingestion:
    """Preserves original resource content, unchanged and versioned.

    Does not interpret content in any way (no parsing, no extraction, no
    LLM). Its only job: given a ResourceDescriptor, get the bytes, hash them,
    and store a new immutable ResourceVersion only if the content actually
    changed since the last known version.

    "Content" means different things depending on descriptor.source_type:
      - file: the original file's bytes, downloaded via MoodleClient.
      - url:  the canonical URL reference string itself - never the bytes of
        whatever that URL points to. For URL resources, a new version
        represents a change in the preserved reference (e.g. Moodle now
        points this module at a different address), not a change in that
        destination's remote content. Ingestion never fetches, crawls, or
        interprets what a URL points to - see _fetch_content() below.
    """

    def __init__(self, client: MoodleClient, storage: Storage, conn: sqlite3.Connection) -> None:
        self._client = client
        self._storage = storage
        self._conn = conn

    def ingest(self, descriptor: ResourceDescriptor) -> ResourceVersion:
        resource = repo.upsert_resource(self._conn, descriptor)

        content = self._fetch_content(descriptor)
        content_hash = hashlib.sha256(content).hexdigest()

        existing = repo.get_latest_version(self._conn, resource.id)
        if existing is not None and existing.content_hash == content_hash:
            return existing  # unchanged content: idempotent no-op, no new version

        try:
            storage_ref = self._storage.store(content, content_hash)
        except StorageError as exc:
            raise IngestionError(
                f"Storage failed for resource {resource.id} ({descriptor.source_type})"
            ) from exc

        next_version_number = existing.version_number + 1 if existing else 1
        version = ResourceVersion(
            id=None,
            resource_id=resource.id,
            version_number=next_version_number,
            content_hash=content_hash,
            storage_ref=storage_ref,
            size_bytes=len(content),
            mimetype=descriptor.mimetype
            or (_URL_MIMETYPE if descriptor.source_type == "url" else None),
            ingested_at=_now(),
        )
        return repo.insert_resource_version(self._conn, version)

    def _fetch_content(self, descriptor: ResourceDescriptor) -> bytes:
        if descriptor.source_type == "file":
            try:
                return self._client.download_file(descriptor.source_url)
            except MoodleError as exc:
                raise IngestionError(
                    f"Download failed for {descriptor.origin}/{descriptor.source_type} "
                    f"resource {descriptor.external_reference!r}"
                ) from exc
        elif descriptor.source_type == "url":
            # Preserve only the canonical reference string. The destination's
            # remote content is never fetched, crawled, or parsed here - so
            # the resulting hash/version tracks changes to the reference
            # itself (e.g. Moodle repointing this module), not to whatever
            # that reference currently leads to.
            return descriptor.source_url.encode("utf-8")
        else:
            raise IngestionError(f"Unsupported source_type: {descriptor.source_type!r}")
