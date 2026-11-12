"""Stage timings identify the expensive phase or say they are unknown."""
import unittest

from scripts.stage_performance import stage_performance


class StagePerformanceTests(unittest.TestCase):
    def test_critical_phase_is_the_slowest_measured_stage(self):
        summary = stage_performance([
            {"phase": "export", "elapsed_seconds": 2, "max_rss_kib": 10},
            {"phase": "nanoda", "elapsed_seconds": 40, "max_rss_kib": 100},
            {"phase": "build", "elapsed_seconds": 9, "max_rss_kib": 20},
        ])
        self.assertEqual(summary["critical_phase"], "nanoda")
        self.assertEqual(summary["critical_elapsed_seconds"], 40)
        self.assertFalse(summary["certificate"])

    def test_missing_elapsed_time_stays_unknown(self):
        summary = stage_performance([{"phase": "build"}, "not-a-record"])
        self.assertIsNone(summary["critical_phase"])
        self.assertEqual(summary["phases"][0]["status"], "unknown")
        self.assertEqual(summary["phases"][1]["phase"], "unknown")

    def test_empty_telemetry_is_an_explicit_unknown(self):
        summary = stage_performance([])
        self.assertIsNone(summary["critical_phase"])
        self.assertEqual(summary["phases"], [])


if __name__ == "__main__":
    unittest.main()
