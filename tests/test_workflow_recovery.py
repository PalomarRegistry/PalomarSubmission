import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import trusted_download, workflow_report
from scripts.verify_submission import VerificationError, check_mathlib_toolchain


class WorkflowRecoveryTests(unittest.TestCase):
    inputs = {"repository": "owner/repo", "commit": "a" * 40, "request_id": "abcdefghijkl"}

    def report(self, status="pending"):
        return {
            "schema_version": 1,
            "source": {"repository": "owner/repo", "commit": "a" * 40},
            "submission": {"submission_id": "abcdefghijkl"},
            "status": status,
            "errors": [],
        }

    def test_setup_failure_finalizes_prepared_report(self):
        result = workflow_report.finalize(
            self.report(), self.inputs, {"elan": {"outcome": "failure"}}, workflow_url="run"
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["stage"], "elan")
        self.assertEqual(result["diagnostics"][0]["owner"], "palomar")

    def test_invalid_binding_and_shapes_cannot_preserve_pass(self):
        for report in (
            None,
            [],
            {"source": 1, "schema_version": 1},
            dict(self.report("pass"), source={"commit": "b" * 40}),
        ):
            result = workflow_report.finalize(report, self.inputs, {}, workflow_url="run")
            self.assertEqual(result["status"], "error")

    def test_terminal_failure_preserved(self):
        report = self.report("fail")
        report["errors"] = ["user error"]
        result = workflow_report.finalize(report, self.inputs, {}, workflow_url="run")
        self.assertEqual(result["errors"], ["user error"])

    def test_upload_retry_success_and_step_failure_gate(self):
        self.assertTrue(
            workflow_report.gate(self.report("pass"), "full", "true", {"upload2": {"outcome": "success"}})
        )
        self.assertFalse(workflow_report.gate(self.report("pass"), "full", "true", {}))
        self.assertFalse(
            workflow_report.gate(
                self.report("pass"),
                "full",
                "true",
                {"upload2": {"outcome": "success"}, "elan": {"outcome": "failure"}},
            )
        )

    def test_checksum_failure_is_not_retried(self):
        from subprocess import CompletedProcess

        with tempfile.TemporaryDirectory() as raw:

            def curl(args, **kwargs):
                Path(args[args.index("--output") + 1]).write_bytes(b"wrong bytes")
                return CompletedProcess(args, 0, "200", "")

            with patch.object(trusted_download.subprocess, "run", side_effect=curl) as run:
                with self.assertRaisesRegex(ValueError, "checksum"):
                    trusted_download.download("https://example.com/pin", Path(raw) / "out", "a" * 64)
                self.assertEqual(run.call_count, 1)

    def test_transient_connection_error_retries_but_certificate_error_does_not(self):
        from subprocess import CompletedProcess

        for code, expected in ((35, 3), (60, 1)):
            with (
                tempfile.TemporaryDirectory() as raw,
                patch.object(
                    trusted_download.subprocess, "run", return_value=CompletedProcess([], code, "000", "")
                ) as run,
                patch.object(trusted_download.time, "sleep"),
            ):
                with self.assertRaises(RuntimeError):
                    trusted_download.download("https://example.com/pin", Path(raw) / "out", "a" * 64)
                self.assertEqual(run.call_count, expected)

    def test_expired_budget_performs_no_network_operation(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(trusted_download.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "budget"):
                trusted_download.download("https://example.com/pin", Path(raw) / "out", "a" * 64, budget=0)
            run.assert_not_called()
        self.assertEqual(trusted_download.retry_delay(0, "Retry-After: 1000\r\n"), 60)

    def test_git_retries_network_failure_but_not_missing_revision(self):
        from subprocess import CompletedProcess

        from scripts import trusted_git_fetch
        for detail, expected in (("Connection reset", 5), ("not our ref", 3)):
            with tempfile.TemporaryDirectory() as raw, patch.object(
                trusted_git_fetch.subprocess, "run",
                return_value=CompletedProcess([], 128, "", detail),
            ) as run, patch.object(trusted_git_fetch.time, "sleep"):
                with self.assertRaises(RuntimeError):
                    trusted_git_fetch.fetch("leanprover/comparator", "a" * 40, Path(raw) / "source")
                self.assertEqual(run.call_count, expected)  # init, remote setup, and bounded fetch attempts

    def test_mathlib_exact_toolchain_with_alias_package_name(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "lean-toolchain").write_text("leanprover/lean4:v4.32.0\n")
            package = {
                "name": "my_mathlib",
                "repository": "leanprover-community/mathlib4",
                "revision": "a" * 40,
            }
            with patch("scripts.verify_submission.package_checkout", return_value=root):
                with self.assertRaisesRegex(VerificationError, "Align the project") as raised:
                    check_mathlib_toolchain(
                        root,
                        [package],
                        checkout=root,
                        project_toolchain="leanprover/lean4:v4.33.1",
                        project_toolchain_path="sub/lean-toolchain",
                    )
                self.assertEqual(raised.exception.code, "toolchain.mathlib_mismatch")
                self.assertEqual(raised.exception.owner, "submitter")
                self.assertFalse(raised.exception.retryable)
                self.assertEqual(
                    len(
                        check_mathlib_toolchain(
                            root,
                            [package],
                            checkout=root,
                            project_toolchain="leanprover/lean4:v4.32.0",
                            project_toolchain_path="lean-toolchain",
                        )
                    ),
                    1,
                )
        self.assertEqual(
            check_mathlib_toolchain(
                Path("."),
                [],
                checkout=Path("."),
                project_toolchain="leanprover/lean4:v4.32.0",
                project_toolchain_path="lean-toolchain",
            ),
            [],
        )
