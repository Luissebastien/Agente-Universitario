from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from notifications.reminders import (
    CYCLE_HOURS,
    TEMPLATE_FIELDS,
    ReminderError,
    ReminderRule,
    validate,
)
from scheduler.schedules import VALID_PERIOD_HOURS

DEFAULT_CONFIG_PATH = Path("config") / "scheduler.toml"

MOODLE_SYNC = "moodle_sync"
INGESTION = "ingestion"
EXTRACTION = "extraction"
NOTIFICATIONS = "notifications"

# The Moodle update chain (.ai/SCHEDULER-MVP-RULES.md §4/§11): each job
# depends on the one before it. A plain tuple, not a dependency graph - there
# are no other jobs to model.
#
# notifications is deliberately NOT here. It decides from the state already
# confirmed locally, so it does not need a sync to have just run; chaining it
# would tie how often the student is reminded to how often Moodle is polled,
# which are unrelated questions. It runs on its own clock schedule instead,
# and keeps its own guard (confirmed_since) against unconfirmed data.
PIPELINE = (MOODLE_SYNC, INGESTION, EXTRACTION)

# Every job the Scheduler knows about, chained or not. Used wherever "all the
# jobs" is meant - validation, status output - rather than PIPELINE, which now
# means specifically "the chain".
ALL_JOBS = PIPELINE + (NOTIFICATIONS,)

# Every value the TOML may set, with its default. Unknown sections/keys are
# rejected so a typo never silently falls back to a default.
_DEFAULTS: dict[str, dict[str, object]] = {
    "scheduler": {
        "timezone": "America/Santo_Domingo",
        "database_path": "../data/agente_u.sqlite3",
        "storage_path": "../data/originals",
        "retry_delay_seconds": 60,
        "allow_manual_disabled_jobs": False,
    },
    MOODLE_SYNC: {"enabled": True, "interval_hours": 6},
    INGESTION: {"enabled": True, "max_items": 100, "max_seconds": 1800},
    EXTRACTION: {"enabled": True, "max_items": 50, "max_seconds": 1800},
    NOTIFICATIONS: {
        "enabled": True,
        # No interval here on purpose: reminders are always checked hourly
        # (notifications.reminders.CYCLE_HOURS). The cycle and the reminders
        # are not independent settings - a longer cycle would silently kill
        # the tightest reminder - so there is one fewer knob and no way to
        # configure a set that does not actually get delivered.
        #
        # The MVP reminder set. Each entry is one reminder; the bands between
        # them are derived (see notifications.reminders.applicable_rule), so
        # adding, removing or disabling one needs no code change.
        "reminders": [
            {
                "id": "24h",
                "offset_hours": 24,
                "title": "Tarea por vencer mañana: {assignment}",
                "body": "{course} — vence el {due_date} a las {due_time}. Quedan {remaining}.",
            },
            {
                "id": "12h",
                "offset_hours": 12,
                "title": "Tarea por vencer hoy: {assignment}",
                "body": "{course} — vence a las {due_time}. Quedan {remaining}.",
            },
            {
                "id": "6h",
                "offset_hours": 6,
                "title": "Quedan {remaining}: {assignment}",
                "body": "{course} — vence hoy a las {due_time}.",
            },
            {
                "id": "1h",
                "offset_hours": 1,
                "title": "Última hora: {assignment}",
                "body": "{course} — vence a las {due_time}, en {remaining}.",
            },
        ],
    },
}


class ConfigError(Exception):
    """The Scheduler configuration is missing or invalid. Startup stops."""


@dataclass(frozen=True)
class MoodleSyncConfig:
    enabled: bool
    interval_hours: int


@dataclass(frozen=True)
class BudgetConfig:
    enabled: bool
    max_items: int
    max_seconds: float


@dataclass(frozen=True)
class NotificationsConfig:
    enabled: bool
    reminders: tuple[ReminderRule, ...]

    @property
    def interval_hours(self) -> int:
        """Fixed, not configured - see reminders.CYCLE_HOURS."""
        return CYCLE_HOURS


@dataclass(frozen=True)
class SchedulerConfig:
    timezone: ZoneInfo
    database_path: Path
    storage_path: Path
    retry_delay_seconds: float
    allow_manual_disabled_jobs: bool
    moodle_sync: MoodleSyncConfig
    ingestion: BudgetConfig
    extraction: BudgetConfig
    notifications: NotificationsConfig

    def is_enabled(self, job_name: str) -> bool:
        return getattr(self, job_name).enabled

    @property
    def schedules(self) -> dict[str, int]:
        """Job -> period in hours, for the jobs that run on a clock schedule.

        Only these two are scheduled. ingestion and extraction have no
        schedule of their own by design: they exist to process what a sync
        brought in, so they run through the chain or not at all.
        """
        return {
            MOODLE_SYNC: self.moodle_sync.interval_hours,
            NOTIFICATIONS: self.notifications.interval_hours,
        }

    @property
    def lock_path(self) -> Path:
        return self.database_path.with_name(self.database_path.name + ".lock")


def load_config(path: str | Path) -> SchedulerConfig:
    """Load and validate the Scheduler TOML. Relative paths inside it are
    resolved against the directory containing the file itself."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(
            f"configuration file not found: {path} "
            "(run from the project root or pass --config)"
        )
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from None

    values = _merge_with_defaults(raw)
    base = path.parent
    s = values["scheduler"]
    return SchedulerConfig(
        timezone=_timezone(s["timezone"]),
        database_path=_path(base, s["database_path"], "scheduler.database_path"),
        storage_path=_path(base, s["storage_path"], "scheduler.storage_path"),
        retry_delay_seconds=_non_negative(s["retry_delay_seconds"], "scheduler.retry_delay_seconds"),
        allow_manual_disabled_jobs=_bool(
            s["allow_manual_disabled_jobs"], "scheduler.allow_manual_disabled_jobs"
        ),
        moodle_sync=MoodleSyncConfig(
            enabled=_bool(values[MOODLE_SYNC]["enabled"], "moodle_sync.enabled"),
            interval_hours=_period_hours(
                values[MOODLE_SYNC]["interval_hours"], "moodle_sync.interval_hours"
            ),
        ),
        ingestion=_budget(values, INGESTION),
        extraction=_budget(values, EXTRACTION),
        notifications=NotificationsConfig(
            enabled=_bool(values[NOTIFICATIONS]["enabled"], "notifications.enabled"),
            reminders=_reminders(values[NOTIFICATIONS]["reminders"]),
        ),
    )


# Keys a deployed scheduler.toml may still carry from an earlier version,
# with what to do about each. deploy/update.sh reinstalls the file when it
# changes, so these are mostly met on a host with local edits.
_REMOVED_KEYS = {
    (NOTIFICATIONS, "due_soon_hours"):
        "no longer exists: the reminder window is now derived from "
        "[[notifications.reminders]]. Reinstall deploy/scheduler.toml "
        "(see docs/deployment.md) and re-apply any local changes.",
    (NOTIFICATIONS, "interval_hours"):
        "no longer exists: reminders are always checked hourly, so the cycle "
        "cannot disagree with the reminders. Remove the line.",
}


def _merge_with_defaults(raw: dict) -> dict[str, dict[str, object]]:
    unknown_sections = set(raw) - set(_DEFAULTS)
    if unknown_sections:
        raise ConfigError(f"unknown section(s): {', '.join(sorted(unknown_sections))}")
    merged: dict[str, dict[str, object]] = {}
    for section, defaults in _DEFAULTS.items():
        given = raw.get(section, {})
        if not isinstance(given, dict):
            raise ConfigError(f"[{section}] must be a table")
        unknown_keys = set(given) - set(defaults)
        for key in sorted(unknown_keys):
            # A key that was deliberately removed deserves its own answer: the
            # generic "unknown key" would send someone hunting for a typo.
            if (section, key) in _REMOVED_KEYS:
                raise ConfigError(f"{section}.{key} {_REMOVED_KEYS[(section, key)]}")
        if unknown_keys:
            raise ConfigError(f"unknown key(s) in [{section}]: {', '.join(sorted(unknown_keys))}")
        merged[section] = {**defaults, **given}
    return merged


def _budget(values: dict, section: str) -> BudgetConfig:
    v = values[section]
    max_items = v["max_items"]
    if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items <= 0:
        raise ConfigError(f"{section}.max_items must be a positive integer")
    return BudgetConfig(
        enabled=_bool(v["enabled"], f"{section}.enabled"),
        max_items=max_items,
        max_seconds=_positive(v["max_seconds"], f"{section}.max_seconds"),
    )


def _timezone(name: object) -> ZoneInfo:
    if not isinstance(name, str) or not name:
        raise ConfigError("scheduler.timezone must be an IANA time zone name")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(
            f"unknown time zone {name!r} (if the name is right, install the 'tzdata' package)"
        ) from None


def _path(base: Path, value: object, key: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key} must be a non-empty path")
    p = Path(value)
    return (p if p.is_absolute() else base / p).resolve()


def _bool(value: object, key: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be true or false")
    return value


def _number(value: object, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{key} must be a number")
    return float(value)


_REMINDER_KEYS = {"id", "offset_hours", "title", "body", "enabled"}
# A context with every allowed placeholder, used only to prove at load time
# that a configured template can actually be rendered.
_TEMPLATE_PROBE = dict.fromkeys(TEMPLATE_FIELDS, "x")


def _reminders(value: object) -> tuple[ReminderRule, ...]:
    """Build the reminder rules, refusing anything that could not be delivered.

    Validated here rather than at send time: a template with a misspelled
    placeholder would otherwise fail in the middle of a run, having already
    sent some of the batch.
    """
    if not isinstance(value, list):
        raise ConfigError("notifications.reminders must be a list of [[notifications.reminders]] entries")

    rules: list[ReminderRule] = []
    every_id: list[str] = []
    for index, entry in enumerate(value):
        where = f"notifications.reminders[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} must be a table")
        unknown = set(entry) - _REMINDER_KEYS
        if unknown:
            raise ConfigError(f"unknown key(s) in {where}: {', '.join(sorted(unknown))}")
        # Note the ordering: a switched-off entry is still checked. Skipping
        # it would let a broken template sit in the file until someone
        # enables it, and fail the next startup far from the edit that caused it.
        is_enabled = _bool(entry.get("enabled", True), f"{where}.enabled")

        rule_id = entry.get("id")
        if not isinstance(rule_id, str) or not rule_id.strip():
            raise ConfigError(f"{where}.id must be a non-empty string")
        offset = entry.get("offset_hours")
        if isinstance(offset, bool) or not isinstance(offset, (int, float)) or offset <= 0:
            raise ConfigError(f"{where}.offset_hours must be a positive number of hours")

        templates = {}
        for field in ("title", "body"):
            template = entry.get(field)
            if not isinstance(template, str) or not template.strip():
                raise ConfigError(f"{where}.{field} must be a non-empty string")
            try:
                template.format(**_TEMPLATE_PROBE)
            except KeyError as exc:
                raise ConfigError(
                    f"{where}.{field} uses unknown placeholder {exc}; "
                    f"available: {', '.join(TEMPLATE_FIELDS)}"
                ) from None
            except (IndexError, ValueError) as exc:
                raise ConfigError(f"{where}.{field} is not a valid template: {exc}") from None
            templates[field] = template

        every_id.append(rule_id.strip())
        if is_enabled:
            rules.append(ReminderRule(
                id=rule_id.strip(),
                offset_seconds=int(offset * 3600),
                title_template=templates["title"],
                body_template=templates["body"],
            ))

    duplicates = {name for name in every_id if every_id.count(name) > 1}
    if duplicates:
        # Checked across every entry, enabled or not: two entries with one id
        # are a mistake in the file whichever of them is switched on.
        raise ConfigError(f"duplicate reminder id(s): {', '.join(sorted(duplicates))}")

    # The same invariant every other caller gets (reminders.validate): offsets
    # at least an hour apart, templates that render. Enforced here too, or a
    # 30-minute reminder could be configured in the file and then silently
    # never delivered - the exact failure the rule set exists to prevent.
    try:
        validate(rules)
    except ReminderError as exc:
        raise ConfigError(f"notifications.reminders: {exc}") from None
    return tuple(rules)


def _period_hours(value: object, key: str) -> int:
    """A schedule period: whole hours that divide the day evenly.

    Runs happen at real clock times anchored to local midnight, so a period
    that does not divide 24 would leave a short step there (see
    schedules.VALID_PERIOD_HOURS). Rejected at load time rather than producing
    a schedule that is subtly not what the file says.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be a whole number of hours, got {value!r}")
    if value not in VALID_PERIOD_HOURS:
        raise ConfigError(
            f"{key} must divide the day evenly so runs land on the same clock "
            f"times every day; valid values: {', '.join(map(str, VALID_PERIOD_HOURS))}. Got {value!r}"
        )
    return value


def _positive(value: object, key: str) -> float:
    number = _number(value, key)
    if number <= 0:
        raise ConfigError(f"{key} must be greater than 0")
    return number


def _non_negative(value: object, key: str) -> float:
    number = _number(value, key)
    if number < 0:
        raise ConfigError(f"{key} must not be negative")
    return number
