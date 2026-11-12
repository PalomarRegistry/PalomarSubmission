"""Git monorepo packages must build inside their contained Lake project."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import verify_submission as verifier
from scripts.verification_errors import VerificationError


class GitSubdirectoryTests(unittest.TestCase):
    def manifest(self, source, subdir):
        (source / "lake-manifest.json").write_text(json.dumps({"packages": [{
            "name": "OSReconstruction", "type": "git",
            "url": "https://github.com/example/OSreconstruction",
            "rev": "a" * 40, "subDir": subdir,
        }]}), encoding="utf-8")
        return verifier.manifest_packages(source)[0]

    def test_manifest_keeps_the_project_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = self.manifest(Path(tmp), "os-reconstruction")
            self.assertEqual(package["sub_dir"], "os-reconstruction")

    def test_unsafe_subdirectories_fail_before_materialization(self):
        for value in ["../escape", "/outside", "a/../b", "a//b", ".git", ".lake/build",
                      "a\\b", "a\nsecret", 3, [], True]:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(VerificationError):
                    self.manifest(Path(tmp), value)

    def test_materializer_grants_only_nested_lake_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp).resolve()
            self.manifest(source, "os-reconstruction")
            package = source / ".lake/packages/OSReconstruction"

            def git(command, **kwargs):
                if "checkout" in command:
                    nested = package / "os-reconstruction"
                    nested.mkdir()
                    stale = nested / ".lake/build/Poison.olean"
                    stale.parent.mkdir(parents=True)
                    stale.write_bytes(b"stale")
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch.object(verifier, "run", side_effect=git), \
                 mock.patch.object(verifier, "revision_release_tags", return_value=[]), \
                 mock.patch.object(verifier, "validate_preservable_git_checkout"):
                writable = verifier.materialize_packages(
                    source, checkout=source, base_env={"PATH": "/usr/bin"})
            self.assertIn(package / "os-reconstruction/.lake/build", writable)
            self.assertIn(package / "os-reconstruction/.lake/config", writable)
            self.assertNotIn(package / ".lake/build", writable)
            self.assertFalse((package / "os-reconstruction/.lake/build/Poison.olean").exists())

    def test_nested_project_resolver_refuses_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            source = Path(tmp).resolve()
            package = self.manifest(source, "nested")
            clone = source / ".lake/packages/OSReconstruction"
            clone.mkdir(parents=True)
            (clone / "nested").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(VerificationError):
                verifier.package_project_directory(source, package, checkout=source)
            self.assertFalse((Path(outside) / ".lake").exists())

    def test_trusted_url_map_binds_subdirectory_as_well_as_commit(self):
        expected = {"name": "trusted", "manifest_name": "trusted",
                    "url": "https://github.com/example/trusted", "revision": "b" * 40}
        actual = {**expected, "sub_dir": "alternate"}
        with mock.patch.object(verifier, "allowed_roots", return_value=([], {})):
            with self.assertRaisesRegex(VerificationError, "subdirectory"):
                verifier.trusted_package_url_map([actual], [expected])

