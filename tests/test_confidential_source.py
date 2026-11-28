"""SUB-2: confidential runs read the server's tarball, never GitHub.

The tarball is refused unless its SHA-256 is the dispatched one, it is
unpacked without GitHub's repository-named top-level directory, and the tree
becomes one commit so the verifier's tracked-file and attribute checks run.
Only HTTP 404 from the tombstone URL lets a run continue. A public run (no
confidential inputs) takes exactly the `clone_commit` path it always did.
"""

import argparse
import contextlib
import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

from scripts import confidential_source, render_challenge, verify_submission, workflow_report
from scripts.verification_errors import VerificationError

SOURCE_URL = "https://account.r2.cloudflarestorage.com/conf/abc/source.tar.gz?X-Amz-Signature=secret"
TOMBSTONE_URL = "https://account.r2.cloudflarestorage.com/conf-tombstones/abc?X-Amz-Signature=secret"
PREFIX = "secret-owner-secret-repo-1234567"


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def http_error(code):
    return HTTPError("https://example.invalid", code, "status", {}, None)


def opener(*outcomes):
    """A stand-in for `_open` answering bytes or raising, one outcome per call."""
    queue = list(outcomes)
    calls = []

    def open_(url, timeout):
        calls.append(url)
        outcome = queue.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return Response(outcome)

    open_.calls = calls
    return open_


def git_archive(files, *, prefix=PREFIX, modes=None):
    """A tarball made the way GitHub makes them: `git archive --prefix`."""
    with tempfile.TemporaryDirectory() as raw:
        repository = Path(raw) / "repository"
        repository.mkdir()
        for name, content in files.items():
            path = repository / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            if modes and name in modes:
                path.chmod(modes[name])
        env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
        git = ["git", "-C", str(repository), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run(["git", "init", "-q", str(repository)], env=env, check=True)
        subprocess.run([*git, "add", "--all", "--force"], env=env, check=True)
        subprocess.run([*git, "commit", "-qm", "fixture"], env=env, check=True)
        return subprocess.run(
            [*git, "archive", "--format=tar.gz", f"--prefix={prefix}/", "HEAD"],
            env=env, check=True, capture_output=True,
        ).stdout


def handmade_tarball(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        for info, data in entries:
            bundle.addfile(info, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()


def entry(name, *, kind=tarfile.REGTYPE, data=b"", linkname=""):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    info.linkname = linkname
    info.mode = 0o755 if kind == tarfile.DIRTYPE else 0o644
    return info, data if kind == tarfile.REGTYPE else None


def tracked(checkout):
    listed = subprocess.run(
        ["git", "-C", str(checkout), "ls-files", "-z"], check=True, capture_output=True, text=True
    ).stdout
    return sorted(name for name in listed.split("\0") if name)


class InputTests(unittest.TestCase):
    def test_public_runs_have_no_confidential_inputs(self):
        self.assertIsNone(confidential_source.confidential_inputs({}))
        self.assertIsNone(
            confidential_source.confidential_inputs(
                {"source_url": "", "source_sha256": "", "tombstone_url": None}
            )
        )

    def test_a_partial_set_is_refused_not_read_as_public(self):
        for missing in confidential_source.INPUTS:
            values = {"source_url": SOURCE_URL, "source_sha256": "a" * 64, "tombstone_url": TOMBSTONE_URL}
            values[missing] = ""
            with self.subTest(missing), self.assertRaises(VerificationError):
                confidential_source.confidential_inputs(values)

    def test_urls_must_be_https_without_credentials_and_the_digest_exact(self):
        for source_url, digest in (
            ("http://account.r2.cloudflarestorage.com/x", "a" * 64),
            ("https://user:pass@account.r2.cloudflarestorage.com/x", "a" * 64),
            ("file:///etc/passwd", "a" * 64),
            (SOURCE_URL, "A" * 64),
            (SOURCE_URL, "a" * 63),
        ):
            with self.subTest(source_url=source_url, digest=digest), self.assertRaises(VerificationError):
                confidential_source.confidential_inputs(
                    {"source_url": source_url, "source_sha256": digest, "tombstone_url": TOMBSTONE_URL}
                )


class DownloadTests(unittest.TestCase):
    def test_matching_digest_is_kept(self):
        data = b"tarball bytes"
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "source.tar.gz"
            with mock.patch.object(confidential_source, "_open", opener(data)):
                confidential_source.download(SOURCE_URL, hashlib.sha256(data).hexdigest(), target)
            self.assertEqual(target.read_bytes(), data)

    def test_hash_mismatch_is_refused_and_nothing_is_kept(self):
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "source.tar.gz"
            with mock.patch.object(confidential_source, "_open", opener(b"other bytes")):
                with self.assertRaisesRegex(VerificationError, "does not match source_sha256"):
                    confidential_source.download(SOURCE_URL, "0" * 64, target)
            self.assertFalse(target.exists())

    def test_transient_failures_retry_and_refusals_never_name_the_url(self):
        data = b"tarball bytes"
        digest = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "source.tar.gz"
            sleeps = []
            with mock.patch.object(confidential_source, "_open", opener(http_error(503), data)):
                confidential_source.download(SOURCE_URL, digest, target, sleep=sleeps.append)
            self.assertEqual(sleeps, [5])
            with mock.patch.object(confidential_source, "_open", opener(http_error(403))):
                with self.assertRaises(VerificationError) as caught:
                    confidential_source.download(SOURCE_URL, digest, target, sleep=sleeps.append)
            self.assertNotIn("secret", str(caught.exception))
            self.assertFalse(target.exists())


class UnpackTests(unittest.TestCase):
    def materialized(self, data, files_check=None):
        raw = tempfile.TemporaryDirectory()
        self.addCleanup(raw.cleanup)
        destination = Path(raw.name) / "source"
        with mock.patch.object(confidential_source, "_open", opener(data)):
            confidential_source.materialize(SOURCE_URL, hashlib.sha256(data).hexdigest(), destination)
        return destination

    def test_tarball_becomes_one_commit_of_every_tracked_file(self):
        data = git_archive(
            {
                "lakefile.toml": 'name = "Example"\n',
                "Challenge.lean": "theorem t : True := trivial\n",
                ".gitignore": "*.lean\nbuild/\n",
                "build/kept.txt": "tracked although ignored\n",
                "run.sh": "#!/bin/sh\n",
            },
            modes={"run.sh": 0o755},
        )
        checkout = self.materialized(data)
        # .gitignore must not drop files the commit tracked.
        self.assertEqual(
            tracked(checkout), [".gitignore", "Challenge.lean", "build/kept.txt", "lakefile.toml", "run.sh"]
        )
        self.assertTrue((checkout / ".git").is_dir())
        stage = subprocess.run(
            ["git", "-C", str(checkout), "ls-files", "--stage", "run.sh"],
            check=True, capture_output=True, text=True,
        ).stdout
        self.assertTrue(stage.startswith("100755 "))
        status = subprocess.run(
            ["git", "-C", str(checkout), "status", "--porcelain", "--ignored"],
            check=True, capture_output=True, text=True,
        ).stdout
        self.assertEqual(status, "")
        # GitHub's top-level directory names the repository; it never lands.
        self.assertNotIn(PREFIX, "\n".join(str(path) for path in checkout.rglob("*")))
        verify_submission.validate_preservable_git_checkout(checkout, "submitted source")

    def test_attribute_checks_still_see_the_tree(self):
        data = git_archive({".gitattributes": "*.bin filter=lfs diff=lfs merge=lfs -text\n", "a.bin": "x\n"})
        checkout = self.materialized(data)
        with self.assertRaisesRegex(VerificationError, "Git LFS"):
            verify_submission.validate_preservable_git_checkout(checkout, "submitted source")

    def test_unsafe_or_unexpected_layouts_are_refused(self):
        top = entry("top", kind=tarfile.DIRTYPE)
        for label, entries in (
            ("two top-level directories", [top, entry("other", kind=tarfile.DIRTYPE)]),
            ("top-level file", [entry("file", data=b"x")]),
            ("parent traversal", [top, entry("top/../../escape", data=b"x")]),
            ("absolute symlink", [top, entry("top/link", kind=tarfile.SYMTYPE, linkname="/etc/passwd")]),
            ("escaping symlink", [top, entry("top/link", kind=tarfile.SYMTYPE, linkname="../../x")]),
            ("hard link", [
                top, entry("top/a", data=b"x"), entry("top/b", kind=tarfile.LNKTYPE, linkname="top/a"),
            ]),
            ("device", [top, entry("top/dev", kind=tarfile.CHRTYPE)]),
            ("git metadata", [
                top, entry("top/.git", kind=tarfile.DIRTYPE), entry("top/.git/config", data=b"x"),
            ]),
        ):
            with self.subTest(label), tempfile.TemporaryDirectory() as raw:
                archive = Path(raw) / "source.tar.gz"
                archive.write_bytes(handmade_tarball(entries))
                with self.assertRaises(VerificationError):
                    confidential_source.unpack(archive, Path(raw) / "source")
                self.assertFalse((Path(raw) / "escape").exists())

    def test_unpacked_size_is_capped_before_extraction(self):
        with tempfile.TemporaryDirectory() as raw:
            archive = Path(raw) / "source.tar.gz"
            archive.write_bytes(
                handmade_tarball([entry("top", kind=tarfile.DIRTYPE), entry("top/a", data=b"x" * 11)])
            )
            with mock.patch.object(confidential_source, "MAX_SOURCE_BYTES", 10):
                with self.assertRaisesRegex(VerificationError, "cap"):
                    confidential_source.unpack(archive, Path(raw) / "source")
            self.assertFalse((Path(raw) / "source").exists())


class TombstoneTests(unittest.TestCase):
    def test_only_404_means_not_withdrawn(self):
        for outcomes, absent in (
            ((http_error(404),), True),
            ((b"",), False),  # the tombstone exists
            ((b'{"id":"abc"}',), False),
            ((http_error(403),), False),  # expired or wrong signature
            ((http_error(400),), False),
            ((http_error(503), http_error(404)), True),
            ((URLError("down"), URLError("down"), URLError("down")), False),
            ((http_error(500), http_error(500), http_error(500)), False),
        ):
            with self.subTest(outcomes=outcomes):
                with mock.patch.object(confidential_source, "_open", opener(*outcomes)):
                    absent_now = confidential_source.tombstone_absent(TOMBSTONE_URL, sleep=lambda _: None)
                self.assertIs(absent_now, absent)

    def run_check(self, url, *outcomes):
        output = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"PALOMAR_TOMBSTONE_URL": url}),
            mock.patch.object(confidential_source, "_open", opener(*outcomes)),
            mock.patch.object(confidential_source.time, "sleep"),
            contextlib.redirect_stdout(output),
        ):
            code = confidential_source.main(["check-tombstone"])
        self.assertNotIn("secret", output.getvalue())
        return code

    def test_check_step_stops_the_job_unless_the_tombstone_is_absent(self):
        self.assertEqual(self.run_check(TOMBSTONE_URL, http_error(404)), 0)
        self.assertEqual(self.run_check(TOMBSTONE_URL, b""), 1)
        self.assertEqual(self.run_check(TOMBSTONE_URL, http_error(403)), 1)
        self.assertEqual(self.run_check(""), 1)
        self.assertEqual(self.run_check("http://insecure.invalid/x"), 1)


def verifier_event(root, **extra):
    event = root / "event.json"
    event.write_text(json.dumps({"inputs": {
        "repository": "owner/repo",
        "commit": "b" * 40,
        "request_id": "abc123def456",
        "options": json.dumps({
            "authorization_relationship": "I am a Palomar Technical Maintainer testing the workflow",
            "comparator_config_path": "comparator.json",
        }),
        **extra,
    }}))
    return argparse.Namespace(
        event=str(event), output=str(root / "report.json"), work_dir=str(root / "work"),
        licensee=str(root / "licensee"),
    )


class VerifierSourceTests(unittest.TestCase):
    def prepare(self, **extra):
        raw = tempfile.TemporaryDirectory()
        self.addCleanup(raw.cleanup)
        root = Path(raw.name)
        (root / "work").mkdir()
        args = verifier_event(root, **extra)
        stop = VerificationError("source stage reached")
        with (
            mock.patch.object(verify_submission, "clone_commit", side_effect=stop) as clone,
            mock.patch.object(verify_submission, "workflow_output"),
        ):
            self.assertEqual(verify_submission.prepare(args), 0)
        return clone, json.loads((root / "report.json").read_text()), root

    def test_public_run_clones_github_exactly_as_before(self):
        with mock.patch.object(confidential_source, "materialize") as materialize:
            clone, report, root = self.prepare()
        clone.assert_called_once_with("https://github.com/owner/repo", "b" * 40, root / "work" / "source")
        materialize.assert_not_called()
        self.assertEqual(report["errors"], ["source stage reached"])

    def test_public_run_with_blank_call_inputs_clones_github(self):
        # A reusable call from a public preflight caller sends the defaults.
        with mock.patch.object(confidential_source, "materialize") as materialize:
            clone, _, _ = self.prepare(source_url="", source_sha256="", tombstone_url="")
        clone.assert_called_once()
        materialize.assert_not_called()

    def test_confidential_run_refuses_a_tarball_with_another_digest(self):
        with mock.patch.object(confidential_source, "_open", opener(b"some other tarball")):
            clone, report, root = self.prepare(
                source_url=SOURCE_URL, source_sha256="0" * 64, tombstone_url=TOMBSTONE_URL
            )
        clone.assert_not_called()
        self.assertIn(report["status"], {"fail", "error"})
        self.assertIn("Confidential source tarball does not match source_sha256", report["errors"])
        self.assertFalse((root / "work" / "source").exists())
        self.assertNotIn("secret", json.dumps(report))

    def test_confidential_run_unpacks_the_tarball_instead_of_cloning(self):
        data = git_archive({"lakefile.toml": 'name = "Example"\n'})
        with mock.patch.object(confidential_source, "_open", opener(data)):
            clone, report, root = self.prepare(
                source_url=SOURCE_URL, source_sha256=hashlib.sha256(data).hexdigest(),
                tombstone_url=TOMBSTONE_URL,
            )
        clone.assert_not_called()
        self.assertEqual(tracked(root / "work" / "source"), ["lakefile.toml"])
        # The run went on to the metadata checks on the unpacked tree.
        self.assertNotIn("Confidential", json.dumps(report["errors"]))
        self.assertNotIn("secret", json.dumps(report))
        self.assertNotIn(PREFIX, json.dumps(report))

    def test_partial_confidential_inputs_are_refused_before_any_fetch(self):
        with mock.patch.object(confidential_source, "_open") as open_:
            clone, report, _ = self.prepare(source_sha256="0" * 64)
        clone.assert_not_called()
        open_.assert_not_called()
        self.assertIn(report["status"], {"fail", "error"})


class RenderSourceTests(unittest.TestCase):
    def args(self, root, **extra):
        values = dict(
            repository="example/project", commit="1" * 40, challenge_sha256="2" * 64, project_path="",
            challenge_path="Challenge.lean", solution_path="Solution.lean",
            comparator_config_path="comparator.json", lakefile_path="lakefile.toml",
            lean_toolchain_path="lean-toolchain", work_dir=str(root / "work"),
            output=str(root / "report.json"),
        )
        values.update(extra)
        return argparse.Namespace(**values)

    def test_public_render_clones_github_exactly_as_before(self):
        for extra in ({}, {"source_url": "", "source_sha256": "", "tombstone_url": ""}):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                with (
                    mock.patch.object(
                        render_challenge, "clone_commit", side_effect=VerificationError("stop")
                    ) as clone,
                    mock.patch.object(confidential_source, "materialize") as materialize,
                ):
                    self.assertEqual(render_challenge.prepare(self.args(root, **extra)), 0)
                clone.assert_called_once_with(
                    "https://github.com/example/project", "1" * 40, root / "work" / "source"
                )
                materialize.assert_not_called()

    def test_confidential_render_refuses_a_tarball_with_another_digest(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            args = self.args(root, source_url=SOURCE_URL, source_sha256="0" * 64, tombstone_url=TOMBSTONE_URL)
            with (
                mock.patch.object(render_challenge, "clone_commit") as clone,
                mock.patch.object(confidential_source, "_open", opener(b"some other tarball")),
            ):
                self.assertEqual(render_challenge.prepare(args), 0)
            clone.assert_not_called()
            report = json.loads((root / "report.json").read_text())
            self.assertIn(report["status"], {"fail", "error"})
            self.assertIn("Confidential source tarball does not match source_sha256", report["errors"])
            self.assertNotIn("secret", json.dumps(report))

    def test_render_cli_defaults_the_source_to_github(self):
        parsed = render_challenge.parser().parse_args([
            "prepare", "--repository", "a/b", "--commit", "1" * 40, "--challenge-sha256", "2" * 64,
            "--challenge-path", "C.lean", "--solution-path", "S.lean", "--comparator-config-path", "c.json",
            "--lakefile-path", "lakefile.toml", "--lean-toolchain-path", "lean-toolchain",
            "--project-path", "",
            "--work-dir", "w", "--output", "o",
        ])
        self.assertEqual((parsed.source_url, parsed.source_sha256, parsed.tombstone_url), ("", "", ""))


class SummaryTests(unittest.TestCase):
    def finalize(self, inputs):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            summary = root / "summary.md"
            report = root / "report.json"
            report.write_text(json.dumps({
                "schema_version": 1, "status": "error", "errors": ["secret-repo/Challenge.lean failed"],
                "source": {"repository": "owner/repo", "commit": "a" * 40},
                "submission": {"submission_id": "abcdef123456"},
            }))
            env = {
                "PALOMAR_INPUTS": json.dumps(inputs),
                "PALOMAR_STEPS": "{}",
                "PALOMAR_WORKFLOW_URL": "https://github.com/PalomarRegistry/PalomarConfidentialRuns/actions/runs/7",
                "PALOMAR_RUN_REPOSITORY": "PalomarRegistry/PalomarConfidentialRuns",
                "PALOMAR_PIPELINE_COMMIT": "c" * 40,
                "GITHUB_STEP_SUMMARY": str(summary),
            }
            with (
                mock.patch.dict(os.environ, env),
                mock.patch("sys.argv", ["workflow_report.py", "finalize", "--report", str(report)]),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(workflow_report.main(), 0)
            return summary.read_text() if summary.exists() else ""

    INPUTS = {"repository": "owner/repo", "commit": "a" * 40, "request_id": "abcdef123456", "mode": "full"}

    def test_public_failures_still_reach_the_job_summary(self):
        self.assertIn("Palomar verification failed", self.finalize(self.INPUTS))

    def test_confidential_failures_never_reach_the_job_summary(self):
        self.assertEqual(self.finalize({**self.INPUTS, "source_url": SOURCE_URL}), "")


if __name__ == "__main__":
    unittest.main()
