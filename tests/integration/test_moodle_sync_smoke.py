import os
import tempfile
import unittest
from pathlib import Path

from database.db import connect
from moodle.client import MoodleClient
from moodle.sync import MoodleSync

_REQUIRED_VARS = ("MOODLE_URL", "MOODLE_TOKEN")


def _integration_enabled() -> bool:
    if os.environ.get("MOODLE_RUN_INTEGRATION_TESTS") != "1":
        return False
    return all(os.environ.get(var) for var in _REQUIRED_VARS)


@unittest.skipUnless(
    _integration_enabled(),
    "Set MOODLE_RUN_INTEGRATION_TESTS=1 plus MOODLE_URL/MOODLE_TOKEN to run against real Moodle",
)
class MoodleSyncSmokeTest(unittest.TestCase):
    def test_sync_profile_courses_and_contents_against_real_moodle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "smoke.sqlite3"
            conn = connect(db_path)
            try:
                with MoodleClient.from_env() as client:
                    sync = MoodleSync(client, conn)

                    user = sync.sync_profile()
                    print(f"[smoke] synced profile userid={user.id}")

                    courses = sync.sync_courses(user.id)
                    print(f"[smoke] synced {len(courses)} courses")
                    self.assertGreater(len(courses), 0)

                    # Re-run to prove idempotency against the real data too.
                    courses_again = sync.sync_courses(user.id)
                    course_count = conn.execute("SELECT COUNT(*) AS n FROM courses").fetchone()["n"]
                    self.assertEqual(course_count, len(courses_again))

                    first_course = courses[0]
                    sections = sync.sync_course_contents(first_course.id)
                    print(
                        f"[smoke] synced {len(sections)} sections for course {first_course.id}"
                    )

                    module_count = conn.execute(
                        "SELECT COUNT(*) AS n FROM course_modules WHERE course_id = ?",
                        (first_course.id,),
                    ).fetchone()["n"]
                    file_count = conn.execute(
                        "SELECT COUNT(*) AS n FROM course_files"
                    ).fetchone()["n"]
                    print(f"[smoke] modules={module_count} files_recorded={file_count}")
            finally:
                conn.close()

    def test_sync_assignments_grades_and_calendar_against_real_moodle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "smoke.sqlite3"
            conn = connect(db_path)
            try:
                with MoodleClient.from_env() as client:
                    sync = MoodleSync(client, conn)
                    user = sync.sync_profile()
                    courses = sync.sync_courses(user.id)
                    self.assertGreater(len(courses), 0)

                    assignments = sync.sync_assignments([c.id for c in courses])
                    print(f"[smoke] synced {len(assignments)} assignments across all courses")

                    # Idempotency against real data.
                    sync.sync_assignments([c.id for c in courses])
                    count = conn.execute("SELECT COUNT(*) AS n FROM assignments").fetchone()["n"]
                    self.assertEqual(count, len(assignments))

                    if assignments:
                        status = sync.sync_assignment_submission_status(assignments[0].id)
                        print(
                            f"[smoke] submission status for assignment {assignments[0].id}: "
                            f"grading_status={status.grading_status}"
                        )
                        grade_course_id = assignments[0].course_id
                    else:
                        grade_course_id = courses[0].id

                    grades = sync.sync_grades(grade_course_id, user.id)
                    print(f"[smoke] synced {len(grades)} grade items for course {grade_course_id}")
                    sync.sync_grades(grade_course_id, user.id)  # idempotency
                    grade_count = conn.execute(
                        "SELECT COUNT(*) AS n FROM grades WHERE course_id = ?",
                        (grade_course_id,),
                    ).fetchone()["n"]
                    self.assertEqual(grade_count, len(grades))

                    events = sync.sync_calendar([c.id for c in courses])
                    print(f"[smoke] synced {len(events)} calendar events across all courses")
                    sync.sync_calendar([c.id for c in courses])  # idempotency
                    event_count = conn.execute(
                        "SELECT COUNT(*) AS n FROM calendar_events"
                    ).fetchone()["n"]
                    self.assertEqual(event_count, len(events))
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
