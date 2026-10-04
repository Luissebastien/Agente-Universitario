from __future__ import annotations

from dataclasses import dataclass

NOTIFICATION_ASSIGNMENT_DUE_SOON = "assignment_due_soon"
NOTIFICATION_JOB_FAILED = "job_failed"


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
