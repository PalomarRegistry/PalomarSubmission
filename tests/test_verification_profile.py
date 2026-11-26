import json
import os
import subprocess
import sys
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

    def test_an_omitted_selection_follows_the_catalogue_default(self):
        from scripts.verification_profile import default_profile_id
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(load_profile()["id"], default_profile_id())
            catalogue = json.loads(Path("execution-profiles.json").read_text())
            self.assertEqual(default_profile_id(), catalogue["default"])

    def test_namespace_profile_and_the_hosted_profile_are_both_selectable(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            profile = load_profile("palomar-namespace-16x32-v1")
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

    def test_larger_profile_requires_32_effective_cpus_and_64_gib_class_memory(self):
        profile = load_profile("palomar-namespace-32x64-v1")
        self.assertEqual(profile["limits"]["execution_budget_seconds"], 19800)
        self.assertEqual(profile["limits"]["minimum_host_memory_bytes"], 60 * 1024**3)
        self.assertIn("32x64", profile["runner"]["label"])
        with tempfile.TemporaryDirectory() as raw, \
             mock.patch("scripts.verification_profile.platform.machine", return_value="x86_64"), \
             mock.patch("scripts.verification_profile.effective_cpu_count", return_value=16), \
             self.assertRaisesRegex(VerificationProfileError, "requires 32"):
            check_host(profile, Path(raw))

    def test_resolver_outputs_carry_the_render_timeout(self):
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "out"
            subprocess.run(
                [sys.executable, "scripts/verification_profile.py", "--resolve",
                 "--profile", "palomar-standard-v1"],
                check=True, env={**os.environ, "GITHUB_OUTPUT": str(output)},
            )
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        self.assertEqual(values["timeout"], "350")
        self.assertEqual(values["render_timeout"], "360")
        self.assertEqual(values["budget"], "19800")

    def test_aws_profile_is_operator_launched_large_memory_capacity(self):
        profile = load_profile("palomar-aws-r7i-16xlarge-v1")
        runner = profile["runner"]
        self.assertEqual(runner["provider"], "aws-self-hosted")
        # One unique label, the profile id: no Namespace or hosted label can match.
        self.assertEqual(runner["labels"], ["palomar-aws-r7i-16xlarge-v1"])
        self.assertEqual(runner["minimum_cpus"], 64)
        self.assertEqual(profile["limits"]["minimum_host_memory_bytes"], 480 * 1024**3)
        self.assertEqual(profile["limits"]["job_timeout_minutes"], 24 * 60)
        self.assertEqual(profile["limits"]["execution_budget_seconds"], 23 * 60 * 60)
        with tempfile.TemporaryDirectory() as raw, \
             mock.patch("scripts.verification_profile.platform.machine", return_value="x86_64"), \
             mock.patch("scripts.verification_profile.effective_cpu_count", return_value=32), \
             self.assertRaisesRegex(VerificationProfileError, "requires 64"):
            check_host(profile, Path(raw))

    def test_aws_profile_resolves_only_for_a_dispatch_in_this_repository(self):
        from scripts.verification_profile import require_operator_dispatch
        aws = load_profile("palomar-aws-r7i-16xlarge-v1")
        dispatch = {"GITHUB_EVENT_NAME": "workflow_dispatch",
                    "GITHUB_REPOSITORY": "PalomarRegistry/PalomarSubmission"}
        require_operator_dispatch(aws, dispatch)
        for name, environment in {
            "pull request": {**dispatch, "GITHUB_EVENT_NAME": "pull_request"},
            "pull request target": {**dispatch, "GITHUB_EVENT_NAME": "pull_request_target"},
            "push": {**dispatch, "GITHUB_EVENT_NAME": "push"},
            "schedule": {**dispatch, "GITHUB_EVENT_NAME": "schedule"},
            "foreign reusable caller": {**dispatch, "GITHUB_REPOSITORY": "someone/fork"},
            "no event": {},
        }.items():
            with self.subTest(name=name), self.assertRaisesRegex(VerificationProfileError, "operator-only"):
                require_operator_dispatch(aws, environment)
        # Namespace and hosted profiles are unaffected.
        require_operator_dispatch(load_profile("palomar-namespace-32x64-v1"), {})
        require_operator_dispatch(load_profile("palomar-standard-v1"), {})
        with tempfile.TemporaryDirectory() as raw:
            completed = subprocess.run(
                [sys.executable, "scripts/verification_profile.py", "--resolve",
                 "--profile", "palomar-aws-r7i-16xlarge-v1"],
                env={**os.environ, "GITHUB_OUTPUT": str(Path(raw) / "out"),
                     "GITHUB_EVENT_NAME": "pull_request",
                     "GITHUB_REPOSITORY": "PalomarRegistry/PalomarSubmission"},
                capture_output=True, text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse((Path(raw) / "out").exists())
            output = Path(raw) / "dispatched"
            subprocess.run(
                [sys.executable, "scripts/verification_profile.py", "--resolve",
                 "--profile", "palomar-aws-r7i-16xlarge-v1"],
                check=True, env={**os.environ, "GITHUB_OUTPUT": str(output), **dispatch},
            )
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        self.assertEqual(json.loads(values["labels"]), ["palomar-aws-r7i-16xlarge-v1"])
        self.assertEqual(values["timeout"], "1440")
        self.assertEqual(values["render_timeout"], "1450")
        self.assertEqual(values["budget"], "82800")

    def test_long_profile_budget_raises_the_per_phase_ceiling(self):
        code = (
            "from scripts import verify_submission as v; "
            "print(v.EXECUTION_BUDGET_SECONDS)"
        )
        ceilings = {}
        for profile in ("palomar-namespace-16x32-v1", "palomar-aws-r7i-16xlarge-v1"):
            ceilings[profile] = int(subprocess.run(
                [sys.executable, "-c", code], check=True, capture_output=True, text=True,
                env={**os.environ, "PALOMAR_EXECUTION_PROFILE": profile},
            ).stdout.strip())
        self.assertEqual(ceilings["palomar-namespace-16x32-v1"], 12 * 60 * 60)
        self.assertEqual(ceilings["palomar-aws-r7i-16xlarge-v1"], 82800)

    def test_trusted_downloads_follow_the_exported_budget(self):
        from scripts import trusted_download, trusted_git_fetch
        for module in (trusted_download, trusted_git_fetch):
            with self.subTest(module=module.__name__):
                with mock.patch.dict("os.environ", {}, clear=True):
                    self.assertEqual(module.job_budget_seconds(), 19800)
                with mock.patch.dict("os.environ", {"PALOMAR_EXECUTION_BUDGET_SECONDS": "82800"}):
                    self.assertEqual(module.job_budget_seconds(), 82800)

    def test_runner_hook_admits_exactly_the_profile_driven_dispatch_jobs(self):
        import re

        import yaml
        hook = Path("ops/aws-runner/job-started.sh").read_text()
        admitted = set(re.findall(r'"\$workflows/([a-z-]+\.yml)@refs/heads/main:([a-z_]+)"\)', hook))
        selecting = set()
        for path in Path(".github/workflows").glob("*.yml"):
            workflow = yaml.safe_load(path.read_text())
            triggers = workflow.get(True, workflow.get("on"))
            for name, job in workflow["jobs"].items():
                if job.get("runs-on") == "${{ fromJSON(needs.profile.outputs.labels) }}":
                    self.assertIn("workflow_dispatch", triggers, path.name)
                    selecting.add((path.name, name))
        self.assertEqual(admitted, selecting)
        self.assertIn('[[ "$event" == workflow_dispatch ]]', hook)
        self.assertIn('[[ "${GITHUB_REF:-}" == refs/heads/main ]]', hook)

    def test_catalogue_is_validated_by_shape(self):
        from scripts.verification_profile import load_catalogue
        good = json.loads(Path("execution-profiles.json").read_text())
        namespace = good["profiles"]["palomar-namespace-16x32-v1"]
        aws = good["profiles"]["palomar-aws-r7i-16xlarge-v1"]
        feature = "namespace-features:container.privileged=true"
        bad_cases = {
            "unknown default": {**good, "default": "palomar-other-v1"},
            "foreign label": {**good, "profiles": {"palomar-x-v1": {
                **namespace,
                "runner": {**namespace["runner"], "label": "ubuntu-24.04",
                           "labels": ["ubuntu-24.04", feature]},
            }}},
            "aws label differs from its id": {**good, "profiles": {"palomar-aws-other-v1": aws}},
            "aws with namespace feature": {**good, "profiles": {"palomar-aws-r7i-16xlarge-v1": {
                **aws, "runner": {**aws["runner"], "labels": [aws["runner"]["label"], feature]},
            }}},
            "aws without cpu floor": {**good, "profiles": {"palomar-aws-r7i-16xlarge-v1": {
                **aws, "runner": {k: v for k, v in aws["runner"].items() if k != "minimum_cpus"},
            }}},
            "aws as default": {**good, "default": "palomar-aws-r7i-16xlarge-v1"},
            "unknown provider": {**good, "profiles": {"palomar-namespace-16x32-v1": {
                **namespace, "runner": {**namespace["runner"], "provider": "self-hosted"},
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
