import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scripts.verify_submission as verifier
from scripts.verification_errors import VerificationError
from scripts.verify_submission import ResourceExhausted

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONFIG = {
    "repository": "example/trusted",
    "revision_endpoint": "https://cache.example.test/trusted/revisions",
    "artifact_endpoint": "https://cache.example.test/trusted/artifacts",
    "window": 4,
}
TOOLCHAIN = "leanprover/lean4:v4.36.0"
PIN = "1" * 40
NEARBY = "b" * 40
INPUT_HASH = "0123456789abcdef"
ARCHIVE = "fedcba9876543210.ltar"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.test",
         "-c", "protocol.file.allow=always", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


class TrustedLakeCacheConfigTests(unittest.TestCase):
    def test_checked_in_tauceti_root_reads_only_the_trusted_channel(self):
        roots, _aliases = verifier.allowed_roots()
        tauceti = next(root for root in roots if root["repository"] == "TauCetiProject/TauCeti")
        config = tauceti["trusted_lake_cache"]
        self.assertEqual(config["revision_endpoint"], "https://cache.taucetiproject.org/trusted/revisions")
        self.assertEqual(config["artifact_endpoint"], "https://cache.taucetiproject.org/trusted/artifacts")
        for root in roots:
            if root is not tauceti:
                self.assertNotIn("trusted_lake_cache", root)

    def test_configuration_is_exact(self):
        self.assertTrue(verifier.valid_trusted_lake_cache_config(CONFIG, "example/trusted"))
        for change in (
            {"repository": "example/other"},
            {"artifact_endpoint": "http://cache.example.test/trusted/artifacts"},
            {"revision_endpoint": "https://cache.example.test/trusted/revisions/"},
            {"revision_endpoint": "https://cache.example.test/../revisions"},
            {"window": -1},
            {"window": verifier.MAX_TRUSTED_LAKE_CACHE_WINDOW + 1},
            {"window": True},
            {"extra": 1},
        ):
            with self.subTest(change=change):
                self.assertFalse(
                    verifier.valid_trusted_lake_cache_config({**CONFIG, **change}, "example/trusted")
                )

    def test_toolchain_scope_matches_lake(self):
        self.assertEqual(
            verifier.lake_toolchain_scope("leanprover/lean4:v4.37.0-rc1"),
            "leanprover--lean4---v4.37.0-rc1",
        )
        with self.assertRaises(VerificationError):
            verifier.lake_toolchain_scope("leanprover/lean4:../../x")

    def test_map_parsing_keeps_only_plain_module_archives(self):
        raw = "\n".join([
            json.dumps("2026-03-17"),
            json.dumps([INPUT_HASH, ARCHIVE]),
            json.dumps(["1111111111111111", {"o": "structured"}]),
        ]).encode()
        self.assertEqual(
            verifier.parse_trusted_lake_cache_map(raw), ("2026-03-17", {INPUT_HASH: ARCHIVE})
        )
        for bad in (
            b"not json",
            json.dumps(["no schema"]).encode(),
            "\n".join([json.dumps("s"), json.dumps([INPUT_HASH, ARCHIVE]),
                       json.dumps([INPUT_HASH, ARCHIVE])]).encode(),
            "\n".join([json.dumps("s"), json.dumps(["XYZ", ARCHIVE])]).encode(),
        ):
            with self.subTest(bad=bad[:30]):
                self.assertIsNone(verifier.parse_trusted_lake_cache_map(bad))
        unsafe = "\n".join([json.dumps("s"), json.dumps([INPUT_HASH, "../escape.ltar"])]).encode()
        self.assertEqual(verifier.parse_trusted_lake_cache_map(unsafe), ("s", {}))

    def test_inherited_cache_settings_never_reach_a_phase(self):
        environment = {
            "PATH": "/usr/bin",
            "LAKE_CACHE_DIR": "/attacker",
            "LAKE_CONFIG": "/attacker/config.toml",
            "LAKE_NO_CACHE": "false",
            "LAKE_ARTIFACT_CACHE": "true",
            "LAKE_RESTORE_ARTIFACTS": "true",
        }
        self.assertEqual(verifier.strip_trusted_lake_cache_environment(environment), {"PATH": "/usr/bin"})


class TrustedLakeCacheRevisionTests(unittest.TestCase):
    def test_shallow_pin_still_yields_ancestors_and_descendants_nearest_first(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream = root / "upstream"
            upstream.mkdir()
            git(upstream, "init", "-q", "-b", "main")
            git(upstream, "config", "uploadpack.allowFilter", "true")
            commits = []
            for index in range(8):
                (upstream / "file").write_text(str(index))
                git(upstream, "add", "file")
                git(upstream, "commit", "-q", "-m", f"c{index}")
                commits.append(git(upstream, "rev-parse", "HEAD"))
            pin = commits[4]
            package = root / "package"
            package.mkdir()
            git(package, "init", "-q")
            git(package, "fetch", "-q", "--depth=1", f"file://{upstream}", pin)
            git(package, "fetch", "-q", f"file://{upstream}",
                "+refs/heads/main:refs/remotes/palomar-official/head")
            self.assertIn(pin, (package / ".git" / "shallow").read_text())
            work = root / "work"
            work.mkdir()
            with mock.patch(
                "scripts.verify_submission.trusted_lake_cache_history_url",
                return_value=f"file://{upstream}",
            ):
                revisions = verifier.trusted_lake_cache_revisions(
                    package, pin, repository="example/trusted", window=2,
                    environment={"PATH": os.environ["PATH"]}, work=work,
                )
            self.assertEqual(list(work.iterdir()), [])
            self.assertEqual(
                revisions, [commits[4], commits[3], commits[5], commits[2], commits[6]]
            )


class TrustedLakeCacheRestoreTests(unittest.TestCase):
    def fixture(self, directory: Path) -> tuple[Path, Path]:
        source = directory / "checkout"
        packages_dir = source / ".lake" / "packages"
        trusted = packages_dir / "trusted"
        shared = packages_dir / "shared"
        for package in (trusted, shared):
            (package / ".lake" / "build").mkdir(parents=True)
            (package / ".lake" / "config").mkdir()
        shared_entry = {"name": "shared", "type": "git",
                        "url": "https://github.com/example/shared", "rev": "2" * 40}
        (source / "lake-manifest.json").write_text(json.dumps({"packages": [
            {"name": "trusted", "type": "git", "url": "https://github.com/example/trusted",
             "rev": PIN},
            shared_entry,
        ]}))
        (trusted / "lake-manifest.json").write_text(json.dumps({"packages": [shared_entry]}))
        (trusted / "lean-toolchain").write_text(TOOLCHAIN + "\n")
        (trusted / "Trusted").mkdir()
        (trusted / "Trusted" / "A.lean").write_text("module\n")
        return source, trusted

    def build(self, source: Path, side_effect, maps: dict[str, str]):
        root = {"repository": "example/trusted", "trust_level": "qualified",
                "trusted_lake_cache": CONFIG}
        tool = Path(sys.executable).resolve()
        with (
            mock.patch("scripts.verify_submission.allowed_roots",
                       return_value=([root], {"example/trusted": root})),
            mock.patch("scripts.verify_submission.trusted_lake_cache_revisions",
                       return_value=[PIN, NEARBY]),
            mock.patch("scripts.verify_submission._trusted_lake_cache_tools",
                       return_value=(tool, tool)),
            mock.patch("scripts.verify_submission.sandboxed_run", side_effect=side_effect),
        ):
            return verifier.build_allowlisted_roots(
                source,
                checkout=source,
                packages=verifier.manifest_packages(source),
                allowlist={"trusted": ("example/trusted", "qualified"),
                           "shared": ("leanprover-community/mathlib4", "high")},
                base_env={"PATH": "/usr/bin", "LAKE_CACHE_DIR": "/inherited"},
                lake=Path("/tools/lake"),
                readable_paths=[source],
                executable_paths=[],
                tools={},
                required_imports=frozenset({"Trusted.A"}),
            )

    @staticmethod
    def serve(command, maps):
        config = Path(command[command.index("--config") + 1])
        lines = config.read_text().splitlines()
        for url_line, output_line in zip(lines[::2], lines[1::2], strict=True):
            url = json.loads(url_line.removeprefix("url = "))
            output = Path(json.loads(output_line.removeprefix("output = ")))
            if url in maps:
                output.write_text(maps[url])
            elif url.startswith(CONFIG["artifact_endpoint"] + "/example/trusted/"):
                output.write_bytes(b"archive")

    def test_trusted_channel_is_fetched_by_curl_and_read_by_offline_lake(self):
        map_url = (f"{CONFIG['revision_endpoint']}/example/trusted/tc/"
                   f"leanprover--lean4---v4.36.0/{NEARBY}.jsonl")
        maps = {map_url: "\n".join([json.dumps("2026-03-17"),
                                     json.dumps([INPUT_HASH, ARCHIVE])]) + "\n"}
        calls: list[str] = []
        probes = iter([
            subprocess.CompletedProcess([], 3, "remote URL: "
                f"{verifier.TRUSTED_LAKE_CACHE_PROBE_ENDPOINT}/artifacts/example/trusted/"
                f"{ARCHIVE.removesuffix('.ltar')}.art\n", ""),
            subprocess.CompletedProcess([], 0, "All targets up-to-date", ""),
        ])
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            source, trusted = self.fixture(directory)

            def sandbox(command, **kwargs):
                network = kwargs.get("unrestricted_network", False)
                writable = [Path(path) for path in kwargs["writable_directories"]]
                if "--config" in command:
                    calls.append("curl")
                    self.assertTrue(network)
                    self.assertEqual(len(writable), 1)
                    self.assertTrue(writable[0].name in {"maps", "artifacts"})
                    self.serve(command, maps)
                    return subprocess.CompletedProcess(command, 0, "", "")
                self.assertFalse(network)
                environment = kwargs["environment"]
                cache_dir = Path(environment["LAKE_CACHE_DIR"])
                self.assertNotEqual(cache_dir, Path("/inherited"))
                self.assertEqual(environment["LAKE_NO_CACHE"], "true")
                self.assertNotIn("LAKE_ARTIFACT_CACHE", environment)
                if command[1:3] == ["cache", "add"]:
                    calls.append("add")
                    self.assertIn(cache_dir, writable)
                    self.assertIn(f"--repo={CONFIG['repository']}", command)
                    return subprocess.CompletedProcess(command, 0, "", "")
                self.assertNotIn(cache_dir, writable)
                self.assertTrue((cache_dir / "artifacts" / ARCHIVE).is_file() or "--no-build" in command)
                if "--no-build" in command:
                    calls.append("probe")
                    return next(probes)
                calls.append("build")
                self.assertEqual(command, ["/tools/lake", "build", "+Trusted.A"])
                return subprocess.CompletedProcess(command, 0, "", "")

            evidence = self.build(source, sandbox, maps)
            self.assertEqual(calls, ["curl", "add", "probe", "curl", "probe", "build"])
            record = evidence["example/trusted"]
            self.assertEqual(record["outcome"], "restored")
            self.assertEqual(record["map_revisions"], [NEARBY])
            self.assertEqual(record["map_entries"], 1)
            self.assertEqual((record["probes"], record["archives"], record["bytes"]), (2, 1, 7))
            self.assertFalse(record["bulk"])
            self.assertEqual(
                [path.name for path in directory.iterdir()], ["checkout"],
                "the disposable cache directory must be removed",
            )
            self.assertFalse((trusted / ".lake" / "packages").exists())

    def test_missing_channel_builds_from_source_without_cache_settings(self):
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as raw:
            source, _trusted = self.fixture(Path(raw))

            def sandbox(command, **kwargs):
                if "--config" in command:
                    calls.append("curl")
                    return subprocess.CompletedProcess(command, 22, "", "404")
                calls.append("build")
                for name in verifier.TRUSTED_LAKE_CACHE_ENVIRONMENT:
                    self.assertNotIn(name, kwargs["environment"])
                return subprocess.CompletedProcess(command, 0, "", "")

            evidence = self.build(source, sandbox, {})
            self.assertEqual(calls, ["curl", "build"])
            self.assertEqual(evidence["example/trusted"]["outcome"], "no-map")

    def test_unexpected_downloads_fail_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            source, _trusted = self.fixture(Path(raw))

            def sandbox(command, **kwargs):
                destination = Path(kwargs["writable_directories"][0])
                (destination / "planted").write_text("x")
                return subprocess.CompletedProcess(command, 0, "", "")

            with self.assertRaisesRegex(VerificationError, "unexpected trusted Lake cache download"):
                self.build(source, sandbox, {})


class DependencyResourceExhaustionTests(unittest.TestCase):
    def test_dependency_stage_exhaustion_never_offers_a_larger_runner(self):
        for stage in sorted(verifier.TRUSTED_DEPENDENCY_STAGES):
            with self.subTest(stage=stage):
                report = {"errors": [], "last_active_stage": stage}
                verifier.record_resource_exhaustion(
                    report, ResourceExhausted("worker resource ceiling reached", kind="oom")
                )
                self.assertNotIn("resource_failure", report)
                diagnostic = report["diagnostics"][0]
                self.assertEqual(diagnostic["code"], "palomar.trusted_dependency_resource_exhausted")
                self.assertEqual(diagnostic["owner"], "palomar")
                self.assertIn("do not ask for a larger runner", diagnostic["next_action"])
                self.assertRegex(report["errors"][0], re.escape(f"(stage {stage})"))

    def test_submission_stage_exhaustion_keeps_the_authenticated_retry(self):
        report = {"errors": [], "last_active_stage": "solution-build"}
        verifier.record_resource_exhaustion(
            report, ResourceExhausted("worker resource ceiling reached", kind="oom")
        )
        self.assertEqual(report["resource_failure"], {"kind": "oom"})
        self.assertEqual(report["diagnostics"][0]["code"], "provider.resource_exhausted")


if __name__ == "__main__":
    unittest.main()
