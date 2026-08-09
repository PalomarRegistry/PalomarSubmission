import argparse
import hashlib
import html
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.render_challenge import (
    RUNTIME_SANITIZER,
    VERSO_RUNTIME,
    artifact_manifest,
    execute,
    extract_module_doc,
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

    def test_workspace_replaces_hostile_fixed_paths_without_following_symlinks(self):
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
            comparator.write_bytes(b'{"accepted": true}')
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
                workspace_project = prepare_workspace(
                    source, workspace, "3" * 40, work, accepted_paths
                )

            self.assertEqual(external.read_bytes(), b"do not replace")
            self.assertEqual(external_comparator.read_bytes(), b"also do not replace")
            self.assertFalse((workspace_project / "Challenge.lean").is_symlink())
            self.assertEqual(
                (workspace_project / "Challenge.lean").read_bytes(), b"accepted challenge"
            )
            self.assertTrue((workspace_project / "Solution.lean").is_file())
            self.assertEqual(
                (workspace_project / "Solution.lean").read_bytes(), b"accepted solution"
            )
            self.assertEqual(
                (workspace_project / "comparator.json").read_bytes(), b'{"accepted": true}'
            )

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
            (project / "comparator.json").write_text("{}")
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
        lakefile = trusted_lakefile(manifest, "3" * 40)
        self.assertIn('name = "direct"', lakefile)
        self.assertNotIn('name = "transitive"', lakefile)
        self.assertIn('name = "verso"', lakefile)
        self.assertIn('name = "Challenge"', lakefile)

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
            metadata = parsed_challenge_metadata(challenge, solution, comparator)
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
        self.assertIn("background: #fff !important", sanitized)
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
            tree_hash = sanitize_bundle(raw, output)
            manifest = json.loads((output / "artifact-manifest.json").read_text())
            self.assertEqual(manifest["artifact_tree_sha256"], tree_hash)
            self.assertEqual(artifact_manifest(output)[1], tree_hash)
            self.assertEqual(len(tree_hash), 64)
            self.assertEqual((output / "palomar-sanitize.js").read_text(), RUNTIME_SANITIZER)
            self.assertEqual((output / "palomar-verso.js").read_text(), VERSO_RUNTIME)
            metadata = json.loads((output / "challenge-metadata.json").read_text())
            self.assertEqual(metadata["declarations"], [])
            self.assertFalse((output / "marked.js").exists())

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


if __name__ == "__main__":
    unittest.main()
