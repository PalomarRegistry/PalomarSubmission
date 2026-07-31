import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.verify_submission import (
    VerificationError,
    allowed_roots,
    audit_challenge_sources,
    canonical_repository,
    direct_imports,
    github_repository,
    lake_environment_value,
    landrun_command,
    load_comparator_config,
    materialize_packages,
    normalize_repository,
    package_allowlist,
    parse_issue_body,
    remove_untrusted_lake_state,
    require_protected_paths,
    systemd_command,
    verify_official_revision,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class VerifySubmissionTests(unittest.TestCase):
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

    def test_outer_landrun_policy(self):
        command = landrun_command(
            ["/tools/comparator", "comparator.json"],
            landrun=Path("/tools/landrun"),
            writable_directories=[Path("/source/.lake/build")],
            executable_paths=[Path("/usr"), Path("/tools/comparator")],
            environment={"PATH": "/usr/bin", "HOME": "/source/.lake/config/home", "SECRET": "no"},
        )
        self.assertEqual(command[:8], [
            "/tools/landrun",
            "--best-effort",
            "--ro",
            "/",
            "--rw",
            "/dev",
            "--ldd",
            "--add-exec",
        ])
        self.assertIn("/source/.lake/build", command)
        self.assertIn("/tools/comparator", command)
        self.assertNotIn("SECRET", command)
        self.assertNotIn("--unrestricted-network", command)
        self.assertEqual(command[command.index("--") + 1 :], ["/tools/comparator", "comparator.json"])

        networked = landrun_command(
            ["lake", "exe", "cache", "get"],
            landrun=Path("landrun"),
            writable_directories=[],
            executable_paths=[],
            environment={},
            unrestricted_network=True,
        )
        self.assertIn("--unrestricted-network", networked)

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
        with mock.patch(
            "scripts.verify_submission.run", side_effect=[remote, fetch, ancestry]
        ) as run_mock:
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
                executable_paths=[],
                tools={},
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
