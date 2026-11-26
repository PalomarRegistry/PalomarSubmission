"""Confidential snapshot bundles: capture, restore, validation and refusals.

Everything runs against local bare repositories standing in for GitHub, and
fake broker and GitHub API transports. The restore tests are contract v2,
part 4 (1, 2 and the negatives that concern bundles); the credential tests
are the SUB-2 negatives in 6.2.
"""

import base64
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import confidential_snapshot as cs
from scripts import source_bundles as sb
from scripts.source_bundles import SnapshotError

TOKEN = "ghs_" + "T" * 36
TICKET = "k" * 43
CAPABILITY = "c" * 43
NONCE = "a" * 32
SUBMISSION = "abcdef123456"
SOURCE_ID = 101
DEPENDENCY_ID = 202
PROJECT = {
    "project_path": "",
    "challenge_path": "Challenge.lean",
    "solution_path": "Solution.lean",
    "comparator_config_path": "comparator.json",
    "lakefile_path": "lakefile.toml",
    "lean_toolchain_path": "lean-toolchain",
}


def run_git(*arguments, cwd=None, env=None):
    base = {
        "PATH": os.environ["PATH"],
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Ada",
        "GIT_AUTHOR_EMAIL": "ada@example.invalid",
        "GIT_COMMITTER_NAME": "Committer",
        "GIT_COMMITTER_EMAIL": "committer@example.invalid",
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    }
    return subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "-c", "init.defaultBranch=main", *arguments],
        cwd=cwd,
        env={**base, **(env or {})},
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit(work, message, author):
    run_git("add", "--all", cwd=work)
    run_git(
        "commit",
        "--quiet",
        "-m",
        message,
        cwd=work,
        env={"GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": f"{author.lower()}@example.invalid"},
    )
    return run_git("rev-parse", "HEAD", cwd=work)


class Fixture:
    """A remote root holding `owner/source` (3 commits, 2 authors, a removed
    file) and `dep-owner/dep`, as bare repositories under `remote/`."""

    def __init__(self, root: Path, manifest_packages=None):
        self.root = root
        self.remote = root / "remote"
        dependency = root / "dep-work"
        dependency.mkdir(parents=True)
        run_git("init", "--quiet", str(dependency))
        (dependency / "Dep.lean").write_text("def dep := 1\n")
        commit(dependency, "dep one", "Ada")
        (dependency / "Dep.lean").write_text("def dep := 2\n")
        self.dependency_rev = commit(dependency, "dep two", "Ada")
        self.publish(dependency, "dep-owner/dep")

        work = root / "work"
        work.mkdir()
        run_git("init", "--quiet", str(work))
        (work / "Removed.lean").write_text("-- removed later\n")
        (work / "Challenge.lean").write_text("theorem t : True := trivial\n")
        commit(work, "one", "Ada")
        for name, text in {
            "Solution.lean": "theorem t : True := trivial\n",
            "comparator.json": "{}\n",
            "lakefile.toml": 'name = "fixture"\n',
            "lean-toolchain": "leanprover/lean4:v4.35.0-rc2\n",
        }.items():
            (work / name).write_text(text)
        packages = (
            manifest_packages
            if manifest_packages is not None
            else [
                {
                    "type": "git",
                    "name": "dep",
                    "url": "https://github.com/dep-owner/dep.git",
                    "rev": self.dependency_rev,
                },
                {"type": "path", "name": "local", "dir": "local"},
            ]
        )
        (work / "lake-manifest.json").write_text(json.dumps({"version": "1.1.0", "packages": packages}))
        commit(work, "two", "Grace")
        (work / "Removed.lean").unlink()
        self.commit = commit(work, "three", "Ada")
        self.work = work
        self.publish(work, "owner/source")

    def publish(self, work, full_name):
        destination = self.remote / full_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        run_git("clone", "--quiet", "--bare", str(work), str(destination))

    def fetcher(self):
        return cs.Fetcher(
            base=f"file://{self.remote}", protocols=("file",), check_addresses=False, home=self.root / "home"
        )


class FakeGitHub(cs.GitHub):
    def __init__(self, private_dependency=False):
        super().__init__()
        self.revoked = []
        self.private_dependency = private_dependency

    def repository(self, full_name, token):
        if full_name == "owner/source":
            return {
                "full_name": "owner/source",
                "id": SOURCE_ID,
                "owner": {"id": 9},
                "private": token is not None,
            }
        return {
            "full_name": full_name,
            "id": DEPENDENCY_ID,
            "owner": {"id": 10},
            "private": self.private_dependency,
        }

    def revoke(self, token):
        self.revoked.append(token)


class FakeBroker(cs.Broker):
    """Records uploads in memory and serves them back, like the Worker."""

    def __init__(self, fixture, *, token=TOKEN, fail_plan=False):
        super().__init__(token_source=lambda: "jwt")
        self.fixture = fixture
        self.token = token
        self.fail_plan = fail_plan
        self.objects = {}
        self.finalized = None

    def open(self, ticket, operation):
        assert ticket == TICKET
        self.capability = CAPABILITY
        base = {"capability": CAPABILITY, "operation": operation, "submission_id": SUBMISSION}
        if operation == "snapshot":
            return {
                **base,
                "repository_ids": [SOURCE_ID],
                "commit": self.fixture.commit,
                "nonce": NONCE,
                "project": PROJECT,
                "object_keys": [],
                "source": {"full_name": "owner/source", "token": self.token, "expires_at": "x"},
            }
        if operation == "validate":
            return {**base, "object_keys": sorted(self.objects)}
        return {
            **base,
            "snapshot_sha256": sb.sha256_bytes(self.objects[f"v2/{SUBMISSION}/snapshot/descriptor.json"]),
        }

    def plan(self, dependencies):
        if self.fail_plan:
            raise SnapshotError("plan refused")
        keys = [
            f"v2/{SUBMISSION}/snapshot/source-manifest.json",
            f"v2/{SUBMISSION}/snapshot/{SOURCE_ID}-{self.fixture.commit}.bundle",
        ]
        keys += [f"v2/{SUBMISSION}/snapshot/{d['repository_id']}-{d['rev']}.bundle" for d in dependencies]
        return keys

    def put(self, key, path):
        self.objects[key] = path.read_bytes()

    def get(self, key, destination):
        destination.write_bytes(self.objects[key])
        return sb.sha256_bytes(self.objects[key])

    def finalize(self, descriptor):
        raw = sb.canonical_json(descriptor)
        self.objects[f"v2/{SUBMISSION}/snapshot/descriptor.json"] = raw
        self.finalized = descriptor
        return sb.sha256_bytes(raw)


IDENTITY = cs.JobIdentity(
    pipeline_commit="p" * 0 + "1" * 40,
    caller_repository_id=5,
    caller_workflow_sha="2" * 40,
    run_id=77,
    run_attempt=1,
)


class Case(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        (self.root / "home").mkdir()
        self.env = sb.git_environment(self.root / "home")

    def take_snapshot(self, fixture, **broker_options):
        broker = FakeBroker(fixture, **broker_options)
        github = FakeGitHub()
        out = self.root / "out"
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            descriptor = cs.snapshot(
                TICKET,
                out,
                self.root / "snapshot-work",
                broker=broker,
                identity=IDENTITY,
                github=github,
                fetcher=fixture.fetcher(),
            )
        return descriptor, broker, github, out, printed.getvalue()


class RestoreTests(Case):
    """Contract v2 part 4, restore tests 1 and 2."""

    def test_bundle_restores_complete_history_into_an_empty_repository(self):
        fixture = Fixture(self.root)
        descriptor, broker, _, out, _ = self.take_snapshot(fixture)
        bundle = out / "bundles" / f"{SOURCE_ID}-{fixture.commit}.bundle"
        _, prerequisites, refs = sb.bundle_header(bundle)
        self.assertEqual(prerequisites, [])
        self.assertEqual(refs, [(fixture.commit, sb.VERIFIED_REF)])
        empty = sb.restore_bundle(bundle, fixture.commit, self.root / "empty.git", self.env)
        record = sb.history(empty, fixture.commit, self.env)
        self.assertEqual(record, descriptor["source"]["history"])
        self.assertEqual(record["commit_count"], 3)
        self.assertEqual(sb.commit_tree(empty, fixture.commit, self.env), descriptor["source"]["tree"])
        manifest, _, _ = sb.source_manifest(empty, fixture.commit, "", self.env)
        self.assertEqual(sb.sha256_bytes(sb.canonical_json(manifest)), descriptor["source_manifest_sha256"])
        self.assertNotIn("Removed.lean", [item["path"] for item in manifest["files"]])
        authors = run_git("--git-dir", str(empty), "log", "--format=%an", sb.VERIFIED_REF)
        self.assertEqual(set(authors.split()), {"Ada", "Grace"})

    def test_a_restored_repository_pushes_whole_into_another_empty_one(self):
        fixture = Fixture(self.root)
        _, _, _, out, _ = self.take_snapshot(fixture)
        bundle = out / "bundles" / f"{SOURCE_ID}-{fixture.commit}.bundle"
        restored = sb.restore_bundle(bundle, fixture.commit, self.root / "restored.git", self.env)
        archive = self.root / "archive.git"
        run_git("init", "--quiet", "--bare", str(archive))
        run_git(
            "--git-dir",
            str(restored),
            "push",
            "--quiet",
            str(archive),
            f"{fixture.commit}:refs/tags/palomar/PALOMAR-X-v1/{fixture.commit}",
            f"{fixture.commit}:refs/heads/palomar-archive",
        )
        clone = self.root / "clone"
        run_git("clone", "--quiet", "--branch", "palomar-archive", f"file://{archive}", str(clone))
        self.assertEqual(run_git("rev-parse", "HEAD", cwd=clone), fixture.commit)
        self.assertEqual(run_git("rev-list", "--count", "HEAD", cwd=clone), "3")
        self.assertEqual(
            sb.history(clone / ".git", fixture.commit, self.env),
            sb.history(restored, fixture.commit, self.env),
        )


class BundleRefusalTests(Case):
    """Contract v2 part 4, negatives 4, for bundles and the snapshot."""

    def bundle(self, fixture, *arguments, name="b.bundle"):
        path = self.root / name
        run_git("bundle", "create", str(path), *arguments, cwd=fixture.work)
        return path

    def test_depth_one_bundle_is_refused(self):
        fixture = Fixture(self.root)
        shallow = self.root / "shallow"
        run_git("clone", "--quiet", "--depth=1", f"file://{fixture.remote}/owner/source", str(shallow))
        run_git("update-ref", sb.VERIFIED_REF, fixture.commit, cwd=shallow)
        path = self.root / "shallow.bundle"
        run_git("bundle", "create", str(path), sb.VERIFIED_REF, cwd=shallow)
        # A shallow repository writes a bundle with no prerequisites line but
        # without the parents either: the header check alone cannot see it,
        # so restoring into an empty repository is what refuses it.
        with self.assertRaises(SnapshotError):
            sb.restore_bundle(path, fixture.commit, self.root / "r.git", self.env)
        with self.assertRaisesRegex(SnapshotError, "shallow"):
            sb.create_bundle(shallow / ".git", fixture.commit, self.root / "again.bundle", self.env)

    def test_incremental_bundle_is_refused(self):
        fixture = Fixture(self.root)
        run_git("update-ref", sb.VERIFIED_REF, fixture.commit, cwd=fixture.work)
        path = self.bundle(fixture, "HEAD~1..refs/palomar/verified")
        with self.assertRaisesRegex(SnapshotError, "prerequisites"):
            sb.check_bundle_header(path, fixture.commit)

    def test_two_refs_another_ref_or_another_commit_are_refused(self):
        fixture = Fixture(self.root)
        run_git("update-ref", sb.VERIFIED_REF, fixture.commit, cwd=fixture.work)
        run_git("update-ref", "refs/palomar/other", fixture.commit, cwd=fixture.work)
        cases = {
            "two": ([sb.VERIFIED_REF, "refs/palomar/other"], fixture.commit),
            "other": (["refs/palomar/other"], fixture.commit),
            "commit": ([sb.VERIFIED_REF], run_git("rev-parse", "HEAD~1", cwd=fixture.work)),
        }
        for name, (refs, target) in cases.items():
            with self.subTest(name):
                path = self.bundle(fixture, *refs, name=f"{name}.bundle")
                with self.assertRaisesRegex(SnapshotError, "exactly"):
                    sb.check_bundle_header(path, target)

    def test_replace_refs_and_grafts_do_not_change_the_history_digest(self):
        fixture = Fixture(self.root)
        git_dir = fixture.remote / "owner/source"
        before = sb.history(git_dir, fixture.commit, self.env)
        first = run_git("--git-dir", str(git_dir), "rev-list", "--max-parents=0", fixture.commit)
        replacement = run_git(
            "--git-dir", str(git_dir), "commit-tree", "-m", "forged", f"{fixture.commit}^{{tree}}"
        )
        run_git("--git-dir", str(git_dir), "replace", first, replacement)
        (git_dir / "info").mkdir(exist_ok=True)
        (git_dir / "info/grafts").write_text(f"{fixture.commit}\n")
        self.assertEqual(sb.history(git_dir, fixture.commit, self.env), before)

    def test_gitlinks_and_lfs_attributes_are_refused_at_snapshot(self):
        for name, change in {
            "gitlink": lambda work: run_git(
                "update-index", "--add", "--cacheinfo", f"160000,{'e' * 40},vendor/sub", cwd=work
            ),
            "lfs": lambda work: (work / "data/.gitattributes").write_text("*.bin filter=lfs diff=lfs\n"),
        }.items():
            with self.subTest(name):
                root = self.root / name
                fixture = Fixture(root)
                (fixture.work / "data").mkdir()
                (fixture.work / "data/keep").write_text("x\n")
                change(fixture.work)
                run_git("add", "data", cwd=fixture.work)
                run_git(
                    "commit",
                    "--quiet",
                    "-m",
                    name,
                    cwd=fixture.work,
                    env={"GIT_AUTHOR_NAME": "Ada", "GIT_AUTHOR_EMAIL": "ada@example.invalid"},
                )
                head = run_git("rev-parse", "HEAD", cwd=fixture.work)
                run_git(
                    "push",
                    "--quiet",
                    "--force",
                    str(fixture.remote / "owner/source"),
                    "HEAD:main",
                    cwd=fixture.work,
                )
                git_dir = fixture.remote / "owner/source"
                with self.assertRaisesRegex(SnapshotError, "submodule|LFS"):
                    sb.source_manifest(git_dir, head, "", self.env)

    def test_paths_must_be_utf8_without_control_characters(self):
        with self.assertRaisesRegex(SnapshotError, "UTF-8"):
            sb.validate_path(b"bad\xff")
        with self.assertRaisesRegex(SnapshotError, "control"):
            sb.validate_path(b"bad\x1bname")


class DependencyRuleTests(unittest.TestCase):
    def test_only_plain_github_urls_are_accepted(self):
        self.assertEqual(
            sb.canonical_github_repository("https://github.com/o/r.git").url, "https://github.com/o/r"
        )
        self.assertEqual(sb.canonical_github_repository("https://github.com/o/r").full_name, "o/r")
        for url in (
            "http://github.com/o/r",
            "https://gitlab.com/o/r",
            "https://github.com.evil.example/o/r",
            "https://user:pass@github.com/o/r",
            "https://x-access-token@github.com/o/r",
            "https://github.com:8443/o/r",
            "https://github.com/o/r?x=1",
            "https://github.com/o/r#x",
            "https://github.com/o/r/tree/main",
            "https://github.com/o",
            "https://github.com/o/..",
            "file:///tmp/o/r",
            "git@github.com:o/r.git",
            "https://127.0.0.1/o/r",
            "https://github.com/o/r\n",
        ):
            with self.subTest(url):
                with self.assertRaises(SnapshotError):
                    sb.canonical_github_repository(url)

    def test_private_addresses_for_github_are_refused(self):
        for address in ("10.0.0.1", "127.0.0.1", "169.254.169.254", "::1", "fd00::1"):
            with self.subTest(address):
                with self.assertRaisesRegex(SnapshotError, "non-public"):
                    sb.require_public_github_addresses(
                        lambda *a, address=address, **k: [(0, 0, 0, "", (address, 443))]
                    )
        sb.require_public_github_addresses(lambda *a, **k: [(0, 0, 0, "", ("140.82.112.3", 443))])

    def test_git_never_follows_redirects_or_uses_ambient_credentials(self):
        flags = " ".join(sb.GIT_BASE)
        for setting in (
            "http.followRedirects=false",
            "credential.helper=",
            "protocol.allow=never",
            "core.hooksPath=/dev/null",
        ):
            self.assertIn(setting, flags)

    def test_redirecting_metadata_is_refused(self):
        def moved(method, url, headers, body):
            return cs.Response(
                200,
                {},
                json.dumps(
                    {"full_name": "other/name", "id": 1, "owner": {"id": 2}, "private": False}
                ).encode(),
            )

        with self.assertRaisesRegex(SnapshotError, "different repository"):
            cs.GitHub(transport=moved).repository("o/r", None)
        self.assertIsNone(cs.NoRedirects().redirect_request(None, None, 301, "", {}, "https://elsewhere"))

    def test_trusted_closure_follows_only_an_official_roots_own_manifest(self):
        rev = "3" * 40
        packages = [
            {"name": "mathlib", "url": "https://github.com/leanprover-community/mathlib4", "rev": rev},
            {
                "name": "batteries",
                "url": "https://github.com/leanprover-community/batteries",
                "rev": "4" * 40,
            },
            {"name": "aesop", "url": "https://github.com/someone/aesop", "rev": "5" * 40},
            {"name": "mine", "url": "https://github.com/me/mine", "rev": "6" * 40},
        ]
        nested = json.dumps(
            {
                "packages": [
                    {
                        "type": "git",
                        "name": "batteries",
                        "url": "https://github.com/leanprover-community/batteries",
                        "rev": "4" * 40,
                    },
                    {
                        "type": "git",
                        "name": "aesop",
                        "url": "https://github.com/leanprover-community/aesop",
                        "rev": "5" * 40,
                    },
                ]
            }
        ).encode()

        class Roots(cs.RootReader):
            def __init__(self, official):
                super().__init__(Path("/nonexistent"), cs.Fetcher(check_addresses=False))
                self.is_official = official

            def official(self, root, revision):
                return self.is_official

            def manifest(self, root, revision):
                return nested if revision == rev else None

        # A substituted fork of a closure package is not trusted, so it is bundled.
        self.assertEqual(cs.trusted_closure(packages, Roots(True)), {"mathlib", "batteries"})
        # A root commit outside official history (say, from a pull request)
        # exempts nothing: everything is bundled.
        self.assertEqual(cs.trusted_closure(packages, Roots(False)), set())


class RootOfficialHistoryTests(Case):
    def test_only_commits_in_official_history_or_accepted_revisions_count(self):
        work = self.root / "root-work"
        work.mkdir()
        run_git("init", "--quiet", "--initial-branch=master", str(work))
        (work / "lake-manifest.json").write_text('{"packages": []}')
        official = commit(work, "official", "Ada")
        run_git("switch", "--quiet", "-c", "pull/1/head", cwd=work)
        (work / "lake-manifest.json").write_text(
            json.dumps(
                {
                    "packages": [
                        {
                            "type": "git",
                            "name": "payload",
                            "url": "https://github.com/attacker/payload",
                            "rev": "7" * 40,
                        }
                    ]
                }
            )
        )
        unofficial = commit(work, "from a pull request", "Mallory")
        remote = self.root / "remote" / "leanprover-community" / "mathlib4"
        remote.parent.mkdir(parents=True)
        run_git("clone", "--quiet", "--bare", "--mirror", str(work), str(remote))
        fetcher = cs.Fetcher(
            base=f"file://{self.root / 'remote'}",
            protocols=("file",),
            check_addresses=False,
            home=self.root / "home",
        )
        config = [
            {
                "repository": "leanprover-community/mathlib4",
                "repository_aliases": [],
                "official_ref": "refs/heads/master",
                "accepted_revisions": [],
                "include_semver_release_tags": True,
            }
        ]
        roots = cs.RootReader(self.root / "roots", fetcher, config)
        (self.root / "roots").mkdir()
        root = roots.root("https://github.com/leanprover-community/mathlib4")
        self.assertTrue(roots.official(root, official))
        self.assertFalse(roots.official(root, unofficial))
        accepting = cs.RootReader(
            self.root / "roots2", fetcher, [{**config[0], "accepted_revisions": [unofficial]}]
        )
        self.assertTrue(
            accepting.official(accepting.root("https://github.com/leanprover-community/mathlib4"), unofficial)
        )
        packages = [
            {"name": "mathlib", "url": "https://github.com/leanprover-community/mathlib4", "rev": unofficial},
            {"name": "payload", "url": "https://github.com/attacker/payload", "rev": "7" * 40},
        ]
        self.assertEqual(cs.trusted_closure(packages, roots), set())


class SnapshotCredentialTests(Case):
    """SUB-2 negatives: the source token never escapes the snapshot job."""

    def test_token_is_masked_used_only_for_the_source_and_revoked(self):
        fixture = Fixture(self.root)
        descriptor, broker, github, out, printed = self.take_snapshot(fixture)
        basic = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
        self.assertIn(f"::add-mask::{TOKEN}", printed)
        self.assertIn(f"::add-mask::{basic}", printed)
        self.assertLess(printed.index(f"::add-mask::{TOKEN}"), len(printed))
        self.assertEqual(github.revoked, [TOKEN])
        self.assertEqual(descriptor["source"]["route"], "source-app")
        for directory in (out, self.root / "snapshot-work", self.root / "home"):
            for path in directory.rglob("*"):
                if path.is_file():
                    data = path.read_bytes()
                    self.assertNotIn(TOKEN.encode(), data, path)
                    self.assertNotIn(basic.encode(), data, path)
        for blob in broker.objects.values():
            self.assertNotIn(TOKEN.encode(), blob)
        config = (self.root / "snapshot-work/source.git/config").read_text()
        self.assertNotIn("extraheader", config)
        self.assertNotIn("credential", config)
        self.assertNotIn(TOKEN, os.environ.get("GIT_CONFIG_VALUE_0", ""))

    def test_token_is_revoked_when_the_snapshot_fails(self):
        fixture = Fixture(self.root)
        with self.assertRaisesRegex(SnapshotError, "plan refused"):
            self.take_snapshot(fixture, fail_plan=True)
        # take_snapshot raised before returning its GitHub fake; repeat with a handle on it.
        github = FakeGitHub()
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SnapshotError):
            cs.snapshot(
                TICKET,
                self.root / "out2",
                self.root / "work2",
                broker=FakeBroker(fixture, fail_plan=True),
                identity=IDENTITY,
                github=github,
                fetcher=fixture.fetcher(),
            )
        self.assertEqual(github.revoked, [TOKEN])

    def test_public_route_has_no_token_and_revokes_nothing(self):
        fixture = Fixture(self.root)
        descriptor, _, github, _, printed = self.take_snapshot(fixture, token=None)
        self.assertEqual(github.revoked, [])
        self.assertEqual(descriptor["source"]["route"], "public")
        self.assertEqual(descriptor["source"]["visibility"], "public")

    def test_private_dependency_is_refused(self):
        fixture = Fixture(self.root)
        github = FakeGitHub(private_dependency=True)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(SnapshotError, "not public"):
            cs.snapshot(
                TICKET,
                self.root / "out",
                self.root / "work",
                broker=FakeBroker(fixture),
                identity=IDENTITY,
                github=github,
                fetcher=fixture.fetcher(),
            )
        self.assertEqual(github.revoked, [TOKEN])

    def test_dependency_urls_outside_github_fail_the_snapshot(self):
        fixture = Fixture(
            self.root,
            manifest_packages=[
                {"type": "git", "name": "evil", "url": "https://evil.example/o/r", "rev": "7" * 40},
            ],
        )
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(SnapshotError, "github.com"):
            self.take_snapshot(fixture)

    def test_scripts_never_export_to_the_job_environment(self):
        for name in ("confidential_snapshot.py", "source_bundles.py"):
            text = (Path(cs.__file__).parent / name).read_text()
            self.assertNotIn("GITHUB_ENV", text)
            self.assertNotIn("GITHUB_PATH", text)


class ValidationTests(Case):
    def validated(self):
        fixture = Fixture(self.root)
        descriptor, broker, _, out, _ = self.take_snapshot(fixture)
        validated = self.root / "validated"
        with contextlib.redirect_stdout(io.StringIO()):
            digest = cs.validate(
                TICKET,
                out / "descriptor-draft.json",
                validated,
                self.root / "validate-work",
                broker=broker,
                identity=IDENTITY,
                fetcher=fixture.fetcher(),
            )
        return fixture, descriptor, broker, validated, digest

    def test_validation_restores_checks_and_finalizes(self):
        fixture, descriptor, broker, validated, digest = self.validated()
        self.assertEqual(broker.finalized, descriptor)
        self.assertEqual(digest, sb.sha256_bytes(sb.canonical_json(descriptor)))
        bundles = sb.SourceBundles.load(validated, digest)
        bundles.require_source("owner/source", fixture.commit)
        row = bundles.dependency("https://github.com/dep-owner/dep.git", fixture.dependency_rev)
        self.assertEqual(row["kind"], "public")
        checkout = self.root / "checkout"
        bundles.checkout(bundles.source_bundle(), fixture.commit, checkout)
        self.assertTrue((checkout / "Challenge.lean").is_file())
        self.assertFalse((checkout / "Removed.lean").exists())

    def test_tampered_descriptors_are_refused(self):
        fixture = Fixture(self.root)
        descriptor, broker, _, out, _ = self.take_snapshot(fixture)
        bundles = out / "bundles"
        manifest = (out / "source-manifest.json").read_bytes()
        changes = {
            "history": lambda d: d["source"]["history"].update(history_sha256="0" * 64),
            "count": lambda d: d["source"]["history"].update(commit_count=2),
            "tree": lambda d: d["source"].update(tree="0" * 40),
            "dependency kind": lambda d: d["dependencies"][0].update(kind="trusted-root", bundle=None),
            "extra dependency": lambda d: d["dependencies"].append({**d["dependencies"][0], "name": "extra"}),
            "dropped dependency": lambda d: d["dependencies"].clear(),
            "project path": lambda d: d["project"].update(challenge_path="Missing.lean"),
            "lake manifest": lambda d: d.update(lake_manifest_sha256="0" * 64),
        }
        for index, (name, change) in enumerate(changes.items()):
            with self.subTest(name):
                tampered = json.loads(json.dumps(descriptor))
                change(tampered)
                with self.assertRaises(SnapshotError):
                    cs.restore_and_check(
                        tampered, bundles, manifest, self.root / f"w{index}", fetcher=fixture.fetcher()
                    )

    def test_bundle_directory_refusals(self):
        fixture, descriptor, _, validated, digest = self.validated()
        with self.assertRaisesRegex(SnapshotError, "not the snapshot"):
            sb.SourceBundles.load(validated, "0" * 64)
        bundles = sb.SourceBundles.load(validated, digest)
        with self.assertRaisesRegex(SnapshotError, "different repository"):
            bundles.require_source("owner/other", fixture.commit)
        with self.assertRaisesRegex(SnapshotError, "not in the snapshot"):
            bundles.dependency("https://github.com/x/y", "8" * 40)
        with self.assertRaisesRegex(SnapshotError, "another revision"):
            bundles.dependency("https://github.com/dep-owner/dep", "8" * 40)
        # The renderer may fetch exactly its own Verso packages, nothing else.
        with self.assertRaisesRegex(SnapshotError, "not in the snapshot"):
            bundles.dependency("https://github.com/leanprover/verso", "8" * 40)
        bundles.renderer_packages = {("https://github.com/leanprover/verso.git", "8" * 40)}
        self.assertIsNone(bundles.dependency("https://github.com/leanprover/verso", "8" * 40))
        with self.assertRaisesRegex(SnapshotError, "not in the snapshot"):
            bundles.dependency("https://github.com/leanprover/verso", "9" * 40)
        with self.assertRaisesRegex(SnapshotError, "not in the snapshot"):
            bundles.dependency("https://github.com/attacker/payload", "8" * 40)
        target = validated / "bundles" / f"{DEPENDENCY_ID}-{fixture.dependency_rev}.bundle"
        target.write_bytes(target.read_bytes() + b"x")
        with self.assertRaisesRegex(SnapshotError, "does not match"):
            sb.SourceBundles.load(validated, digest)

    def test_restore_rechecks_everything(self):
        fixture, descriptor, broker, _, digest = self.validated()
        out = self.root / "restored"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                cs.restore(
                    TICKET,
                    out,
                    self.root / "restore-work",
                    broker=broker,
                    fetcher=fixture.fetcher(),
                    expected=digest,
                ),
                digest,
            )
        sb.SourceBundles.load(out, digest)
        with (
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(SnapshotError, "another snapshot"),
        ):
            cs.restore(
                TICKET,
                self.root / "r2",
                self.root / "rw2",
                broker=broker,
                fetcher=fixture.fetcher(),
                expected="0" * 64,
            )


class JobIdentityTests(unittest.TestCase):
    CLAIMS = {
        "job_workflow_ref": "PalomarRegistry/PalomarSubmission/.github/workflows/confidential-snapshot.yml@"
        + "1" * 40,
        "job_workflow_sha": "1" * 40,
        "repository_id": "5",
        "workflow_sha": "2" * 40,
        "run_id": "77",
        "run_attempt": "1",
    }

    def test_identity_comes_from_the_token_not_from_inputs(self):
        identity = cs.JobIdentity.from_claims(self.CLAIMS, "confidential-snapshot.yml")
        self.assertEqual(identity.pipeline_commit, "1" * 40)
        self.assertEqual((identity.run_id, identity.run_attempt), (77, 1))

    def test_another_workflow_is_refused(self):
        for claims in (
            {**self.CLAIMS, "job_workflow_ref": "Someone/Fork/.github/workflows/confidential-snapshot.yml@x"},
            {**self.CLAIMS, "job_workflow_sha": "not-a-sha"},
            {key: value for key, value in self.CLAIMS.items() if key != "run_id"},
        ):
            with self.subTest(claims=claims):
                with self.assertRaises(SnapshotError):
                    cs.JobIdentity.from_claims(claims, "confidential-snapshot.yml")
        with self.assertRaises(SnapshotError):
            cs.JobIdentity.from_claims(self.CLAIMS, "confidential-restore.yml")

    def test_no_oidc_permission_means_no_token(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(SnapshotError, "OIDC"):
            cs.oidc_token()


if __name__ == "__main__":
    unittest.main()


class VerifierBundleModeTests(Case):
    """The credential-free verify job reads source from the bundles only."""

    def prepare(self, bundles, repository, commit):
        from scripts import verify_submission
        from scripts.verification_errors import VerificationError

        inputs = {
            "repository": repository,
            "commit": commit,
            "request_id": SUBMISSION,
            "mode": "full",
            "pipeline_commit": "1" * 40,
            "options": json.dumps(
                {
                    "comparator_config_path": "comparator.json",
                    "authorization_relationship": "I am a responsible author or maintainer",
                }
            ),
        }
        event = self.root / "event.json"
        event.write_text(json.dumps({"inputs": inputs}))
        report = self.root / "report.json"
        with (
            patch.object(verify_submission, "SOURCE_BUNDLES", bundles),
            patch.object(
                verify_submission, "clone_commit", side_effect=AssertionError("network fetch attempted")
            ),
            patch.object(
                sb.SourceBundles, "checkout", side_effect=VerificationError("bundle checkout reached")
            ),
            patch.dict(os.environ, {"PALOMAR_JOB_STARTED_AT": "1", "GITHUB_OUTPUT": str(self.root / "o")}),
            patch.object(verify_submission, "install_execution_deadline"),
        ):
            verify_submission.prepare(
                __import__("argparse").Namespace(
                    event=event,
                    work_dir=self.root / "verify-work",
                    output=report,
                    licensee="/not-invoked",
                )
            )
        return json.loads(report.read_text())

    def bundles(self):
        fixture = Fixture(self.root)
        _, broker, _, out, _ = self.take_snapshot(fixture)
        validated = self.root / "validated"
        with contextlib.redirect_stdout(io.StringIO()):
            digest = cs.validate(
                TICKET,
                out / "descriptor-draft.json",
                validated,
                self.root / "vw",
                broker=broker,
                identity=IDENTITY,
                fetcher=fixture.fetcher(),
            )
        return fixture, sb.SourceBundles.load(validated, digest)

    def test_prepare_checks_out_from_the_bundle_and_records_the_snapshot(self):
        fixture, bundles = self.bundles()
        report = self.prepare(bundles, "owner/source", fixture.commit)
        self.assertEqual(report["errors"], ["bundle checkout reached"])
        self.assertEqual(report["snapshot_sha256"], bundles.snapshot_sha256)

    def test_prepare_refuses_a_snapshot_of_another_commit(self):
        fixture, bundles = self.bundles()
        report = self.prepare(bundles, "owner/source", "9" * 40)
        self.assertIn("different repository or commit", report["errors"][0])

    def test_substantive_sources_are_refused_in_bundle_mode(self):
        from scripts import verify_submission
        from scripts.verification_errors import VerificationError

        _, bundles = self.bundles()
        with (
            patch.object(verify_submission, "SOURCE_BUNDLES", bundles),
            self.assertRaisesRegex(VerificationError, "one repository"),
        ):
            verify_submission.validate_preservable_remote_source(
                self.root, {"repository_url": "https://github.com/o/r", "commit": "1" * 40}, "substantive"
            )


class MaterializeFromBundlesTests(Case):
    def test_public_dependencies_come_from_bundles_and_unlisted_ones_are_refused(self):
        from scripts import verify_submission
        from scripts.verification_errors import VerificationError

        fixture = Fixture(self.root, manifest_packages=None)
        manifest = json.loads((fixture.work / "lake-manifest.json").read_text())
        manifest["packages"] = [package for package in manifest["packages"] if package["type"] == "git"]
        (fixture.work / "lake-manifest.json").write_text(json.dumps(manifest))
        head = commit(fixture.work, "git packages only", "Ada")
        run_git(
            "push", "--quiet", "--force", str(fixture.remote / "owner/source"), "HEAD:main", cwd=fixture.work
        )
        fixture.commit = head
        _, broker, _, out, _ = self.take_snapshot(fixture)
        validated = self.root / "validated"
        with contextlib.redirect_stdout(io.StringIO()):
            digest = cs.validate(
                TICKET,
                out / "descriptor-draft.json",
                validated,
                self.root / "vw",
                broker=broker,
                identity=IDENTITY,
                fetcher=fixture.fetcher(),
            )
        bundles = sb.SourceBundles.load(validated, digest)
        checkout = self.root / "checkout"
        bundles.checkout(bundles.source_bundle(), head, checkout)
        real_run = verify_submission.run

        def local_only(command, **kwargs):
            if "fetch" in command or "remote" in command:
                raise AssertionError("network fetch attempted")
            return real_run(command, **kwargs)

        with (
            patch.object(verify_submission, "SOURCE_BUNDLES", bundles),
            patch.object(verify_submission, "run", side_effect=local_only),
        ):
            verify_submission.materialize_packages(
                checkout, checkout=checkout, base_env={"PATH": os.environ["PATH"]}
            )
        package = checkout / ".lake/packages/dep"
        self.assertEqual(run_git("rev-parse", "HEAD", cwd=package), fixture.dependency_rev)

        manifest["packages"][0]["rev"] = "8" * 40
        (checkout / "lake-manifest.json").write_text(json.dumps(manifest))
        subprocess.run(["rm", "-rf", str(checkout / ".lake")], check=True)
        with (
            patch.object(verify_submission, "SOURCE_BUNDLES", bundles),
            self.assertRaisesRegex(VerificationError, "another revision"),
        ):
            verify_submission.materialize_packages(
                checkout, checkout=checkout, base_env={"PATH": os.environ["PATH"]}
            )


class ReviewFollowUpTests(Case):
    def test_a_malformed_capability_still_masks_and_revokes_its_token(self):
        fixture = Fixture(self.root)

        class Malformed(FakeBroker):
            def open(self, ticket, operation):
                capability = super().open(ticket, operation)
                del capability["nonce"]
                return capability

        github = FakeGitHub()
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed), self.assertRaisesRegex(SnapshotError, "malformed"):
            cs.snapshot(
                TICKET,
                self.root / "out",
                self.root / "work",
                broker=Malformed(fixture),
                identity=IDENTITY,
                github=github,
                fetcher=fixture.fetcher(),
            )
        self.assertIn(f"::add-mask::{TOKEN}", printed.getvalue())
        self.assertEqual(github.revoked, [TOKEN])

    def test_one_repository_under_two_names_and_at_two_revisions(self):
        probe = Fixture(self.root / "probe")
        first = run_git("rev-parse", "HEAD~1", cwd=self.root / "probe/dep-work")
        fixture = Fixture(
            self.root / "real",
            manifest_packages=[
                {
                    "type": "git",
                    "name": "dep",
                    "url": "https://github.com/dep-owner/dep",
                    "rev": probe.dependency_rev,
                },
                {
                    "type": "git",
                    "name": "«dep alias»".replace(" ", "-"),
                    "url": "https://github.com/dep-owner/dep.git",
                    "rev": probe.dependency_rev,
                },
                {"type": "git", "name": "old", "url": "https://github.com/dep-owner/dep", "rev": first},
            ],
        )
        # The dependency repository is rebuilt identically, so the revisions match.
        self.assertEqual(fixture.dependency_rev, probe.dependency_rev)
        self.root = self.root / "real"
        (self.root / "home").mkdir(exist_ok=True)
        descriptor, broker, _, out, _ = self.take_snapshot(fixture)
        self.assertEqual([row["name"] for row in descriptor["dependencies"]], ["dep", "dep-alias", "old"])
        validated = self.root / "validated"
        with contextlib.redirect_stdout(io.StringIO()):
            digest = cs.validate(
                TICKET,
                out / "descriptor-draft.json",
                validated,
                self.root / "vw",
                broker=broker,
                identity=IDENTITY,
                fetcher=fixture.fetcher(),
            )
        bundles = sb.SourceBundles.load(validated, digest)
        self.assertEqual(bundles.dependency("https://github.com/dep-owner/dep", first)["name"], "old")
        self.assertEqual(
            bundles.dependency("https://github.com/dep-owner/dep", probe.dependency_rev)["name"], "dep"
        )

    def test_the_verifier_refuses_paths_the_snapshot_did_not_admit(self):
        from scripts import verify_submission
        from scripts.verification_errors import VerificationError

        class Bundles:
            descriptor = {"project": dict(PROJECT)}

        report = {
            "source": {"project_path": None},
            "comparator": {"path": "comparator.json"},
            "lakefile": {"path": "lakefile.toml"},
            "lean_toolchain_path": "lean-toolchain",
            "challenge": {"path": "Challenge.lean"},
            "solution": {"path": "Solution.lean"},
        }
        with patch.object(verify_submission, "SOURCE_BUNDLES", Bundles()):
            verify_submission.require_snapshot_project(report, tuple(PROJECT))
            for field, value in (("challenge", "Other/Challenge.lean"), ("lakefile", "b/lakefile.toml")):
                changed = json.loads(json.dumps(report))
                changed[field]["path"] = value
                with self.subTest(field), self.assertRaisesRegex(VerificationError, "differs"):
                    verify_submission.require_snapshot_project(changed, tuple(PROJECT))
            with self.assertRaisesRegex(VerificationError, "project_path"):
                verify_submission.require_snapshot_project(
                    {**report, "source": {"project_path": "other"}}, ("project_path",)
                )


class BrokerWireFixtureTests(unittest.TestCase):
    """The wire shapes the SRV-3 broker is to match (tests/fixtures/confidential-broker)."""

    FIXTURES = Path(__file__).resolve().parent / "fixtures" / "confidential-broker"

    def load(self, name):
        return json.loads((self.FIXTURES / f"{name}.json").read_text())

    def test_the_snapshot_capability_fixture_is_accepted(self):
        capability = self.load("snapshot-capability")
        self.assertIs(cs.check_snapshot_capability(capability), capability)
        self.assertEqual(set(capability["project"]), set(sb.PROJECT_FIELDS))

    def test_the_client_sends_the_fixture_request_shapes(self):
        requests = self.load("requests")
        sent = []

        def transport(method, url, headers, body):
            sent.append((method, url.split("?")[0].removeprefix("https://broker"), headers, json.loads(body)))
            if url.endswith("/broker"):
                return cs.Response(200, {}, json.dumps(self.load("snapshot-capability")).encode())
            if url.endswith("/plan"):
                return cs.Response(200, {}, json.dumps(requests["plan_response"]).encode())
            return cs.Response(200, {}, json.dumps(requests["finalize_response"]).encode())

        broker = cs.Broker(token_source=lambda: "jwt", base="https://broker", transport=transport)
        with contextlib.redirect_stdout(io.StringIO()):
            broker.open("k" * 43, "snapshot")
        broker.plan(requests["plan"]["dependencies"])
        broker.finalize({"schema": "x"})
        self.assertEqual(
            [item[1] for item in sent],
            [
                "/api/confidential/broker",
                "/api/confidential/snapshot/plan",
                "/api/confidential/snapshot/finalize",
            ],
        )
        self.assertEqual(sent[0][3], requests["broker"])
        self.assertEqual(sent[1][3], requests["plan"])
        self.assertEqual(set(sent[2][3]), {"descriptor"})
        self.assertNotIn("X-Palomar-Capability", sent[0][2])
        self.assertEqual(sent[1][2]["X-Palomar-Capability"], "c" * 43)
        self.assertTrue(all(item[2]["Authorization"] == "Bearer jwt" for item in sent))
