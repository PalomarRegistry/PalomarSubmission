import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

import yaml

import scripts.verify_submission as verifier
from scripts.verify_submission import (
    EXECUTION_BUDGET_SECONDS,
    LicenseValidationError,
    LicenseDetectorError,
    PERMISSIVE_RESOURCE_PROPERTIES,
    ResourceExhausted,
    VerificationError,
    _deadline_timeout,
    allowed_roots,
    audit_challenge_sources,
    build_indexed_roots,
    canonical_repository,
    compile_canonical_challenge,
    direct_imports,
    detect_spdx_identifier,
    enforced_comparator_config,
    execute,
    github_repository,
    indexed_versions,
    lake_environment_value,
    landrun_command,
    load_comparator_config,
    load_formalization_metadata,
    materialize_packages,
    normalize_repository,
    package_allowlist,
    parse_issue_body,
    protected_lean_path,
    reject_committed_build_artifacts,
    reject_untrusted_package_artifacts,
    remove_untrusted_lake_state,
    repository_license_file,
    require_protected_paths,
    run,
    sandboxed_run,
    systemd_command,
    trusted_package_url_map,
    verify_official_revision,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class VerifySubmissionTests(unittest.TestCase):
    def test_repository_license_file_is_one_nonempty_regular_root_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            license_path = root / "licence.MD"
            license_path.write_text("standard terms\n")
            self.assertEqual(repository_license_file(root), license_path)

            (root / "COPYING").write_text("other terms\n")
            with self.assertRaisesRegex(LicenseValidationError, "exactly one"):
                repository_license_file(root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "LICENSE").write_text("  \n")
            with self.assertRaisesRegex(LicenseValidationError, "must not be empty"):
                repository_license_file(root)

    def test_repository_license_file_rejects_missing_and_symlinked_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(LicenseValidationError, "no conventional"):
                repository_license_file(root)
            target = root / "terms"
            target.write_text("terms\n")
            (root / "LICENSE").symlink_to(target)
            with self.assertRaisesRegex(LicenseValidationError, "not a regular"):
                repository_license_file(root)

    def test_detect_spdx_identifier_requires_one_consistent_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "bundle"
            bundle.touch()
            license_path = root / "LICENSE"
            license_path.write_text("terms\n")
            result = {
                "licenses": [{"spdx_id": "Apache-2.0"}],
                "matched_files": [{"matched_license": "Apache-2.0"}],
            }
            completed = subprocess.CompletedProcess(
                [str(bundle)], 0, json.dumps(result), ""
            )
            with mock.patch("scripts.verify_submission.run", return_value=completed):
                self.assertEqual(
                    detect_spdx_identifier(license_path, bundle), "Apache-2.0"
                )

            result["licenses"] = []
            rejected = subprocess.CompletedProcess([str(bundle)], 0, json.dumps(result), "")
            with (
                mock.patch("scripts.verify_submission.run", return_value=rejected),
                self.assertRaisesRegex(LicenseValidationError, "unambiguous"),
            ):
                detect_spdx_identifier(license_path, bundle)

            for malformed in (
                {
                    "licenses": [{"spdx_id": "NOASSERTION"}],
                    "matched_files": [{"matched_license": "NOASSERTION"}],
                },
                {
                    "licenses": [{"spdx_id": "Apache-2.0"}],
                    "matched_files": [{"matched_license": "MIT"}],
                },
                {
                    "licenses": [{"spdx_id": "Apache-2.0"}],
                    "matched_files": [
                        {"matched_license": "Apache-2.0"},
                        {"matched_license": "Apache-2.0"},
                    ],
                },
            ):
                rejected = subprocess.CompletedProcess(
                    [str(bundle)], 0, json.dumps(malformed), ""
                )
                with (
                    mock.patch("scripts.verify_submission.run", return_value=rejected),
                    self.assertRaisesRegex(LicenseValidationError, "unambiguous"),
                ):
                    detect_spdx_identifier(license_path, bundle)

            failed = subprocess.CompletedProcess(
                [str(bundle)], 7, "", "bundler could not load licensee"
            )
            with (
                mock.patch("scripts.verify_submission.run", return_value=failed),
                self.assertRaisesRegex(
                    LicenseDetectorError, "exit 7: bundler could not load licensee"
                ),
            ):
                detect_spdx_identifier(license_path, bundle)

    @unittest.skipUnless(
        os.environ.get("PALOMAR_TEST_LICENSEE"),
        "set PALOMAR_TEST_LICENSEE to a Bundler executable for the detector integration test",
    )
    def test_real_licensee_detects_repository_mit_license(self):
        bundle = Path(os.environ["PALOMAR_TEST_LICENSEE"])
        self.assertEqual(detect_spdx_identifier(REPOSITORY_ROOT / "LICENSE", bundle), "MIT")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy2(REPOSITORY_ROOT / "LICENSE", root / "LICENSE")
            (root / "LICENSES").mkdir()
            shutil.copy2(REPOSITORY_ROOT / "LICENSE", root / "LICENSES" / "LICENSE")
            self.assertEqual(detect_spdx_identifier(root / "LICENSE", bundle), "MIT")

    def test_phase_timeout_is_capped_by_global_deadline(self):
        with (
            mock.patch("scripts.verify_submission._EXECUTION_DEADLINE", 100.0),
            mock.patch("scripts.verify_submission._MONOTONIC", return_value=90.0),
        ):
            self.assertEqual(_deadline_timeout(600, ["probe"]), 10)
        with (
            mock.patch("scripts.verify_submission._EXECUTION_DEADLINE", 100.0),
            mock.patch("scripts.verify_submission._MONOTONIC", return_value=101.0),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            _deadline_timeout(600, ["probe"])

    def test_expired_job_deadline_is_reported_and_restored(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "report.json"
            report_path.write_text(json.dumps({"status": "pending", "errors": []}))
            tools = []
            for name in ("comparator", "lean4export", "landrun", "nanoda"):
                tool = root / name
                tool.touch()
                tools.append(tool)
            args = Namespace(
                output=report_path,
                work_dir=root / "work",
                database=root / "database",
                comparator=tools[0],
                lean4export=tools[1],
                landrun=tools[2],
                nanoda=tools[3],
                comparator_commit="a" * 40,
                landrun_commit="b" * 40,
                nanoda_commit="c" * 40,
                workflow_url="https://github.com/example/project/actions/runs/1",
            )
            with (
                mock.patch.dict(os.environ, {"PALOMAR_JOB_STARTED_AT": "1"}),
                mock.patch.object(verifier, "_EXECUTION_DEADLINE", 123.0),
                mock.patch("scripts.verify_submission.shutil.which", return_value=sys.executable),
            ):
                self.assertEqual(execute(args), 0)
                self.assertEqual(verifier._EXECUTION_DEADLINE, 123.0)

            report = json.loads(report_path.read_text())
            self.assertEqual(report["status"], "error")
            self.assertEqual(report["stage"], "resource-exhausted")
            self.assertEqual(report["error_kind"], "infrastructure/resource-exhausted")
            self.assertTrue(report["retryable"])
            self.assertIn("retry on a longer-running worker", report["errors"][0])

    def test_default_capacity_supports_ten_hour_verification(self):
        self.assertGreaterEqual(EXECUTION_BUDGET_SECONDS, 10 * 60 * 60)
        self.assertIn("MemoryMax=98%", PERMISSIVE_RESOURCE_PROPERTIES)
        self.assertFalse(
            any(
                property_value.startswith("CPUQuota=")
                for property_value in PERMISSIVE_RESOURCE_PROPERTIES
            )
        )

    def test_clear_resource_termination_is_retryable_not_a_phase_failure(self):
        completed = subprocess.CompletedProcess(["systemd-run"], 137, "", "killed")
        with (
            mock.patch("scripts.verify_submission.verify_tool_snapshot"),
            mock.patch("scripts.verify_submission.landrun_command", return_value=["confined"]),
            mock.patch("scripts.verify_submission.systemd_command", return_value=["systemd-run"]),
            mock.patch("scripts.verify_submission.run", return_value=completed),
            mock.patch("scripts.verify_submission._RESOURCE_METRICS_PATH", None),
            self.assertRaisesRegex(ResourceExhausted, "resource ceiling"),
        ):
            sandboxed_run(
                ["lean", "Challenge.lean"],
                cwd=REPOSITORY_ROOT,
                environment={},
                landrun=Path("landrun"),
                writable_directories=[],
                executable_paths=[],
                tools={},
            )

    def test_candidate_output_cannot_forge_resource_exhaustion(self):
        completed = subprocess.CompletedProcess(
            ["systemd-run"], 0, "out of memory; timed out; no space left on device", ""
        )
        with (
            mock.patch("scripts.verify_submission.verify_tool_snapshot"),
            mock.patch("scripts.verify_submission.landrun_command", return_value=["confined"]),
            mock.patch("scripts.verify_submission.systemd_command", return_value=["systemd-run"]),
            mock.patch("scripts.verify_submission.run", return_value=completed),
            mock.patch("scripts.verify_submission._RESOURCE_METRICS_PATH", None),
        ):
            result = sandboxed_run(
                ["lean", "Challenge.lean"],
                cwd=REPOSITORY_ROOT,
                environment={},
                landrun=Path("landrun"),
                writable_directories=[],
                executable_paths=[],
                tools={},
            )
        self.assertEqual(result.returncode, 0)

    def test_resource_wrapper_records_bounded_usage(self):
        with tempfile.TemporaryDirectory() as temporary:
            metrics = Path(temporary) / "metrics.jsonl"
            subprocess.run(
                [
                    sys.executable,
                    str(REPOSITORY_ROOT / "scripts" / "measure_resources.py"),
                    "--output",
                    str(metrics),
                    "--phase",
                    "fixture",
                    "--disk-path",
                    temporary,
                    "--",
                    sys.executable,
                    "-c",
                    "value = bytearray(1024 * 1024); print(len(value))",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            record = json.loads(metrics.read_text())
            self.assertEqual(record["phase"], "fixture")
            self.assertEqual(record["returncode"], 0)
            self.assertGreater(record["max_rss_kib"], 0)
            self.assertGreaterEqual(record["peak_tasks_observed"], 1)

    def test_command_output_capture_is_bounded(self):
        with mock.patch("scripts.verify_submission.MAX_CAPTURE_BYTES", 1024):
            proc = run(
                [
                    sys.executable,
                    "-c",
                    "import sys; print('a' * 4096); print('b' * 4096, file=sys.stderr)",
                ]
            )
        self.assertIn("<output truncated", proc.stdout)
        self.assertIn("<output truncated", proc.stderr)
        self.assertTrue(proc.stdout.endswith("\n"))
        self.assertLess(len(proc.stdout.encode()), 1200)
        self.assertLess(len(proc.stderr.encode()), 1200)

    def test_issue_form_scalar_values(self) -> None:
        form = (REPOSITORY_ROOT / ".github" / "ISSUE_TEMPLATE" / "submit.yml").read_text()
        self.assertIn(
            'placeholder: "0000000000000000000000000000000000000000"',
            form,
        )
        self.assertIn('placeholder: PALOMAR-2026-07-29-000123', form)

    def test_submission_workflow_run_name_includes_issue_number(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "submission.yml").read_text()
        self.assertIn(
            'run-name: "Verify submission #${{ github.event.issue.number }}"',
            workflow,
        )

    def test_submission_workflow_accepts_only_guarded_author_reverification(self) -> None:
        path = REPOSITORY_ROOT / ".github" / "workflows" / "submission.yml"
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(workflow["on"]["issues"]["types"], ["labeled"])
        self.assertEqual(workflow["on"]["issue_comment"]["types"], ["created"])

        condition = " ".join(workflow["jobs"]["mark"]["if"].split())
        expected = (
            "(github.event_name == 'issues' && github.event.label.name == 'submission') || "
            "(github.event_name == 'issue_comment' && "
            "github.event.issue.pull_request == null && github.event.issue.state == 'open' && "
            "github.event.comment.body == '/reverify' && "
            "github.event.comment.user.login == github.event.issue.user.login && "
            "contains(github.event.issue.labels.*.name, 'submission') && "
            "(contains(github.event.issue.labels.*.name, 'status:verification-error') || "
            "contains(github.event.issue.labels.*.name, 'status:changes-requested')))"
        )
        self.assertEqual(condition, expected)

    def test_submission_workflow_runs_downstream_only_after_mark_gate(self) -> None:
        path = REPOSITORY_ROOT / ".github" / "workflows" / "submission.yml"
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertNotIn("concurrency", workflow)
        mark = workflow["jobs"]["mark"]
        condition = "".join(mark["if"].split())
        group = "".join(mark["concurrency"]["group"].split())
        self.assertEqual(
            group,
            "${{(" + condition + ")&&"
            "format('palomar-submission-claim-{0}',github.event.issue.number)||"
            "format('palomar-submission-ignore-{0}',github.run_id)}}",
        )
        self.assertEqual(mark["concurrency"]["cancel-in-progress"], "false")
        self.assertEqual(mark["outputs"]["claimed"], "${{ steps.claim.outputs.claimed }}")
        claim_step = next(step for step in mark["steps"] if step.get("id") == "claim")
        self.assertIn("scripts/claim_submission.py", claim_step["run"])
        self.assertIn("$GITHUB_EVENT_PATH", claim_step["run"])
        self.assertEqual(workflow["jobs"]["verify"]["needs"], "mark")
        self.assertEqual(
            workflow["jobs"]["verify"]["if"],
            "needs.mark.outputs.claimed == 'true'",
        )
        self.assertEqual(workflow["jobs"]["report"]["needs"], ["mark", "verify"])
        report = workflow["jobs"]["report"]
        self.assertEqual(
            " ".join(report["if"].split()),
            "always() && needs.mark.result != 'skipped' && "
            "(needs.mark.result == 'failure' || needs.mark.outputs.claimed == 'true')",
        )
        self.assertEqual(
            report["concurrency"]["group"],
            "palomar-submission-claim-${{ github.event.issue.number }}",
        )
        self.assertEqual(report["concurrency"]["cancel-in-progress"], "false")
        report_step = next(
            step for step in report["steps"] if step["name"] == "Report result and transition issue"
        )
        download_step = next(
            step for step in report["steps"] if step["name"] == "Download mechanical report"
        )
        self.assertEqual(download_step["if"], "needs.mark.outputs.claimed == 'true'")
        self.assertEqual(report_step["env"]["MARK_RESULT"], "${{ needs.mark.result }}")
        self.assertIn("scripts/claim_submission.py", report_step["run"])
        self.assertIn("scripts/report_issue.py", report_step["run"])

    def test_every_workflow_builds_landrun_without_proc_enumerating_cgo(self):
        expected = re.compile(
            r"^\s*CGO_ENABLED=0 go install github\.com/zouuup/landrun/cmd/landrun@"
            r"811cfff51ceaf3d9843708aa6d22e9b84ccac8b4\s*$"
        )
        workflows = REPOSITORY_ROOT / ".github" / "workflows"
        installers = []
        for path in [*workflows.glob("*.yml"), *workflows.glob("*.yaml")]:
            text = path.read_text()
            lines = [
                line
                for line in text.splitlines()
                if "go install github.com/zouuup/landrun/cmd/landrun@" in line
            ]
            if lines:
                installers.append(path.name)
                for line in lines:
                    self.assertRegex(line, expected, path.name)
        self.assertEqual(
            sorted(installers),
            ["ci.yml", "compatibility.yml", "render-challenge.yml", "submission.yml"],
        )

    def test_every_verifier_workflow_installs_hash_pinned_dependencies(self):
        workflows = REPOSITORY_ROOT / ".github" / "workflows"
        verifier_entrypoints = (
            "scripts.render_challenge",
            "scripts/smoke_trusted_challenge.py",
            "scripts/verify_submission",
        )
        installers = []
        for path in [*workflows.glob("*.yml"), *workflows.glob("*.yaml")]:
            text = path.read_text()
            if any(entrypoint in text for entrypoint in verifier_entrypoints):
                installers.append(path.name)
                self.assertIn(
                    "pip install --disable-pip-version-check --require-hashes --no-deps -r",
                    text,
                    path.name,
                )
        self.assertEqual(
            sorted(installers),
            ["compatibility.yml", "render-challenge.yml", "submission.yml"],
        )

    def test_issue_form_sections(self):
        body = """### Repository URL

https://github.com/example/result

### Commit SHA

0123456789012345678901234567890123456789

### Existing Palomar ID (updates only)

_No response_

### Relationship to the substantive formalization

I am a responsible author or maintainer

### Authorization evidence (optional)

_No response_
"""
        parsed = parse_issue_body(body)
        self.assertEqual(parsed["repository_url"], "https://github.com/example/result")
        self.assertEqual(parsed["existing_id"], "")

    def test_duplicate_recognized_issue_section_is_rejected(self):
        body = """### Repository URL

https://github.com/example/result

### Commit SHA

0123456789012345678901234567890123456789

### Additional context (optional)

Context before a misleading duplicate.

### Commit SHA

1111111111111111111111111111111111111111
"""
        with self.assertRaisesRegex(VerificationError, "duplicate recognized issue section"):
            parse_issue_body(body)

    def test_repository_normalization(self):
        self.assertEqual(
            normalize_repository("https://github.com/example/result.git"),
            ("example/result", "https://github.com/example/result"),
        )
        with self.assertRaises(VerificationError):
            normalize_repository("https://evil.example/example/result")

    def test_github_repository_variants(self):
        self.assertEqual(github_repository("https://github.com/a/b.git"), "a/b")
        self.assertEqual(github_repository("git@github.com:a/b.git"), "a/b")
        self.assertIsNone(github_repository("https://gitlab.com/a/b"))

    def test_allowlisted_repository_aliases(self):
        roots, aliases = allowed_roots()
        self.assertEqual(
            {root["official_ref"] for root in roots},
            {"refs/heads/master", "refs/heads/main"},
        )
        self.assertEqual(
            canonical_repository("formalfrontier/tauceti", aliases),
            "TauCetiProject/TauCeti",
        )
        tauceti = next(root for root in roots if root["repository"] == "TauCetiProject/TauCeti")
        self.assertEqual(
            tauceti["accepted_revisions"],
            ["221bb56a017bb794421eac4fa543d7a5e85add75"],
        )

    def test_imports(self):
        source = """/- leading block comment -/ import Mathlib -- trailing comment
public /- nested /- comment -/ still -/ import TauCeti.Topology
-- import NotReal
"""
        self.assertEqual(direct_imports(source), ["Mathlib", "TauCeti.Topology"])

    def test_comparator_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "comparator.json"
            path.write_text(
                json.dumps(
                    {
                        "challenge_module": "Challenge",
                        "solution_module": "Solution",
                        "theorem_names": ["headline"],
                        "definition_names": [],
                        "permitted_axioms": ["propext", "Quot.sound", "Classical.choice"],
                        "enable_nanoda": False,
                    }
                )
            )
            self.assertEqual(load_comparator_config(path)["theorem_names"], ["headline"])

            enforced = Path(directory) / "enforced.json"
            enforced_comparator_config(path, enforced)
            self.assertTrue(json.loads(enforced.read_text())["enable_nanoda"])
            self.assertFalse(json.loads(path.read_text())["enable_nanoda"])

            config = json.loads(path.read_text())
            config["enable_nanoda"] = True
            path.write_text(json.dumps(config))
            enforced_comparator_config(path, enforced)
            self.assertTrue(json.loads(enforced.read_text())["enable_nanoda"])

            config = json.loads(path.read_text())
            config["future_relaxation"] = True
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(VerificationError, "unknown keys"):
                load_comparator_config(path)

    def test_formalization_metadata_mechanical_minimum(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "formalization.yaml"
            path.write_text(
                """\
version: v0.3
project:
  name: Example result
  authors: [Ada Lovelace]
  license: Apache-2.0
  responsible_maintainers: [Ada Lovelace]
provenance:
  result_origin: source-based
repository:
  role: substantive-development
classification:
  arxiv: [math.LO, cs.LO]
  msc2020: [03B35, 68V15]
sources:
  - title: A source theorem
    authors:
      - name: Emmy Noether
    id: doi:10.1000/example
    relationship: formalizes
automation:
  methods:
    - method: manual
review:
  status: self-assessed
"""
            )
            metadata = load_formalization_metadata(path)
            self.assertEqual(metadata["project"]["name"], "Example result")
            self.assertEqual(metadata["classification"]["arxiv"], ["math.LO", "cs.LO"])

    def test_formalization_metadata_rejects_unknown_or_too_many_classifications(self):
        valid = """\
project:
  name: Example result
  authors: [Ada Lovelace]
  license: Apache-2.0
  responsible_maintainers: [Ada Lovelace]
provenance:
  result_origin: source-based
repository:
  role: substantive-development
classification:
  arxiv: [math.LO]
  msc2020: [03B35]
sources:
  - title: A source theorem
    authors: [Emmy Noether]
    id: doi:10.1000/example
    relationship: formalizes
automation:
  methods:
    - method: manual
review:
  status: self-assessed
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "formalization.yaml"
            path.write_text(valid.replace("math.LO", "math.NOTREAL"))
            with self.assertRaisesRegex(VerificationError, "not a recognized classification"):
                load_formalization_metadata(path)
            path.write_text(valid.replace("[math.LO]", "[{code: math.LO}]"))
            with self.assertRaisesRegex(VerificationError, "not a recognized classification"):
                load_formalization_metadata(path)
            path.write_text(valid.replace("[math.LO]", "[math.LO, cs.LO, math.CO]"))
            with self.assertRaisesRegex(VerificationError, "1 or 2 classification codes"):
                load_formalization_metadata(path)
            path.write_text(valid.replace("03B35", "99Z99"))
            with self.assertRaisesRegex(VerificationError, "not a recognized classification"):
                load_formalization_metadata(path)

    def test_provenance_accepts_a_source_free_original_result(self):
        provenance = verifier.normalized_provenance(
            {
                "project": {"responsible_maintainers": ["Ada Lovelace"]},
                "provenance": {"result_origin": "original"},
                "repository": {"role": "substantive-development"},
            }
        )
        self.assertEqual(provenance["mathematical_sources"], [])
        self.assertEqual(provenance["result_origin"], "original")

    def test_source_based_provenance_without_a_substantive_relationship_warns(self):
        data = {
            "project": {"responsible_maintainers": ["Ada Lovelace"]},
            "provenance": {"result_origin": "source-based"},
            "repository": {"role": "substantive-development"},
            "sources": [
                {
                    "title": "Background textbook",
                    "relationship": "background",
                }
            ],
        }
        warnings = []
        provenance = verifier.normalized_provenance(data, warnings=warnings)
        self.assertEqual(provenance["result_origin"], "source-based")
        self.assertTrue(any("no source explicitly marked" in warning for warning in warnings))

    def test_legacy_provenance_fields_are_inferred_instead_of_rejected(self):
        warnings = []
        provenance = verifier.normalized_provenance(
            {
                "project": {"authors": ["Ada Lovelace"]},
                "sources": [
                    {
                        "title": "An older source record",
                        "author": "Emmy Noether",
                        "kind": "informal_proof",
                    }
                ],
            },
            warnings=warnings,
        )
        self.assertEqual(provenance["result_origin"], "unspecified")
        self.assertEqual(provenance["repository_role"], "unspecified")
        self.assertEqual(provenance["responsible_maintainers"], [])
        self.assertEqual(provenance["mathematical_sources"][0]["relationship"], "other")
        self.assertEqual(
            provenance["mathematical_sources"][0]["authors"], [{"name": "Emmy Noether"}]
        )
        self.assertEqual(
            provenance["declared"],
            {
                "result_origin": False,
                "repository_role": False,
                "responsible_maintainers": False,
            },
        )
        self.assertTrue(any("result_origin" in warning for warning in warnings))
        self.assertTrue(any("repository.role" in warning for warning in warnings))
        self.assertTrue(any("responsible maintainer" in warning for warning in warnings))
        self.assertTrue(any("relationship" in warning for warning in warnings))

    def test_conflicting_or_unrecognized_provenance_is_not_published_as_a_claim(self):
        warnings = []
        provenance = verifier.normalized_provenance(
            {
                "project": {"responsible_maintainers": ["Ada Lovelace"]},
                "provenance": {"result_origin": "original"},
                "repository": {"role": "thin_wrapper"},
                "sources": [{"title": "Source", "relationship": "formalizes"}],
            },
            warnings=warnings,
        )
        self.assertEqual(provenance["result_origin"], "unspecified")
        self.assertEqual(provenance["repository_role"], "unspecified")
        self.assertFalse(provenance["declared"]["result_origin"])
        self.assertFalse(provenance["declared"]["repository_role"])

    def test_unquoted_yaml_boolean_author_contacted_is_normalized(self):
        provenance = verifier.normalized_provenance(
            {
                "project": {"responsible_maintainers": ["Ada Lovelace"]},
                "provenance": {"result_origin": "source-based"},
                "repository": {"role": "substantive-development"},
                "sources": [
                    {
                        "title": "Source",
                        "relationship": "formalizes",
                        "author_contacted": False,
                    }
                ],
            }
        )
        self.assertEqual(provenance["mathematical_sources"][0]["author_contacted"], "no")

    def test_thin_wrapper_records_the_substantive_repository_at_a_full_commit(self):
        revision = "a" * 40
        provenance = verifier.normalized_provenance(
            {
                "project": {"responsible_maintainers": ["Ada Lovelace"]},
                "provenance": {"result_origin": "original"},
                "repository": {
                    "role": "thin-wrapper",
                    "substantive_formalization": {
                        "id": "example/substantive",
                        "revision": revision,
                    },
                },
            }
        )
        self.assertEqual(
            provenance["substantive_formalization"]["tree_url"],
            f"https://github.com/example/substantive/tree/{revision}",
        )
    def test_formalization_metadata_must_be_valid_yaml(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "formalization.yaml"
            path.write_text("project: [unterminated\n")
            with self.assertRaisesRegex(VerificationError, "not valid YAML"):
                load_formalization_metadata(path)

    def test_formalization_metadata_must_contain_required_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "formalization.yaml"
            path.write_text("{}\n")
            with self.assertRaisesRegex(VerificationError, "field project must be a mapping"):
                load_formalization_metadata(path)

    def test_formalization_metadata_rejects_empty_required_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "formalization.yaml"
            path.write_text(
                """\
project:
  name: ""
  authors: []
  license: ""
classification:
  arxiv: []
  msc2020: []
sources: []
automation:
  methods: []
review:
  status: ""
"""
            )
            with self.assertRaisesRegex(VerificationError, "project.name"):
                load_formalization_metadata(path)

    def test_formalization_metadata_rejects_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "formalization.yaml"
            path.write_text("project:\n  name: first\n  name: second\n")
            with self.assertRaisesRegex(VerificationError, "duplicate key"):
                load_formalization_metadata(path)

    def test_formalization_metadata_rejects_yaml_merge_keys_before_expansion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "formalization.yaml"
            path.write_text(
                "base: &base {key: value}\n"
                "nested: &nested {<<: [*base, *base]}\n"
                "<<: [*nested, *nested]\n"
            )
            with self.assertRaisesRegex(VerificationError, "must not use YAML merge keys"):
                load_formalization_metadata(path)

    def test_outer_landrun_policy(self):
        command = landrun_command(
            ["/tools/comparator", "comparator.json"],
            landrun=Path("/tools/landrun"),
            writable_directories=[Path("/source/.lake/build")],
            readable_paths=[Path("/source")],
            executable_paths=[Path("/usr"), Path("/tools/comparator")],
            environment={
                "PATH": "/usr/bin",
                "HOME": "/source/.lake/config/home",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "COMPARATOR_NANODA": "/tools/nanoda_bin",
                "SECRET": "no",
            },
            readable_directories=(Path("/source"),),
        )
        self.assertEqual(
            command[:4],
            [
                "/tools/landrun",
                "--best-effort",
                "--ldd",
                "--add-exec",
            ],
        )
        self.assertNotIn("/", command)
        self.assertIn("/source", command)
        self.assertNotIn("/dev", command)
        self.assertIn("/dev/null", command)
        self.assertIn("/source/.lake/build", command)
        self.assertNotIn("/source/replay.hash", command)
        self.assertIn("/tools/comparator", command)
        self.assertIn("GIT_CONFIG_GLOBAL", command)
        self.assertIn("GIT_CONFIG_NOSYSTEM", command)
        self.assertIn("GIT_TERMINAL_PROMPT", command)
        self.assertIn("COMPARATOR_NANODA", command)
        self.assertNotIn("SECRET", command)
        self.assertIn("GIT_CONFIG_GLOBAL", command)
        self.assertIn("GIT_CONFIG_NOSYSTEM", command)
        self.assertIn("GIT_TERMINAL_PROMPT", command)
        self.assertNotIn("--unrestricted-network", command)
        self.assertEqual(command[command.index("--") + 1 :], ["/tools/comparator", "comparator.json"])

        replay = landrun_command(
            ["lake", "build"],
            landrun=Path("/tools/landrun"),
            writable_directories=[],
            writable_files=[Path("/source/replay.hash")],
            readable_paths=[Path("/source")],
            executable_paths=[Path("/tools/lake")],
            environment={},
        )
        marker = replay.index("/source/replay.hash")
        self.assertEqual(replay[marker - 1], "--rw")

        networked = landrun_command(
            ["lake", "exe", "cache", "get"],
            landrun=Path("landrun"),
            writable_directories=[],
            readable_paths=[],
            executable_paths=[],
            environment={},
            unrestricted_network=True,
        )
        self.assertIn("--unrestricted-network", networked)

    def test_protected_lean_path_precedes_candidate_shadow_modules(self):
        canonical = Path("/protected/Challenge.olean")
        value = protected_lean_path(
            canonical,
            [Path("/toolchain/lib/lean"), Path("/mathlib/lib/lean")],
            "/evil/lib/lean:/source/.lake/build/lib/lean",
        )
        self.assertEqual(
            value.split(":"),
            [
                "/protected",
                "/toolchain/lib/lean",
                "/mathlib/lib/lean",
                "/evil/lib/lean",
                "/source/.lake/build/lib/lean",
            ],
        )

    def test_indexed_source_cannot_shadow_a_trusted_module(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            source = work / "source"
            package = source / ".lake" / "packages" / "indexed"
            package.mkdir(parents=True)
            (package / "Indexed.lean").write_text("import Init\ndef indexed := true\n")
            (package / "Init.lean").write_text("axiom forged : False\n")
            (package / "lake-manifest.json").write_text(
                '{"version":"1.2.0","packages":[]}\n'
            )
            subprocess.run(["git", "init", "-q"], cwd=package, check=True)
            subprocess.run(["git", "add", "."], cwd=package, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Palomar test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "indexed shadow fixture",
                ],
                cwd=package,
                check=True,
            )
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=package,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            source.mkdir(exist_ok=True)
            (source / "Challenge.lean").write_text("import Indexed\n")
            lean_prefix = work / "toolchain"
            trusted_init = lean_prefix / "lib" / "lean" / "Init.olean"
            trusted_init.parent.mkdir(parents=True)
            trusted_init.write_bytes(b"trusted core module")
            package_record = {
                "name": "indexed",
                "repository": "example/indexed",
                "url": "https://github.com/example/indexed",
                "revision": revision,
            }
            with self.assertRaisesRegex(VerificationError, "shadows a Lean core"):
                build_indexed_roots(
                    work,
                    source,
                    packages=[package_record],
                    indexed={"indexed": {}},
                    allowlist={},
                    base_env={},
                    lean=Path("/tools/lean"),
                    lean_prefix=lean_prefix,
                    landrun=Path("/tools/landrun"),
                    readable_paths=[source],
                    executable_paths=[],
                    tools={},
                )
    def test_hostile_canonical_build_cannot_publish_sibling_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            source = work / "source"
            source.mkdir()
            (source / "Challenge.lean").write_text("theorem result : True := by trivial\n")
            indexed_lean = work / "indexed-olean"
            indexed_lean.mkdir()
            lean_prefix = work / "toolchain"
            (lean_prefix / "lib" / "lean").mkdir(parents=True)

            def fake_sandbox(command, **_kwargs):
                if "-o" in command:
                    output = Path(command[command.index("-o") + 1])
                    output.write_bytes(b"canonical")
                    (output.parent / "Mathlib.Forged.olean").write_bytes(b"hostile sibling")
                return mock.Mock(stdout="", stderr="", returncode=0)

            with mock.patch("scripts.verify_submission.sandboxed_run", side_effect=fake_sandbox):
                canonical, dependencies, trusted_paths = compile_canonical_challenge(
                    work,
                    source,
                    lean=Path("/tools/lean"),
                    lean_prefix=lean_prefix,
                    allowlist={},
                    indexed_lean_path=indexed_lean,
                    indexed_source_roots=[],
                    environment={},
                    landrun=Path("/tools/landrun"),
                    readable_paths=[source],
                    executable_paths=[],
                    tools={},
                )
            self.assertEqual(canonical.read_bytes(), b"canonical")
            self.assertEqual([path.name for path in canonical.parent.iterdir()], ["Challenge.olean"])
            self.assertEqual(dependencies, [])
            self.assertLess(trusted_paths.index(indexed_lean.resolve()), len(trusted_paths))

    def test_submitted_lake_state_is_discarded(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            poison = package / ".lake" / "packages" / "poison" / "payload"
            poison.parent.mkdir(parents=True)
            poison.write_text("untrusted")
            build, config = remove_untrusted_lake_state(package)
            self.assertEqual({path.name for path in (package / ".lake").iterdir()}, {"build", "config"})
            self.assertEqual(build, (package / ".lake" / "build").resolve())
            self.assertEqual(config, (package / ".lake" / "config").resolve())

    def test_committed_artifacts_outside_lake_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            artifact = package / "custom-build" / "lib" / "lean" / "Poison.olean"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"not a trusted build")
            with self.assertRaisesRegex(VerificationError, "committed build artifact"):
                materialize_packages(package, base_env={"PATH": "/usr/bin"})

    def test_fresh_lake_artifacts_are_removed_not_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            artifact = package / ".lake" / "build" / "lib" / "lean" / "Stale.olean"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"stale")
            remove_untrusted_lake_state(package)
            reject_committed_build_artifacts(package)
            self.assertFalse(artifact.exists())

    def test_official_closure_may_contain_trusted_trace_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            package = source / ".lake" / "packages" / "trusted"
            trace = package / "widget" / "package-lock.json.trace"
            trace.parent.mkdir(parents=True)
            trace.write_text('{"schemaVersion":"trusted"}')
            packages = [
                {
                    "name": "trusted",
                    "repository": "official/trusted",
                    "url": "https://github.com/official/trusted",
                    "revision": "1" * 40,
                }
            ]
            reject_untrusted_package_artifacts(source, packages, {"trusted": ("official/trusted", "high")})
            with self.assertRaisesRegex(VerificationError, "committed build artifact"):
                reject_untrusted_package_artifacts(source, packages, {})

    def test_path_dependency_with_custom_prebuilt_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            dependency = source / "vendor" / "helper"
            artifact = dependency / "prebuilt" / "Helper.trace"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("untrusted trace")
            (source / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {
                                "name": "helper",
                                "type": "path",
                                "dir": "vendor/helper",
                            }
                        ]
                    }
                )
            )
            with self.assertRaisesRegex(VerificationError, "committed build artifact"):
                materialize_packages(source, base_env={"PATH": "/usr/bin"})

    def test_path_dependency_lake_state_is_removed_before_artifact_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            dependency = source / "vendor" / "helper"
            stale = dependency / ".lake" / "build" / "lib" / "lean" / "Stale.olean"
            stale.parent.mkdir(parents=True)
            stale.write_text("stale object")
            (source / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {
                                "name": "helper",
                                "type": "path",
                                "dir": "vendor/helper",
                            }
                        ]
                    }
                )
            )
            materialize_packages(source, base_env={"PATH": "/usr/bin"})
            self.assertFalse(stale.exists())

    def test_report_and_tools_must_not_be_writable(self):
        with self.assertRaisesRegex(VerificationError, "sandbox-writable"):
            require_protected_paths(
                [Path("/source/.lake/build/comparator")],
                [Path("/source/.lake/build")],
            )
        require_protected_paths(
            [Path("/runner/report.json"), Path("/tools/comparator")],
            [Path("/source/.lake/build")],
        )

    def test_nonofficial_revision_is_rejected(self):
        remote = mock.Mock(returncode=0)
        fetch = mock.Mock(returncode=0)
        ancestry = mock.Mock(returncode=1)
        with mock.patch("scripts.verify_submission.run", side_effect=[remote, fetch, ancestry]) as run_mock:
            with self.assertRaisesRegex(VerificationError, "not an ancestor"):
                verify_official_revision(
                    Path("/source/.lake/packages/mathlib"),
                    repository="leanprover-community/mathlib4",
                    revision="1" * 40,
                    official_ref="refs/heads/master",
                    git_env={"PATH": "/usr/bin"},
                )
        remote_command = run_mock.call_args_list[0].args[0]
        fetch_command = run_mock.call_args_list[1].args[0]
        self.assertIn("https://github.com/leanprover-community/mathlib4", remote_command)
        self.assertIn("--filter=tree:0", fetch_command)
        self.assertIn(
            "+refs/heads/master:refs/remotes/palomar-official/head",
            fetch_command,
        )

    def test_explicit_legacy_revision_is_accepted_without_broadening_history(self):
        with mock.patch("scripts.verify_submission.run") as run_mock:
            verify_official_revision(
                Path("/source/.lake/packages/TauCeti"),
                repository="TauCetiProject/TauCeti",
                revision="2" * 40,
                official_ref="refs/heads/main",
                accepted_revisions=["2" * 40],
                git_env={"PATH": "/usr/bin"},
            )
        run_mock.assert_not_called()

    def test_writable_dependency_source_is_untrusted(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            writable = source / ".lake" / "packages" / "mathlib" / ".lake" / "build"
            writable.mkdir(parents=True)
            injected = writable / "Evil.lean"
            injected.write_text("def injected := True")
            (source / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {
                                "name": "mathlib",
                                "type": "git",
                                "url": "https://github.com/leanprover-community/mathlib4",
                                "rev": "1" * 40,
                            }
                        ]
                    }
                )
            )
            database = Path(directory) / "database"
            (database / "entries").mkdir(parents=True)
            audit = audit_challenge_sources(
                source,
                database=database,
                dependency_sources=[injected],
                lean_prefix=Path(directory) / "toolchain",
                allowlist={"mathlib": ("leanprover-community/mathlib4", "high")},
                indexed={},
                writable_directories=[writable],
            )
            self.assertEqual(audit["untrusted_sources"], [str(injected)])

    def test_indexed_snapshot_resolution_is_versioned_and_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory)
            entries = database / "entries"
            entries.mkdir()
            repository = "example/indexed"
            revision = "1" * 40
            records = [
                ("PALOMAR-2026-07-30-000003", 1, "2026-07-30"),
                ("PALOMAR-2026-07-29-000002", 2, "2026-07-29"),
                ("PALOMAR-2026-07-29-000002", 1, "2026-07-29"),
            ]
            for identifier, version, accepted_at in records:
                (entries / f"{identifier}-v{version}.json").write_text(
                    json.dumps(
                        {
                            "id": identifier,
                            "version": version,
                            "accepted_at": accepted_at,
                            "source": {"repository": repository, "commit": revision},
                        }
                    )
                )
            resolved = indexed_versions(database)[(repository, revision)]
            self.assertEqual(resolved["palomar_id"], "PALOMAR-2026-07-29-000002")
            self.assertEqual(resolved["palomar_version"], 1)
            self.assertEqual(resolved["revision"], revision)

    def test_indexed_challenge_source_has_versioned_review_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            package = source / ".lake" / "packages" / "indexed"
            package.mkdir(parents=True)
            dependency = package / "Indexed" / "Definitions.lean"
            dependency.parent.mkdir()
            dependency.write_text("def Indexed.answer : Nat := 42\n")
            subprocess.run(["git", "init", "-q"], cwd=package, check=True)
            subprocess.run(["git", "add", "."], cwd=package, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Palomar test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                cwd=package,
                check=True,
            )
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=package,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            (source / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {
                                "name": "indexed",
                                "type": "git",
                                "url": "https://github.com/example/indexed",
                                "rev": revision,
                            }
                        ]
                    }
                )
            )
            record = {
                "repository": "example/indexed",
                "revision": revision,
                "palomar_id": "PALOMAR-2026-07-29-000001",
                "palomar_version": 2,
            }
            audit = audit_challenge_sources(
                source,
                database=Path(directory) / "database",
                dependency_sources=[dependency],
                lean_prefix=Path(directory) / "toolchain",
                allowlist={},
                indexed={"indexed": record},
                writable_directories=[],
            )
            self.assertEqual(audit["untrusted_sources"], [])
            self.assertEqual(audit["trust_level"], "qualified")
            self.assertEqual(
                audit["dependencies"],
                [
                    {
                        "repository": "example/indexed",
                        "provenance": "palomar-indexed",
                        "palomar_id": "PALOMAR-2026-07-29-000001",
                        "palomar_version": 2,
                        "revision": revision,
                    }
                ],
            )
            evidence = audit["review_source_files"][0]
            self.assertEqual(evidence["path"], "Indexed/Definitions.lean")
            self.assertEqual(evidence["sha256"], verifier.sha256(dependency))

            substituted = dict(record, revision="2" * 40)
            bad = audit_challenge_sources(
                source,
                database=Path(directory) / "database",
                dependency_sources=[dependency],
                lean_prefix=Path(directory) / "toolchain",
                allowlist={},
                indexed={"indexed": substituted},
                writable_directories=[],
            )
            self.assertEqual(bad["untrusted_sources"], [str(dependency.resolve())])

            unindexed_package = source / ".lake" / "packages" / "unindexed"
            unindexed_package.mkdir()
            recursive = unindexed_package / "Unindexed.lean"
            recursive.write_text("def hiddenMeaning : Nat := 0\n")
            subprocess.run(["git", "init", "-q"], cwd=unindexed_package, check=True)
            subprocess.run(["git", "add", "."], cwd=unindexed_package, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Palomar test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ],
                cwd=unindexed_package,
                check=True,
            )
            unindexed_revision = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=unindexed_package,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            manifest = json.loads((source / "lake-manifest.json").read_text())
            manifest["packages"].append(
                {
                    "name": "unindexed",
                    "type": "git",
                    "url": "https://github.com/example/unindexed",
                    "rev": unindexed_revision,
                }
            )
            (source / "lake-manifest.json").write_text(json.dumps(manifest))
            recursive_audit = audit_challenge_sources(
                source,
                database=Path(directory) / "database",
                dependency_sources=[dependency, recursive],
                lean_prefix=Path(directory) / "toolchain",
                allowlist={},
                indexed={"indexed": record},
                writable_directories=[],
            )
            self.assertEqual(
                recursive_audit["untrusted_sources"],
                [str(recursive.resolve())],
            )

    def test_solution_only_package_is_outside_challenge_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            (source / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {
                                "name": "proofOnly",
                                "type": "git",
                                "url": "https://github.com/example/proof-only",
                                "rev": "3" * 40,
                            }
                        ]
                    }
                )
            )
            audit = audit_challenge_sources(
                source,
                database=Path(directory) / "database",
                dependency_sources=[],
                lean_prefix=Path(directory) / "toolchain",
                allowlist={},
                indexed={},
                writable_directories=[],
            )
            self.assertEqual(audit["untrusted_sources"], [])
            self.assertEqual(audit["dependencies"], [])

    def test_path_package_may_not_point_under_lake(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {
                                "name": "helper",
                                "type": "path",
                                "dir": ".lake/config",
                            }
                        ]
                    }
                )
            )
            with self.assertRaisesRegex(VerificationError, "may not live under .lake"):
                materialize_packages(source, base_env={"PATH": "/usr/bin"})

    def test_trusted_package_url_map_uses_verified_manifest_urls(self):
        packages = [
            {"name": "mathlib", "url": "https://github.com/leanprover-community/mathlib4"},
            {"name": "plausible", "url": "https://github.com/leanprover-community/plausible"},
            {"name": "candidate", "url": "https://github.com/example/candidate"},
        ]
        authoritative = [
            {"name": "mathlib", "url": "https://github.com/leanprover-community/mathlib4"},
            {"name": "plausible", "url": "https://github.com/leanprover-community/plausible"},
        ]
        self.assertEqual(
            json.loads(trusted_package_url_map(packages, authoritative)),
            {
                "mathlib": "https://github.com/leanprover-community/mathlib4",
                "plausible": "https://github.com/leanprover-community/plausible",
            },
        )
        packages[0]["url"] = "path:../mathlib"
        with self.assertRaisesRegex(VerificationError, "may not use a path dependency"):
            trusted_package_url_map(packages, authoritative[:1])
        packages[0]["url"] = "https://github.com/leanprover-community/mathlib4.git"
        self.assertEqual(
            json.loads(trusted_package_url_map(packages, authoritative[:1])),
            {"mathlib": "https://github.com/leanprover-community/mathlib4"},
        )
        packages[0]["url"] = "https://github.com/attacker/mathlib4"
        with self.assertRaisesRegex(VerificationError, "does not match"):
            trusted_package_url_map(packages, authoritative[:1])
        authoritative[0]["url"] = "git://github.com/leanprover-community/mathlib4"
        with self.assertRaisesRegex(VerificationError, "credential-free HTTPS"):
            trusted_package_url_map(packages, authoritative[:1])
        with self.assertRaisesRegex(VerificationError, "absent from the manifest"):
            trusted_package_url_map(packages, [{"name": "missing", "url": "https://example.com"}])

    def test_systemd_network_namespace_defaults_closed(self):
        def which(command):
            return f"/usr/bin/{command}" if command in {"systemd-run", "true"} else None

        with (
            mock.patch("scripts.verify_submission.shutil.which", side_effect=which),
            mock.patch("scripts.verify_submission.run", return_value=mock.Mock(returncode=0)),
            mock.patch("scripts.verify_submission._SYSTEMD_MANAGER", None),
        ):
            confined = systemd_command(["true"], cwd=Path("/source"), environment={})
            networked = systemd_command(
                ["true"],
                cwd=Path("/source"),
                environment={},
                unrestricted_network=True,
            )
        self.assertIn("--property=PrivateNetwork=yes", confined)
        self.assertNotIn("--property=PrivateNetwork=yes", networked)
        self.assertIn("--property=ProtectProc=invisible", confined)
        self.assertIn("--property=ProcSubset=pid", confined)
        self.assertIn("--property=NoNewPrivileges=yes", confined)
        self.assertIn("--property=PrivateDevices=yes", confined)
        self.assertIn("--property=RuntimeMaxSec=600s", confined)

    def test_systemd_prefers_privileged_manager_and_drops_to_runner_identity(self):
        def which(command):
            if command in {"systemd-run", "sudo", "true"}:
                return f"/usr/bin/{command}"
            return None

        with (
            mock.patch("scripts.verify_submission.shutil.which", side_effect=which),
            mock.patch("scripts.verify_submission.run", return_value=mock.Mock(returncode=0)),
            mock.patch("scripts.verify_submission._SYSTEMD_MANAGER", None),
            mock.patch("scripts.verify_submission.os.getuid", return_value=1001),
            mock.patch("scripts.verify_submission.os.getgid", return_value=1002),
        ):
            command = systemd_command(["true"], cwd=Path("/source"), environment={})

        self.assertEqual(command[:3], ["/usr/bin/sudo", "-n", "/usr/bin/systemd-run"])
        self.assertIn("--uid=1001", command)
        self.assertIn("--gid=1002", command)
        self.assertNotIn("--user", command)

    def test_systemd_falls_back_to_capable_user_manager(self):
        def which(command):
            if command in {"systemd-run", "sudo", "true"}:
                return f"/usr/bin/{command}"
            return None

        with (
            mock.patch("scripts.verify_submission.shutil.which", side_effect=which),
            mock.patch(
                "scripts.verify_submission.run",
                side_effect=[mock.Mock(returncode=1), mock.Mock(returncode=0)],
            ),
            mock.patch("scripts.verify_submission._SYSTEMD_MANAGER", None),
        ):
            command = systemd_command(["true"], cwd=Path("/source"), environment={})

        self.assertEqual(command[:2], ["/usr/bin/systemd-run", "--user"])
        self.assertNotIn("--uid=", " ".join(command))

    def test_systemd_rejects_incapable_managers_and_environment_controls(self):
        def which(command):
            if command in {"systemd-run", "sudo", "true"}:
                return f"/usr/bin/{command}"
            return None

        with (
            mock.patch("scripts.verify_submission.shutil.which", side_effect=which),
            mock.patch("scripts.verify_submission.run", return_value=mock.Mock(returncode=1)),
            mock.patch("scripts.verify_submission._SYSTEMD_MANAGER", None),
            self.assertRaisesRegex(VerificationError, "can apply the required confinement"),
        ):
            systemd_command(["true"], cwd=Path("/source"), environment={})

        with (
            mock.patch("scripts.verify_submission.shutil.which", side_effect=which),
            mock.patch("scripts.verify_submission.run", return_value=mock.Mock(returncode=0)),
            mock.patch("scripts.verify_submission._SYSTEMD_MANAGER", None),
            self.assertRaisesRegex(VerificationError, "invalid control character"),
        ):
            systemd_command(
                ["true"],
                cwd=Path("/source"),
                environment={"LAKE_PKG_URL_MAP": "bad\nvalue"},
            )

    def test_systemd_applies_trusted_resource_properties(self):
        def which(command):
            return f"/usr/bin/{command}" if command in {"systemd-run", "true"} else None

        with (
            mock.patch("scripts.verify_submission.shutil.which", side_effect=which),
            mock.patch("scripts.verify_submission.run", return_value=mock.Mock(returncode=0)),
            mock.patch("scripts.verify_submission._SYSTEMD_MANAGER", None),
        ):
            command = systemd_command(
                ["true"],
                cwd=Path("/source"),
                environment={},
                resource_properties=("MemoryMax=12G", "TasksMax=512"),
            )
        self.assertIn("--property=MemoryMax=12G", command)
        self.assertIn("--property=TasksMax=512", command)

    def test_lake_environment_uses_final_absolute_path_line(self):
        proc = mock.Mock(stdout="untrusted Lake diagnostic\n/first:/second\n")
        with mock.patch("scripts.verify_submission.sandboxed_run", return_value=proc):
            value = lake_environment_value(
                "LEAN_PATH",
                source=Path("/source"),
                lake=Path("/tools/lake"),
                printenv=Path("/usr/bin/printenv"),
                environment={},
                landrun=Path("/tools/landrun"),
                writable_directories=[],
                readable_paths=[Path("/source")],
                executable_paths=[],
                tools={},
                allowed_roots=[Path("/")],
            )
        self.assertEqual(value, "/first:/second")

    def test_official_manifest_closure_rejects_substitution(self):
        good_revision = "1" * 40
        bad_revision = "2" * 40
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            mathlib = source / ".lake" / "packages" / "mathlib"
            mathlib.mkdir(parents=True)
            (mathlib / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {
                                "name": "batteries",
                                "type": "git",
                                "url": "https://github.com/leanprover-community/batteries",
                                "rev": good_revision,
                            }
                        ]
                    }
                )
            )
            packages = [
                {
                    "name": "mathlib",
                    "repository": "leanprover-community/mathlib4",
                    "url": "https://github.com/leanprover-community/mathlib4",
                    "revision": good_revision,
                },
                {
                    "name": "batteries",
                    "repository": "attacker/batteries",
                    "url": "https://github.com/attacker/batteries",
                    "revision": bad_revision,
                },
            ]
            with mock.patch("scripts.verify_submission.verify_official_revision"):
                with self.assertRaisesRegex(VerificationError, "substitutes"):
                    package_allowlist(source, packages, base_env={"PATH": "/usr/bin"})


if __name__ == "__main__":
    unittest.main()
