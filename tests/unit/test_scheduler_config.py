import os
import tempfile
import tomllib
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from scheduler.config import ConfigError, load_config
from scheduler.lock import SingleInstanceLock, is_locked
from scheduler.redaction import redact, safe_error

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config" / "scheduler.toml"


class ConfigTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, text: str) -> Path:
        path = self.dir / "scheduler.toml"
        path.write_text(text, encoding="utf-8")
        return path


class LoadConfigTests(ConfigTestCase):
    def test_repository_config_loads_with_the_spec_values(self) -> None:
        config = load_config(REPO_CONFIG)
        self.assertEqual(config.timezone.key, "America/Santo_Domingo")
        self.assertEqual(config.moodle_sync.interval_hours, 6)
        self.assertTrue(all(config.is_enabled(j) for j in
                            ("moodle_sync", "ingestion", "extraction", "notifications")))
        # Private data defaults to <project>/data, which .gitignore excludes.
        self.assertEqual(config.database_path.parent, REPO_CONFIG.parents[1] / "data")

    def test_empty_file_gets_reasonable_defaults(self) -> None:
        config = load_config(self.write(""))
        self.assertEqual(config.timezone.key, "America/Santo_Domingo")
        self.assertEqual(config.moodle_sync.interval_hours, 6)
        self.assertEqual(config.retry_delay_seconds, 60)
        self.assertFalse(config.allow_manual_disabled_jobs)
        self.assertGreater(config.ingestion.max_items, 0)
        self.assertGreater(config.extraction.max_seconds, 0)
        self.assertEqual([r.id for r in config.notifications.reminders],
                         ["24h", "12h", "6h", "1h"])

    def test_the_repository_config_declares_the_same_reminders_as_the_defaults(self) -> None:
        # The shipped file and the built-in defaults must not drift apart:
        # a reminder added to one and not the other is invisible until it
        # fails to arrive.
        from scheduler.config import load_config as load
        shipped = load(REPO_CONFIG).notifications.reminders
        defaults = load(self.write("")).notifications.reminders
        self.assertEqual([(r.id, r.offset_seconds) for r in shipped],
                         [(r.id, r.offset_seconds) for r in defaults])

    def test_a_reminder_can_be_switched_off_without_removing_it(self) -> None:
        config = load_config(self.write(
            "[[notifications.reminders]]\n"
            'id = "solo"\n'
            "offset_hours = 3\n"
            'title = "t"\n'
            'body = "b"\n'
            "\n"
            "[[notifications.reminders]]\n"
            'id = "apagado"\n'
            "offset_hours = 1\n"
            "enabled = false\n"
            'title = "t"\n'
            'body = "b"\n'
        ))
        self.assertEqual([r.id for r in config.notifications.reminders], ["solo"])

    def test_the_file_cannot_configure_a_reminder_that_would_never_be_sent(self) -> None:
        # Regression: the hourly floor was enforced when an interface saved
        # rules, but not when the file declared them - so a 30-minute reminder
        # could be configured here and then silently never delivered.
        half_hour = (
            '[[notifications.reminders]]\nid = "30m"\noffset_hours = 0.5\n'
            'title = "t"\nbody = "b"\n'
        )
        narrow_gap = (
            '[[notifications.reminders]]\nid = "a"\noffset_hours = 24\ntitle = "t"\nbody = "b"\n'
            '[[notifications.reminders]]\nid = "b"\noffset_hours = 23.5\ntitle = "t"\nbody = "b"\n'
        )
        for text in (half_hour, narrow_gap):
            with self.subTest(text=text), self.assertRaises(ConfigError) as raised:
                load_config(self.write(text))
            self.assertIn("30 minutes", str(raised.exception))

    def test_a_switched_off_entry_is_still_checked(self) -> None:
        # Regression: a disabled entry was skipped before validation, so a
        # broken template waited there until someone enabled it and then
        # failed a startup far from the edit that caused it.
        with self.assertRaises(ConfigError) as raised:
            load_config(self.write(
                '[[notifications.reminders]]\nid = "x"\noffset_hours = 2\n'
                'enabled = false\ntitle = "{materia}"\nbody = "b"\n'
            ))
        self.assertIn("materia", str(raised.exception))

    def test_a_duplicate_id_counts_even_when_one_is_switched_off(self) -> None:
        with self.assertRaises(ConfigError) as raised:
            load_config(self.write(
                '[[notifications.reminders]]\nid = "a"\noffset_hours = 6\ntitle = "t"\nbody = "b"\n'
                '[[notifications.reminders]]\nid = "a"\noffset_hours = 2\nenabled = false\n'
                'title = "t"\nbody = "b"\n'
            ))
        self.assertIn("duplicate reminder id", str(raised.exception))

    def test_a_template_with_an_unknown_placeholder_is_rejected_at_load(self) -> None:
        with self.assertRaises(ConfigError) as raised:
            load_config(self.write(
                "[[notifications.reminders]]\n"
                'id = "x"\n'
                "offset_hours = 1\n"
                'title = "{materia}"\n'
                'body = "b"\n'
            ))
        self.assertIn("materia", str(raised.exception))

    def test_reminders_that_would_collide_are_rejected(self) -> None:
        duplicate_id = (
            '[[notifications.reminders]]\nid = "x"\noffset_hours = 2\ntitle = "t"\nbody = "b"\n'
            '[[notifications.reminders]]\nid = "x"\noffset_hours = 1\ntitle = "t"\nbody = "b"\n'
        )
        same_offset = (
            '[[notifications.reminders]]\nid = "a"\noffset_hours = 2\ntitle = "t"\nbody = "b"\n'
            '[[notifications.reminders]]\nid = "b"\noffset_hours = 2\ntitle = "t"\nbody = "b"\n'
        )
        for text in (duplicate_id, same_offset):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                load_config(self.write(text))

    def test_the_old_single_threshold_key_explains_itself(self) -> None:
        # A scheduler.toml deployed before the reminder rules.
        with self.assertRaises(ConfigError) as raised:
            load_config(self.write("[notifications]\ndue_soon_hours = 24\n"))
        self.assertIn("reminders", str(raised.exception))

    def test_values_are_read(self) -> None:
        config = load_config(self.write(
            '[moodle_sync]\nenabled = false\ninterval_hours = 2\n'
            '[ingestion]\nmax_items = 5\nmax_seconds = 30\n'
            '[scheduler]\nallow_manual_disabled_jobs = true\n'
        ))
        self.assertFalse(config.moodle_sync.enabled)
        self.assertEqual(config.moodle_sync.interval_hours, 2)
        self.assertEqual((config.ingestion.max_items, config.ingestion.max_seconds), (5, 30))
        self.assertTrue(config.allow_manual_disabled_jobs)

    def test_relative_paths_resolve_against_the_config_file_directory(self) -> None:
        config = load_config(self.write('[scheduler]\ndatabase_path = "db/x.sqlite3"\n'))
        self.assertEqual(config.database_path, (self.dir / "db" / "x.sqlite3").resolve())
        self.assertEqual(config.lock_path.name, "x.sqlite3.lock")

    def test_missing_file_fails_closed(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self.dir / "nope.toml")

    def test_unknown_key_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self.write("[moodle_sync]\nintervall_hours = 6\n"))

    def test_unknown_section_is_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self.write("[forums]\nenabled = true\n"))

    def test_a_period_that_does_not_divide_the_day_is_rejected(self) -> None:
        # 5 hours would give 00, 05, 10, 15, 20 and then a 4-hour step, so the
        # schedule would not be the uniform one the file appears to describe.
        for text in (
            "[moodle_sync]\ninterval_hours = 5\n",
            "[moodle_sync]\ninterval_hours = 7\n",
            "[moodle_sync]\ninterval_hours = 1.5\n",
            "[moodle_sync]\ninterval_hours = 48\n",
            "[notifications]\ninterval_hours = 5\n",
        ):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                load_config(self.write(text))

    def test_every_period_that_divides_the_day_is_accepted(self) -> None:
        for hours in (1, 2, 3, 4, 6, 8, 12, 24):
            config = load_config(self.write(f"[moodle_sync]\ninterval_hours = {hours}\n"))
            self.assertEqual(config.moodle_sync.interval_hours, hours)

    def test_the_notification_cycle_is_fixed_and_cannot_be_configured(self) -> None:
        # Pinned so it can never disagree with the reminders it must deliver.
        self.assertEqual(load_config(self.write("")).notifications.interval_hours, 1)

        with self.assertRaises(ConfigError) as raised:
            load_config(self.write("[notifications]\ninterval_hours = 2\n"))
        self.assertIn("always checked hourly", str(raised.exception))

    def test_schedules_cover_exactly_the_jobs_that_have_a_clock_grid(self) -> None:
        config = load_config(self.write(""))
        self.assertEqual(config.schedules, {"moodle_sync": 6, "notifications": 1})

    def test_invalid_values_are_rejected(self) -> None:
        for text in (
            "[moodle_sync]\ninterval_hours = 0\n",
            "[moodle_sync]\nenabled = \"yes\"\n",
            "[ingestion]\nmax_items = 0\n",
            "[extraction]\nmax_items = 1.5\n",
            "[scheduler]\nretry_delay_seconds = -1\n",
            "[scheduler]\ntimezone = \"Mars/Olympus\"\n",
            "not toml = = =",
        ):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                load_config(self.write(text))

    def test_timezone_is_independent_of_the_host(self) -> None:
        config = load_config(self.write(""))
        moment = datetime(2026, 7, 1, 12, tzinfo=config.timezone)  # no DST in DR
        self.assertEqual(moment.utcoffset(), timedelta(hours=-4))


class LockTests(ConfigTestCase):
    def test_second_holder_is_refused_until_release(self) -> None:
        path = self.dir / "a.sqlite3.lock"
        first, second = SingleInstanceLock(path), SingleInstanceLock(path)
        self.assertTrue(first.try_acquire())
        try:
            self.assertFalse(second.try_acquire())
            self.assertTrue(is_locked(path))
        finally:
            first.release()
        self.assertFalse(is_locked(path))
        self.assertTrue(second.try_acquire())
        second.release()


class RedactionTests(unittest.TestCase):
    TOKEN = "0123456789abcdef0123456789abcdef"

    def test_token_query_parameters_are_redacted(self) -> None:
        text = redact("https://x/pluginfile.php/a.pdf?forcedownload=1&token=abc&wstoken=def x")
        self.assertNotIn("abc", text)
        self.assertNotIn("def", text)
        self.assertIn("token=***", text)

    def test_literal_token_from_environment_is_redacted(self) -> None:
        with patch.dict(os.environ, {"MOODLE_TOKEN": self.TOKEN}):
            self.assertEqual(redact(f"bad {self.TOKEN} here"), "bad *** here")

    def test_empty_or_short_token_does_not_mangle_text(self) -> None:
        for value in ("", "   ", "ab"):
            with self.subTest(value=value), patch.dict(os.environ, {"MOODLE_TOKEN": value}):
                self.assertEqual(redact("abc"), "abc")

    def test_safe_error_is_bounded(self) -> None:
        text = safe_error(RuntimeError("x" * 5000))
        self.assertLessEqual(len(text), 1000)
        self.assertTrue(text.startswith("RuntimeError: "))


class ShippedConfigFilesTests(unittest.TestCase):
    """The repository ships two configurations: config/scheduler.toml for local
    development and deploy/scheduler.toml for the VPS (absolute FHS paths).
    They must stay structurally identical, or one of them silently rots."""

    ROOT = Path(__file__).resolve().parents[2]

    def load(self, relative: str):
        path = self.ROOT / relative
        self.assertTrue(path.is_file(), f"missing {relative}")
        return tomllib.loads(path.read_text(encoding="utf-8"))

    def test_both_shipped_configs_are_accepted_by_the_loader(self) -> None:
        for relative in ("config/scheduler.toml", "deploy/scheduler.toml"):
            with self.subTest(config=relative):
                load_config(self.ROOT / relative)  # raises ConfigError if invalid

    def test_the_two_configs_declare_exactly_the_same_keys(self) -> None:
        local = self.load("config/scheduler.toml")
        vps = self.load("deploy/scheduler.toml")

        self.assertEqual(sorted(local), sorted(vps))
        for section in local:
            with self.subTest(section=section):
                self.assertEqual(sorted(local[section]), sorted(vps[section]))

    def test_the_vps_config_uses_absolute_paths_outside_the_repository(self) -> None:
        """The whole point of the VPS variant: private data must not land in a
        repository-relative (and, on the dev machine, cloud-synced) folder."""
        vps = self.load("deploy/scheduler.toml")["scheduler"]

        for key in ("database_path", "storage_path"):
            with self.subTest(key=key):
                value = vps[key]
                self.assertTrue(value.startswith("/"), value)
                self.assertNotIn("..", value)

    def test_neither_shipped_config_contains_a_secret_key(self) -> None:
        for relative in ("config/scheduler.toml", "deploy/scheduler.toml"):
            text = (self.ROOT / relative).read_text(encoding="utf-8").lower()
            with self.subTest(config=relative):
                for forbidden in ("moodle_token", "wstoken", "api_key", "password"):
                    self.assertNotIn(f"{forbidden} =", text)
                    self.assertNotIn(f"{forbidden}=", text)


if __name__ == "__main__":
    unittest.main()
