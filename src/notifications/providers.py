from __future__ import annotations

import abc
import logging

from notifications.models import NOTIFICATION_JOB_FAILED, Notification


class NotificationProvider(abc.ABC):
    """HOW a notification reaches the student. Knows nothing about why it
    was decided - that is NotificationService's job."""

    name: str

    @abc.abstractmethod
    def send(self, notification: Notification) -> None:
        """Deliver the notification or raise. Must not swallow failures: an
        unsent notification is retried by NotificationService next run."""


class LogNotificationProvider(NotificationProvider):
    """MVP delivery channel: the process log. A future Telegram provider
    replaces this without touching NotificationService or the Scheduler."""

    name = "log"

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("notifications")

    def send(self, notification: Notification) -> None:
        level = (
            logging.WARNING
            if notification.notification_type == NOTIFICATION_JOB_FAILED
            else logging.INFO
        )
        self._logger.log(level, "NOTIFICATION %s | %s", notification.title, notification.body)
