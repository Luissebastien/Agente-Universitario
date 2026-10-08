"""Reminder rules: which reminders exist, and which one a deadline has reached.

The rules are data, not branches. Adding a reminder, removing one, turning one
off or moving its offset is a configuration change; nothing in this module
mentions 24, 12, 6 or 1, and the bands below are derived from whichever rules
happen to be enabled.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

# Placeholders a template may use. Kept here, next to the renderer, because
# this tuple is also what validates a configured template at load time.
TEMPLATE_FIELDS = ("assignment", "course", "due", "due_date", "due_time", "remaining", "tz")

# Reminders are evaluated once an hour, always. Not configurable, deliberately:
# the cycle and the reminders are not independent settings. A clock-aligned
# cycle of period P lands in every band at least P wide exactly once and can
# step clean over a narrower one, so any cycle longer than an hour would
# silently kill the hour-before reminder, and anything shorter is not even
# expressible (schedules.VALID_PERIOD_HOURS is whole hours dividing the day).
# Pinning it makes the floor on a reminder band a constant: one hour.
# Measured cost on the target host: ~19 ms per evaluation, roughly a seventh
# of the manual-request polling the daemon already does.
CYCLE_HOURS = 1
MIN_BAND_SECONDS = CYCLE_HOURS * 3600


@dataclass(frozen=True)
class ReminderRule:
    """One reminder: how far ahead of the deadline it fires, and how it reads.

    `id` is part of the notification's stored identity, so renaming a rule
    starts a fresh series for every assignment currently in flight. Changing
    only its templates does not - the text is not part of the identity.
    """

    id: str
    offset_seconds: int
    title_template: str
    body_template: str

    @property
    def notification_type(self) -> str:
        """The dedup type stored in notifications_sent.

        The rule IS the notification type (DEC-069): a reminder a day ahead
        and one an hour ahead are different notifications with different text
        and different urgency, not variants of one. Modelling them this way
        also keeps the existing three-column identity, so no schema migration.
        """
        return f"assignment_due_{self.id}"


def applicable_rule(
    rules: Sequence[ReminderRule], remaining_seconds: float
) -> ReminderRule | None:
    """The rule whose band contains this much time remaining, or None.

    A rule's band runs from the next smaller offset up to its own, so with
    offsets of 24h, 12h, 6h and 1h the bands are (12,24], (6,12], (1,6] and
    (0,1]. Expressed as a search rather than as ranges, the bands follow the
    rule set automatically: drop the 12h rule and the 24h band simply widens
    to (6,24].

    This is also what makes a late discovery safe. An assignment first seen
    with five hours left matches only the 6h band; the 24h and 12h reminders
    were never applicable and are not sent retrospectively. The same holds
    after downtime - no state is needed to remember what was missed.
    """
    reached = [rule for rule in rules if rule.offset_seconds >= remaining_seconds]
    if not reached:
        return None  # the deadline is further away than any reminder
    return min(reached, key=lambda rule: rule.offset_seconds)


class ReminderError(ValueError):
    """A proposed set of reminder rules could not be accepted.

    Raised by validate() rather than returned, so no interface can store a set
    of rules that would not actually be delivered.
    """


def narrowest_band_seconds(rules: Sequence[ReminderRule]) -> int:
    """The width of the tightest band in this rule set.

    Not simply the smallest offset: with reminders at 24h and 23h the tightest
    band is one hour wide, even though no reminder is anywhere near the
    deadline. It is the smallest gap between consecutive offsets, counting
    the deadline itself as the bottom of the last band.
    """
    if not rules:
        return 0
    edges = sorted(rule.offset_seconds for rule in rules)
    return min(upper - lower for lower, upper in zip([0, *edges], edges))


def validate(rules: Sequence[ReminderRule]) -> None:
    """Check a rule set can actually be delivered.

    This is THE invariant of the whole reminder design: reminders are
    evaluated hourly, so a band narrower than an hour can fall between two
    runs. A 30-minute reminder is not one that arrives late - it is one that
    never arrives, silently.

    Enforced here, at the point rules are stored, so every interface that ever
    offers reminder settings - Telegram, a phone app, the CLI - inherits the
    check without restating it, and the person gets an error instead of a
    reminder that quietly does nothing.
    """
    ids = [rule.id for rule in rules]
    duplicates = sorted({name for name in ids if ids.count(name) > 1})
    if duplicates:
        raise ReminderError(f"duplicate reminder id(s): {', '.join(duplicates)}")
    offsets = [rule.offset_seconds for rule in rules]
    if len(set(offsets)) != len(offsets):
        raise ReminderError("two reminders cannot share the same offset")
    probe = dict.fromkeys(TEMPLATE_FIELDS, "x")
    for rule in rules:
        if rule.offset_seconds <= 0:
            raise ReminderError(f"reminder {rule.id!r} must be a positive time before the deadline")
        # Checked here so no caller can store a template that would fail
        # mid-send, with part of the batch already delivered.
        for field, template in (("title", rule.title_template), ("body", rule.body_template)):
            try:
                template.format(**probe)
            except KeyError as exc:
                raise ReminderError(
                    f"reminder {rule.id!r} {field} uses unknown placeholder {exc}; "
                    f"available: {', '.join(TEMPLATE_FIELDS)}"
                ) from None
            except (IndexError, ValueError) as exc:
                raise ReminderError(f"reminder {rule.id!r} {field} is not a valid template: {exc}") from None

    narrowest = narrowest_band_seconds(rules)
    if rules and narrowest < MIN_BAND_SECONDS:
        raise ReminderError(
            f"the tightest gap between reminders is {narrowest // 60} minutes, but "
            f"reminders are checked once an hour, so that one would fall between two "
            f"runs and never be sent. Reminders must be at least {CYCLE_HOURS} hour "
            "apart, and the earliest one at least that far before the deadline."
        )


def render(template: str, context: dict[str, str]) -> str:
    """Fill a template's placeholders.

    Values are substituted, never interpreted, so an assignment called
    "Taller {1}" is delivered as written.
    """
    return template.format(**context)


def describe_remaining(seconds: float) -> str:
    """How much time is left, in words, for the body of a reminder.

    Always rounded down. Rounding to the nearest unit would turn 23h58m into
    "1 dia", telling the student they have more time than they do - the one
    error a deadline reminder must not make.
    """
    if seconds < 60:
        return "menos de un minuto"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} minutos" if minutes != 1 else "1 minuto"
    hours = int(seconds // 3600)
    if hours < 24:
        return f"{hours} horas" if hours != 1 else "1 hora"
    days = int(hours // 24)
    return f"{days} dias" if days != 1 else "1 dia"
