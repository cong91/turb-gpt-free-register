import unittest

from config import humanize
from core.browser_use_registration import _cloud_typing_delay


class TypingSpeedTests(unittest.TestCase):
    def test_shared_humanize_typing_ranges_are_quicker(self):
        self.assertLessEqual(humanize.HUMANIZE_DELAYS["keystroke"][1], 0.12)
        self.assertLessEqual(humanize.HUMANIZE_DELAYS["typing_pause"][1], 0.5)

    def test_browser_use_typing_ranges_are_quicker_in_fast_mode(self):
        self.assertLessEqual(_cloud_typing_delay("email")[1], 90)
        self.assertLessEqual(_cloud_typing_delay("otp")[1], 70)
        self.assertLessEqual(_cloud_typing_delay("name")[1], 110)


if __name__ == "__main__":
    unittest.main()
