"""Exercise the workflow's real prepare -> finalize -> gate handoff (#154)."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from scripts import workflow_report

ROOT = Path(__file__).resolve().parents[1]


class PreparationReportingTests(unittest.TestCase):
    inputs = {
        "repository": "carlok/magma-1518-obstruction-lean",
        "commit": "e61f71faf5d9c6573cabe87eb402178cb91eb3c4",
        "pipeline_commit": "a" * 40,
        "request_id": "preflight001",
        "execution_profile": "palomar-standard-v1",
        "options": "{}",
    }

    def test_invalid_calls_keep_the_original_preparation_failure(self):
        path = json.dumps({"comparator_config_path": "comparator.json"})
        complete = {
            "comparator_config_path": "comparator.json",
            "authorization_relationship": "I am a responsible author or maintainer",
        }
        cases = (
            ({"request_id": "magma1518-preflight-20260927"}, "request_id", "twelve"),
            ({"request_id": "magma1518-preflight-retry-1"}, "request_id", "twelve"),
            ({"request_id": ""}, "request_id", "twelve"),
            ({"request_id": "a" * 101}, "request_id", "twelve"),
            ({"repository": "not-a-repository"}, "repository", "owner/name"),
            ({"repository": "../repo"}, "repository", "dot segments"),
            ({"repository": "owner/repo.git"}, "repository", ".git suffix"),
            ({"commit": "bad-commit"}, "commit", "40 lowercase"),
            ({"execution_attempt": "bad"}, "execution_attempt", "32 lowercase"),
            ({"mode": "Full", "options": json.dumps(complete)}, "mode", "unsupported"),
            ({}, "options.comparator_config_path", "supplied explicitly"),
            ({"options": path}, "options.authorization_relationship", "must be declared"),
            ({"options": "{"}, "options", "not valid JSON"),
            ({"options": "[]"}, "options", "JSON object"),
            ({"options": '{"unknown":"x"}'}, "options", "Unrecognized"),
            ({"options": '{"comparator_config_path":1}'}, "options.comparator_config_path", "string"),
            ({"options": json.dumps({
                "comparator_config_path": "comparator.json", "authorization_relationship": "guess",
            })}, "options.authorization_relationship", "not recognized"),
            ({"options": json.dumps({**complete, "existing_id": "bad"})}, "options.existing_id", "form"),
            ({"options": json.dumps({**complete, "authorization_evidence": "x" * 4001})},
             "options.authorization_evidence", "4,000"),
            ({"options": json.dumps({**complete, "project_path": "../outside"})},
             "options.project_path", "safe repository-relative"),
            ({"options": json.dumps({**complete, "registry_correction": "{}"})},
             "options.registry_correction", "Registry correction"),
        )
        # Both triggers materialize the same inputs context; their event
        # payloads must not replace it. Exercise both preparation modes too.
        for mode in ("full", "preflight"):
            for overrides, field, explanation in cases:
                with self.subTest(mode=mode, overrides=overrides), tempfile.TemporaryDirectory() as raw:
                    directory = Path(raw)
                    inputs = {**self.inputs, "mode": mode, **overrides}
                    event = directory / "event.json"
                    event.write_text(json.dumps({"inputs": inputs}))
                    report = directory / "report.json"
                    outputs = directory / "outputs.txt"
                    summary = directory / "summary.md"
                    steps = {"prepare": {"outcome": "success", "outputs": {"ready": "false"}}}
                    environment = {
                        **os.environ,
                        "PALOMAR_EXECUTION_PROFILE": "palomar-standard-v1",
                        "PALOMAR_JOB_STARTED_AT": str(int(time.time())),
                        "GITHUB_OUTPUT": str(outputs),
                        "GITHUB_STEP_SUMMARY": str(summary),
                        "PALOMAR_INPUTS": json.dumps(inputs),
                        "PALOMAR_STEPS": json.dumps(steps),
                        "PALOMAR_WORKFLOW_URL": "https://example.invalid/run",
                    }
                    prepared = subprocess.run([
                        sys.executable, str(ROOT / "scripts/verify_submission.py"), "prepare",
                        "--event", str(event), "--work-dir", str(directory / "work"),
                        "--output", str(report), "--licensee", "/not-invoked",
                    ], cwd=directory, env=environment, capture_output=True, text=True)
                    self.assertEqual(prepared.returncode, 0, prepared.stderr)
                    before = json.loads(report.read_text())
                    self.assertIn("ready=false", outputs.read_text())
                    self.assertFalse((directory / "work/source").exists())
                    # Finalization must also work before dependencies have
                    # installed: -S excludes PyYAML and all site packages.
                    finalized = subprocess.run([
                        sys.executable, "-S", str(ROOT / "scripts/workflow_report.py"),
                        "finalize", "--report", str(report),
                    ], cwd=directory, env=environment, capture_output=True, text=True)
                    self.assertEqual(finalized.returncode, 0, finalized.stderr)
                    after = json.loads(report.read_text())
                    self.assertEqual(after["errors"], before["errors"])
                    self.assertIn(explanation, after["errors"][0])
                    self.assertEqual(after["status"], "fail")
                    self.assertEqual(after["phase"], "preparation")
                    self.assertEqual(after["stage"], "intake")
                    diagnostic = after["diagnostics"][0]
                    self.assertEqual(diagnostic["field"], field)
                    self.assertEqual(diagnostic["owner"], "submitter")
                    self.assertFalse(diagnostic["retryable"])
                    self.assertIn("same source commit", diagnostic["next_action"])
                    self.assertIn(explanation, finalized.stdout)
                    self.assertIn("Palomar preparation failed", summary.read_text())
                    self.assertIn(explanation, summary.read_text())
                    steps["upload1"] = {"outcome": "success"}
                    environment["PALOMAR_STEPS"] = json.dumps(steps)
                    gated = subprocess.run([
                        sys.executable, "-S", str(ROOT / "scripts/workflow_report.py"),
                        "gate", "--report", str(report),
                    ], cwd=directory, env=environment, capture_output=True, text=True)
                    self.assertEqual(gated.returncode, 1, gated.stderr)
                    self.assertIn(explanation, gated.stdout)

    def test_invalid_or_mismatched_identity_cannot_preserve_a_pass(self):
        report = {
            "schema_version": 1, "status": "pass", "errors": [],
            "source": {"repository": self.inputs["repository"], "commit": self.inputs["commit"]},
            "submission": {"submission_id": self.inputs["request_id"]},
        }
        for field, value in (("request_id", "bad-id"), ("repository", "bad"), ("commit", "BAD")):
            with self.subTest(invalid_input=field):
                inputs = {**self.inputs, field: value}
                forged = json.loads(json.dumps(report))
                if field == "request_id":
                    forged["submission"]["submission_id"] = value
                else:
                    forged["source"][field] = value
                result = workflow_report.finalize(forged, inputs, {}, workflow_url="run")
                self.assertEqual(result["status"], "fail")
                self.assertFalse(workflow_report.gate(
                    result, "full", "true", {"upload1": {"outcome": "success"}},
                ))
        for change in (
            {"source": {"repository": "other/repo", "commit": self.inputs["commit"]}},
            {"source": {"repository": self.inputs["repository"], "commit": "b" * 40}},
            {"submission": {"submission_id": "othercall001"}},
            {"schema_version": []},
            {"schema_version": True},
            {"status": []},
        ):
            with self.subTest(mismatch=change):
                result = workflow_report.finalize({**report, **change}, self.inputs, {}, workflow_url="run")
                self.assertEqual(result["status"], "error")
        for schema in (1, 2):
            result = workflow_report.finalize(
                {**report, "schema_version": schema}, self.inputs, {}, workflow_url="run",
            )
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["schema_version"], schema)

    def test_attempt_validation_and_failed_finalization_cannot_pass(self):
        report = {
            "schema_version": 2, "status": "pass", "errors": [],
            "source": {"repository": self.inputs["repository"], "commit": self.inputs["commit"]},
            "submission": {"submission_id": self.inputs["request_id"]},
        }
        for attempt in ("bad", "A" * 32, 7):
            result = workflow_report.finalize(
                report, {**self.inputs, "execution_attempt": attempt}, {}, workflow_url="run",
            )
            self.assertEqual(result["status"], "fail")
            self.assertEqual(result["diagnostics"][0]["field"], "execution_attempt")
            self.assertNotIn("execution_attempt", result)
        valid = workflow_report.finalize(
            report, {**self.inputs, "execution_attempt": "b" * 32}, {}, workflow_url="run",
        )
        self.assertEqual(valid["status"], "pass")
        self.assertEqual(valid["execution_attempt"], "b" * 32)
        self.assertFalse(workflow_report.gate(valid, "full", "true", {
            "upload1": {"outcome": "success"}, "finalize": {"outcome": "failure"},
        }))
        result = workflow_report.finalize(
            None, {**self.inputs, "request_id": "bad"},
            {"dependencies": {"outcome": "failure"}}, workflow_url="run",
        )
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["diagnostics"][0]["field"], "request_id")

    def test_summary_is_bounded_and_candidate_text_stays_inert(self):
        hostile = '</pre><img src=x>\n::error::fake\n```\n' + '"<&\u2603' * 5000
        report = {
            "status": "fail", "phase": "preparation",
            "errors": [hostile] * 20,
            "diagnostics": [{"next_action": str(index) + hostile} for index in range(20)],
        }
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "summary.md"
            workflow_report.summarize_failure(report, destination)
            summary = destination.read_text()
        self.assertNotIn("<img", summary)
        self.assertNotIn("\n::error::", summary)
        self.assertEqual(summary.count("</pre>"), 1)
        self.assertEqual(summary.count("Next action:"), 10)
        self.assertLess(len(summary), 250_000)
        # Exercise the CLI log path too; every output line must remain a JSON
        # string even if a diagnostic contains workflow-command syntax.
        report.update({
            "schema_version": 1,
            "source": {"repository": self.inputs["repository"], "commit": self.inputs["commit"]},
            "submission": {"submission_id": self.inputs["request_id"]},
        })
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            destination = directory / "report.json"
            destination.write_text(json.dumps(report))
            result = subprocess.run([
                sys.executable, "-S", str(ROOT / "scripts/workflow_report.py"),
                "finalize", "--report", str(destination),
            ], cwd=directory, env={
                **os.environ, "PALOMAR_INPUTS": json.dumps(self.inputs), "PALOMAR_STEPS": "{}",
                "PALOMAR_WORKFLOW_URL": "run", "GITHUB_STEP_SUMMARY": str(directory / "summary.md"),
            }, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(result.stdout.splitlines()), 10)
            for line in result.stdout.splitlines():
                self.assertEqual(json.loads(line), hostile[:2000])


if __name__ == "__main__":
    unittest.main()
