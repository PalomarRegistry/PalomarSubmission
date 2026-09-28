import json
import os
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from scripts import source_requirements as sources
from scripts import verify_submission as verifier
from scripts.verification_errors import VerificationError

ROOT = Path(__file__).resolve().parents[1]


class SourceRequirementsTests(unittest.TestCase):
    def test_shared_header_and_boundary_cases(self):
        fixture = json.loads((ROOT / "tests/fixtures/lean-source-requirements.json").read_text())
        for case in fixture["cases"]:
            with self.subTest(case=case["id"]):
                self.assertEqual(
                    [issue.code for issue in sources.source_issues(
                        case["text"], case.get("path", "Source.lean")
                    )],
                    case["expected_codes"],
                )

    def test_all_sources_including_unused_nested_files_and_exclusions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ["Good.lean", "nested/Unused.lean", "deps/local/Large.lean",
                         "lakefile.lean", "nested/lakefile.lean", ".lake/Bad.lean", ".git/Bad.lean"]:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("module\n" if name == "Good.lean" else "-- legacy\n")
            (root / "deps/local/Large.lean").write_text("module\n" + "--\n" * 10000)
            (root / "Link.lean").symlink_to(root / "nested/Unused.lean")
            (root / "linkdir").symlink_to(root / "nested", target_is_directory=True)
            evidence, issues = sources.inspect_lean_sources(root)
            self.assertEqual(evidence["files_checked"], 6)
            self.assertEqual({(issue.path, issue.code) for issue in issues}, {
                ("nested/Unused.lean", "source.module_required"),
                ("Link.lean", "source.symlink_not_allowed"),
                ("deps/local/Large.lean", "source.file_too_long"),
            })
            long = next(issue for issue in issues if issue.code == "source.file_too_long")
            self.assertEqual(long.line, 10001)
            self.assertEqual(long.owner, "submitter")
            self.assertFalse(long.retryable)

    def test_invalid_utf8_is_a_file_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Bad.lean").write_bytes(b"module\n\xff")
            _, issues = sources.inspect_lean_sources(root)
            self.assertEqual([(issue.path, issue.code) for issue in issues],
                             [("Bad.lean", "source.invalid_utf8")])

    def test_separately_pinned_sources_are_scanned_and_corrections_skip_new_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def clone(_url, _commit, destination):
                destination.mkdir()
                (destination / "Remote.lean").write_text("import Init\n")
            source = {"repository_url": "https://github.com/owner/substantive", "commit": "b" * 40}
            with mock.patch.object(verifier, "clone_commit", side_effect=clone), \
                 mock.patch.object(verifier, "validate_preservable_git_checkout"):
                self.assertIsNone(verifier.validate_preservable_remote_source(root, source, "substantive"))
                with self.assertRaises(VerificationError) as caught:
                    verifier.validate_preservable_remote_source(root, source, "substantive",
                                                               check_lean_sources=True)
                issue = caught.exception.issues[0]
                self.assertEqual(issue.code, "source.module_required")
                self.assertIn("owner/substantive", str(issue))
                self.assertIn("Remote.lean", str(issue))
                # Do not link this file into the wrapper repository by mistake.
                self.assertIsNone(issue.path)

    def test_execution_rejects_legacy_prepared_source_before_candidate_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            checkout = work / "source"
            (checkout / ".git").mkdir(parents=True)
            (checkout / "Unused.lean").write_text("import Init\n")
            output = work / "report.json"
            output.write_text(json.dumps({"status": "pending", "source": {}, "errors": []}))
            args = Namespace(output=str(output), work_dir=str(work),
                             bwrap_source_tag="v0.11.0", workflow_url="https://example.test/run")
            with mock.patch.object(verifier, "configure_bwrap") as setup:
                self.assertEqual(verifier.execute(args), 0)
                setup.assert_not_called()
            report = json.loads(output.read_text())
            self.assertEqual(report["status"], "fail")
            self.assertEqual(report["stage"], "source-requirements")
            self.assertEqual(report["diagnostics"][0]["code"], "source.module_required")
            self.assertEqual(report["source_requirements"]["files_checked"], 1)

    def test_compiler_headers_cover_unused_files_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Unused.lean").write_text("module\n")
            arguments = dict(source=root, lean=Path("/lean"), environment={},
                             writable_directories=[], readable_paths=[], executable_paths=[], tools={})
            for payload, code in [
                ({"imports": []}, "palomar.source_header_unavailable"),
                ({"imports": [{"result": {"isModule": False, "imports": []}}]},
                 "source.module_required"),
                ({"imports": [{"errors": ["bad header"]}]}, "source.invalid_header"),
            ]:
                with mock.patch.object(verifier, "sandboxed_run", return_value=
                                       subprocess.CompletedProcess([], 0, json.dumps(payload), "")) as run:
                    with self.assertRaises(VerificationError) as caught:
                        verifier.confirm_source_modules(root, **arguments)
                    self.assertEqual(caught.exception.code, code)
                    self.assertIn(str(root / "Unused.lean"), run.call_args.args[0])

    @unittest.skipUnless(os.environ.get("PALOMAR_TEST_LEAN"), "requires Lean header parser")
    def test_real_compiler_batch_accepts_valid_module_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in [("A.lean", "module\npublic theorem t : True := trivial\n"),
                               ("B.lean", "/-/- x -/\nmodule\n"),
                               ("D.lean", "module#check Nat\n"),
                               ("E.lean", "module@[simp] public theorem t : True := trivial\n")]:
                (root / name).write_text(text)
            # Only header parsing runs; no candidate imports or elaboration.
            with mock.patch.object(verifier, "sandboxed_run", side_effect=
                                   lambda command, **_: subprocess.run(command, capture_output=True,
                                                                        text=True, check=True)):
                verifier.confirm_source_modules(root, source=root,
                    lean=Path(os.environ["PALOMAR_TEST_LEAN"]), environment={},
                    writable_directories=[], readable_paths=[], executable_paths=[], tools={})
                (root / "C.lean").write_text("/-/- x -/ theorem t : True := trivial -/\nmodule\n")
                with self.assertRaises(VerificationError) as caught:
                    verifier.confirm_source_modules(root, source=root,
                        lean=Path(os.environ["PALOMAR_TEST_LEAN"]), environment={},
                        writable_directories=[], readable_paths=[], executable_paths=[], tools={})
                self.assertEqual(caught.exception.code, "source.module_required")
                self.assertEqual(caught.exception.path, "C.lean")
