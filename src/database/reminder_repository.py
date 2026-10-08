"""The student's reminder rules: the one place runtime settings are stored.

This is the seam a future interface writes through - Telegram commands, a
phone app, a web page, the CLI. None of them appear here, and none of them
need to: an interface collects the rules, calls replace_rules(), and the
Scheduler picks them up on its next cycle without a restart.

Nothing stored here can be delivered unless it passes
notifications.reminders.validate(), which is called on the way in. That keeps
the check in one place instead of in every interface that ever offers these
settings.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime, timezone

from notifications.reminders import ReminderRule, validate


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def has_saved(conn: sqlite3.Connection) -> bool:
    """Whether the student has ever saved a choice, whatever that choice was."""
    return conn.execute("SELECT 1 FROM reminder_preferences_saved").fetchone() is not None


def get_rules(conn: sqlite3.Connection) -> tuple[ReminderRule, ...] | None:
    """The student's rules, or None when they have never set any.

    None and "an empty set" are different answers on purpose. None means fall
    back to the rules configured for the host; an empty tuple means they
    deliberately turned every reminder off, and must be obeyed - which is why
    the fact that a choice was made is recorded separately from the rules
    themselves.
    """
    if not has_saved(conn):
        return None
    rows = conn.execute(
        """
        SELECT id, offset_seconds, enabled, title_template, body_template
        FROM reminder_preferences ORDER BY offset_seconds DESC
        """
    ).fetchall()
    return tuple(
        ReminderRule(
            id=row["id"],
            offset_seconds=row["offset_seconds"],
            title_template=row["title_template"],
            body_template=row["body_template"],
        )
        for row in rows
        if row["enabled"]
    )


def replace_rules(
    conn: sqlite3.Connection,
    rules: Sequence[ReminderRule],
    *,
    disabled: Sequence[ReminderRule] = (),
) -> None:
    """Store `rules` as the student's complete set, replacing whatever was there.

    Refuses a set that could not actually be delivered (see validate), which
    is what makes an impossible set impossible to save.

    `disabled` keeps rules the student switched off without discarding their
    text, so turning one back on does not mean rewriting it. Disabled rules
    are stored but never returned by get_rules().

    Raises ReminderError, having written nothing.
    """
    validate(rules)
    now = _now()
    payload = [(rule, True) for rule in rules] + [(rule, False) for rule in disabled]
    with conn:  # one transaction: a half-replaced rule set is never visible
        conn.execute("DELETE FROM reminder_preferences")
        conn.execute(
            "INSERT INTO reminder_preferences_saved (id, saved_at) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET saved_at = excluded.saved_at",
            (now,),
        )
        conn.executemany(
            """
            INSERT INTO reminder_preferences
                (id, offset_seconds, enabled, title_template, body_template, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (rule.id, rule.offset_seconds, int(is_enabled),
                 rule.title_template, rule.body_template, now)
                for rule, is_enabled in payload
            ],
        )


def clear_rules(conn: sqlite3.Connection) -> None:
    """Forget the student's choices and go back to the host's configured rules.

    Different from saving an empty set: that one means "send me nothing".
    """
    with conn:
        conn.execute("DELETE FROM reminder_preferences")
        conn.execute("DELETE FROM reminder_preferences_saved")


def effective_rules(
    conn: sqlite3.Connection, configured: Sequence[ReminderRule]
) -> tuple[ReminderRule, ...]:
    """The rules actually in force: the student's if they set any, else the
    host's. Read once per cycle, which is what makes a change take effect
    without restarting the process."""
    stored = get_rules(conn)
    return tuple(configured) if stored is None else stored
