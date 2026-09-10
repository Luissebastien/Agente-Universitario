import os
import unittest

from moodle.client import MoodleClient

_REQUIRED_VARS = ("MOODLE_URL", "MOODLE_TOKEN")


def _integration_enabled() -> bool:
    # Two separate gates on purpose: having MOODLE_URL/MOODLE_TOKEN configured
    # for the app (e.g. via a local .env) must not, by itself, cause a normal
    # `unittest discover` run to hit the real network.
    if os.environ.get("MOODLE_RUN_INTEGRATION_TESTS") != "1":
        return False
    return all(os.environ.get(var) for var in _REQUIRED_VARS)


@unittest.skipUnless(
    _integration_enabled(),
    "Set MOODLE_RUN_INTEGRATION_TESTS=1 plus MOODLE_URL/MOODLE_TOKEN to run against real Moodle",
)
class MoodleClientSmokeTest(unittest.TestCase):
    def test_real_site_info_courses_and_contents(self) -> None:
        with MoodleClient.from_env() as client:
            site_info = client.get_site_info()
            self.assertIn("sitename", site_info)
            print(f"[smoke] site: {site_info.get('sitename')} ({site_info.get('release')})")

            courses = client.get_courses()
            self.assertIsInstance(courses, list)
            print(f"[smoke] courses: {len(courses)}")

            if not courses:
                return

            first_course = courses[0]
            contents = client.get_course_contents(first_course["id"])
            self.assertIsInstance(contents, list)
            print(f"[smoke] sections in course {first_course['id']}: {len(contents)}")

            for section in contents:
                for module in section.get("modules", []):
                    for file_info in module.get("contents", []) or []:
                        file_url = file_info.get("fileurl")
                        if not file_url:
                            continue
                        data = client.download_file(file_url)
                        print(f"[smoke] downloaded {len(data)} bytes from a course file")
                        return


if __name__ == "__main__":
    unittest.main()
