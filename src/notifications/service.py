from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo

from database import notification_repository as repo
from notifications.models import (
    NOTIFICATION_ASSIGNMENT_DUE_SOON,
    NOTIFICATION_JOB_FAILED,
    Notification,
)
from notifications.providers import NotificationProvider
from read_model import AcademicReadModel, is_assignment_pending

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NotificationBatchResult:
    pending: int
    sent: int
    failed: int
    first_error: str | None = None


class NotificationService:
    """WHAT to notify and WHY - deterministic rules over the Read Model only.

    No LLM, no inference. The single MVP rule ('assignment_due_soon'): an
    assignment confirmed by the latest successful Moodle sync, that the Read
    Model considers pending (real future due date, known and not submitted -
    DEC-062), and is due within `due_soon_hours`. Each (type, assignment,
    deadline) is notified once.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        provider: NotificationProvider,
        tz: tzinfo,
        due_soon_hours: float,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._conn = conn
        self._provider = provider
        self._tz = tz
        self._due_soon_seconds = int(due_soon_hours * 3600)
        self._clock = clock or (lambda: datetime.now(tz))

    def pending(self, confirmed_since: str | None) -> list[Notification]:
        """Due-soon reminders not sent yet.

        confirmed_since: start (UTC ISO) of the latest successful Moodle sync.
        Only assignments that sync saw again are considered - rows are never
        deleted, so an assignment Moodle stopped returning (deleted, course
        dropped) must not produce reminders. None (no successful sync yet)
        means nothing is confirmed, so nothing is notified (fail closed).
        """
        if confirmed_since is None:
            return []
        confirmed = datetime.fromisoformat(confirmed_since)
        now = int(self._clock().timestamp())
        horizon = now + self._due_soon_seconds
        read_model = AcademicReadModel(self._conn)

        notifications: list[Notification] = []
        for assignment in read_model.get_assignments():
            if assignment.last_synced_at is None:
                continue
            if datetime.fromisoformat(assignment.last_synced_at) < confirmed:
                continue
            if not is_assignment_pending(assignment.duedate, assignment.submission_status, now):
                continue
            if assignment.duedate > horizon:
                continue
            scheduled_for = datetime.fromtimestamp(assignment.duedate, timezone.utc).isoformat()
            subject_key = f"assignment:{assignment.id}"
            if repo.was_sent(self._conn, NOTIFICATION_ASSIGNMENT_DUE_SOON, subject_key, scheduled_for):
                continue
            course = read_model.get_course(assignment.course_id)
            local_due = datetime.fromtimestamp(assignment.duedate, self._tz)
            notifications.append(
                Notification(
                    notification_type=NOTIFICATION_ASSIGNMENT_DUE_SOON,
                    subject_key=subject_key,
                    scheduled_for=scheduled_for,
                    title=f"Tarea por vencer: {assignment.name}",
                    body=(
                        f"{course.fullname if course else f'Curso {assignment.course_id}'} - "
                        f"vence el {local_due:%Y-%m-%d %H:%M} ({self._tz_name()})"
                    ),
                )
            )
        return notifications

    def send_pending(self, confirmed_since: str | None) -> NotificationBatchResult:
        """Send every pending reminder, then record it (at-least-once: a send
        failure leaves it unrecorded, so it is retried next run)."""
        notifications = self.pending(confirmed_since)
        sent = failed = 0
        first_error: str | None = None
        for notification in notifications:
            try:
                self._provider.send(notification)
            except Exception as exc:  # noqa: BLE001 - one notification never blocks the rest
                failed += 1
                first_error = first_error or f"{type(exc).__name__}: {exc}"
                logger.warning("Notification %s for %s could not be sent: %s",
                               notification.notification_type, notification.subject_key, exc)
                continue
            repo.record_sent(
                self._conn,
                notification.notification_type,
                notification.subject_key,
                notification.scheduled_for,
                self._clock().astimezone(timezone.utc).isoformat(),
            )
            sent += 1
        return NotificationBatchResult(
            pending=len(notifications), sent=sent, failed=failed, first_error=first_error
        )

    def notify_job_failure(self, job_name: str, error: str, failed_at: datetime) -> None:
        """Operational alert after a Scheduler job exhausted its attempts.

        Every failure is a distinct event, so nothing is deduplicated or
        stored here (the failure itself is already in scheduler history).
        `error` must already be redacted by the caller.
        """
        local = failed_at.astimezone(self._tz)
        self._provider.send(
            Notification(
                notification_type=NOTIFICATION_JOB_FAILED,
                subject_key=f"job:{job_name}",
                scheduled_for=failed_at.astimezone(timezone.utc).isoformat(),
                title=f"Agente U: el job '{job_name}' falló",
                body=(
                    f"Falló todos sus intentos ({local:%Y-%m-%d %H:%M} {self._tz_name()}); "
                    f"los jobs dependientes no corrieron en este ciclo. Error: {error}"
                ),
            )
        )

    def _tz_name(self) -> str:
        return getattr(self._tz, "key", str(self._tz))
