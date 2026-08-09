"""The retired-wording check is only worth having if a bad file still fails it.

Every behavioural test here plants a file and runs the checked-in checker over
it as a subprocess, because the failure this check was written to prevent was a
production file drifting past a rule that never read it. A test that asserted a
glob appears in the workflow would have passed throughout that drift.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPOSITORY_ROOT / ".github/scripts/check_retired_intake_wording.py"


def load_checker():
    specification = importlib.util.spec_from_file_location(
        "check_retired_intake_wording", CHECKER
    )
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def run(root=None):
    return subprocess.run(
        [sys.executable, str(CHECKER)] + ([str(root)] if root is not None else []),
        capture_output=True,
        text=True,
        check=False,
    )


class TemporaryRootTestCase(unittest.TestCase):
    def temporary_root(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name)


class PlantedFileTests(TemporaryRootTestCase):
    def plant(self, files, *, prose=""):
        """Write a repository shaped like this one and return its root."""
        root = self.temporary_root()
        written = {"SECURITY.md": prose, "README.md": prose, **files}
        for relative, text in written.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(text, bytes):
                path.write_bytes(text)
            else:
                path.write_text(text, encoding="utf-8")
        (root / "docs").mkdir(exist_ok=True)
        (root / "scripts").mkdir(exist_ok=True)
        return root

    def test_the_repository_passes(self):
        # Both entry points, because CI runs the argument-free one and every
        # other test here runs the one that takes a root.
        for result in (run(), run(REPOSITORY_ROOT)):
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_planted_top_level_script_fails(self):
        root = self.plant(
            {
                "scripts/example.py": (
                    '"""Prepare and mechanically verify one issue-based '
                    'Palomar submission."""\n'
                )
            }
        )
        result = run(root)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(
            result.stdout,
            "scripts/example.py:1: retired intake wording: issue-based\n",
        )

    def test_a_planted_nested_script_fails(self):
        root = self.plant(
            {"scripts/package/example.py": "# read the issue number\nvalue = 1\n"}
        )
        result = run(root)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(
            result.stdout,
            "scripts/package/example.py:1: retired intake wording: issue number\n",
        )

    def test_a_clean_script_passes(self):
        root = self.plant(
            {
                "scripts/example.py": '"""Verify one submission."""\n',
                "scripts/package/example.py": "value = 1\n",
            }
        )
        result = run(root)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_prose_scan_stays_active(self):
        for relative in ("SECURITY.md", "README.md", "docs/record.md"):
            with self.subTest(relative=relative):
                root = self.plant({relative: "Fill in the issue field.\n"})
                result = run(root)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(
                    result.stdout,
                    f"{relative}:1: retired intake wording: issue field\n",
                )

    def test_ordinary_talk_about_an_issue_is_accepted(self):
        root = self.plant(
            {
                "docs/record.md": (
                    "Do not open a public GitHub issue containing exploit "
                    "details.\nThe issue tracker is not the place for it.\n"
                ),
                "scripts/example.py": "# raise this issue privately\nvalue = 1\n",
            },
            prose="Report the issue to the maintainer.\n",
        )
        result = run(root)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_every_rejected_collocation_still_fails(self):
        # Written out rather than read from the checker, so that deleting one
        # from the production regular expression fails here instead of
        # quietly agreeing with itself.
        for phrase in (
            "issue-form",
            "issue field",
            "issue number",
            "issue-triggered",
            "issue-write",
            "issue-reporting",
            "issue-based",
            "triggering issue",
            "reporting job",
            "report_issue",
            "issues/new",
        ):
            with self.subTest(phrase=phrase):
                root = self.plant({"scripts/example.py": f"# {phrase}\nvalue = 1\n"})
                result = run(root)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(
                    result.stdout,
                    f"scripts/example.py:1: retired intake wording: {phrase}\n",
                )

    def test_a_target_that_cannot_be_decoded_fails(self):
        root = self.plant({"scripts/example.py": b"# \xff\xfe not utf-8\n"})
        result = run(root)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("scripts/example.py: not scanned:", result.stdout)

    def test_a_missing_prose_target_fails(self):
        root = self.plant({})
        (root / "README.md").unlink()
        result = run(root)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("README.md: not scanned:", result.stdout)


class IncompleteSurfaceTests(TemporaryRootTestCase):
    """A scan surface that quietly shrinks is the failure this check exists for,
    so every way of losing one has to be louder than a passing run."""

    def bare_root(self):
        root = self.temporary_root()
        for name in ("SECURITY.md", "README.md"):
            (root / name).write_text("", encoding="utf-8")
        (root / "docs").mkdir()
        (root / "scripts").mkdir()
        return root

    def test_a_missing_scripts_root_fails(self):
        root = self.bare_root()
        (root / "scripts").rmdir()
        result = run(root)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(
            "scripts: not scanned: not a directory the scan can enumerate",
            result.stdout,
        )

    def test_a_scripts_root_that_is_a_file_fails(self):
        root = self.bare_root()
        (root / "scripts").rmdir()
        (root / "scripts").write_text("value = 1\n", encoding="utf-8")
        result = run(root)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(
            "scripts: not scanned: not a directory the scan can enumerate",
            result.stdout,
        )

    def test_a_missing_docs_root_fails(self):
        root = self.bare_root()
        (root / "docs").rmdir()
        result = run(root)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(
            "docs: not scanned: not a directory the scan can enumerate",
            result.stdout,
        )

    def test_a_symbolically_linked_subtree_fails(self):
        # `os.walk` does not descend through one, and this repository does not
        # hold the tree behind it. Saying so beats scanning nothing quietly.
        root = self.bare_root()
        (root / "scripts/package").mkdir()
        (root / "scripts/package/example.py").write_text("value = 1\n", encoding="utf-8")
        (root / "scripts/alias").symlink_to(root / "scripts/package")
        result = run(root)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(
            "scripts/alias: not scanned: symbolic link to a directory", result.stdout
        )

    @unittest.skipIf(os.geteuid() == 0, "root reads directories regardless of mode")
    def test_a_subdirectory_that_cannot_be_listed_fails(self):
        root = self.bare_root()
        closed = root / "scripts/closed"
        closed.mkdir()
        (closed / "example.py").write_text("value = 1\n", encoding="utf-8")
        closed.chmod(0o000)
        self.addCleanup(closed.chmod, 0o700)
        result = run(root)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("scripts/closed: not scanned:", result.stdout)


class ScanSurfaceTests(TemporaryRootTestCase):
    def test_the_surface_is_the_prose_roots_and_recursive_scripts(self):
        root = self.temporary_root()
        for relative in (
            "SECURITY.md",
            "README.md",
            "docs/record.md",
            "docs/nested/record.md",
            "docs/not-markdown.txt",
            "scripts/top.py",
            "scripts/package/nested.py",
            "scripts/not-python.rb",
            "tests/test_elsewhere.py",
        ):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
        scanned, blocked = load_checker().scan_surface(root)
        self.assertEqual(blocked, [])
        self.assertEqual(
            [str(path.relative_to(root)) for path in scanned],
            [
                "SECURITY.md",
                "README.md",
                "docs/nested/record.md",
                "docs/record.md",
                "scripts/package/nested.py",
                "scripts/top.py",
            ],
        )

    def test_the_default_root_is_this_repository(self):
        self.assertEqual(load_checker().REPOSITORY_ROOT, REPOSITORY_ROOT)


class WorkflowTests(unittest.TestCase):
    def test_the_step_invokes_the_checked_in_checker(self):
        workflow = yaml.safe_load(
            (REPOSITORY_ROOT / ".github/workflows/ci.yml").read_text()
        )
        step = next(
            step
            for step in workflow["jobs"]["test"]["steps"]
            if step.get("name") == "Reject retired intake wording"
        )
        self.assertEqual(
            step["run"].strip(),
            "python .github/scripts/check_retired_intake_wording.py",
        )
        self.assertTrue(CHECKER.is_file())


if __name__ == "__main__":
    unittest.main()
