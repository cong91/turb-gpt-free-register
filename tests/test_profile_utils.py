import unittest
from unittest.mock import patch

from core import profile_utils
from core.time_utils import local_today


class ProfileUtilsTests(unittest.TestCase):
    def test_default_birthday_range_is_strictly_under_thirty(self):
        today = local_today()
        oldest = profile_utils._shift_year_safe(today, -29)
        youngest = profile_utils._shift_year_safe(today, -18)
        span_days = (youngest - oldest).days

        with patch.object(profile_utils.random, "randint", return_value=0) as randint:
            birthday = profile_utils.generate_random_birthday()

        self.assertEqual(birthday, oldest.isoformat())
        randint.assert_called_once_with(0, span_days)

    def test_default_birthday_upper_boundary_is_eighteen(self):
        today = local_today()
        oldest = profile_utils._shift_year_safe(today, -29)
        youngest = profile_utils._shift_year_safe(today, -18)
        span_days = (youngest - oldest).days

        with patch.object(profile_utils.random, "randint", return_value=span_days):
            birthday = profile_utils.generate_random_birthday()

        self.assertEqual(birthday, youngest.isoformat())


if __name__ == "__main__":
    unittest.main()
