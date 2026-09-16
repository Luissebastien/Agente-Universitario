from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ExtractedDocument:
    """One deterministic extraction attempt against an immutable ResourceVersion.

    Every call to Extraction.extract() inserts a new row - existing rows are
    never updated or overwritten (DEC-038: processing attempts are append-only
    and auditable). This differs deliberately from ResourceVersion's
    content-hash deduplication: Ingestion dedups because its job is preserving
    distinct *original content*, while Extraction's job is auditing distinct
    *processing attempts* against a fixed, already-immutable input.

    status is "done" (extracted_text/metadata populated) or "failed"
    (error_reason populated, extracted_text is None). "failed" covers both
    genuine errors (e.g. undecodable bytes) and a deliberately unsupported
    format - DEC-048 requires an unsupported format to fail explicitly with
    the original preserved, not to be silently skipped or guessed at.

    depth is "basic" or "deep" (DEC-031: a processing depth, not a layer).
    Both depths currently share the same extractors for the formats this
    phase supports - see src/extraction/extractors.py.
    """

    id: int | None
    resource_version_id: int
    depth: str
    extractor_name: str
    extractor_version: str
    status: str
    error_reason: str | None
    extracted_text: str | None
    metadata: dict = field(default_factory=dict)
    extracted_at: str = ""
