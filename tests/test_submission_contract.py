import os
import subprocess
import sys
import unittest
from pathlib import Path

import scripts.verify_submission as verifier
from scripts import submission_contract

ROOT = Path(__file__).resolve().parents[1]


class SubmissionContractBoundaryTests(unittest.TestCase):
    def test_lake_package_names_keep_paths_safe_and_numeric_names_stable(self):
        self.assertIsNone(submission_contract.lake_package_name("\u00ab..\u00bb"))
        self.assertIsNone(submission_contract.lake_package_name("\u00ab.git\u00bb"))
        self.assertIsNone(submission_contract.lake_package_name("\u00ab.lake\u00bb"))
        self.assertEqual(submission_contract.lake_manifest_name("1.2"), "1.2")
        self.assertEqual(submission_contract.lake_manifest_name("my-package"), "\u00abmy-package\u00bb")

    def test_orchestrator_has_no_contract_compatibility_entry_points(self):
        self.assertTrue(set(submission_contract.__all__).isdisjoint(vars(verifier)))

    def test_workflow_script_entrypoints_load_one_canonical_verifier(self):
        for script, marker in (
            ("verify_submission.py", "{check-capacity,prepare,execute}"),
            ("smoke_trusted_challenge.py", "--source SOURCE"),
        ):
            with self.subTest(script=script):
                completed = subprocess.run(
                    [
                        sys.executable,
                        f"{ROOT.name}/scripts/{script}",
                        "--help",
                    ],
                    cwd=ROOT.parent,
                    env={**os.environ, "PYTHONPATH": ""},
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn(marker, completed.stdout)


if __name__ == "__main__":
    unittest.main()
