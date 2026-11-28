"""Contract for callers of the two registry workflows.

Palomar's private confidential runs repository calls `submission.yml` and
`render-challenge.yml` through `workflow_call` at a pinned commit, so the
called jobs run in the caller's repository. These tests keep the two triggers
interchangeable, keep source names out of run titles, and stop the jobs from
assuming that `github.repository` is PalomarSubmission.
"""

import hashlib
import re
import unittest
from pathlib import Path

import yaml

from scripts import workflow_report
from scripts.verification_errors import VerificationError
from scripts.workflow_inputs import run_repository

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = {
    name: (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
    for name in ("submission.yml", "render-challenge.yml")
}
PIPELINE_REF = "${{ inputs.pipeline_commit || github.workflow_sha }}"


def parsed(name):
    return yaml.safe_load(WORKFLOWS[name])


def triggers(workflow):
    # PyYAML reads a bare `on:` key as the boolean True.
    return workflow.get("on", workflow.get(True))


def steps(workflow):
    for job in workflow["jobs"].values():
        yield from job["steps"]


class CallerParityTests(unittest.TestCase):
    def test_both_workflows_are_callable_and_dispatchable(self):
        for name in WORKFLOWS:
            with self.subTest(name):
                self.assertEqual(set(triggers(parsed(name))), {"workflow_call", "workflow_dispatch"})

    def test_call_inputs_are_the_dispatch_inputs_plus_the_pipeline_pin(self):
        for name in WORKFLOWS:
            with self.subTest(name):
                on = triggers(parsed(name))
                called = on["workflow_call"]["inputs"]
                dispatched = on["workflow_dispatch"]["inputs"]
                self.assertEqual(set(called), set(dispatched) | {"pipeline_commit"})
                self.assertNotIn("pipeline_commit", dispatched)
                self.assertIs(called["pipeline_commit"]["required"], True)
                for field, spec in dispatched.items():
                    self.assertEqual(
                        bool(called[field].get("required")), bool(spec.get("required")), field
                    )
                    self.assertEqual(called[field]["type"], "string", field)

    def test_neither_workflow_declares_secrets(self):
        # Public and confidential calls carry no credential into these jobs.
        for name in WORKFLOWS:
            with self.subTest(name):
                self.assertNotIn("secrets", triggers(parsed(name))["workflow_call"])
                self.assertNotIn("secrets.", WORKFLOWS[name])


class RunTitleTests(unittest.TestCase):
    def test_no_run_title_names_the_source(self):
        for name in WORKFLOWS:
            with self.subTest(name):
                title = parsed(name)["run-name"]
                self.assertNotIn("inputs.repository", title)
                self.assertNotIn("inputs.commit", title)
                self.assertIn("inputs.request_id", title)

    def test_render_title_is_request_id_only(self):
        self.assertEqual(
            parsed("render-challenge.yml")["run-name"], "Render challenge [${{ inputs.request_id }}]"
        )


class NoSourceNamesInRunSurfacesTests(unittest.TestCase):
    """A confidential caller's run page shows step names, artifact names and
    summaries. None of them may be built from the source's identity."""

    IDENTIFYING = re.compile(
        r"inputs\.(repository|commit|challenge_sha256|[a-z_]*_path|options)\b"
    )

    def test_step_and_job_names_are_static(self):
        for name in WORKFLOWS:
            with self.subTest(name):
                workflow = parsed(name)
                for job in workflow["jobs"].values():
                    self.assertNotIn("${{", str(job.get("name", "")))
                for step in steps(workflow):
                    self.assertNotIn("${{", str(step.get("name", "")))

    def test_artifact_names_carry_only_the_request(self):
        for name in WORKFLOWS:
            with self.subTest(name):
                uploads = [
                    step for step in steps(parsed(name))
                    if str(step.get("uses", "")).startswith("actions/upload-artifact@")
                ]
                self.assertTrue(uploads)
                for step in uploads:
                    self.assertIn("inputs.request_id", step["with"]["name"])
                    self.assertNotRegex(step["with"]["name"], self.IDENTIFYING)

    def test_render_writes_no_step_summary(self):
        from scripts import render_challenge

        self.assertNotIn("GITHUB_STEP_SUMMARY", WORKFLOWS["render-challenge.yml"])
        self.assertNotIn("GITHUB_STEP_SUMMARY", Path(render_challenge.__file__).read_text(encoding="utf-8"))


TEMPLATES = ROOT / "templates/confidential-callers"
PLACEHOLDER = "<pipeline-commit>"
# Template name, the PalomarSubmission workflow it calls, and its one job.
CALLERS = {"verify.yml": ("submission.yml", "verify"), "render.yml": ("render-challenge.yml", "render")}


def caller(name, commit="c" * 40):
    return yaml.safe_load((TEMPLATES / name).read_text(encoding="utf-8").replace(PLACEHOLDER, commit))


class CanonicalCallerTemplateTests(unittest.TestCase):
    """PalomarConfidentialRuns copies these templates with one commit P
    substituted. The broker and the reviewer hash the copy back against
    SHA256SUMS at P, so the templates are part of this repository's contract."""

    def test_sha256sums_lists_exactly_the_templates(self):
        recorded = {}
        for line in (TEMPLATES / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
            digest, file_name = line.split("  ", 1)
            recorded[file_name] = digest
        self.assertEqual(set(recorded), set(CALLERS))
        self.assertEqual({path.name for path in TEMPLATES.iterdir()}, {*CALLERS, "SHA256SUMS"})
        for file_name, digest in recorded.items():
            with self.subTest(file_name):
                self.assertEqual(hashlib.sha256((TEMPLATES / file_name).read_bytes()).hexdigest(), digest)

    def test_one_commit_pins_both_the_workflow_and_its_scripts(self):
        # A uses pin fixes only the called YAML; the called jobs run the
        # scripts at pipeline_commit. The template makes them one literal.
        for name, (called, job) in CALLERS.items():
            with self.subTest(name):
                text = (TEMPLATES / name).read_text(encoding="utf-8")
                self.assertEqual(text.count(PLACEHOLDER), 2)
                call = caller(name)["jobs"][job]
                self.assertEqual(
                    call["uses"], f"PalomarRegistry/PalomarSubmission/.github/workflows/{called}@{'c' * 40}"
                )
                self.assertEqual(call["with"]["pipeline_commit"], "c" * 40)

    def test_callers_declare_the_called_dispatch_inputs_and_the_ticket(self):
        for name, (called, _) in CALLERS.items():
            with self.subTest(name):
                declared = triggers(caller(name))["workflow_dispatch"]["inputs"]
                dispatched = triggers(parsed(called))["workflow_dispatch"]["inputs"]
                self.assertEqual(set(declared), set(dispatched) | {"ticket"})
                for field, spec in dispatched.items():
                    self.assertEqual(bool(declared[field].get("required")), bool(spec.get("required")), field)
                    self.assertEqual(declared[field].get("default"), spec.get("default"), field)
                self.assertIs(declared["ticket"].get("required"), False)

    def test_callers_forward_every_call_input_unchanged(self):
        for name, (called, job) in CALLERS.items():
            with self.subTest(name):
                forwarded = dict(caller(name)["jobs"][job]["with"])
                forwarded.pop("pipeline_commit")
                accepted = set(triggers(parsed(called))["workflow_call"]["inputs"]) - {"pipeline_commit"}
                self.assertEqual(forwarded, {field: f"${{{{ inputs.{field} }}}}" for field in accepted})

    def test_callers_hold_no_credential_and_only_read(self):
        for name, (_, job) in CALLERS.items():
            with self.subTest(name):
                text = (TEMPLATES / name).read_text(encoding="utf-8")
                self.assertNotIn("secrets", text)
                self.assertNotIn("id-token", text)
                workflow = caller(name)
                self.assertEqual(list(workflow["jobs"]), [job])
                self.assertEqual(workflow["permissions"], {"contents": "read"})
                self.assertEqual(workflow["jobs"][job]["permissions"], {"contents": "read"})

    def test_caller_run_titles(self):
        self.assertEqual(
            caller("verify.yml")["run-name"],
            "Verify ${{ inputs.request_id }}${{ inputs.execution_attempt "
            "&& format(' [{0}]', inputs.execution_attempt) || '' }}",
        )
        self.assertEqual(caller("render.yml")["run-name"], "Render challenge [${{ inputs.request_id }}]")


class RunRepositoryAssumptionTests(unittest.TestCase):
    def test_every_checkout_names_the_pipeline_at_the_pinned_commit(self):
        # Without an explicit repository, actions/checkout reads the caller.
        # In a reusable call `github.workflow_sha` and `github.sha` are the
        # caller's commit, so only the explicit pin names the pipeline.
        for name in WORKFLOWS:
            with self.subTest(name):
                checkouts = [
                    step for step in steps(parsed(name))
                    if str(step.get("uses", "")).startswith("actions/checkout@")
                ]
                self.assertTrue(checkouts)
                for step in checkouts:
                    self.assertEqual(step["with"]["repository"], "PalomarRegistry/PalomarSubmission")
                    self.assertEqual(step["with"]["ref"], PIPELINE_REF)
                    self.assertIs(step["with"]["persist-credentials"], False)

    def test_no_job_reads_the_caller_commit_as_the_pipeline(self):
        for name in WORKFLOWS:
            with self.subTest(name):
                self.assertNotRegex(WORKFLOWS[name], r"github\.sha\b")
                self.assertNotRegex(WORKFLOWS[name], r"ref: \$\{\{ github\.workflow_sha \}\}")

    def test_render_records_the_pinned_renderer_and_the_run_repository(self):
        step = next(
            step for step in steps(parsed("render-challenge.yml"))
            if "scripts.render_challenge execute" in step.get("run", "")
        )
        self.assertEqual(step["env"]["RENDERER_COMMIT"], PIPELINE_REF)
        self.assertEqual(step["env"]["RUN_REPOSITORY"], "${{ github.repository }}")
        self.assertIn('--renderer-commit "$RENDERER_COMMIT"', step["run"])
        self.assertIn('--run-repository "$RUN_REPOSITORY"', step["run"])

    def test_render_validates_the_pin_before_using_it(self):
        render = parsed("render-challenge.yml")["jobs"]["render"]
        names = [step.get("name") for step in render["steps"]]
        self.assertLess(
            names.index("Validate pinned pipeline revision"), names.index("Checkout trusted renderer")
        )
        self.assertEqual(render["permissions"], {"contents": "read"})

    def test_mechanical_finalizer_records_the_run_repository(self):
        step = next(
            step for step in steps(parsed("submission.yml"))
            if "workflow_report.py finalize" in step.get("run", "")
        )
        self.assertEqual(step["env"]["PALOMAR_RUN_REPOSITORY"], "${{ github.repository }}")
        self.assertEqual(step["env"]["PALOMAR_PIPELINE_COMMIT"], PIPELINE_REF)
        self.assertEqual(
            step["env"]["PALOMAR_WORKFLOW_URL"],
            "${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}",
        )

    def test_ci_calls_render_through_workflow_call(self):
        ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
        job = ci["jobs"]["reusable_render"]
        self.assertEqual(job["uses"], "./.github/workflows/render-challenge.yml")
        self.assertEqual(job["with"]["pipeline_commit"], "${{ github.sha }}")


class RunRepositoryReportFieldTests(unittest.TestCase):
    INPUTS = {"repository": "owner/repo", "commit": "a" * 40, "request_id": "abcdef123456"}

    def test_finalized_reports_name_the_run_repository(self):
        for report in (None, {}, {"status": "pass"}):
            with self.subTest(report=report):
                result = workflow_report.finalize(
                    report, self.INPUTS, {},
                    workflow_url="https://github.com/PalomarRegistry/PalomarConfidentialRuns/actions/runs/7",
                    run_repository="PalomarRegistry/PalomarConfidentialRuns",
                    pipeline_commit="c" * 40,
                )
                self.assertEqual(result["run_repository"], "PalomarRegistry/PalomarConfidentialRuns")
                self.assertEqual(result["pipeline_commit"], "c" * 40)

    def test_run_repository_must_match_the_workflow_url(self):
        url = "https://github.com/PalomarRegistry/PalomarSubmission/actions/runs/12"
        self.assertEqual(
            run_repository("PalomarRegistry/PalomarSubmission", url), "PalomarRegistry/PalomarSubmission"
        )
        for repository, workflow_url in (
            ("PalomarRegistry/PalomarConfidentialRuns", url),
            ("PalomarRegistry", url),
            ("PalomarRegistry/PalomarSubmission", url + "/attempts/1"),
            ("PalomarRegistry/PalomarSubmission", "https://example.invalid/run"),
            ("Palomar.Registry/x", "https://github.com/PalomarXRegistry/x/actions/runs/1"),
        ):
            with self.subTest(repository=repository, workflow_url=workflow_url):
                with self.assertRaises(VerificationError):
                    run_repository(repository, workflow_url)

    def test_render_execute_requires_the_run_repository(self):
        from scripts import render_challenge

        source = Path(render_challenge.__file__).read_text(encoding="utf-8")
        self.assertRegex(source, re.escape('"--run-repository", required=True'))
        self.assertIn('"run_repository": args.run_repository', source)


if __name__ == "__main__":
    unittest.main()
