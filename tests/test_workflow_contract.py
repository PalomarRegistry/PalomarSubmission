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
# Call-only inputs of a confidential run; blank for every public run.
CONFIDENTIAL_INPUTS = {"source_url", "source_sha256", "tombstone_url"}


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

    def test_call_inputs_are_the_dispatch_inputs_plus_the_pin_and_the_confidential_source(self):
        for name in WORKFLOWS:
            with self.subTest(name):
                on = triggers(parsed(name))
                called = on["workflow_call"]["inputs"]
                dispatched = on["workflow_dispatch"]["inputs"]
                self.assertEqual(set(called), set(dispatched) | {"pipeline_commit"} | CONFIDENTIAL_INPUTS)
                self.assertNotIn("pipeline_commit", dispatched)
                self.assertIs(called["pipeline_commit"]["required"], True)
                # A dispatch runs in public PalomarSubmission, so it can
                # never name a confidential source.
                self.assertFalse(CONFIDENTIAL_INPUTS & set(dispatched))
                for field in CONFIDENTIAL_INPUTS:
                    self.assertIs(called[field]["required"], False, field)
                    self.assertEqual(called[field]["default"], "", field)
                    self.assertEqual(called[field]["type"], "string", field)
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
                for field in CONFIDENTIAL_INPUTS:
                    self.assertNotIn(field, title)
                self.assertIn("inputs.request_id", title)

    def test_render_title_is_request_id_only(self):
        self.assertEqual(
            parsed("render-challenge.yml")["run-name"], "Render challenge [${{ inputs.request_id }}]"
        )


class NoSourceNamesInRunSurfacesTests(unittest.TestCase):
    """A confidential caller's run page shows step names, artifact names and
    summaries. None of them may be built from the source's identity."""

    IDENTIFYING = re.compile(
        r"inputs\.(repository|commit|challenge_sha256|[a-z_]*_path|options|source_url|source_sha256"
        r"|tombstone_url)\b"
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


CONFIDENTIAL_REFERENCE = re.compile(r"inputs\.(source_url|source_sha256|tombstone_url)\b")
# Only these forms may mention the confidential inputs. Each one leaves a
# public run (all three inputs blank) exactly as it was without them.
CONFIDENTIAL_ONLY_IF = re.compile(r"^(always\(\) && )?inputs\.source_url( && .+)?$", re.S)
PUBLIC_PASSES = re.compile(r" && \(!inputs\.source_url \|\| steps\.tombstone[1-3]\.outcome == 'success'\)")
CHECK_TOMBSTONE = "python3 pipeline/scripts/confidential_source.py check-tombstone"


def job_steps(name):
    job = "verify" if name == "submission.yml" else "render"
    return parsed(name)["jobs"][job]["steps"]


def without_confidential_forms(step):
    """The step as a public run evaluates it: blank inputs, nothing else."""
    step = dict(step)
    if "if" in step:
        condition = " ".join(str(step["if"]).split())
        step["if"] = PUBLIC_PASSES.sub("", condition)
    if step.get("with", {}).get("bundler-cache") == "${{ !inputs.source_url }}":
        step["with"] = {**step["with"], "bundler-cache": True}
    if "scripts.render_challenge prepare" in step.get("run", ""):
        step["env"] = {key: value for key, value in step["env"].items()
                       if key not in {"SOURCE_URL", "SOURCE_SHA256", "TOMBSTONE_URL"}}
        step["run"] = re.sub(
            r'^ *--(source-url|source-sha256|tombstone-url) "\$[A-Z0-9_]+" \\\n', "", step["run"], flags=re.M
        )
    return step


class ConfidentialSourceWorkflowTests(unittest.TestCase):
    """SUB-2: a call with `source_url` verifies or renders the server's
    tarball, and stops while the submission's tombstone exists."""

    def test_public_runs_evaluate_every_step_as_before(self):
        # A public run leaves source_url blank, so a confidential-only step is
        # skipped, `(!inputs.source_url || ...)` is true, the Ruby cache is
        # on and the render's source arguments are blank (which
        # render_challenge reads as "check out GitHub", tested separately).
        # Removing exactly those forms must leave no mention of the inputs.
        for name in WORKFLOWS:
            with self.subTest(name):
                for step in job_steps(name):
                    if CONFIDENTIAL_ONLY_IF.match(" ".join(str(step.get("if", "")).split())):
                        continue
                    public = str(without_confidential_forms(step))
                    self.assertNotRegex(public, CONFIDENTIAL_REFERENCE, step.get("name"))
                    self.assertNotRegex(public, r"SOURCE_URL|SOURCE_SHA256|TOMBSTONE_URL|source-url|"
                                                r"source-sha256|tombstone-url", step.get("name"))

    def test_confidential_only_steps_are_the_tombstone_checks_and_the_gem_install(self):
        for name, expected in (
            ("submission.yml", {"withdrawal", "gems", "tombstone1", "tombstone2", "tombstone3"}),
            ("render-challenge.yml", {"withdrawal", "tombstone1"}),
        ):
            with self.subTest(name):
                only = {
                    step.get("id") for step in job_steps(name)
                    if CONFIDENTIAL_ONLY_IF.match(" ".join(str(step.get("if", "")).split()))
                }
                self.assertEqual(only, expected)

    def test_tombstone_is_checked_before_any_source_is_read(self):
        for name in WORKFLOWS:
            with self.subTest(name):
                steps_ = job_steps(name)
                ids = [step.get("id") for step in steps_]
                start = steps_[ids.index("withdrawal")]
                self.assertEqual(start["run"], CHECK_TOMBSTONE)
                self.assertEqual(start["env"], {"PALOMAR_TOMBSTONE_URL": "${{ inputs.tombstone_url }}"})
                self.assertNotIn("continue-on-error", start)
                self.assertLess(ids.index("withdrawal"), ids.index("prepare"))
                # Every step between the start check and the source fetch
                # runs only on success, so a withdrawn run never reaches it.
                for step in steps_[ids.index("withdrawal") + 1 : ids.index("prepare") + 1]:
                    self.assertNotIn("always()", str(step.get("if", "")), step.get("name"))

    def test_every_upload_requires_a_fresh_tombstone_check(self):
        for name in WORKFLOWS:
            with self.subTest(name):
                steps_ = job_steps(name)
                uploads = [
                    position for position, step in enumerate(steps_)
                    if str(step.get("uses", "")).startswith("actions/upload-artifact@")
                ]
                self.assertTrue(uploads)
                for position in uploads:
                    upload, check = steps_[position], steps_[position - 1]
                    self.assertEqual(check["run"], CHECK_TOMBSTONE)
                    self.assertNotIn("continue-on-error", check)
                    upload_if = " ".join(str(upload["if"]).split())
                    required = f"&& (!inputs.source_url || steps.{check['id']}.outcome == 'success')"
                    self.assertTrue(upload_if.endswith(required), upload_if)
                    # The check runs whenever the upload would, in a confidential run.
                    public_if = PUBLIC_PASSES.sub("", upload_if)
                    self.assertEqual(
                        " ".join(str(check["if"]).split()),
                        public_if.replace("always()", "always() && inputs.source_url", 1),
                    )

    def test_confidential_runs_write_no_cache(self):
        for name in WORKFLOWS:
            with self.subTest(name):
                self.assertNotIn("actions/cache", WORKFLOWS[name])
                for step in job_steps(name):
                    if str(step.get("uses", "")).startswith("ruby/setup-ruby@"):
                        self.assertEqual(step["with"]["bundler-cache"], "${{ !inputs.source_url }}")
        gems = next(step for step in job_steps("submission.yml") if step.get("id") == "gems")
        self.assertIn("bundle install", gems["run"])
        self.assertEqual(gems["working-directory"], "pipeline")

    def test_confidential_runs_refuse_the_aws_profile_before_choosing_a_runner(self):
        for name in WORKFLOWS:
            with self.subTest(name):
                profile = parsed(name)["jobs"]["profile"]["steps"]
                names = [step.get("name") for step in profile]
                refusal = profile[names.index("Refuse the AWS profile for confidential runs")]
                self.assertEqual(
                    refusal["if"],
                    "inputs.source_url && inputs.execution_profile == 'palomar-aws-r7i-16xlarge-v1'",
                )
                self.assertIn("exit 1", refusal["run"])
                resolve = next(i for i, step in enumerate(profile) if step.get("id") == "resolve")
                self.assertLess(names.index("Refuse the AWS profile for confidential runs"), resolve)

    def test_render_passes_the_source_tarball_to_prepare(self):
        step = next(step for step in job_steps("render-challenge.yml") if step.get("id") == "prepare")
        self.assertEqual(step["env"]["SOURCE_URL"], "${{ inputs.source_url }}")
        self.assertEqual(step["env"]["SOURCE_SHA256"], "${{ inputs.source_sha256 }}")
        self.assertEqual(step["env"]["TOMBSTONE_URL"], "${{ inputs.tombstone_url }}")
        for flag, variable in (
            ("--source-url", "SOURCE_URL"), ("--source-sha256", "SOURCE_SHA256"),
            ("--tombstone-url", "TOMBSTONE_URL"),
        ):
            self.assertIn(f'{flag} "${variable}"', step["run"])

    def test_verify_reads_the_source_tarball_from_its_inputs(self):
        # The verifier's event file is toJSON(inputs), so the three call
        # inputs reach `verify_submission.py prepare` without new arguments.
        step = next(step for step in job_steps("submission.yml") if step.get("id") == "prepare")
        self.assertEqual(step["env"]["PALOMAR_INPUTS"], "${{ toJSON(inputs) }}")


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

    def test_callers_declare_the_called_dispatch_inputs_and_require_the_source_tarball(self):
        for name, (called, _) in CALLERS.items():
            with self.subTest(name):
                declared = triggers(caller(name))["workflow_dispatch"]["inputs"]
                dispatched = triggers(parsed(called))["workflow_dispatch"]["inputs"]
                self.assertEqual(set(declared), set(dispatched) | CONFIDENTIAL_INPUTS)
                for field, spec in dispatched.items():
                    self.assertEqual(bool(declared[field].get("required")), bool(spec.get("required")), field)
                    self.assertEqual(declared[field].get("default"), spec.get("default"), field)
                # A confidential caller without a tarball would fall back to
                # a GitHub checkout, so the caller refuses that dispatch.
                for field in CONFIDENTIAL_INPUTS:
                    self.assertIs(declared[field].get("required"), True, field)

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
