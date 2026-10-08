from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo

from database import notification_repository as repo
from notifications.models import NOTIFICATION_JOB_FAILED, Notification
from notifications.providers import NotificationProvider
from notifications.reminders import (
    ReminderRule,
    applicable_rule,
    describe_remaining,
    render,
)
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

    No LLM, no inference. An assignment produces a reminder when it is
    confirmed by the latest successful Moodle sync, the Read Model considers
    it pending (real future due date, known and not submitted - DEC-062), and
    the time left has reached one of the configured reminder bands.

    A deadline can therefore produce several reminders, each at most once:
    the stored identity is (rule, assignment, deadline), so a rule fires once
    per deadline, and a deadline the professor moves starts a fresh series.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        provider: NotificationProvider,
        tz: tzinfo,
        rules_source: Callable[[], Sequence[ReminderRule]],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """`rules_source` is asked on every evaluation, not once here.

        That is deliberate: it is what lets the student's rules change while
        the process is running - a setting saved from any interface takes
        effect on the next cycle, with no restart. A caller with a fixed set
        passes `lambda: rules`.
        """
        self._conn = conn
        self._provider = provider
        self._tz = tz
        self._rules_source = rules_source
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
        rules = tuple(self._rules_source())
        if not rules:
            return []
        confirmed = datetime.fromisoformat(confirmed_since)
        now = int(self._clock().timestamp())
        # Nothing is looked at beyond the widest reminder. Derived from the
        # rules in force rather than configured separately, so the horizon and
        # the reminders can never disagree.
        horizon = now + max(rule.offset_seconds for rule in rules)
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
            rule = applicable_rule(rules, assignment.duedate - now)
            if rule is None:
                continue
            scheduled_for = datetime.fromtimestamp(assignment.duedate, timezone.utc).isoformat()
            subject_key = f"assignment:{assignment.id}"
            if repo.was_sent(self._conn, rule.notification_type, subject_key, scheduled_for):
                continue
            course = read_model.get_course(assignment.course_id)
            context = self._context(assignment, course, assignment.duedate - now)
            notifications.append(
                Notification(
                    notification_type=rule.notification_type,
                    subject_key=subject_key,
                    scheduled_for=scheduled_for,
                    title=render(rule.title_template, context),
                    body=render(rule.body_template, context),
                )
            )
        return notifications

    def _context(self, assignment, course, remaining_seconds: float) -> dict[str, str]:
        """The values a reminder template may use. See TEMPLATE_FIELDS."""
        local_due = datetime.fromtimestamp(assignment.duedate, self._tz)
        return {
            "assignment": assignment.name,
            "course": course.fullname if course else f"Curso {assignment.course_id}",
            "due": f"{local_due:%Y-%m-%d %H:%M}",
            "due_date": f"{local_due:%Y-%m-%d}",
            "due_time": f"{local_due:%H:%M}",
            "remaining": describe_remaining(remaining_seconds),
            "tz": self._tz_name(),
        }

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
