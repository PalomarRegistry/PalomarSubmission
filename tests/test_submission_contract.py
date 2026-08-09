import subprocess
import sys
import unittest
from pathlib import Path

import scripts.verify_submission as verifier

ROOT = Path(__file__).resolve().parents[1]


class SubmissionContractBoundaryTests(unittest.TestCase):
    def test_orchestrator_has_no_contract_compatibility_entry_points(self):
        for name in (
            "load_formalization_metadata",
            "normalize_repository",
            "normalized_provenance",
            "submission_request",
        ):
            self.assertNotIn(name, vars(verifier))

    def test_workflow_script_entrypoint_loads_the_extracted_contract(self):
        completed = subprocess.run(
            [
                sys.executable,
                f"{ROOT.name}/scripts/verify_submission.py",
                "--help",
            ],
            cwd=ROOT.parent,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("{prepare,execute}", completed.stdout)


if __name__ == "__main__":
    unittest.main()
