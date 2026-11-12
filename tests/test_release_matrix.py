"""The dispatch conformance selector names a pinned matrix and does not run it."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from scripts.release_matrix import (
    COMPATIBILITY_WORKFLOW_TOOLCHAIN,
    TOOLCHAIN_FLOOR,
    ReleaseMatrixError,
    conformance_report,
    matrix_entries,
    select_release,
    supported_baseline,
)

ROOT = Path(__file__).resolve().parents[1]


class ReleaseMatrixTests(unittest.TestCase):
    def test_selector_names_the_baseline_and_two_selected_constants(self):
        minimum = json.loads((ROOT / "toolchains.json").read_text(encoding="utf-8"))["minimum"]
        floor = json.loads((ROOT / "verification-profile.json").read_text(encoding="utf-8"))
        workflow = (ROOT / ".github" / "workflows" / "compatibility.yml").read_text(encoding="utf-8")
        self.assertEqual(supported_baseline(), f"leanprover/lean4:{minimum}")
        self.assertEqual(COMPATIBILITY_WORKFLOW_TOOLCHAIN, "leanprover/lean4:v4.35.0-rc2")
        self.assertEqual(TOOLCHAIN_FLOOR, "v4.35.0-rc2")
        self.assertEqual(TOOLCHAIN_FLOOR, floor["trusted_tools"]["toolchain_floor"])
        self.assertIn(COMPATIBILITY_WORKFLOW_TOOLCHAIN, workflow)
        entries = matrix_entries()
        self.assertEqual(
            [entry["role"] for entry in entries],
            ["supported_baseline", "compatibility_workflow", "toolchain_floor"],
        )
        self.assertEqual(
            [entry["origin"] for entry in entries],
            ["toolchains.json", "selected-at-implementation", "selected-at-implementation"],
        )
        self.assertTrue(all(entry["toolchain"] == COMPATIBILITY_WORKFLOW_TOOLCHAIN for entry in entries))
        report = conformance_report()
        self.assertEqual(report["pull_request_cold_build"], "single-toolchain")
        self.assertEqual(report["distinct_releases"], [COMPATIBILITY_WORKFLOW_TOOLCHAIN])
        self.assertEqual(select_release("v4.35.0-rc2"), COMPATIBILITY_WORKFLOW_TOOLCHAIN)

    def test_an_unknown_release_is_refused(self):
        # v4.36.0-rc1 and v4.35.0 are named elsewhere in this repository.
        # They are not part of this matrix, so they are not tested.
        for release in (
            "leanprover/lean4:v9.9.9",
            "leanprover/lean4:v4.36.0-rc1",
            "leanprover/lean4:v4.35.0",
            "nightly-2026-08-01",
        ):
            with self.subTest(release=release):
                with self.assertRaisesRegex(ReleaseMatrixError, "unknown release"):
                    select_release(release)
                with self.assertRaisesRegex(ReleaseMatrixError, "unknown release"):
                    conformance_report(release=release, checker=Path("/usr/bin/true"))
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "report.json"
            checker = Path(raw) / "checker"
            checker.write_text("#!/bin/true\n", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/release_matrix.py",
                    "--output",
                    str(output),
                    "--checker",
                    str(checker),
                    "--release",
                    "leanprover/lean4:v4.36.0-rc1",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("unknown release", result.stderr)
            self.assertFalse(output.exists())

    def test_a_missing_checker_is_skipped_and_inconclusive(self):
        report = conformance_report(checker=None)
        self.assertEqual(report["status"], "skipped")
        self.assertEqual(report["conclusion"], "inconclusive")
        self.assertIn("absent", report["reason"])
        self.assertNotIn("passed", json.dumps(report))
        with tempfile.TemporaryDirectory() as raw:
            missing = Path(raw) / "no-checker"
            report = conformance_report(checker=missing)
            self.assertEqual(report["status"], "skipped")
            self.assertEqual(report["conclusion"], "inconclusive")
            present = Path(raw) / "checker"
            present.write_bytes(b"checker")
            link = Path(raw) / "linked"
            link.symlink_to(present)
            self.assertEqual(conformance_report(checker=link)["status"], "skipped")
            selected = conformance_report(checker=present)
            self.assertEqual(selected["status"], "selected")
            self.assertEqual(selected["conclusion"], "would-test")
            self.assertEqual(selected["pull_request_cold_build"], "single-toolchain")
            output = Path(raw) / "report.json"
            result = subprocess.run(
                [sys.executable, "scripts/release_matrix.py", "--output", str(output)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            written = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(written["status"], "skipped")
            self.assertEqual(written["conclusion"], "inconclusive")

    def test_pull_request_cold_build_stays_a_single_toolchain(self):
        path = ROOT / ".github" / "workflows" / "compatibility.yml"
        text = path.read_text(encoding="utf-8")
        workflow = yaml.load(text, Loader=yaml.BaseLoader)
        self.assertEqual(text.count("toolchain install"), 1)
        cold = workflow["jobs"]["cold_build"]
        self.assertNotIn("strategy", cold)
        report_job = workflow["jobs"]["release_conformance"]
        self.assertEqual(report_job["if"], "github.event_name == 'workflow_dispatch'")
        self.assertNotIn("strategy", report_job)
        runs = "\n".join(step.get("run", "") for step in report_job["steps"])
        self.assertIn("scripts/release_matrix.py", runs)
        self.assertNotIn("toolchain install", runs)
        self.assertNotIn("release_conformance", workflow["jobs"]["compatibility"]["needs"])
        self.assertEqual(workflow["on"]["pull_request"], "")


if __name__ == "__main__":
    unittest.main()
