import argparse
import hashlib
import html
import json
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from scripts.render_challenge import (
    RUNTIME_SANITIZER,
    VERSO_RUNTIME,
    artifact_manifest,
    download_mathlib_cache,
    execute,
    extract_module_doc,
    hydrate_mathlib_cache,
    merge_renderer_manifest,
    parsed_challenge_metadata,
    parser,
    prepare,
    prepare_workspace,
    sanitize_bundle,
    static_html_sanitize,
    toolchain_verso_commit,
    trusted_lakefile,
    verify_filesystem_confinement,
)
from scripts.render_report import AcceptedRenderPaths
from scripts.verification_errors import VerificationError


class RenderChallengeTests(unittest.TestCase):
    def test_missing_mathlib_cache_is_an_optimization_miss(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            environment = {"HOME": str(root / "home"), "TMPDIR": str(root / "tmp")}
            with mock.patch(
                "scripts.render_challenge.run",
                return_value=subprocess.CompletedProcess([], 22, "", "404"),
            ), mock.patch(
                "scripts.render_challenge.systemd_command",
                side_effect=lambda command, **_kwargs: command,
            ):
                downloaded, size = download_mathlib_cache(
                    {"0123456789abcdef"},
                    cache,
                    trusted_work=root,
                    environment=environment,
                    curl=Path("/usr/bin/curl"),
                    env_tool=Path("/usr/bin/env"),
                )
        self.assertEqual((downloaded, size), (0, 0))

    def test_partial_mathlib_cache_is_used_and_the_build_may_fill_the_rest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            environment = {"HOME": str(root / "home"), "TMPDIR": str(root / "tmp")}

            def download(*_args, **_kwargs):
                (cache / "0123456789abcdef.ltar").write_bytes(b"archive")
                return subprocess.CompletedProcess([], 22, "", "one archive was missing")

            with mock.patch(
                "scripts.render_challenge.run", side_effect=download
            ), mock.patch(
                "scripts.render_challenge.systemd_command",
                side_effect=lambda command, **_kwargs: command,
            ):
                downloaded, size = download_mathlib_cache(
                    {"0123456789abcdef", "fedcba9876543210"},
                    cache,
                    trusted_work=root,
                    environment=environment,
                    curl=Path("/usr/bin/curl"),
                    env_tool=Path("/usr/bin/env"),
                )
        self.assertEqual((downloaded, size), (1, len(b"archive")))

    def test_zero_cache_downloads_skip_unpack_and_leave_source_build_to_follow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / ".lake" / "config" / "mathlib-cache"
            cache.mkdir(parents=True)
            with (
                mock.patch(
                    "scripts.render_challenge.discover_mathlib_cache_hashes",
                    return_value=({"0123456789abcdef"}, cache),
                ),
                mock.patch(
                    "scripts.render_challenge.download_mathlib_cache",
                    return_value=(0, 0),
                ),
                mock.patch("scripts.render_challenge.sandboxed_run") as sandbox,
            ):
                result = hydrate_mathlib_cache(
                    root,
                    trusted_work=root,
                    environment={"HOME": str(root), "TMPDIR": str(root)},
                    lake=Path("/tools/lake"),
                    landrun=Path("/tools/landrun"),
                    curl=Path("/usr/bin/curl"),
                    env_tool=Path("/usr/bin/env"),
                    writable_directories=[],
                    readable_paths=[],
                    executable_paths=[],
                    tools={},
                )
        self.assertEqual(result, {"requested": 1, "downloaded": 0, "bytes": 0})
        sandbox.assert_not_called()

    def test_renderer_confinement_cleans_probe_artifacts_on_runner_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writable = root / "writable"
            writable.mkdir()
            denied = root / "render-write-denied"
            allowed = writable / ".palomar-landrun-write-probe"

            def fail_after_creation(command, **_kwargs):
                path = Path(command[-1])
                path.touch()
                if path == allowed:
                    return subprocess.CompletedProcess(command, 0, "", "")
                raise VerificationError("renderer probe runner failed")

            with (
                mock.patch(
                    "scripts.verify_submission.sandboxed_run",
                    side_effect=fail_after_creation,
                ),
                self.assertRaisesRegex(
                    VerificationError, "renderer probe runner failed"
                ),
            ):
                verify_filesystem_confinement(
                    denied,
                    touch=Path("/usr/bin/touch"),
                    cwd=root,
                    environment={},
                    landrun=Path("/tools/landrun"),
                    writable_directories=[writable],
                    readable_paths=[],
                    executable_paths=[],
                    tools={},
                )

            self.assertFalse(allowed.exists())
            self.assertFalse(denied.exists())

    def test_renderer_confinement_rejects_a_created_denied_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writable = root / "writable"
            writable.mkdir()
            denied = root / "render-write-denied"
            allowed = writable / ".palomar-landrun-write-probe"

            def escape_write(command, **_kwargs):
                path = Path(command[-1])
                path.touch()
                return subprocess.CompletedProcess(
                    command,
                    0 if path == allowed else 1,
                    "",
                    "",
                )

            with (
                mock.patch(
                    "scripts.verify_submission.sandboxed_run",
                    side_effect=escape_write,
                ),
                self.assertRaisesRegex(
                    VerificationError,
                    "outer Landrun filesystem policy was not enforced",
                ),
            ):
                verify_filesystem_confinement(
                    denied,
                    touch=Path("/usr/bin/touch"),
                    cwd=root,
                    environment={},
                    landrun=Path("/tools/landrun"),
                    writable_directories=[writable],
                    readable_paths=[],
                    executable_paths=[],
                    tools={},
                )

            self.assertFalse(allowed.exists())
            self.assertFalse(denied.exists())

    def test_workspace_uses_accepted_paths_without_touching_fixed_name_decoys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            project = source / "project"
            accepted = project / "accepted"
            accepted.mkdir(parents=True)
            challenge = accepted / "Task.lean"
            solution = accepted / "Answer.lean"
            comparator = accepted / "settings.json"
            challenge.write_bytes(b"accepted challenge")
            solution.write_bytes(b"accepted solution")
            comparator.write_text(
                json.dumps(
                    {
                        "challenge_module": "accepted.Task",
                        "solution_module": "accepted.Answer",
                        "theorem_names": ["accepted.headline"],
                        "definition_names": [],
                        "permitted_axioms": [],
                        "enable_nanoda": True,
                    }
                )
            )
            (source / "lean-toolchain").write_text("leanprover/lean4:v4.31.0-rc2\n")
            (project / "lake-manifest.json").write_text(
                json.dumps({"version": "1.2.0", "packages": []})
            )

            external = root / "external"
            external.write_bytes(b"do not replace")
            (project / "Challenge.lean").symlink_to(external)
            external_comparator = root / "external-comparator"
            external_comparator.write_bytes(b"also do not replace")
            (project / "comparator.json").symlink_to(external_comparator)
            hostile_solution = project / "Solution.lean"
            hostile_solution.mkdir()
            (hostile_solution / "payload").write_bytes(b"hostile directory")

            work = root / "work"
            work.mkdir()
            workspace = root / "workspace"
            accepted_paths = AcceptedRenderPaths(
                project_path="project",
                challenge_path="project/accepted/Task.lean",
                solution_path="project/accepted/Answer.lean",
                comparator_config_path="project/accepted/settings.json",
                lakefile_path="project/lakefile.toml",
                lean_toolchain_path="lean-toolchain",
            )

            def clone_verso(_url, _commit, destination):
                destination.mkdir()
                (destination / "lean-toolchain").write_text(
                    "leanprover/lean4:v4.31.0-rc2\n"
                )
                (destination / "lake-manifest.json").write_text(
                    json.dumps({"version": "1.2.0", "packages": []})
                )

            with mock.patch("scripts.render_challenge.clone_commit", side_effect=clone_verso):
                render_workspace = prepare_workspace(
                    source, workspace, "3" * 40, work, accepted_paths
                )

            self.assertEqual(external.read_bytes(), b"do not replace")
            self.assertEqual(external_comparator.read_bytes(), b"also do not replace")
            self.assertTrue((render_workspace.project / "Challenge.lean").is_symlink())
            self.assertTrue((render_workspace.project / "comparator.json").is_symlink())
            self.assertTrue((render_workspace.project / "Solution.lean").is_dir())
            self.assertEqual(
                render_workspace.challenge.read_bytes(), b"accepted challenge"
            )
            self.assertEqual(render_workspace.solution.read_bytes(), b"accepted solution")
            self.assertEqual(render_workspace.comparator.read_bytes(), comparator.read_bytes())
            self.assertEqual(render_workspace.challenge_module, "accepted.Task")

    def test_workspace_preserves_nested_module_identity_and_private_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            project = source / "project"
            module_dir = project / "src" / "Audit"
            module_dir.mkdir(parents=True)
            challenge = module_dir / "Task.lean"
            challenge.write_text(
                """namespace Audit.Task
private theorem modulePrivate : True := by trivial
theorem headline : True := modulePrivate
end Audit.Task
""",
                encoding="utf-8",
            )
            solution = module_dir / "Answer.lean"
            solution.write_text(
                "namespace Audit.Task\ntheorem headline : True := by trivial\nend Audit.Task\n",
                encoding="utf-8",
            )
            comparator = project / "settings.json"
            comparator.write_text(
                json.dumps(
                    {
                        "challenge_module": "Audit.Task",
                        "solution_module": "Audit.Answer",
                        "theorem_names": ["Audit.Task.headline"],
                        "definition_names": [],
                        "permitted_axioms": [],
                        "enable_nanoda": True,
                    }
                ),
                encoding="utf-8",
            )
            (project / "lake-manifest.json").write_text(
                json.dumps({"version": "1.2.0", "packages": []})
            )
            (source / "lean-toolchain").write_text(
                "leanprover/lean4:v4.31.0-rc2\n"
            )
            work = root / "work"
            work.mkdir()

            def clone_verso(_url, _commit, destination):
                destination.mkdir()
                (destination / "lean-toolchain").write_text(
                    "leanprover/lean4:v4.31.0-rc2\n"
                )
                (destination / "lake-manifest.json").write_text(
                    json.dumps({"version": "1.2.0", "packages": []})
                )

            with mock.patch(
                "scripts.render_challenge.clone_commit", side_effect=clone_verso
            ):
                render_workspace = prepare_workspace(
                    source,
                    root / "workspace",
                    "3" * 40,
                    work,
                    AcceptedRenderPaths(
                        project_path="project",
                        challenge_path="project/src/Audit/Task.lean",
                        solution_path="project/src/Audit/Answer.lean",
                        comparator_config_path="project/settings.json",
                        lakefile_path="project/lakefile.toml",
                        lean_toolchain_path="lean-toolchain",
                    ),
                )

            self.assertEqual(render_workspace.challenge_module, "Audit.Task")
            self.assertEqual(
                render_workspace.challenge.relative_to(render_workspace.project).as_posix(),
                "src/Audit/Task.lean",
            )
            self.assertIn("private theorem modulePrivate", render_workspace.challenge.read_text())
            self.assertFalse((render_workspace.project / "Challenge.lean").exists())
            lakefile = (render_workspace.project / "lakefile.toml").read_text()
            self.assertIn('srcDir = "src"', lakefile)
            self.assertIn('roots = ["Audit.Task"]', lakefile)

    def test_workspace_revalidates_source_project_symlink_components(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            real_project = source / "real" / "project"
            real_project.mkdir(parents=True)
            (source / "linked").symlink_to(source / "real", target_is_directory=True)
            work = root / "work"
            work.mkdir()

            with self.assertRaisesRegex(
                VerificationError, "render project_path contains a symlinked path component"
            ):
                prepare_workspace(
                    source,
                    root / "workspace",
                    "3" * 40,
                    work,
                    AcceptedRenderPaths(
                        project_path="linked/project",
                        challenge_path="linked/project/Challenge.lean",
                        solution_path="linked/project/Solution.lean",
                        comparator_config_path="linked/project/comparator.json",
                        lakefile_path="linked/project/lakefile.toml",
                        lean_toolchain_path="lean-toolchain",
                    ),
                )

    def test_workspace_revalidates_project_components_after_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            project = source / "nested" / "project"
            project.mkdir(parents=True)
            (project / "Challenge.lean").write_text("theorem challenge : True := by trivial\n")
            (project / "Solution.lean").write_text("theorem challenge : True := by trivial\n")
            (project / "comparator.json").write_text(
                json.dumps(
                    {
                        "challenge_module": "Challenge",
                        "solution_module": "Solution",
                        "theorem_names": ["challenge"],
                        "definition_names": [],
                        "permitted_axioms": [],
                        "enable_nanoda": True,
                    }
                )
            )
            (project / "lake-manifest.json").write_text(
                json.dumps({"version": "1.2.0", "packages": []})
            )
            (source / "lean-toolchain").write_text("leanprover/lean4:v4.31.0-rc2\n")
            work = root / "work"
            work.mkdir()
            workspace = root / "workspace"
            accepted_paths = AcceptedRenderPaths(
                project_path="nested/project",
                challenge_path="nested/project/Challenge.lean",
                solution_path="nested/project/Solution.lean",
                comparator_config_path="nested/project/comparator.json",
                lakefile_path="nested/project/lakefile.toml",
                lean_toolchain_path="lean-toolchain",
            )

            def clone_verso(_url, _commit, destination):
                destination.mkdir()
                (destination / "lean-toolchain").write_text(
                    "leanprover/lean4:v4.31.0-rc2\n"
                )
                (destination / "lake-manifest.json").write_text(
                    json.dumps({"version": "1.2.0", "packages": []})
                )

            real_copytree = shutil.copytree

            def copy_with_workspace_symlink(*args, **kwargs):
                # Restore the real module attribute during recursion: patching the
                # renderer's imported module also patches shutil.copytree itself.
                with mock.patch("shutil.copytree", real_copytree):
                    result = real_copytree(*args, **kwargs)
                nested = workspace / "nested"
                relocated = workspace / "relocated"
                nested.rename(relocated)
                nested.symlink_to(relocated.name, target_is_directory=True)
                return result

            with (
                mock.patch(
                    "scripts.render_challenge.clone_commit", side_effect=clone_verso
                ),
                mock.patch(
                    "scripts.render_challenge.shutil.copytree",
                    side_effect=copy_with_workspace_symlink,
                ),
                self.assertRaisesRegex(
                    VerificationError,
                    "render workspace project_path contains a symlinked path component",
                ),
            ):
                prepare_workspace(source, workspace, "3" * 40, work, accepted_paths)

    def test_prepare_binds_a_nested_project_and_configured_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "template"
            project = template / "examples" / "headline"
            sources = project / "Audit"
            sources.mkdir(parents=True)
            challenge = sources / "Task.lean"
            challenge.write_text("theorem headline : True := by trivial\n", encoding="utf-8")
            (sources / "Answer.lean").write_text(
                "theorem headline : True := by trivial\n", encoding="utf-8"
            )
            (project / "settings.json").write_text(
                json.dumps(
                    {
                        "challenge_module": "Audit.Task",
                        "solution_module": "Audit.Answer",
                        "theorem_names": ["headline"],
                        "definition_names": [],
                        "permitted_axioms": [],
                        "enable_nanoda": True,
                    }
                ),
                encoding="utf-8",
            )
            (project / "lakefile.toml").write_text(
                'name = "headline"\n[[lean_lib]]\nname = "Audit"\n', encoding="utf-8"
            )
            (template / "lean-toolchain").write_text(
                "leanprover/lean4:v4.31.0-rc2\n", encoding="utf-8"
            )
            output = root / "report.json"
            args = argparse.Namespace(
                repository="example/project",
                commit="1" * 40,
                challenge_sha256=hashlib.sha256(challenge.read_bytes()).hexdigest(),
                project_path="examples/headline",
                challenge_path="examples/headline/Audit/Task.lean",
                solution_path="examples/headline/Audit/Answer.lean",
                comparator_config_path="examples/headline/settings.json",
                lakefile_path="examples/headline/lakefile.toml",
                lean_toolchain_path="lean-toolchain",
                work_dir=str(root / "work"),
                output=str(output),
            )

            with mock.patch(
                "scripts.render_challenge.clone_commit",
                side_effect=lambda _url, _commit, destination: shutil.copytree(
                    template, destination
                ),
            ):
                self.assertEqual(prepare(args), 0)

            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["schema_version"], 2)
            self.assertEqual(report["status"], "pending")
            self.assertEqual(
                report["source"],
                {
                    "repository": "example/project",
                    "repository_url": "https://github.com/example/project",
                    "commit": "1" * 40,
                    "challenge_sha256": args.challenge_sha256,
                    "project_path": "examples/headline",
                    "challenge_path": "examples/headline/Audit/Task.lean",
                    "solution_path": "examples/headline/Audit/Answer.lean",
                    "comparator_config_path": "examples/headline/settings.json",
                    "lakefile_path": "examples/headline/lakefile.toml",
                    "lean_toolchain_path": "lean-toolchain",
                },
            )

    def test_prepare_refuses_an_incomplete_accepted_path_set_before_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "report.json"
            args = argparse.Namespace(
                repository="example/project",
                commit="1" * 40,
                challenge_sha256="2" * 64,
                project_path="",
                challenge_path="",
                solution_path="Solution.lean",
                comparator_config_path="comparator.json",
                lakefile_path="lakefile.toml",
                lean_toolchain_path="lean-toolchain",
                work_dir=str(root / "work"),
                output=str(output),
            )

            with mock.patch("scripts.render_challenge.clone_commit") as clone:
                self.assertEqual(prepare(args), 0)

            clone.assert_not_called()
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["schema_version"], 2)
            self.assertEqual(report["status"], "error")
            self.assertEqual(report["stage"], "intake")
            self.assertIn(
                "render report source.challenge_path must be a nonempty string",
                report["errors"],
            )

    def test_prepare_preserves_an_explicit_root_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "template"
            template.mkdir()
            challenge = template / "Challenge.lean"
            challenge.write_text("theorem headline : True := by trivial\n", encoding="utf-8")
            (template / "Solution.lean").write_text(
                "theorem headline : True := by trivial\n", encoding="utf-8"
            )
            (template / "comparator.json").write_text(
                json.dumps(
                    {
                        "challenge_module": "Challenge",
                        "solution_module": "Solution",
                        "theorem_names": ["headline"],
                        "definition_names": [],
                        "permitted_axioms": [],
                        "enable_nanoda": True,
                    }
                ),
                encoding="utf-8",
            )
            (template / "lakefile.toml").write_text(
                'name = "headline"\n[[lean_lib]]\nname = "Headline"\n', encoding="utf-8"
            )
            (template / "lean-toolchain").write_text(
                "leanprover/lean4:v4.31.0-rc2\n", encoding="utf-8"
            )
            output = root / "report.json"
            args = argparse.Namespace(
                repository="example/project",
                commit="1" * 40,
                challenge_sha256=hashlib.sha256(challenge.read_bytes()).hexdigest(),
                project_path="",
                challenge_path="Challenge.lean",
                solution_path="Solution.lean",
                comparator_config_path="comparator.json",
                lakefile_path="lakefile.toml",
                lean_toolchain_path="lean-toolchain",
                work_dir=str(root / "work"),
                output=str(output),
            )

            with (
                mock.patch(
                    "scripts.render_challenge.clone_commit",
                    side_effect=lambda _url, _commit, destination: shutil.copytree(
                        template, destination
                    ),
                ),
                mock.patch(
                    "scripts.render_challenge.toolchain_verso_commit",
                    return_value="3" * 40,
                ),
            ):
                self.assertEqual(prepare(args), 0)

            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "pending")
            self.assertIn("project_path", report["source"])
            self.assertEqual(report["source"]["project_path"], "")

    def test_prepare_parser_requires_the_complete_path_set(self):
        command = next(
            action
            for action in parser()._actions
            if isinstance(action, argparse._SubParsersAction)
        ).choices["prepare"]
        required = {action.dest for action in command._actions if action.required}
        self.assertTrue(
            {
                "project_path",
                "challenge_path",
                "solution_path",
                "comparator_config_path",
                "lakefile_path",
                "lean_toolchain_path",
            }.issubset(required)
        )

        sanitize = next(
            action
            for action in parser()._actions
            if isinstance(action, argparse._SubParsersAction)
        ).choices["sanitize"]
        self.assertIn(
            "challenge_module",
            {action.dest for action in sanitize._actions if action.required},
        )

    def test_execute_marks_a_legacy_report_as_a_contract_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "report.json"
            output.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "pending",
                        "stage": "prepared",
                        "errors": [],
                    }
                ),
                encoding="utf-8",
            )

            result = execute(
                argparse.Namespace(work_dir=str(root / "work"), output=str(output))
            )

            self.assertEqual(result, 1)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["schema_version"], 2)
            self.assertEqual(report["status"], "error")
            self.assertEqual(report["stage"], "contract")
            self.assertEqual(
                set(report),
                {"schema_version", "status", "stage", "errors", "prepared_at"},
            )
            self.assertIn("schema_version 2", report["errors"][0])

    def test_runtime_script_digests_match_database_contract(self):
        self.assertEqual(
            hashlib.sha256(RUNTIME_SANITIZER.encode()).hexdigest(),
            "d15fb1c3eca7a3eb32293cff66a913301c25fb03706004a0e27319b631c6ff60",
        )
        self.assertEqual(
            hashlib.sha256(VERSO_RUNTIME.encode()).hexdigest(),
            "332773edafa3ac712ec3fba31d4c6ece2339693958873a9020a9be1bddb22538",
        )

    def test_the_renderer_revision_is_derived_from_the_toolchain(self):
        """No table to keep current: the tag is the toolchain's own version."""
        with mock.patch(
            "scripts.render_challenge.resolve_release_commit",
            side_effect=lambda repo, tag: f"{repo}@{tag}",
        ) as resolve:
            self.assertEqual(
                toolchain_verso_commit("leanprover/lean4:v4.31.0-rc2"),
                "leanprover/verso@v4.31.0-rc2",
            )
        self.assertEqual(resolve.call_args.args, ("leanprover/verso", "v4.31.0-rc2"))

    def test_a_toolchain_below_the_minimum_is_refused(self):
        with self.assertRaisesRegex(VerificationError, "older than the minimum"):
            toolchain_verso_commit("leanprover/lean4:v4.20.0")

    def test_a_toolchain_that_is_not_a_lean_release_is_refused(self):
        for value in ("leanprover/lean4:nightly-2026-08-01", "v4.32.0", "leanprover/lean4:v4"):
            with self.subTest(value):
                with self.assertRaisesRegex(VerificationError, "unsupported Lean toolchain"):
                    toolchain_verso_commit(value)

    def test_manifest_merge_reuses_identical_dependency(self):
        shared = {
            "name": "plausible",
            "type": "git",
            "url": "https://github.com/leanprover-community/plausible",
            "rev": "1" * 40,
            "inherited": True,
        }
        merged = merge_renderer_manifest(
            {"version": "1.2.0", "packages": [shared]},
            {"version": "1.2.0", "packages": [shared]},
            "2" * 40,
        )
        names = [package["name"] for package in merged["packages"]]
        self.assertEqual(names.count("plausible"), 1)
        self.assertIn("verso", names)

    def test_manifest_merge_rejects_dependency_substitution(self):
        source = {
            "packages": [
                {
                    "name": "shared",
                    "type": "git",
                    "url": "https://github.com/example/shared",
                    "rev": "1" * 40,
                }
            ]
        }
        verso = {
            "packages": [
                {
                    "name": "shared",
                    "type": "git",
                    "url": "https://github.com/example/shared",
                    "rev": "2" * 40,
                }
            ]
        }
        with self.assertRaisesRegex(VerificationError, "conflicts"):
            merge_renderer_manifest(source, verso, "3" * 40)

    def test_trusted_lakefile_uses_only_direct_exact_dependencies(self):
        manifest = {
            "packages": [
                {
                    "name": "direct",
                    "type": "git",
                    "url": "https://github.com/example/direct",
                    "rev": "1" * 40,
                    "inherited": False,
                },
                {
                    "name": "transitive",
                    "type": "git",
                    "url": "https://github.com/example/transitive",
                    "rev": "2" * 40,
                    "inherited": True,
                },
            ]
        }
        lakefile = trusted_lakefile(
            manifest,
            "3" * 40,
            challenge_module="Audit.Task",
            challenge_source_root=PurePosixPath("src"),
        )
        self.assertIn('name = "direct"', lakefile)
        self.assertNotIn('name = "transitive"', lakefile)
        self.assertIn('name = "verso"', lakefile)
        self.assertIn('name = "Challenge"', lakefile)
        self.assertIn('srcDir = "src"', lakefile)
        self.assertIn('roots = ["Audit.Task"]', lakefile)

    def test_static_html_gets_csp_and_runtime_sanitizer(self):
        html = """<!doctype html><html><head><base href="../">
<meta http-equiv="refresh" content="0;url=https://attacker.example/refresh">
<script src="marked.js"></script><script>window.safe = true;</script>
</head><body><a href="https://attacker.example/">leave</a><a href="data:text/html,bad">data</a>
<img src="javascript:bad" onerror="steal()"></body></html>"""
        sanitized = static_html_sanitize(
            html, "../palomar-sanitize.js", "../palomar-verso.js"
        )
        self.assertIn("Content-Security-Policy", sanitized)
        self.assertIn("../palomar-sanitize.js", sanitized)
        self.assertIn("../palomar-verso.js", sanitized)
        self.assertNotIn("window.safe", sanitized)
        self.assertNotIn("marked.js", sanitized)
        self.assertNotIn("onerror", sanitized)
        self.assertNotIn("javascript:bad", sanitized)
        self.assertNotIn("attacker.example", sanitized)
        self.assertNotIn("http-equiv=\"refresh\"", sanitized.lower())
        self.assertNotIn("data:text/html", sanitized)

    def test_static_html_strips_slash_separated_handlers_and_rejects_unclosed_scripts(self):
        html = """<html><head><base href="../"></head><body>
<a href="#"/onclick="alert(1)">safe</a>
</body></html>"""
        sanitized = static_html_sanitize(
            html, "../palomar-sanitize.js", "../palomar-verso.js"
        )
        self.assertNotIn("onclick", sanitized.lower())
        self.assertEqual(sanitized.lower().count("<script"), 2)
        with self.assertRaisesRegex(VerificationError, "incomplete markup"):
            static_html_sanitize(
                """<html><head><base href="../"></head><body>
<script src="https://attacker.invalid/payload.js">
</body></html>""",
                "../palomar-sanitize.js",
                "../palomar-verso.js",
            )

    def test_static_html_rewrites_unquoted_urls(self):
        html = """<html><head><base href="../"></head><body>
<a href=javascript:alert(1)>bad</a><img src=asset.png>
</body></html>"""
        sanitized = static_html_sanitize(
            html, "../palomar-sanitize.js", "../palomar-verso.js"
        )
        self.assertIn('href="#"', sanitized)
        self.assertIn('src="../asset.png"', sanitized)
        self.assertNotIn("javascript:", sanitized)

    def test_static_html_rewrites_namespaced_urls_and_rejects_incomplete_markup(self):
        html_text = (
            '<html><head><base href="../"></head><body>'
            '<svg><a xlink:href="javascript:bad">bad</a></svg></body></html>'
        )
        sanitized = static_html_sanitize(
            html_text, "../palomar-sanitize.js", "../palomar-verso.js"
        )
        self.assertIn('xlink:href="#"', sanitized)
        self.assertNotIn("javascript:bad", sanitized)
        with self.assertRaisesRegex(VerificationError, "incomplete markup"):
            static_html_sanitize(
                '<html><head><base href="../"></head><body><',
                "../palomar-sanitize.js",
                "../palomar-verso.js",
            )

    def test_static_html_places_csp_before_scripts(self):
        html = '<html><head><base href="../"></head><body></body></html>'
        sanitized = static_html_sanitize(
            html, "../palomar-sanitize.js", "../palomar-verso.js"
        )
        head = sanitized[sanitized.index("<head>") : sanitized.index("</head>")]
        self.assertLess(head.index("Content-Security-Policy"), head.index("<script"))

    def test_static_html_does_not_strip_words_that_resemble_attribute_names(self):
        html = (
            '<html><head><base href="../"></head><body>'
            '<p>one only once target action ping</p></body></html>'
        )
        sanitized = static_html_sanitize(
            html, "../palomar-sanitize.js", "../palomar-verso.js"
        )
        self.assertIn("one only once target action ping", sanitized)

    def test_module_doc_and_surface_metadata_are_parsed_from_lean(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            challenge = root / "Challenge.lean"
            solution = root / "Solution.lean"
            comparator = root / "comparator.json"
            challenge.write_text(
                """-- /-! not a module doc -/
import Mathlib
import Batteries

/-!
# Module title

Metadata body.
-/
namespace Example
/-- Headline. -/
theorem headline : True := trivial
end Example
""",
                encoding="utf-8",
            )
            solution.write_text("import ErdosUnitDistance\n", encoding="utf-8")
            comparator.write_text(
                json.dumps(
                    {
                        "challenge_module": "Challenge",
                        "solution_module": "Solution",
                        "theorem_names": ["Example.headline"],
                        "definition_names": [],
                        "permitted_axioms": [],
                        "enable_nanoda": True,
                    }
                ),
                encoding="utf-8",
            )
            metadata = parsed_challenge_metadata(
                challenge,
                solution,
                comparator,
                challenge_module="Challenge",
            )
            self.assertEqual(metadata["schema_version"], 2)
            self.assertEqual(metadata["imports"], ["Batteries", "Mathlib"])
            self.assertEqual(metadata["module_doc"], "# Module title\n\nMetadata body.")
            self.assertEqual(metadata["declarations"], ["Example.headline"])
            self.assertEqual(metadata["solution_imports"], ["ErdosUnitDistance"])

    def test_module_doc_parser_skips_strings_and_nested_regular_comments(self):
        source = '''def fake := "/-! nope -/"\n/- outer /-! nested -/ -/\n/-! real doc -/'''
        self.assertEqual(extract_module_doc(source), "real doc")

    def test_static_html_binds_the_compared_declaration_and_scroll_surface(self):
        html_text = '''<!doctype html><html><head><base href="../"></head><body>
<section class="code-content"><div class="md-text"><p>Headline.</p></div>
<code class="hl lean block"><span class="const token"
data-binding="const-Example.headline" id="Example___headline">headline</span></code>
</section></body></html>'''
        sanitized = static_html_sanitize(
            html_text,
            "../palomar-sanitize.js",
            "../palomar-verso.js",
            ["Example.headline"],
        )
        self.assertIn("data-palomar-declarations=\"[&quot;Example.headline&quot;]\"", sanitized)
        self.assertIn("html { height: auto !important", sanitized)
        self.assertIn("overflow: visible !important", sanitized)
        self.assertIn("white-space: nowrap", sanitized)
        self.assertIn("background: var(--palomar-panel) !important", sanitized)
        self.assertIn("palomar-declaration-style", sanitized)

    def test_declaration_metadata_is_injected_after_url_rewriting(self):
        declaration = "Example.«one href=x onload=y»"
        html_text = f'''<!doctype html><html><head><base href="../"></head><body>
<span data-binding="const-{declaration}" id="decl">headline</span>
</body></html>'''
        sanitized = static_html_sanitize(
            html_text,
            "../palomar-sanitize.js",
            "../palomar-verso.js",
            [declaration],
        )
        encoded = html.escape(
            json.dumps([declaration], ensure_ascii=False, separators=(",", ":")),
            quote=True,
        )
        self.assertIn(f'data-palomar-declarations="{encoded}"', sanitized)

    def test_static_html_rejects_a_missing_compared_declaration(self):
        html_text = "<!doctype html><html><head><base href=\"../\"></head><body></body></html>"
        with self.assertRaisesRegex(VerificationError, "does not contain compared declaration"):
            static_html_sanitize(
                html_text,
                "../palomar-sanitize.js",
                "../palomar-verso.js",
                ["Example.missing"],
            )

    def test_raw_svg_is_not_an_accepted_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "attack.svg").write_text("<svg><script>attack()</script></svg>")
            with self.assertRaisesRegex(VerificationError, "unexpected file type"):
                artifact_manifest(root)

    def test_bundle_is_bounded_sanitized_and_content_addressed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw"
            output = root / "output"
            (raw / "Challenge").mkdir(parents=True)
            output.mkdir()
            (raw / "Challenge" / "index.html").write_text(
                """<!doctype html><html><head><base href="../"><script src="marked.js"></script>
<script>window.safe = true;</script></head><body><p>Challenge</p></body></html>""",
                encoding="utf-8",
            )
            (raw / "marked.js").write_text("window.marked = {parse: x => x};", encoding="utf-8")
            (raw / "code.css").write_text("code { color: black; }", encoding="utf-8")
            tree_hash = sanitize_bundle(raw, output, challenge_module="Challenge")
            manifest = json.loads((output / "artifact-manifest.json").read_text())
            self.assertEqual(manifest["artifact_tree_sha256"], tree_hash)
            self.assertEqual(artifact_manifest(output)[1], tree_hash)
            self.assertEqual(len(tree_hash), 64)
            self.assertEqual((output / "palomar-sanitize.js").read_text(), RUNTIME_SANITIZER)
            self.assertEqual((output / "palomar-verso.js").read_text(), VERSO_RUNTIME)
            metadata = json.loads((output / "challenge-metadata.json").read_text())
            self.assertEqual(metadata["declarations"], [])
            self.assertFalse((output / "marked.js").exists())

    def test_bundle_maps_the_original_nested_module_to_the_stable_entrypoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw"
            output = root / "output"
            module_output = raw / "Audit" / "Task"
            module_output.mkdir(parents=True)
            output.mkdir()
            (module_output / "index.html").write_text(
                '''<!doctype html><html><head><base href="../../"></head><body>
<span data-binding="const-Audit.Task.headline" id="headline">headline</span>
<a href="code.css">style</a><code>private theorem modulePrivate</code></body></html>''',
                encoding="utf-8",
            )
            (raw / "code.css").write_text("code {}", encoding="utf-8")

            sanitize_bundle(
                raw,
                output,
                challenge_module="Audit.Task",
                metadata={
                    "schema_version": 2,
                    "imports": [],
                    "module_doc": None,
                    "declarations": ["Audit.Task.headline"],
                    "solution_imports": [],
                },
            )

            entrypoint = output / "Challenge" / "index.html"
            self.assertTrue(entrypoint.is_file())
            rendered = entrypoint.read_text()
            self.assertIn("Audit.Task.headline", rendered)
            self.assertIn("private theorem modulePrivate", rendered)
            self.assertIn('href="../code.css"', rendered)
            self.assertFalse((output / "Audit" / "Task" / "index.html").exists())

    def test_bundle_does_not_broaden_the_auxiliary_page_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw"
            output = root / "output"
            module_output = raw / "Audit" / "Task"
            nested = module_output / "notes"
            nested.mkdir(parents=True)
            output.mkdir()
            (module_output / "index.html").write_text(
                '<html><head><base href="../../"></head><body>Task</body></html>',
                encoding="utf-8",
            )
            (nested / "index.html").write_text(
                '<html><head><base href="../../../"></head><body>Notes</body></html>',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(VerificationError, "unexpected base URL"):
                sanitize_bundle(raw, output, challenge_module="Audit.Task")

    def test_artifact_manifest_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.js"
            target.write_text("safe", encoding="utf-8")
            (root / "linked.js").symlink_to(target)
            with self.assertRaisesRegex(VerificationError, "symbolic link"):
                artifact_manifest(root)

    def test_only_root_artifact_manifest_is_excluded_from_content_address(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "artifact-manifest.json").write_text("root")
            nested = root / "nested" / "artifact-manifest.json"
            nested.parent.mkdir()
            nested.write_text("nested")
            files, _tree_hash = artifact_manifest(root)
            self.assertEqual([item["path"] for item in files], ["nested/artifact-manifest.json"])


class CallSignatureTests(unittest.TestCase):
    """Every helper the render path calls, called the way it calls it.

    Registration failed on its first real use because `execute` passed
    `readable_paths=` to a function that did not take it. Nothing caught it:
    the render path runs only when a submission is being registered, which had
    never happened, and a TypeError raised there is indistinguishable in the
    workflow log from any other failed render.
    """

    def test_the_render_path_calls_its_helpers_with_arguments_they_accept(self):
        import ast
        import inspect

        from scripts import render_challenge, verify_submission

        def resolved(node):
            """The verify_submission callable a call node names, or None.

            Resolved through render_challenge's own namespace, so an alias is
            followed and a same-named local is not mistaken for an import:
            render_challenge has its own `parser` and `main`.
            """
            if isinstance(node.func, ast.Name):
                target = getattr(render_challenge, node.func.id, None)
            elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                module = getattr(render_challenge, node.func.value.id, None)
                target = getattr(module, node.func.attr, None) if module is not None else None
            else:
                return None
            if not callable(target) or inspect.isclass(target):
                return None
            return target if inspect.getmodule(target) is verify_submission else None

        source = Path(inspect.getfile(render_challenge)).read_text()
        checked = 0
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            target = resolved(node)
            if target is None:
                continue
            # A splat says nothing checkable, so it is outside the guarantee.
            if any(kw.arg is None for kw in node.keywords) or any(
                isinstance(arg, ast.Starred) for arg in node.args
            ):
                continue
            names = [kw.arg for kw in node.keywords]
            try:
                # bind, not bind_partial: a required argument left out is the
                # same TypeError at the same place as one that does not exist.
                inspect.signature(target).bind(
                    *[mock.ANY] * len(node.args), **{name: mock.ANY for name in names}
                )
            except TypeError as error:
                self.fail(
                    f"render_challenge line {node.lineno} calls "
                    f"{target.__name__}() with arguments it does not accept: {error}"
                )
            checked += 1
        self.assertGreater(checked, 10, "no cross-module calls were checked")


def _injected_surface_style() -> str:
    """The stylesheet Palomar adds to a rendered Challenge, as it is shipped."""
    sanitized = static_html_sanitize(
        '<!doctype html><html><head><base href="../"></head><body></body></html>',
        "../palomar-sanitize.js",
        "../palomar-verso.js",
    )
    match = re.search(
        r'<style id="palomar-declaration-style">(.*?)</style>', sanitized, re.DOTALL
    )
    if match is None:
        raise AssertionError("the sanitized page carries no declaration stylesheet")
    return match.group(1)


def _blocks(css: str) -> list[str]:
    """The bodies of the innermost braced blocks, comments already gone."""
    return re.findall(r"\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL))


def _declarations(body: str) -> list[tuple[str, str]]:
    pairs = []
    for chunk in body.split(";"):
        name, separator, value = chunk.partition(":")
        if separator:
            pairs.append((name.strip().lower(), value.strip()))
    return pairs


def _is_palette(body: str) -> bool:
    """A block that only binds tokens, plus the `color-scheme` that opens the light one."""
    pairs = _declarations(body)
    return bool(pairs) and all(
        name.startswith("--") or name == "color-scheme" for name, _ in pairs
    )


def _palettes(css: str) -> tuple[dict[str, str], dict[str, str]]:
    """The light palette, and the dark block that overrides it. Tokens only."""
    blocks = [
        {name: value for name, value in _declarations(body) if name.startswith("--")}
        for body in _blocks(css)
        if _is_palette(body)
    ]
    if len(blocks) != 2:
        raise AssertionError(f"expected a light and a dark palette, found {len(blocks)}")
    return blocks[0], blocks[1]


_COLOUR_KEYWORDS = (
    "black|white|red|blue|green|gray|grey|silver|maroon|navy|orange|purple|yellow"
)
_NAMES_A_COLOUR = re.compile(
    rf"#[0-9a-fA-F]{{3,8}}\b|\b(?:{_COLOUR_KEYWORDS})\b|\b(?:rgba?|hsla?)\("
)
_PAINTS = re.compile(
    r"^(?:background|background-color|color|border|border-[a-z-]+|box-shadow|outline"
    r"|outline-color|text-decoration-color|fill|stroke)$"
)


def _painting_selectors(css: str) -> set[str]:
    """Every single selector in `css` that gives some element a literal colour.

    Comma groups are split, because a group is answered one selector at a time.
    """
    found: set[str] = set()
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    for block in re.finditer(r"([^{}]*)\{([^{}]*)\}", stripped):
        group = " ".join(block.group(1).split())
        if not group or group.startswith("@"):
            continue
        for name, separator, value in (
            chunk.partition(":") for chunk in block.group(2).split(";")
        ):
            if not separator or not _PAINTS.match(name.strip().lower()):
                continue
            if _NAMES_A_COLOUR.search(value):
                found.update(part.strip() for part in group.split(",") if part.strip())
                break
    return found


def _answered_selectors(css: str) -> set[str]:
    """Every single selector the injected stylesheet writes a rule for."""
    answered: set[str] = set()
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    for block in re.finditer(r"([^{}]*)\{([^{}]*)\}", stripped):
        group = " ".join(block.group(1).split())
        if not group or group.startswith("@"):
            continue
        answered.update(part.strip() for part in group.split(",") if part.strip())
    return answered


def _luminance(colour: str) -> float:
    raw = colour.lstrip("#")
    if len(raw) == 3:
        raw = "".join(channel * 2 for channel in raw)
    channels = [int(raw[index:index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(one: str, other: str) -> float:
    high, low = sorted((_luminance(one), _luminance(other)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class RenderSurfaceAppearanceTests(unittest.TestCase):
    """The framed render follows the browser's light and dark preference.

    The frame is a separate document on a separate origin, so nothing hands it
    a theme: it answers `prefers-color-scheme` itself, exactly as the page
    around it does. What is checkable here is that the stylesheet Palomar
    injects is in a position to answer, that no rule in it quietly names a
    light colour instead of spending a palette token, and that both answers are
    readable. A published bundle is immutable, so a palette that goes out wrong
    stays wrong for that record.
    """

    # Text, and the surfaces it is set on. WCAG AA for body text.
    READABLE = (
        ("--palomar-ink", "--palomar-paper"),
        ("--palomar-ink", "--palomar-panel"),
        ("--palomar-ink", "--palomar-raise"),
        ("--palomar-ink", "--palomar-alarm-paper"),
        ("--palomar-link", "--palomar-paper"),
        # The selected node in Verso's module tree reverses the two.
        ("--palomar-paper", "--palomar-link"),
        ("--palomar-alarm", "--palomar-paper"),
    )
    # Indicators, and what they are drawn on. WCAG AA for non-text.
    #
    # `--palomar-info-paper` is here rather than in READABLE because it is a
    # saturated fill Verso already used, and ink on it clears 3.74:1 in light.
    # This change does not move a light value, so asserting 4.5 would be
    # asserting a change that was deliberately not made.
    #
    # `--palomar-mark` and `--palomar-mark-on` are absent on purpose: they are
    # the tactic-toggle pill, which Verso draws at 1.94:1 in light because a
    # disclosure affordance that shouts drowns the code it sits in.
    VISIBLE = (
        ("--palomar-edge", "--palomar-paper"),
        ("--palomar-edge", "--palomar-panel"),
        ("--palomar-info", "--palomar-paper"),
        ("--palomar-ink", "--palomar-info-paper"),
    )
    # Properties that put a colour on the page. `border` and `box-shadow` are
    # shorthands that carry one.
    COLOUR_PROPERTIES = frozenset(
        {
            "background",
            "background-color",
            "border",
            "border-color",
            "border-left-color",
            "border-top-color",
            "box-shadow",
            "color",
            "text-decoration-color",
        }
    )
    # A colour property may decline to name a colour at all.
    DEFERRED = frozenset({"inherit", "currentcolor", "transparent", "none", "unset"})

    def test_the_surface_style_answers_the_browser_colour_preference(self):
        style = _injected_surface_style()
        self.assertIn("color-scheme: light dark", style)
        self.assertIn("@media (prefers-color-scheme: dark)", style)
        light, override = _palettes(style)
        # A token bound in light and left out of dark is the whole bug this
        # change fixes, one declaration at a time.
        self.assertEqual(
            sorted(light),
            sorted(override),
            "the dark palette must answer every token the light one binds",
        )
        self.assertEqual(
            [],
            [name for name, value in override.items() if light[name] == value],
            "a dark token that repeats its light value says nothing",
        )

    def test_every_surface_colour_is_a_palette_token(self):
        style = _injected_surface_style()
        rules = [body for body in _blocks(style) if not _is_palette(body)]
        for body in rules:
            for name, value in _declarations(body):
                if name.startswith("--") or name not in self.COLOUR_PROPERTIES:
                    continue
                cleaned = value.replace("!important", "").strip().lower()
                self.assertTrue(
                    "var(--palomar-" in cleaned or cleaned in self.DEFERRED,
                    f"{name}: {value} names a colour instead of spending a token",
                )
        outside = "".join(rules)
        self.assertNotRegex(outside, r"#[0-9a-fA-F]{3,8}\b")
        self.assertNotRegex(outside, r"\b(?:rgba?|hsla?)\(")

    # Verso selectors that name a colour and are deliberately left as they are.
    # Everything else it paints must be answered, or the frame keeps a light
    # patch in dark mode that no palette test can see.
    LEFT_TO_VERSO = {
        # Tippy's script is dropped by `sanitize_bundle`, so nothing in a
        # published bundle ever carries `data-theme`, and the popup a reader
        # actually sees is Palomar's own `.palomar-hover`.
        "tippy": "the Tippy runtime is not shipped, so no element matches",
        # A half-opaque black scrim over the narrow-screen menu. Black at half
        # opacity darkens either paper, which is what a scrim is for.
        ".menu-toggle:checked + .hamburger + .layout::after": "a scrim reads on both",
    }

    def test_every_verso_colour_selector_is_answered_or_left_alone(self):
        """Read Verso's stylesheets, not just ours.

        A palette can be complete, internally consistent and still leave the
        frame half light, because the rule that paints the light patch lives
        upstream and no test that reads only Palomar's own block can see it.
        This is the test that would have caught the tactic-label hover.
        """
        fixtures = pathlib.Path(__file__).parent / "fixtures" / "verso-render-css"
        answered = _answered_selectors(_injected_surface_style())
        unanswered = []
        for stylesheet in sorted(fixtures.glob("*.css")):
            for selector in _painting_selectors(stylesheet.read_text()):
                if selector in answered or selector in self.LEFT_TO_VERSO:
                    continue
                if "tippy" in selector and "tippy" in self.LEFT_TO_VERSO:
                    continue
                unanswered.append(f"{stylesheet.name}: {selector}")
        self.assertEqual(
            [],
            sorted(unanswered),
            "Verso paints these and the injected stylesheet does not answer them; "
            "either answer them or record why they are left alone",
        )

    def test_both_palettes_are_readable(self):
        light, override = _palettes(_injected_surface_style())
        for mode, palette in (("light", light), ("dark", {**light, **override})):
            for ink, paper in self.READABLE:
                ratio = _contrast(palette[ink], palette[paper])
                self.assertGreaterEqual(
                    ratio, 4.5, f"{mode}: {ink} on {paper} is only {ratio:.2f}:1"
                )
            for edge, paper in self.VISIBLE:
                ratio = _contrast(palette[edge], palette[paper])
                self.assertGreaterEqual(
                    ratio, 3.0, f"{mode}: {edge} on {paper} is only {ratio:.2f}:1"
                )


if __name__ == "__main__":
    unittest.main()
