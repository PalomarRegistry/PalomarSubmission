import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts import claim_submission


class ClaimSubmissionTests(unittest.TestCase):
    def issue(self, *labels: str, state: str = "open") -> dict:
        return {
            "number": 17,
            "state": state,
            "user": {"login": "author"},
            "labels": [{"name": label} for label in labels],
        }

    def comment_event(self, *, commenter: str = "author", body: str = "/reverify") -> dict:
        return {
            "action": "created",
            "issue": {"number": 17, "user": {"login": "author"}},
            "comment": {"body": body, "user": {"login": commenter}},
        }

    def test_exact_author_command_can_retry_current_retryable_states(self) -> None:
        event = self.comment_event()
        for label in claim_submission.RETRY_LABELS:
            with self.subTest(label=label):
                self.assertTrue(
                    claim_submission.eligible("issue_comment", event, self.issue("submission", label))
                )

    def test_live_state_prevents_stale_or_duplicate_claims(self) -> None:
        event = self.comment_event()
        for label in claim_submission.BLOCKING_LABELS:
            with self.subTest(label=label):
                self.assertFalse(
                    claim_submission.eligible(
                        "issue_comment",
                        event,
                        self.issue("submission", "status:verification-error", label),
                    )
                )

    def test_comment_claim_rejects_untrusted_or_ineligible_events(self) -> None:
        cases = [
            (self.comment_event(commenter="outsider"), self.issue("submission", "status:verification-error")),
            (self.comment_event(body="/reverify now"), self.issue("submission", "status:verification-error")),
            (self.comment_event(), self.issue("submission")),
            (self.comment_event(), self.issue("status:verification-error")),
            (self.comment_event(), self.issue("submission", "status:verification-error", state="closed")),
        ]
        for event, issue in cases:
            with self.subTest(event=event, issue=issue):
                self.assertFalse(claim_submission.eligible("issue_comment", event, issue))

        pull_request_event = self.comment_event()
        pull_request_event["issue"]["pull_request"] = {"url": "https://example.invalid"}
        self.assertFalse(
            claim_submission.eligible(
                "issue_comment",
                pull_request_event,
                self.issue("submission", "status:verification-error"),
            )
        )

    def test_submission_label_event_can_claim_only_current_open_submission(self) -> None:
        event = {
            "action": "labeled",
            "issue": {"number": 17, "user": {"login": "author"}},
            "label": {"name": "submission"},
        }
        self.assertTrue(claim_submission.eligible("issues", event, self.issue("submission")))
        self.assertFalse(
            claim_submission.eligible("issues", event, self.issue("submission", "status:accepted"))
        )

    def test_main_binds_live_query_to_event_issue(self) -> None:
        event = self.comment_event()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            path.write_text(json.dumps(event))
            argv = [
                "claim_submission.py",
                "--event",
                str(path),
                "--event-name",
                "issue_comment",
                "--issue-number",
                "17",
                "--repo",
                "example/repository",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch(
                    "scripts.claim_submission.gh",
                    return_value=json.dumps(self.issue("submission", "status:verification-error")),
                ) as gh,
                redirect_stdout(StringIO()) as stdout,
            ):
                self.assertEqual(claim_submission.main(), 0)
            self.assertEqual(stdout.getvalue(), "true\n")
            gh.assert_called_once_with(["api", "repos/example/repository/issues/17"])

    def test_main_rejects_mismatched_issue_before_api_access(self) -> None:
        event = self.comment_event()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            path.write_text(json.dumps(event))
            argv = [
                "claim_submission.py",
                "--event",
                str(path),
                "--event-name",
                "issue_comment",
                "--issue-number",
                "18",
                "--repo",
                "example/repository",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch("scripts.claim_submission.gh") as gh,
                self.assertRaisesRegex(SystemExit, "does not match requested issue"),
            ):
                claim_submission.main()
            gh.assert_not_called()

    def test_main_surfaces_live_api_failure_instead_of_denying_claim(self) -> None:
        event = self.comment_event()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            path.write_text(json.dumps(event))
            argv = [
                "claim_submission.py",
                "--event",
                str(path),
                "--event-name",
                "issue_comment",
                "--issue-number",
                "17",
                "--repo",
                "example/repository",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch(
                    "scripts.claim_submission.gh",
                    side_effect=SystemExit("temporary API failure"),
                ),
                self.assertRaisesRegex(SystemExit, "temporary API failure"),
            ):
                claim_submission.main()


if __name__ == "__main__":
    unittest.main()
