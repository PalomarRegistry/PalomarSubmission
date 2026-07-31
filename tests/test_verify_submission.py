import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

import scripts.verify_submission as verifier
from scripts.verify_submission import (
    VerificationError,
    _deadline_timeout,
    allowed_roots,
    audit_challenge_sources,
    canonical_repository,
    compile_canonical_challenge,
    direct_imports,
    execute,
    github_repository,
    lake_environment_value,
    landrun_command,
    load_comparator_config,
    materialize_packages,
    normalize_repository,
    package_allowlist,
    parse_issue_body,
    protected_lean_path,
    reject_committed_build_artifacts,
    reject_untrusted_package_artifacts,
    remove_untrusted_lake_state,
    require_protected_paths,
    run,
    systemd_command,
    verify_official_revision,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class VerifySubmissionTests(unittest.TestCase):
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
            for name in ("comparator", "lean4export", "landrun"):
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
                comparator_commit="a" * 40,
                landrun_commit="b" * 40,
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
            self.assertEqual(report["stage"], "setup")
            self.assertIn("mechanical verification timed out", report["errors"])

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

    def test_issue_form_sections(self):
        body = """### Repository URL

https://github.com/example/result

### Commit SHA

0123456789012345678901234567890123456789

### Existing Palomar ID (updates only)

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

            config = json.loads(path.read_text())
            config["future_relaxation"] = True
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(VerificationError, "unknown keys"):
                load_comparator_config(path)

    def test_outer_landrun_policy(self):
        command = landrun_command(
            ["/tools/comparator", "comparator.json"],
            landrun=Path("/tools/landrun"),
            writable_directories=[Path("/source/.lake/build")],
            readable_paths=[Path("/source")],
            executable_paths=[Path("/usr"), Path("/tools/comparator")],
            environment={"PATH": "/usr/bin", "HOME": "/source/.lake/config/home", "SECRET": "no"},
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
        self.assertNotIn("SECRET", command)
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

    def test_hostile_canonical_build_cannot_publish_sibling_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            source = work / "source"
            source.mkdir()
            (source / "Challenge.lean").write_text("theorem result : True := by trivial\n")
            lean_prefix = work / "toolchain"
            (lean_prefix / "lib" / "lean").mkdir(parents=True)

            def fake_sandbox(command, **_kwargs):
                if "-o" in command:
                    output = Path(command[command.index("-o") + 1])
                    output.write_bytes(b"canonical")
                    (output.parent / "Mathlib.Forged.olean").write_bytes(b"hostile sibling")
                return mock.Mock(stdout="", stderr="", returncode=0)

            with mock.patch("scripts.verify_submission.sandboxed_run", side_effect=fake_sandbox):
                canonical, dependencies, _trusted_paths = compile_canonical_challenge(
                    work,
                    source,
                    lean=Path("/tools/lean"),
                    lean_prefix=lean_prefix,
                    allowlist={},
                    environment={},
                    landrun=Path("/tools/landrun"),
                    readable_paths=[source],
                    executable_paths=[],
                    tools={},
                )
            self.assertEqual(canonical.read_bytes(), b"canonical")
            self.assertEqual([path.name for path in canonical.parent.iterdir()], ["Challenge.olean"])
            self.assertEqual(dependencies, [])

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
                writable_directories=[writable],
            )
            self.assertEqual(audit["untrusted_sources"], [str(injected)])

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

    def test_systemd_network_namespace_defaults_closed(self):
        def which(command):
            return f"/usr/bin/{command}" if command in {"systemd-run", "systemctl"} else None

        with (
            mock.patch("scripts.verify_submission.shutil.which", side_effect=which),
            mock.patch("scripts.verify_submission.subprocess.run", return_value=mock.Mock(returncode=0)),
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
            if command in {"systemd-run", "systemctl", "sudo"}:
                return f"/usr/bin/{command}"
            return None

        with (
            mock.patch("scripts.verify_submission.shutil.which", side_effect=which),
            mock.patch("scripts.verify_submission.run", return_value=mock.Mock(returncode=0)),
            mock.patch("scripts.verify_submission.subprocess.run", return_value=mock.Mock(returncode=0)),
            mock.patch("scripts.verify_submission.os.getuid", return_value=1001),
            mock.patch("scripts.verify_submission.os.getgid", return_value=1002),
        ):
            command = systemd_command(["true"], cwd=Path("/source"), environment={})

        self.assertEqual(command[:3], ["/usr/bin/sudo", "-n", "/usr/bin/systemd-run"])
        self.assertIn("--uid=1001", command)
        self.assertIn("--gid=1002", command)
        self.assertNotIn("--user", command)

    def test_systemd_applies_trusted_resource_properties(self):
        def which(command):
            return f"/usr/bin/{command}" if command in {"systemd-run", "systemctl"} else None

        with (
            mock.patch("scripts.verify_submission.shutil.which", side_effect=which),
            mock.patch("scripts.verify_submission.subprocess.run", return_value=mock.Mock(returncode=0)),
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
