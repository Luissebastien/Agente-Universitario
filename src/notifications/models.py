from __future__ import annotations

from dataclasses import dataclass

NOTIFICATION_ASSIGNMENT_DUE_SOON = "assignment_due_soon"
NOTIFICATION_JOB_FAILED = "job_failed"

# How an assignment is named in a notification's subject_key. Written once
# here and read back by assignment_id_of(), so the format is stated in one
# place instead of being split between whoever builds the key and whoever
# needs the id again.
ASSIGNMENT_SUBJECT_PREFIX = "assignment:"


def assignment_subject_key(assignment_id: int) -> str:
    return f"{ASSIGNMENT_SUBJECT_PREFIX}{assignment_id}"


def assignment_id_of(subject_key: str) -> int | None:
    """The assignment a subject_key refers to, or None for any other subject."""
    if not subject_key.startswith(ASSIGNMENT_SUBJECT_PREFIX):
        return None
    rest = subject_key[len(ASSIGNMENT_SUBJECT_PREFIX):]
    return int(rest) if rest.isdigit() else None


@dataclass(frozen=True)
class Notification:
    """One deterministic notification.

    (notification_type, subject_key, scheduled_for) is its identity for
    deduplication - e.g. ('assignment_due_soon', 'assignment:104926',
    <deadline as UTC ISO>). A changed deadline is a new identity, so the
    student is reminded about the new date.
    """

    notification_type: str
    subject_key: str
    scheduled_for: str
    title: str
    body: str
