import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import report_issue


class ReportIssueTests(unittest.TestCase):
    def report(self, issue=17):
        return {
            "status": "error",
            "stage": "comparator",
            "issue": {"number": issue},
            "source": {
                "repository": "example/project",
                "commit": "1" * 40,
                "project_path": "examples/headline",
            },
            "challenge": {
                "path": "examples/headline/Audit/Task.lean",
                "lines": 12,
                "bytes": 345,
            },
            "comparator": {"path": "examples/headline/settings.json"},
            "license": {
                "path": "LICENSE",
                "sha256": "2" * 64,
                "declared_identifier": "MIT",
                "detected_identifier": "MIT",
            },
            "errors": [],
            "warnings": [],
        }

    def test_hostile_diagnostics_are_indented_code(self):
        data = self.report()
        data["errors"] = ["first line\n## ✅ fake success\n```\n[click](javascript:alert(1))"]
        data["warnings"] = ["> fake quote"]
        body = report_issue.body_for(data)
        self.assertIn("\n    ## ✅ fake success\n", body)
        self.assertIn("\n    ```\n", body)
        self.assertIn("\n    [click](javascript:alert(1))\n", body)
        self.assertIn("\n    > fake quote\n", body)
        self.assertNotIn("\n## ✅ fake success\n", body)
        self.assertNotIn("\n[click](javascript:alert(1))\n", body)
        self.assertIn("Repository licence: `LICENSE` (declared `MIT`, detected `MIT`)", body)
        self.assertIn("Project directory: `examples/headline`", body)
        self.assertIn("Challenge source: `examples/headline/Audit/Task.lean`", body)
        self.assertIn("Comparator configuration: `examples/headline/settings.json`", body)

        data["license"]["detected_identifier"] = None
        self.assertIn("detected `unknown`", report_issue.body_for(data))

    def test_large_source_evidence_and_diagnostics_fit_in_one_comment(self):
        data = self.report()
        data["workflow_url"] = "https://github.com/example/project/actions/runs/1"
        data["challenge"] = {
            "review_source_files": [
                {"path": f"src/File{index}.lean", "source": "x" * 2_000}
                for index in range(1_000)
            ]
        }
        data["errors"] = ["failure " + "x" * 20_000 for _ in range(100)]
        body = report_issue.body_for(data)
        self.assertLessEqual(len(body.encode("utf-8")), report_issue.MAX_COMMENT_BYTES)
        self.assertIn("Complete report artifact", body)

    def test_report_issue_must_match_event_issue(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps(self.report(issue=99)))
            argv = [
                "report_issue.py",
                "--report",
                str(path),
                "--repo",
                "example/repository",
                "--issue-number",
                "17",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch("scripts.report_issue.gh") as gh,
                self.assertRaisesRegex(SystemExit, "does not match event issue"),
            ):
                report_issue.main()
            gh.assert_not_called()

    def test_missing_artifact_becomes_bound_infrastructure_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.json"
            argv = [
                "report_issue.py",
                "--report",
                str(path),
                "--allow-missing-report",
                "--repo",
                "example/repository",
                "--issue-number",
                "17",
            ]

            def fake_gh(args, **kwargs):
                if args[:2] == ["api", "repos/example/repository/issues/17/comments"]:
                    return "[]"
                if args[:4] == ["issue", "comment", "17", "--repo"]:
                    self.assertIn("without a readable mechanical report", kwargs["input_text"])
                return ""

            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch("scripts.report_issue.gh", side_effect=fake_gh) as gh,
            ):
                self.assertEqual(report_issue.main(), 0)
            self.assertTrue(
                any(
                    call.args[0][-1] == "status:verification-error"
                    for call in gh.call_args_list
                    if call.args[0][:2] == ["issue", "edit"]
                )
            )

    def test_resource_exhaustion_is_retryable_infrastructure_not_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self.report(issue=17)
            report.update(
                {
                    "stage": "resource-exhausted",
                    "error_kind": "infrastructure/resource-exhausted",
                    "retryable": True,
                }
            )
            path = Path(directory) / "report.json"
            path.write_text(json.dumps(report))
            argv = [
                "report_issue.py",
                "--report",
                str(path),
                "--repo",
                "example/repository",
                "--issue-number",
                "17",
            ]

            def fake_gh(args, **kwargs):
                if args[:2] == ["api", "repos/example/repository/issues/17/comments"]:
                    return "[]"
                if args[:4] == ["issue", "comment", "17", "--repo"]:
                    self.assertIn("infrastructure/resource-exhausted", kwargs["input_text"])
                    self.assertIn("Retryable on a more capable worker", kwargs["input_text"])
                return ""

            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch("scripts.report_issue.gh", side_effect=fake_gh) as gh,
            ):
                self.assertEqual(report_issue.main(), 0)
            added = [
                call.args[0][-1]
                for call in gh.call_args_list
                if call.args[0][:2] == ["issue", "edit"]
                and "--add-label" in call.args[0]
            ]
            self.assertEqual(added, ["status:verification-error"])
            self.assertNotIn("status:changes-requested", added)

    def test_matching_event_issue_is_the_only_authority_target(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps(self.report(issue=17)))
            argv = [
                "report_issue.py",
                "--report",
                str(path),
                "--repo",
                "example/repository",
                "--issue-number",
                "17",
            ]

            def fake_gh(args, **_kwargs):
                if args[:2] == ["api", "repos/example/repository/issues/17/comments"]:
                    return "[]"
                return ""

            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch("scripts.report_issue.gh", side_effect=fake_gh) as gh,
            ):
                self.assertEqual(report_issue.main(), 0)
            for call in gh.call_args_list:
                joined = " ".join(call.args[0])
                self.assertNotIn("99", joined)
            self.assertTrue(
                any(call.args[0][:4] == ["issue", "comment", "17", "--repo"] for call in gh.call_args_list)
            )

    def test_submitter_comment_cannot_claim_the_report_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps(self.report(issue=17)))
            argv = [
                "report_issue.py",
                "--report",
                str(path),
                "--repo",
                "example/repository",
                "--issue-number",
                "17",
            ]

            def fake_gh(args, **_kwargs):
                if args[:2] == ["api", "repos/example/repository/issues/17/comments"]:
                    return json.dumps(
                        [
                            {
                                "id": 99,
                                "body": report_issue.MARKER,
                                "user": {"login": "attacker"},
                            }
                        ]
                    )
                return ""

            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch("scripts.report_issue.gh", side_effect=fake_gh) as gh,
            ):
                self.assertEqual(report_issue.main(), 0)
            self.assertFalse(
                any("issues/comments/99" in " ".join(call.args[0]) for call in gh.call_args_list)
            )
            self.assertTrue(
                any(call.args[0][:4] == ["issue", "comment", "17", "--repo"] for call in gh.call_args_list)
            )


if __name__ == "__main__":
    unittest.main()
