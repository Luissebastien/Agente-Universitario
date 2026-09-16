from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResourceDescriptor:
    """What a source adapter knows about a candidate resource, before ingestion.

    Carries only provenance/reference data - never the resource's content.
    Ingestion decides whether/how to fetch the actual bytes.
    """

    origin: str  # "moodle" in this phase
    source_type: str  # "file" | "url"
    external_reference: str  # stable identity key within (origin, source_type)
    course_id: int | None
    section_id: int | None
    module_id: int | None
    name: str
    source_url: str  # fileurl to download (file) or the target URL itself (url)
    mimetype: str | None = None


@dataclass(frozen=True)
class Resource:
    """Stable identity of a resource, independent of its content over time.

    Identity is (origin, source_type, external_reference) - this is what stays
    constant across re-syncs and across content changes (new ResourceVersion).
    """

    id: int | None
    origin: str
    source_type: str
    external_reference: str
    course_id: int | None
    section_id: int | None
    module_id: int | None
    name: str
    source_url: str


@dataclass(frozen=True)
class ResourceVersion:
    """One immutable, concrete snapshot of a Resource's content.

    storage_ref is opaque - its format is owned by whichever Storage backend
    produced it. Never a filesystem path baked into the contract.

    content_hash has a source_type-dependent meaning:
      - source_type == "file": hash of the original file bytes preserved as-is.
      - source_type == "url":  hash of the canonical URL reference string that
        was preserved (see ResourceDescriptor.source_url), NOT of whatever the
        URL points to. A new version means "this module now points somewhere
        else"; it says nothing about the remote destination's own content
        having changed - Ingestion never fetches or inspects that content.
        Detecting remote content changes, if ever wanted, would require a
        future acquisition/crawling mechanism outside Ingestion's scope.
    """

    id: int | None
    resource_id: int
    version_number: int
    content_hash: str
    storage_ref: str
    size_bytes: int
    mimetype: str | None
    ingested_at: str
