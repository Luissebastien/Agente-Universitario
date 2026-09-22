import unittest

from read_model.read_model import is_assignment_pending, is_assignment_upcoming

NOW = 1_800_000_000
DAY = 86_400


class IsAssignmentPendingTests(unittest.TestCase):
    def test_future_due_date_not_submitted_is_pending(self) -> None:
        self.assertTrue(is_assignment_pending(NOW + DAY, "new", NOW))

    def test_future_due_date_already_submitted_is_not_pending(self) -> None:
        self.assertFalse(is_assignment_pending(NOW + DAY, "submitted", NOW))

    def test_past_due_date_not_submitted_is_not_pending(self) -> None:
        # "pendiente" is explicitly defined as próxima (not yet past) - a
        # late, unsubmitted assignment is a different concept, not "pending".
        self.assertFalse(is_assignment_pending(NOW - DAY, "new", NOW))

    def test_due_exactly_now_counts_as_not_yet_past(self) -> None:
        self.assertTrue(is_assignment_pending(NOW, "new", NOW))

    def test_no_due_date_none_is_never_pending(self) -> None:
        self.assertFalse(is_assignment_pending(None, "new", NOW))

    def test_no_due_date_zero_is_never_pending(self) -> None:
        # Moodle's own "no due date set" convention (duedate == 0) - a real
        # case confirmed against INTEC, not hypothetical.
        self.assertFalse(is_assignment_pending(0, "new", NOW))

    def test_unknown_submission_status_is_never_pending(self) -> None:
        # Status was never synced (AssignmentSubmissionStatus not fetched
        # for this assignment) - unknown must never be assumed "not
        # submitted" (DEC-013 fail closed).
        self.assertFalse(is_assignment_pending(NOW + DAY, None, NOW))

    def test_draft_status_counts_as_not_submitted(self) -> None:
        self.assertTrue(is_assignment_pending(NOW + DAY, "draft", NOW))

    def test_no_due_date_and_unknown_status_is_not_pending(self) -> None:
        self.assertFalse(is_assignment_pending(None, None, NOW))


class IsAssignmentUpcomingTests(unittest.TestCase):
    def test_due_tomorrow_within_7_days_is_upcoming(self) -> None:
        self.assertTrue(is_assignment_upcoming(NOW + DAY, NOW, days=7))

    def test_due_in_10_days_is_not_within_7_day_window(self) -> None:
        self.assertFalse(is_assignment_upcoming(NOW + 10 * DAY, NOW, days=7))

    def test_due_exactly_at_the_window_boundary_is_upcoming(self) -> None:
        self.assertTrue(is_assignment_upcoming(NOW + 7 * DAY, NOW, days=7))

    def test_due_one_second_past_the_boundary_is_not_upcoming(self) -> None:
        self.assertFalse(is_assignment_upcoming(NOW + 7 * DAY + 1, NOW, days=7))

    def test_already_past_due_is_not_upcoming(self) -> None:
        self.assertFalse(is_assignment_upcoming(NOW - DAY, NOW, days=7))

    def test_no_due_date_is_never_upcoming(self) -> None:
        self.assertFalse(is_assignment_upcoming(None, NOW, days=7))
        self.assertFalse(is_assignment_upcoming(0, NOW, days=7))

    def test_already_submitted_assignment_can_still_be_upcoming(self) -> None:
        # "upcoming" is deliberately independent of submission status -
        # that's what distinguishes it from is_assignment_pending().
        self.assertTrue(is_assignment_upcoming(NOW + DAY, NOW, days=7))


if __name__ == "__main__":
    unittest.main()
