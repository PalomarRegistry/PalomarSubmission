import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.verification_profile import (
    PROFILE_PATH,
    VerificationProfileError,
    check_host,
    load_profile,
)


class VerificationProfileTests(unittest.TestCase):
    def test_hosted_profile_is_closed_and_current(self):
        profile = load_profile("palomar-standard-v1")
        self.assertEqual(profile["id"], "palomar-standard-v1")
        self.assertEqual(profile["runner"]["label"], "ubuntu-24.04")
        self.assertEqual(
            profile,
            json.loads(PROFILE_PATH.read_text(encoding="utf-8")),
        )

    def test_host_capacity_is_checked_before_candidate_execution(self):
        profile = load_profile()
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "scripts.verification_profile.platform.machine", return_value="x86_64"
        ), mock.patch(
            "scripts.verification_profile.host_memory_bytes",
            return_value=profile["limits"]["minimum_host_memory_bytes"] - 1,
        ), self.assertRaisesRegex(VerificationProfileError, "profile requires"):
            check_host(profile, Path(temporary))

    def test_namespace_is_the_default_and_the_hosted_profile_stays_selectable(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            profile = load_profile()
            self.assertEqual(profile["id"], "palomar-namespace-16x32-v1")
            self.assertEqual(profile["runner"]["provider"], "namespace")
            self.assertEqual(profile["runner"]["labels"][1], "namespace-features:container.privileged=true")
            self.assertEqual(profile["limits"]["execution_budget_seconds"], 19800)
            self.assertEqual(profile["limits"]["job_timeout_minutes"], 350)
            self.assertEqual(profile["limits"]["minimum_host_memory_bytes"], 28 * 1024**3)
            self.assertEqual(load_profile("palomar-standard-v1")["runner"]["provider"], "github-hosted")
            with self.assertRaisesRegex(VerificationProfileError, "not approved"):
                load_profile("arbitrary-runner")
        with mock.patch.dict("os.environ", {"PALOMAR_EXECUTION_PROFILE": "palomar-standard-v1"}):
            self.assertEqual(load_profile()["id"], "palomar-standard-v1")

    def test_catalogue_is_validated_by_shape(self):
        from scripts.verification_profile import load_catalogue
        good = json.loads(Path("execution-profiles.json").read_text())
        namespace = good["profiles"]["palomar-namespace-16x32-v1"]
        feature = "namespace-features:container.privileged=true"
        bad_cases = {
            "unknown default": {**good, "default": "palomar-other-v1"},
            "foreign label": {**good, "profiles": {"palomar-x-v1": {
                **namespace,
                "runner": {**namespace["runner"], "label": "ubuntu-24.04",
                           "labels": ["ubuntu-24.04", feature]},
            }}},
            "budget over timeout": {**good, "profiles": {"palomar-namespace-16x32-v1": {
                **namespace,
                "limits": {"minimum_host_memory_bytes": 1, "job_timeout_minutes": 1,
                           "execution_budget_seconds": 61},
            }}},
        }
        for name, catalogue in bad_cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as raw, \
                 mock.patch("scripts.verification_profile.CATALOGUE_PATH", Path(raw) / "c.json"):
                (Path(raw) / "c.json").write_text(json.dumps(catalogue))
                with self.assertRaises(VerificationProfileError):
                    load_catalogue()

    def test_effective_capacity_observes_parent_cgroup_limits(self):
        from scripts.verification_profile import effective_cpu_count, effective_memory_bytes
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            child = root / "child"
            child.mkdir()
            (root / "memory.max").write_text(str(32 * 1024**3))
            (child / "memory.max").write_text("max")
            (root / "cpu.max").write_text("1600000 100000")
            (child / "cpu.max").write_text("max 100000")
            with mock.patch("scripts.verification_profile.cgroup_directories", return_value=[child, root]), \
                 mock.patch("scripts.verification_profile.host_memory_bytes", return_value=128 * 1024**3), \
                 mock.patch("scripts.verification_profile.os.sched_getaffinity", return_value=set(range(64))):
                self.assertEqual(effective_memory_bytes(), 32 * 1024**3)
                self.assertEqual(effective_cpu_count(), 16)


if __name__ == "__main__":
    unittest.main()
