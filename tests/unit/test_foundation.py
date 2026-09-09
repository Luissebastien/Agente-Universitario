import unittest

import agent


class FoundationTestCase(unittest.TestCase):
    def test_agent_package_is_importable(self) -> None:
        self.assertTrue(hasattr(agent, "__name__"))


if __name__ == "__main__":
    unittest.main()
