"""The clock grid: when a scheduled Job's next run is due."""
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from scheduler.schedules import VALID_PERIOD_HOURS, next_aligned

TZ = ZoneInfo("America/Santo_Domingo")


def at(hour: int, minute: int = 0, second: int = 0, day: int = 2) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=TZ)


class NextAlignedTests(unittest.TestCase):
    def test_hourly_grid_is_the_next_whole_hour(self) -> None:
        self.assertEqual(next_aligned(at(17, 43), 1), at(18))
        self.assertEqual(next_aligned(at(17, 0, 1), 1), at(18))
        self.assertEqual(next_aligned(at(0, 0, 1), 1), at(1))

    def test_six_hourly_grid_is_midnight_six_noon_eighteen(self) -> None:
        self.assertEqual(next_aligned(at(17, 43), 6), at(18))
        self.assertEqual(next_aligned(at(8), 6), at(12))
        self.assertEqual(next_aligned(at(0, 1), 6), at(6))

    def test_a_slot_instant_returns_the_following_slot_never_itself(self) -> None:
        # Strictly future: otherwise the job due *now* would be requeued on
        # every pass of the loop instead of once.
        self.assertEqual(next_aligned(at(12), 6), at(18))
        self.assertEqual(next_aligned(at(9), 1), at(10))

    def test_midnight_is_crossed_without_a_short_step(self) -> None:
        self.assertEqual(next_aligned(at(23, 30), 6), at(0, day=3))
        self.assertEqual(next_aligned(at(23, 30), 1), at(0, day=3))
        self.assertEqual(next_aligned(at(18, 0, 1), 6), at(0, day=3))

    def test_downtime_lands_on_the_next_future_slot_not_on_the_missed_ones(self) -> None:
        # Back at 09:17 after a day down: the next run is 12:00, and the four
        # slots that passed are simply gone.
        self.assertEqual(next_aligned(at(9, 17, day=3), 6), at(12, day=3))

    def test_the_grid_does_not_depend_on_when_the_process_started(self) -> None:
        # Two processes starting 37 minutes apart compute the same instant,
        # which is what makes a restart free of stored state.
        self.assertEqual(next_aligned(at(13, 5), 6), next_aligned(at(13, 42), 6))

    def test_every_valid_period_divides_the_day(self) -> None:
        self.assertEqual(VALID_PERIOD_HOURS, (1, 2, 3, 4, 6, 8, 12, 24))

    def test_every_valid_period_produces_a_uniform_grid(self) -> None:
        """Walk a full day for each period: every step is exactly the period."""
        for period in VALID_PERIOD_HOURS:
            moment = at(0)
            for _ in range(24 // period):
                following = next_aligned(moment, period)
                self.assertEqual(following - moment, timedelta(hours=period), period)
                moment = following
            self.assertEqual(moment, at(0, day=3), period)  # landed exactly on midnight

    def test_a_window_of_the_period_always_contains_exactly_one_slot(self) -> None:
        """Why the period must not exceed the narrowest reminder window.

        A reminder whose window is one hour wide is only ever delivered if a
        run falls inside it. With an hourly grid that is guaranteed; with a
        wider one it is not, and the reminder is silently lost.
        """
        for minute in range(0, 60, 7):
            window_end = at(14, minute)
            window_start = window_end - timedelta(hours=1)
            slot = next_aligned(window_start, 1)
            self.assertGreater(slot, window_start)
            self.assertLessEqual(slot, window_end)

    def test_a_window_narrower_than_the_period_can_be_missed(self) -> None:
        # The same one-hour window against a two-hour grid: 14:30 -> 15:30
        # contains no even hour, so the next run is at 16:00, after the
        # deadline has already passed. That reminder is never sent.
        window_start, window_end = at(14, 30), at(15, 30)
        self.assertEqual(next_aligned(window_start, 2), at(16))
        self.assertGreater(next_aligned(window_start, 2), window_end)


if __name__ == "__main__":
    unittest.main()
