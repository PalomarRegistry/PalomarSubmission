import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ACTIONS_BASH = ["bash", "--noprofile", "--norc", "-eo", "pipefail"]
EXEMPT_PROSE_PATHS = (
    "README.md",
    "SECURITY.md",
    "LICENSE",
    "docs/comparator-declaration-closure.md",
    "docs/launch-security-review.md",
    "docs/mathlib-cache-trust.md",
    "taxonomies/README.md",
    "taxonomies/LICENSE.md",
)


class ColdBuildWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = REPOSITORY_ROOT / ".github" / "workflows" / "compatibility.yml"
        cls.workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        cls.scope_script = cls.step("scope", "scope")["run"]
        cls.gate_script = cls.step("compatibility", "gate")["run"]

    @classmethod
    def step(cls, job, identifier):
        return next(
            step
            for step in cls.workflow["jobs"][job]["steps"]
            if step.get("id") == identifier
        )

    @staticmethod
    def git(repository, environment, *arguments, output=False):
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=output,
            env=environment,
            text=True,
        )
        return result.stdout.strip() if output else None

    @staticmethod
    def write(repository, relative, text="content\n"):
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def commit(self, repository, environment, message):
        self.git(repository, environment, "add", "--all")
        self.git(
            repository,
            environment,
            "-c",
            "user.name=Palomar test",
            "-c",
            "user.email=test@palomar.invalid",
            "commit",
            "--quiet",
            "-m",
            message,
        )
        return self.git(repository, environment, "rev-parse", "HEAD", output=True)

    @staticmethod
    def run_actions_script(script, directory, environment, name):
        path = directory / name
        path.write_text(script)
        return subprocess.run(
            [*ACTIONS_BASH, str(path)],
            cwd=directory,
            env=environment,
            text=True,
            check=False,
            capture_output=True,
        )

    def run_scope(
        self,
        changed_paths=(),
        *,
        base_advance_paths=(),
        deleted_paths=(),
        renames=(),
        event="pull_request",
        broken_diff=False,
    ):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            home = repository / "isolated-home"
            home.mkdir()
            global_config = repository / "isolated-gitconfig"
            global_config.write_text("")
            environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith("GIT_CONFIG_")
            } | {
                "GIT_CONFIG_GLOBAL": str(global_config),
                "GIT_CONFIG_NOSYSTEM": "1",
                "HOME": str(home),
            }
            self.git(repository, environment, "init", "--quiet")
            self.write(repository, "seed")
            for relative in deleted_paths:
                self.write(repository, relative)
            for source, _ in renames:
                self.write(repository, source)
            base = self.commit(repository, environment, "base")

            for relative in changed_paths:
                self.write(repository, relative, "changed\n")
            for relative in deleted_paths:
                (repository / relative).unlink()
            for source, destination in renames:
                target = repository / destination
                target.parent.mkdir(parents=True, exist_ok=True)
                (repository / source).rename(target)
            if changed_paths or deleted_paths or renames:
                head = self.commit(repository, environment, "change")
            else:
                head = base

            if base_advance_paths:
                self.git(repository, environment, "checkout", "--quiet", "--detach", base)
                for relative in base_advance_paths:
                    self.write(repository, relative, "base advance\n")
                base = self.commit(repository, environment, "base advance")

            runner_temp = repository / "runner-temp"
            runner_temp.mkdir()
            output = runner_temp / "github-output"
            environment |= {
                "BASE_SHA": "not-a-commit" if broken_diff else base,
                "GITHUB_EVENT_NAME": event,
                "GITHUB_OUTPUT": str(output),
                "HEAD_SHA": head,
                "RUNNER_TEMP": str(runner_temp),
            }
            result = self.run_actions_script(
                self.scope_script,
                runner_temp,
                environment,
                "scope.sh",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return output.read_text().strip()

    def run_gate(self, scope_result, cold_build_requested, cold_build_result):
        environment = os.environ | {
            "COLD_BUILD_REQUESTED": cold_build_requested,
            "COLD_BUILD_RESULT": cold_build_result,
            "SCOPE_RESULT": scope_result,
        }
        with tempfile.TemporaryDirectory() as directory:
            return self.run_actions_script(
                self.gate_script,
                Path(directory),
                environment,
                "gate.sh",
            )

    def test_only_the_exact_current_prose_paths_skip_the_cold_build(self):
        pattern = next(
            line.strip().removesuffix(")")
            for line in self.scope_script.splitlines()
            if line.strip().startswith("README.md|")
        )
        self.assertEqual(pattern.split("|"), list(EXEMPT_PROSE_PATHS))
        for path in EXEMPT_PROSE_PATHS:
            with self.subTest(path):
                self.assertEqual(self.run_scope((path,)), "cold_build=false")
        self.assertEqual(self.run_scope(("docs/new-note.md",)), "cold_build=true")
        self.assertEqual(self.run_scope(("docs/design.txt",)), "cold_build=true")

    def test_each_current_non_prose_input_runs_the_cold_build(self):
        isolated_git = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_CONFIG_")
        } | {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        tracked = self.git(
            REPOSITORY_ROOT, isolated_git, "ls-files", output=True
        ).splitlines()
        behavioral = [path for path in tracked if path not in EXEMPT_PROSE_PATHS]
        for path in behavioral:
            with self.subTest(path):
                self.assertEqual(self.run_scope((path,)), "cold_build=true")

    def test_unknown_mixed_and_space_containing_paths_are_conservative(self):
        self.assertEqual(self.run_scope(("new-tool.lock",)), "cold_build=true")
        self.assertEqual(
            self.run_scope(("README.md", "scripts/new verifier.py")),
            "cold_build=true",
        )
        self.assertEqual(self.run_scope(("docs/design notes.md",)), "cold_build=true")
        self.assertEqual(self.run_scope(("new tool.lock",)), "cold_build=true")

    def test_deletions_and_behavioral_to_docs_renames_are_conservative(self):
        self.assertEqual(
            self.run_scope(deleted_paths=("scripts/removed.py",)),
            "cold_build=true",
        )
        self.assertEqual(
            self.run_scope(deleted_paths=("docs/mathlib-cache-trust.md",)),
            "cold_build=false",
        )
        self.assertEqual(
            self.run_scope(renames=(("scripts/verifier.py", "docs/verifier.md"),)),
            "cold_build=true",
        )
        self.assertIn("--no-renames", self.scope_script)

    def test_three_dot_diff_ignores_behavioral_changes_added_only_to_the_base(self):
        self.assertEqual(
            self.run_scope(
                ("README.md",),
                base_advance_paths=("scripts/base-only-change.py",),
            ),
            "cold_build=false",
        )

    def test_manual_runs_and_failed_diffs_run_the_cold_build(self):
        self.assertIn("workflow_dispatch", self.workflow["on"])
        self.assertEqual(
            self.workflow["jobs"]["cold_build"]["if"],
            "needs.scope.outputs.cold_build == 'true'",
        )
        self.assertIn('"$BASE_SHA...$HEAD_SHA"', self.scope_script)
        for event in ("workflow_dispatch", "schedule"):
            with self.subTest(event):
                self.assertEqual(self.run_scope(event=event), "cold_build=true")
        self.assertEqual(
            self.run_scope(("README.md",), broken_diff=True),
            "cold_build=true",
        )

    def test_template_contract_is_pinned_on_prs_and_live_drift_is_scheduled(self):
        ci_path = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
        ci = yaml.load(ci_path.read_text(), Loader=yaml.BaseLoader)
        pinned_step = next(
            step
            for step in ci["jobs"]["test"]["steps"]
            if step.get("name") == "Match the current PalomarTemplate authoring contracts"
        )
        template_commit = "d720f59dbe2edd29e0b9273c113139cdb1f24d2b"
        self.assertIn(f"/{template_commit}/formalization.yaml", pinned_step["run"])
        self.assertIn(f"/{template_commit}/comparator.json", pinned_step["run"])
        self.assertIn("cmp tests/fixtures/palomar-template-comparator.json", pinned_step["run"])
        self.assertIn("load_comparator_config", pinned_step["run"])
        self.assertNotIn("/main/formalization.yaml", pinned_step["run"])
        self.assertNotIn("/main/comparator.json", pinned_step["run"])

        drift_step = self.step("scope", "template")
        python_step = self.step("scope", "template-python")
        dependency_step = self.step("scope", "template-dependencies")
        self.assertEqual(
            python_step["uses"],
            "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
        )
        self.assertEqual(python_step["with"]["python-version"], "3.11.10")
        self.assertEqual(python_step["if"], "github.event_name != 'pull_request'")
        self.assertEqual(dependency_step["if"], "github.event_name != 'pull_request'")
        self.assertIn("--require-hashes", dependency_step["run"])
        self.assertIn("--no-deps -r requirements.txt", dependency_step["run"])
        self.assertEqual(drift_step["if"], "github.event_name != 'pull_request'")
        self.assertIn("/main/formalization.yaml", drift_step["run"])
        self.assertIn("/main/comparator.json", drift_step["run"])
        self.assertIn("load_comparator_config", drift_step["run"])
        self.assertIn("cmp tests/fixtures/palomar-template-formalization.yaml", drift_step["run"])
        self.assertIn("cmp tests/fixtures/palomar-template-comparator.json", drift_step["run"])

        scope_steps = self.workflow["jobs"]["scope"]["steps"]
        self.assertLess(scope_steps.index(python_step), scope_steps.index(drift_step))
        self.assertLess(scope_steps.index(dependency_step), scope_steps.index(drift_step))

        fixture_step = next(
            step
            for step in self.workflow["jobs"]["cold_build"]["steps"]
            if step.get("name") == "Prepare the checked multi-dependency fixture"
        )
        self.assertIn("cp -R pipeline/tests/fixtures/cold-tauceti fixture", fixture_step["run"])
        self.assertIn("git -C fixture init --quiet", fixture_step["run"])
        self.assertIn("git -C fixture add --all", fixture_step["run"])
        self.assertIn("commit --quiet", fixture_step["run"])
        self.assertIn(
            'fixture/lean-toolchain)" = "leanprover/lean4:v4.31.0-rc2"',
            fixture_step["run"],
        )

        fixture = REPOSITORY_ROOT / "tests/fixtures/palomar-template-comparator.json"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    "from scripts.verify_submission import load_comparator_config; "
                    f"load_comparator_config(Path({str(fixture)!r}))"
                ),
            ],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_checked_cold_fixture_reaches_the_exact_tauceti_closure(self):
        fixture = REPOSITORY_ROOT / "tests/fixtures/cold-tauceti"
        for path in sorted(fixture.iterdir()):
            with self.subTest(classified_path=path.name):
                relative = path.relative_to(REPOSITORY_ROOT).as_posix()
                self.assertEqual(self.run_scope((relative,)), "cold_build=true")
        self.assertEqual(
            (fixture / "lean-toolchain").read_text().strip(),
            "leanprover/lean4:v4.31.0-rc2",
        )

        config = json.loads((fixture / "comparator.json").read_text())
        self.assertIs(config["enable_nanoda"], True)
        self.assertEqual(config["challenge_module"], "Challenge")
        self.assertEqual(config["solution_module"], "Solution")
        self.assertEqual(
            config["theorem_names"],
            ["PalomarColdTauCetiFixture.dependencyClosure"],
        )

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; "
                    "from scripts.verify_submission import load_comparator_config; "
                    f"load_comparator_config(Path({str(fixture / 'comparator.json')!r}))"
                ),
            ],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        manifest = json.loads((fixture / "lake-manifest.json").read_text())
        revisions = {
            package["name"]: package["rev"] for package in manifest["packages"]
        }
        self.assertEqual(
            revisions,
            {
                "TauCeti": "221bb56a017bb794421eac4fa543d7a5e85add75",
                "mathlib": "0be66d77ba290828a5260d883ace636f56bce89a",
                "plausible": "744117af710b1c0400cd297c9ce91f8d0ad3a347",
                "LeanSearchClient": "c5d5b8fe6e5158def25cd28eb94e4141ad97c843",
                "importGraph": "99c763c8a96d3d44fb4994e96eaa51ca4568449d",
                "proofwidgets": "b2da7698bdf22804095ea5b5007f23c09398f687",
                "aesop": "7897ea6e5cfc6522d355083bdfa798377ab35e11",
                "Qq": "94346b7b49c36ae871639d1434232f057c193d60",
                "batteries": "c6f7103faab35720af56784a9553733832f17349",
                "Cli": "baf3e62fbb3502305076ca077e004aea78157c63",
            },
        )
        tauceti = next(
            package for package in manifest["packages"] if package["name"] == "TauCeti"
        )
        self.assertEqual(tauceti["url"], "https://github.com/FormalFrontier/TauCeti")
        self.assertEqual(tauceti["inherited"], False)
        lakefile = (fixture / "lakefile.toml").read_text()
        self.assertIn('name = "TauCeti"', lakefile)
        self.assertIn('git = "https://github.com/FormalFrontier/TauCeti"', lakefile)
        self.assertIn(
            'rev = "221bb56a017bb794421eac4fa543d7a5e85add75"', lakefile
        )
        self.assertNotIn("import TauCeti", (fixture / "Challenge.lean").read_text())
        self.assertIn("import Mathlib", (fixture / "Challenge.lean").read_text())
        self.assertIn("import TauCeti", (fixture / "Solution.lean").read_text())

        cold_steps = self.workflow["jobs"]["cold_build"]["steps"]
        install_step = next(
            step for step in cold_steps if step.get("name") == "Install pinned Landrun and Lean"
        )
        build_step = next(
            step
            for step in cold_steps
            if step.get("name") == "Build pinned Comparator, lean4export, and NanoDa"
        )
        exercise_step = next(
            step
            for step in cold_steps
            if step.get("name") == "Exercise cold trusted and candidate builds"
        )
        self.assertIn(
            "toolchain install leanprover/lean4:v4.31.0-rc2", install_step["run"]
        )
        self.assertIn(
            'supported_toolchain("leanprover/lean4:v4.31.0-rc2")',
            build_step["run"],
        )
        self.assertEqual(
            exercise_step["env"]["ELAN_TOOLCHAIN"],
            "leanprover/lean4:v4.31.0-rc2",
        )
        self.assertIn("--source fixture", exercise_step["run"])

    def test_required_gate_passes_only_the_two_valid_outcomes(self):
        prose = self.run_gate("success", "false", "skipped")
        built = self.run_gate("success", "true", "success")
        self.assertEqual(prose.returncode, 0, prose.stderr)
        self.assertIn("confined to reviewed prose paths", prose.stdout)
        self.assertEqual(built.returncode, 0, built.stderr)
        self.assertIn("required cold build succeeded", built.stdout)

        for case in (
            ("failure", "", "skipped"),
            ("cancelled", "", "skipped"),
            ("success", "true", "failure"),
            ("success", "true", "cancelled"),
            ("success", "true", "skipped"),
            ("success", "false", "success"),
            ("success", "", "skipped"),
        ):
            with self.subTest(case):
                result = self.run_gate(*case)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("::error::", result.stdout)

    def test_required_gate_always_runs_and_the_workflow_has_no_path_filter(self):
        gate = self.workflow["jobs"]["compatibility"]
        self.assertEqual(gate["needs"], ["scope", "cold_build"])
        self.assertEqual(gate["if"], "always()")
        self.assertEqual(self.workflow["jobs"]["scope"]["timeout-minutes"], "5")
        self.assertEqual(self.workflow["on"]["pull_request"], "")
        self.assertEqual(self.step("scope", "scope")["shell"], "bash")
        self.assertEqual(self.step("compatibility", "gate")["shell"], "bash")
        self.assertEqual(self.workflow["on"]["schedule"], [{"cron": "17 3 * * 1"}])


if __name__ == "__main__":
    unittest.main()
