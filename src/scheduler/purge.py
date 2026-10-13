"""End-of-term reset: remove the academic data a finished term left behind.

Deliberately all-or-nothing rather than selective. Deleting one course would
mean reasoning about which sections, modules, files, versions and extractions
belong to it and which are shared - and getting that subtly wrong leaves
orphans that are worse than either keeping everything or keeping nothing.
A whole-database reset has no such edge: the next startup runs a full sync and
rebuilds the current term from Moodle.

Archiving is NOT done here. The originals are stored by content hash, so an
archive worth keeping has to reconstruct real filenames and course structure,
and that belongs wherever the archive is going - not on the host being
emptied.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Deleted children-first, so every foreign key still resolves while the
# deletion is in progress (the connection runs with foreign_keys = ON).
ACADEMIC_TABLES = (
    "extracted_documents",
    "ingestion_deferrals",
    "ingestion_source_checks",
    "resource_versions",
    "resources",
    "assignment_submission_status",
    "grades",
    "calendar_events",
    "assignments",
    "course_files",
    "course_modules",
    "course_sections",
    "courses",
    "moodle_users",
)

# Kept on purpose:
#   scheduler_executions   operational history, tiny, and the only durable
#                          record of what the system did
#   scheduler_requests     transient; a stale one is discarded at startup
#   notifications_sent     dedup. Clearing it would re-announce deadlines that
#                          survive the purge, which is the one way this command
#                          could actively annoy someone
#   reminder_preferences*  the student's own settings, not academic data
KEPT_TABLES = (
    "scheduler_executions",
    "scheduler_requests",
    "notifications_sent",
    "reminder_preferences",
    "reminder_preferences_saved",
)


class PurgeRefused(Exception):
    """The purge did not run, and nothing was changed."""


@dataclass(frozen=True)
class PurgeReport:
    backup_path: Path
    rows_by_table: dict[str, int]
    originals_removed: int
    bytes_freed: int

    @property
    def total_rows(self) -> int:
        return sum(self.rows_by_table.values())


def count_academic_rows(conn: sqlite3.Connection) -> dict[str, int]:
    """How much academic data is stored, per table. Read-only."""
    counts = {}
    for table in ACADEMIC_TABLES:
        counts[table] = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    return counts


def backup_to(conn: sqlite3.Connection, destination: Path) -> Path:
    """A consistent copy of the database, taken the same way deploy/backup.sh
    does it: sqlite3's own backup API, not a file copy, so a scheduler holding
    a write lock cannot produce a torn file."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(destination)
    try:
        # Closed explicitly: `with sqlite3.connect(...)` manages a transaction,
        # not the connection, and would leave the backup file held open.
        conn.backup(target)
    finally:
        target.close()
    return destination


def purge(
    conn: sqlite3.Connection,
    storage_root: Path,
    backup_dir: Path,
    *,
    now: datetime | None = None,
) -> PurgeReport:
    """Delete every academic row and every stored original, after backing up.

    The caller is responsible for making sure no scheduler is writing (see
    run_purge). Raises before deleting anything if the backup fails: a purge
    without a recoverable copy is not something to do on a best-effort basis.
    """
    moment = now or datetime.now(timezone.utc)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_to(conn, backup_dir / f"agente_u-before-purge-{stamp}.sqlite3")

    counts = count_academic_rows(conn)
    with conn:
        for table in ACADEMIC_TABLES:
            conn.execute(f"DELETE FROM {table}")

    removed, freed = _remove_originals(storage_root)
    return PurgeReport(
        backup_path=backup_path,
        rows_by_table=counts,
        originals_removed=removed,
        bytes_freed=freed,
    )


def _remove_originals(storage_root: Path) -> tuple[int, int]:
    """Delete the stored original files. Nothing references them any more once
    resource_versions is empty, and they are re-downloadable from Moodle while
    the course still exists."""
    if not storage_root.is_dir():
        return 0, 0
    removed = freed = 0
    for path in sorted(storage_root.iterdir()):
        if not path.is_file():
            continue
        size = path.stat().st_size
        path.unlink()
        removed += 1
        freed += size
    return removed, freed
