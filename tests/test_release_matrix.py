"""The conformance matrix stays on known releases and skips a missing checker."""
import json
import unittest
from pathlib import Path

from scripts.release_matrix import ReleaseMatrixError, conformance_report, refuse_unknown, selected_releases

ROOT = Path(__file__).resolve().parents[1]


class ReleaseMatrixTests(unittest.TestCase):
    def revisions(self):
        document = json.loads((ROOT / "checker-revisions.json").read_text(encoding="utf-8"))
        return document

    def test_the_matrix_is_baseline_stable_and_one_rc(self):
        minimum = json.loads((ROOT / "toolchains.json").read_text())["minimum"]
        rows = selected_releases(self.revisions(), minimum)
        self.assertEqual([row["role"] for row in rows], ["baseline", "stable", "release-candidate"])
        self.assertTrue(rows[0]["per_pr"])
        self.assertFalse(rows[1]["per_pr"])
        self.assertFalse(rows[2]["per_pr"])

    def test_an_unknown_release_is_refused(self):
        with self.assertRaisesRegex(ReleaseMatrixError, "unknown release"):
            refuse_unknown("leanprover/lean4:v9.9.9", self.revisions())

    def test_a_missing_checker_is_skipped_not_passed(self):
        minimum = json.loads((ROOT / "toolchains.json").read_text())["minimum"]
        report = conformance_report(self.revisions(), minimum, {})
        self.assertEqual(report["pull_request_cold_build"], "baseline-only")
        self.assertTrue(all(row["status"] == "skipped" for row in report["releases"]))
        self.assertNotIn("passed", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
