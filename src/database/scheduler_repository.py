from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# Same status vocabulary as the processing tables (DEC-049): no new
# synonyms. An attempt interrupted by a crash or Ctrl+C is 'failed' with an
# explanatory error, not a separate status.
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class Execution:
    id: int
    job_name: str
    trigger: str
    attempt: int
    status: str
    started_at: str
    finished_at: str | None
    duration_seconds: float | None
    items_processed: int | None
    detail: str | None
    error: str | None


def _row_to_execution(row: sqlite3.Row) -> Execution:
    return Execution(
        id=row["id"],
        job_name=row["job_name"],
        trigger=row["trigger"],
        attempt=row["attempt"],
        status=row["status"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        duration_seconds=row["duration_seconds"],
        items_processed=row["items_processed"],
        detail=row["detail"],
        error=row["error"],
    )


def start_execution(
    conn: sqlite3.Connection, job_name: str, trigger: str, attempt: int, started_at: str
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO scheduler_executions (job_name, trigger, attempt, status, started_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (job_name, trigger, attempt, STATUS_RUNNING, started_at),
    )
    conn.commit()
    return cursor.lastrowid


def finish_execution(
    conn: sqlite3.Connection,
    execution_id: int,
    status: str,
    finished_at: str,
    duration_seconds: float,
    items_processed: int | None = None,
    detail: str | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE scheduler_executions
        SET status = ?, finished_at = ?, duration_seconds = ?, items_processed = ?,
            detail = ?, error = ?
        WHERE id = ?
        """,
        (status, finished_at, duration_seconds, items_processed, detail, error, execution_id),
    )
    conn.commit()


def fail_stale_running(conn: sqlite3.Connection, finished_at: str, error: str) -> int:
    """Mark every 'running' row as failed. Only called while holding the
    single-instance lock, so any row still 'running' belongs to a process
    that died mid-attempt."""
    cursor = conn.execute(
        "UPDATE scheduler_executions SET status = ?, finished_at = ?, error = ? WHERE status = ?",
        (STATUS_FAILED, finished_at, error, STATUS_RUNNING),
    )
    conn.commit()
    return cursor.rowcount


def last_execution(
    conn: sqlite3.Connection, job_name: str, status: str | None = None
) -> Execution | None:
    if status is None:
        row = conn.execute(
            "SELECT * FROM scheduler_executions WHERE job_name = ? ORDER BY id DESC LIMIT 1",
            (job_name,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT * FROM scheduler_executions WHERE job_name = ? AND status = ?
            ORDER BY id DESC LIMIT 1
            """,
            (job_name, status),
        ).fetchone()
    return _row_to_execution(row) if row else None


def running_executions(conn: sqlite3.Connection) -> list[Execution]:
    rows = conn.execute(
        "SELECT * FROM scheduler_executions WHERE status = ? ORDER BY id", (STATUS_RUNNING,)
    ).fetchall()
    return [_row_to_execution(r) for r in rows]


def recent_executions(conn: sqlite3.Connection, limit: int = 10) -> list[Execution]:
    rows = conn.execute(
        "SELECT * FROM scheduler_executions ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_row_to_execution(r) for r in rows]


def ran_at_or_after(conn: sqlite3.Connection, job_name: str, timestamp: str) -> bool:
    """Whether some attempt of job_name was running at `timestamp` or started
    after it - i.e. a manual request made at `timestamp` is already served.
    Timestamps are fixed-width UTC ISO strings, so string order is time order."""
    row = conn.execute(
        """
        SELECT 1 FROM scheduler_executions
        WHERE job_name = ? AND (finished_at IS NULL OR finished_at >= ?)
        LIMIT 1
        """,
        (job_name, timestamp),
    ).fetchone()
    return row is not None


def add_request(conn: sqlite3.Connection, job_name: str, requested_at: str) -> int:
    cursor = conn.execute(
        "INSERT INTO scheduler_requests (job_name, requested_at) VALUES (?, ?)",
        (job_name, requested_at),
    )
    conn.commit()
    return cursor.lastrowid


def pending_requests(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, job_name, requested_at FROM scheduler_requests ORDER BY id"
    ).fetchall()


def delete_request(conn: sqlite3.Connection, request_id: int) -> None:
    conn.execute("DELETE FROM scheduler_requests WHERE id = ?", (request_id,))
    conn.commit()


def purge_requests(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Remove (and return) every leftover request - used at startup, so a
    request addressed to a process that has since exited is never replayed."""
    rows = pending_requests(conn)
    conn.execute("DELETE FROM scheduler_requests")
    conn.commit()
    return rows
