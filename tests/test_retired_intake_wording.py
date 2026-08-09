"""The retired-wording check is only worth having if a bad file still fails it.

Every behavioural test here plants a file and runs the checked-in checker over
it as a subprocess, because the failure this check was written to prevent was a
production file drifting past a rule that never read it. A test that asserted a
glob appears in the workflow would have passed throughout that drift.
"""

import importlib.util
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

    def test_a_target_that_cannot_be_decoded_fails(self):
        root = self.plant({"scripts/example.py": b"# \xff\xfe not utf-8\n"})
        result = run(root)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("scripts/example.py: unreadable:", result.stdout)

    def test_a_missing_prose_target_fails(self):
        root = self.plant({})
        (root / "README.md").unlink()
        result = run(root)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("README.md: unreadable:", result.stdout)


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
        scanned = load_checker().scanned_paths(root)
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
