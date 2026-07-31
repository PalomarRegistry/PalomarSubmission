import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.verify_submission import (
    VerificationError,
    allowed_roots,
    canonical_repository,
    direct_imports,
    github_repository,
    landrun_command,
    load_comparator_config,
    normalize_repository,
    package_allowlist,
    parse_issue_body,
    remove_untrusted_lake_state,
    require_protected_paths,
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
        source = """import Mathlib
public import TauCeti.Topology
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
        fetch = mock.Mock(returncode=0)
        ancestry = mock.Mock(returncode=1)
        with mock.patch("scripts.verify_submission.run", side_effect=[fetch, ancestry]) as run_mock:
            with self.assertRaisesRegex(VerificationError, "not an ancestor"):
                verify_official_revision(
                    Path("/source/.lake/packages/mathlib"),
                    repository="leanprover-community/mathlib4",
                    revision="1" * 40,
                    official_ref="refs/heads/master",
                    git_env={"PATH": "/usr/bin"},
                )
        fetch_command = run_mock.call_args_list[0].args[0]
        self.assertIn("https://github.com/leanprover-community/mathlib4", fetch_command)
        self.assertIn(
            "+refs/heads/master:refs/remotes/palomar-official/head",
            fetch_command,
        )

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
