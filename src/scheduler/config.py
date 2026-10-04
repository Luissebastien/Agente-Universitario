from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_CONFIG_PATH = Path("config") / "scheduler.toml"

MOODLE_SYNC = "moodle_sync"
INGESTION = "ingestion"
EXTRACTION = "extraction"
NOTIFICATIONS = "notifications"

# The fixed MVP pipeline (.ai/SCHEDULER-MVP-RULES.md §4/§11): each job depends
# on the one before it. A plain tuple, not a dependency graph - there are no
# other jobs to model.
PIPELINE = (MOODLE_SYNC, INGESTION, EXTRACTION, NOTIFICATIONS)

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
    NOTIFICATIONS: {"enabled": True, "due_soon_hours": 24},
}


class ConfigError(Exception):
    """The Scheduler configuration is missing or invalid. Startup stops."""


@dataclass(frozen=True)
class MoodleSyncConfig:
    enabled: bool
    interval_hours: float


@dataclass(frozen=True)
class BudgetConfig:
    enabled: bool
    max_items: int
    max_seconds: float


@dataclass(frozen=True)
class NotificationsConfig:
    enabled: bool
    due_soon_hours: float


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
            interval_hours=_positive(values[MOODLE_SYNC]["interval_hours"], "moodle_sync.interval_hours"),
        ),
        ingestion=_budget(values, INGESTION),
        extraction=_budget(values, EXTRACTION),
        notifications=NotificationsConfig(
            enabled=_bool(values[NOTIFICATIONS]["enabled"], "notifications.enabled"),
            due_soon_hours=_positive(
                values[NOTIFICATIONS]["due_soon_hours"], "notifications.due_soon_hours"
            ),
        ),
    )


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
