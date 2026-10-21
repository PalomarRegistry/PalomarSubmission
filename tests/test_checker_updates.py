import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import checker_updates
from scripts.verification_errors import VerificationError


class CheckerUpdatesTests(unittest.TestCase):
    def test_only_the_exact_maintainer_selected_source_toolchain_gets_an_update(self):
        self.assertEqual(
            checker_updates.selected_revision("leanprover/lean4:v4.35.0")["release_tag"], "v4.36.0-rc1"
        )
        for source in ("leanprover/lean4:v4.36.0-rc1", "custom/lean4:v4.35.0", "v4.35.0"):
            self.assertIsNone(checker_updates.selected_revision(source))

    def test_authenticated_archive_and_binary_are_both_required_and_errors_never_fall_back(self):
        binary = b"authenticated checker executable"
        revision = copy.deepcopy(checker_updates.selected_revision("leanprover/lean4:v4.35.0"))
        revision["binary_sha256"] = hashlib.sha256(binary).hexdigest()

        def download(url, destination, digest, **kwargs):
            self.assertEqual(digest, revision["archive_sha256"])
            self.assertEqual(kwargs["max_bytes"], 700 * 1024 * 1024)
            destination.write_bytes(b"verified archive")

        for actual, valid in ((binary, True), (b"different checker", False)):
            with (
                self.subTest(valid=valid), tempfile.TemporaryDirectory() as raw,
                patch.object(checker_updates, "selected_revision", return_value=revision),
                patch.object(checker_updates, "download", side_effect=download),
                patch.object(checker_updates.platform, "system", return_value="Linux"),
                patch.object(checker_updates.platform, "machine", return_value="x86_64"),
                patch.object(checker_updates.subprocess, "run", return_value=subprocess.CompletedProcess(
                    [], 0, actual, b""
                )) as extract,
            ):
                root = Path(raw)
                if valid:
                    path, provenance = checker_updates.materialize_checker(
                        "source", root / "old", root / "new"
                    )
                    self.assertEqual(path.read_bytes(), binary)
                    self.assertEqual(path.stat().st_mode & 0o777, 0o555)
                    self.assertEqual(provenance["binary_sha256"], hashlib.sha256(binary).hexdigest())
                    self.assertEqual(extract.call_args.args[0][-1], "lean-4.36.0-rc1-linux/bin/con-ron")
                else:
                    with self.assertRaises(VerificationError) as error:
                        checker_updates.materialize_checker("source", root / "old", root / "new")
                    self.assertEqual(error.exception.code, "palomar.checker_authentication_failed")
                    self.assertFalse(error.exception.retryable)
                    self.assertFalse((root / "new" / "con-ron").exists())
                self.assertFalse((root / "new" / "release.tar.zst").exists())

    def test_manifest_rejects_unreviewed_fields_and_non_release_paths(self):
        document = json.loads(checker_updates.MANIFEST.read_text())
        for field, value in (("release_tag", "../../candidate"), ("binary_sha256", ""), ("url", "evil")):
            broken = copy.deepcopy(document)
            broken["con_ron"]["leanprover/lean4:v4.35.0"][field] = value
            with tempfile.TemporaryDirectory() as raw:
                manifest = Path(raw) / "manifest.json"
                manifest.write_text(json.dumps(broken))
                with patch.object(checker_updates, "MANIFEST", manifest), self.assertRaises(ValueError):
                    checker_updates.selected_revision("leanprover/lean4:v4.35.0")

    def test_only_temporary_provider_failures_are_retryable(self):
        failures = (
            (checker_updates.DownloadTransientError("bounded network retries exhausted"), True),
            (checker_updates.DownloadPermanentError("HTTP 404"), False),
            (checker_updates.DownloadAuthenticationError("checksum mismatch"), False),
            (ValueError("invalid manifest"), False),
        )
        for failure, retryable in failures:
            with (
                self.subTest(failure=failure), tempfile.TemporaryDirectory() as raw,
                patch.object(checker_updates.platform, "system", return_value="Linux"),
                patch.object(checker_updates.platform, "machine", return_value="x86_64"),
                patch.object(checker_updates, "download", side_effect=failure),
                self.assertRaises(VerificationError) as error,
            ):
                checker_updates.materialize_checker(
                    "leanprover/lean4:v4.35.0", Path(raw) / "old", Path(raw) / "new"
                )
            self.assertEqual(error.exception.retryable, retryable)
        with patch.object(checker_updates.platform, "system", return_value="Darwin"), self.assertRaises(
            VerificationError
        ) as error:
            checker_updates.materialize_checker("leanprover/lean4:v4.35.0", Path("old"), Path("new"))
        self.assertEqual(error.exception.code, "palomar.checker_policy_invalid")
        self.assertFalse(error.exception.retryable)

    def test_duplicate_manifest_keys_fail_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            manifest = Path(raw) / "manifest.json"
            manifest.write_text('{"schema_version":1,"con_ron":{},"con_ron":{}}')
            with patch.object(checker_updates, "MANIFEST", manifest), self.assertRaisesRegex(
                ValueError, "duplicate"
            ):
                checker_updates.selected_revision("leanprover/lean4:v4.35.0")

    def test_release_commit_checks_cover_updates_outside_the_cold_build_fixture(self):
        revisions = checker_updates.revision_manifest()
        revisions["leanprover/lean4:v4.35.0-rc3"] = {
            **revisions["leanprover/lean4:v4.35.0-rc3"], "release_commit": "a" * 40,
        }
        with (
            patch.object(checker_updates, "revision_manifest", return_value=revisions),
            self.assertRaisesRegex(ValueError, "disagrees"),
        ):
            checker_updates.verify_release_commits(
                lambda _repo, _tag: "c9afcddf514a6772bd4d649f7cfc04eff0b9f15e"
            )
