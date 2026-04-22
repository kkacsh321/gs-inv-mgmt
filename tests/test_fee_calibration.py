import unittest

from app.services.fee_calibration import build_final_value_rate_calibration


class FeeCalibrationTests(unittest.TestCase):
    def test_returns_current_when_no_usable_samples(self) -> None:
        payload = build_final_value_rate_calibration(
            [],
            current_final_value_rate_percent=13.25,
        )
        self.assertEqual(payload["sample_count"], 0.0)
        self.assertAlmostEqual(payload["suggested_final_value_rate_percent"], 13.25, places=3)
        self.assertAlmostEqual(payload["delta_percent"], 0.0, places=3)

    def test_uses_median_implied_rate_and_clamps(self) -> None:
        rows = [
            {"fee_estimate_present": True, "sale_gross": 100.0, "implied_final_value_rate_percent": 12.0},
            {"fee_estimate_present": True, "sale_gross": 120.0, "implied_final_value_rate_percent": 14.0},
            {"fee_estimate_present": True, "sale_gross": 80.0, "implied_final_value_rate_percent": 13.0},
            # outlier (ignored by default floor/ceiling)
            {"fee_estimate_present": True, "sale_gross": 90.0, "implied_final_value_rate_percent": 99.0},
        ]
        payload = build_final_value_rate_calibration(
            rows,
            current_final_value_rate_percent=13.25,
        )
        self.assertEqual(payload["sample_count"], 3.0)
        self.assertAlmostEqual(payload["median_implied_final_value_rate_percent"], 13.0, places=3)
        self.assertAlmostEqual(payload["suggested_final_value_rate_percent"], 13.0, places=3)
        self.assertAlmostEqual(payload["delta_percent"], -0.25, places=3)


if __name__ == "__main__":
    unittest.main()

