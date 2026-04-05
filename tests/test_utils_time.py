import unittest
from datetime import datetime

from app.utils.time import utc_today, utcnow_naive


class UtilsTimeTests(unittest.TestCase):
    def test_utcnow_naive(self) -> None:
        value = utcnow_naive()
        self.assertIsInstance(value, datetime)
        self.assertIsNone(value.tzinfo)

    def test_utc_today(self) -> None:
        today = utc_today()
        self.assertEqual(str(today), utcnow_naive().date().isoformat())


if __name__ == "__main__":
    unittest.main()
