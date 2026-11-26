import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.check_release_primitive_targets import release_problems, supported_release_tags

ROOT = Path(__file__).resolve().parent.parent

FIXTURES = Path(__file__).parent / "fixtures" / "primitive-targets"


class ReleasePrimitiveTargetTests(unittest.TestCase):
    def test_only_release_tags_at_or_above_the_minimum_are_checked(self):
        tags = [
            "v4.36.0", "v4.35.0-rc1", "v4.35.0", "v4.35.0-rc2", "v4.34.1", "v4.37.0-rc1",
            "nightly-2026-11-01", "v4.36.0-m1", "v4.35.0-rc10",
        ]
        self.assertEqual(
            supported_release_tags(tags, "v4.35.0-rc2"),
            ["v4.35.0-rc2", "v4.35.0-rc10", "v4.35.0", "v4.36.0", "v4.37.0-rc1"],
        )

    def test_released_lists_pass_and_an_unparseable_one_fails(self):
        for release in ("v4.36.0", "v4.37.0-rc1"):
            with self.subTest(release=release):
                text = (FIXTURES / f"{release}.txt").read_bytes()
                self.assertEqual(release_problems(release, text), [])
        broken = (FIXTURES / "v4.37.0-rc1.txt").read_bytes().replace(
            b"set_option linter.deprecated false in ``String.mk",
            b"set_option pp.all true in ``String.mk",
        )
        [problem] = release_problems("v9.9.9", broken)
        self.assertIn("unrecognised export target list", problem)

    def test_a_list_missing_an_expected_primitive_is_reported(self):
        text = (FIXTURES / "v4.36.0.txt").read_bytes().replace(b"    ``String.mk,\n", b"")
        [problem] = release_problems("v9.9.9", text)
        self.assertIn("lacks String.mk", problem)

    def test_the_check_reads_each_supported_tag_through_a_blobless_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            upstream = Path(directory) / "lean4"
            check = upstream / "src" / "lake" / "Lake" / "CLI" / "Check.lean"
            check.parent.mkdir(parents=True)

            def git(*argv):
                subprocess.run(
                    ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *argv],
                    cwd=upstream, check=True, capture_output=True,
                )

            git("init", "--quiet")
            git("config", "uploadpack.allowFilter", "true")
            for tag, fixture in (("v4.34.0", None), ("v4.36.0", "v4.36.0"),
                                 ("v4.37.0-rc1", "v4.37.0-rc1")):
                text = b"def primitiveTargets := 0\n" if fixture is None else (
                    FIXTURES / f"{fixture}.txt"
                ).read_bytes()
                check.write_bytes(text)
                git("add", "-A")
                git("commit", "--quiet", "-m", tag)
                git("tag", tag)
            run = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "check_release_primitive_targets.py"),
                 "--remote", str(upstream)],
                capture_output=True, text=True,
            )
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertEqual(run.stdout, "v4.36.0: ok\nv4.37.0-rc1: ok\n")

            check.write_bytes(b"def primitiveTargets := do\n  return #[``Nat.add] ++ more\ndef x := 0\n")
            git("commit", "--quiet", "-am", "broken")
            git("tag", "v4.38.0")
            run = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "check_release_primitive_targets.py"),
                 "--remote", str(upstream)],
                capture_output=True, text=True,
            )
            self.assertEqual(run.returncode, 1)
            self.assertIn("v4.38.0: FAILED", run.stdout)
            self.assertIn("::error::v4.38.0:", run.stdout)


if __name__ == "__main__":
    unittest.main()
