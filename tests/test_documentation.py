import json
import unittest
from pathlib import Path

import yaml

from scripts.submission_contract import OPTIONAL_FIELDS, REPAIRABLE_FORMALIZATION_FIELDS

ROOT = Path(__file__).resolve().parent.parent
SECURITY = (ROOT / "SECURITY.md").read_text()
WORKFLOW_TEXT = (ROOT / ".github/workflows/submission.yml").read_text()
WORKFLOW = yaml.safe_load(WORKFLOW_TEXT)


def triggers(workflow):
    # PyYAML reads a bare `on:` key as the boolean True, so ask for both.
    return workflow.get("on", workflow.get(True))


class SecurityPolicyMatchesTheWorkflowTests(unittest.TestCase):
    """SECURITY.md invites a reader to check its description of the pipeline
    against the workflow file. That invitation is only worth making if the two
    are kept in agreement, and prose does not fail on its own."""

    def test_the_workflow_is_dispatch_only(self):
        self.assertEqual(list(triggers(WORKFLOW)), ["workflow_dispatch"])
        self.assertIn("single trigger, `workflow_dispatch`", SECURITY)

    def test_there_is_one_job_and_it_only_reads(self):
        self.assertEqual(list(WORKFLOW["jobs"]), ["verify"])
        self.assertEqual(WORKFLOW["jobs"]["verify"]["permissions"], {"contents": "read"})
        self.assertIn("There is one job, `verify`", SECURITY)
        self.assertIn("`contents: read`", SECURITY)

    def test_the_dispatch_carries_no_identity_and_no_review(self):
        # The outer inputs are only half of it: `options` is a JSON object, and
        # what may be in it is the allowlist the verifier enforces. A key added
        # there is a new thing the server can send, so the claim that none of
        # them is an identity or a review has to be rechecked when it changes.
        inputs = triggers(WORKFLOW)["workflow_dispatch"]["inputs"]
        self.assertEqual(
            sorted(inputs), ["commit", "mode", "options", "repository", "request_id"]
        )
        self.assertEqual(
            sorted(OPTIONAL_FIELDS),
            [
                "authorization_evidence",
                "authorization_relationship",
                "comparator_config_path",
                "existing_id",
                "formalization_metadata_path",
                "project_path",
            ],
        )
        self.assertIn("fixed `OPTIONAL_FIELDS`", SECURITY)
        self.assertIn("There is no\ninput for the submitter's GitHub identity", SECURITY)

    def test_the_documented_wall_clock_allowance_is_the_one_dispatched(self):
        # The verifier's own default is larger than what this workflow asks
        # for, so the number a reader can check is the one on the command line.
        self.assertIn("--execution-budget-seconds 19800", WORKFLOW_TEXT)
        self.assertIn("19,800 seconds", SECURITY)

    def test_the_only_output_is_the_bounded_report_artifact(self):
        uploads = [
            step
            for step in WORKFLOW["jobs"]["verify"]["steps"]
            if str(step.get("uses", "")).startswith("actions/upload-artifact@")
        ]
        self.assertEqual(len(uploads), 1)
        artifact_name = uploads[0]["with"]["name"]
        self.assertIn("preflight-report", artifact_name)
        self.assertIn("mechanical-report", artifact_name)
        self.assertIn("inputs.request_id", artifact_name)
        self.assertIn("uploads exactly one mode-specific artifact", SECURITY)

    def test_the_formalization_profile_names_the_canonical_repair_allowlist(self):
        profile = json.loads((ROOT / "formalization-profile.json").read_text())
        repairable = {
            field
            for field, presentation in profile["fields"].items()
            if presentation.get("repairable") is True
        }
        self.assertEqual(repairable, set(REPAIRABLE_FORMALIZATION_FIELDS))


if __name__ == "__main__":
    unittest.main()
