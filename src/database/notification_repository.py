from __future__ import annotations

import sqlite3


def was_sent(
    conn: sqlite3.Connection, notification_type: str, subject_key: str, scheduled_for: str
) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM notifications_sent
        WHERE notification_type = ? AND subject_key = ? AND scheduled_for = ?
        """,
        (notification_type, subject_key, scheduled_for),
    ).fetchone()
    return row is not None


def record_sent(
    conn: sqlite3.Connection,
    notification_type: str,
    subject_key: str,
    scheduled_for: str,
    sent_at: str,
) -> None:
    """Idempotent: the UNIQUE identity makes a repeated record a no-op."""
    conn.execute(
        """
        INSERT OR IGNORE INTO notifications_sent
            (notification_type, subject_key, scheduled_for, sent_at)
        VALUES (?, ?, ?, ?)
        """,
        (notification_type, subject_key, scheduled_for, sent_at),
    )
    conn.commit()
