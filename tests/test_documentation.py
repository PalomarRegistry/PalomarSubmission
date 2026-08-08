import unittest
from pathlib import Path

import yaml

from scripts.verify_submission import OPTIONAL_FIELDS

ROOT = Path(__file__).resolve().parent.parent
SECURITY = (ROOT / "SECURITY.md").read_text()
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/submission.yml").read_text())


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
            sorted(inputs), ["commit", "options", "repository", "request_id"]
        )
        self.assertEqual(
            sorted(OPTIONAL_FIELDS),
            [
                "authorization_evidence",
                "authorization_relationship",
                "comparator_config_path",
                "context",
                "existing_id",
                "formalization_metadata_path",
                "project_path",
            ],
        )
        self.assertIn("fixed `OPTIONAL_FIELDS`", SECURITY)
        self.assertIn("There is no\ninput for the submitter's GitHub identity", SECURITY)

    def test_the_only_output_is_the_bounded_report_artifact(self):
        uploads = [
            step
            for step in WORKFLOW["jobs"]["verify"]["steps"]
            if str(step.get("uses", "")).startswith("actions/upload-artifact@")
        ]
        self.assertEqual(len(uploads), 1)
        self.assertEqual(
            uploads[0]["with"]["name"], "mechanical-report-${{ inputs.request_id }}"
        )
        self.assertIn("uploads exactly one artifact,\n`mechanical-report-<request_id>`", SECURITY)


if __name__ == "__main__":
    unittest.main()
