"""Exercise the report contract and the actual resource boundary."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

import scripts.verify_submission as verifier
from scripts.verification_profile import VerificationProfileError


class CapacityReportTests(unittest.TestCase):
    def test_undersized_host_emits_a_terminal_provider_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            output.write_text(json.dumps({
                "status": "pending", "errors": [],
                "verification_profile": verifier.verification_profile_evidence(),
            }))
            with mock.patch.object(verifier, "check_host", side_effect=VerificationProfileError(
                "runner has insufficient memory"
            )):
                code = verifier.check_capacity(Namespace(output=output, disk_path=directory))
            report = json.loads(output.read_text())
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["diagnostics"][0]["code"], "provider.host_below_profile")
        self.assertEqual(report["diagnostics"][0]["owner"], "provider")
        self.assertIn("cooldowns", report["diagnostics"][0]["next_action"])

    def test_sufficient_host_preserves_preparation_and_records_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            original = {"status": "pending", "errors": [], "source": {"commit": "a" * 40},
                        "verification_profile": verifier.verification_profile_evidence()}
            output.write_text(json.dumps(original))
            with mock.patch.object(verifier, "check_host", return_value={"memory_bytes": 123}):
                self.assertEqual(verifier.check_capacity(Namespace(
                    output=output, disk_path=directory
                )), 0)
            report = json.loads(output.read_text())
        self.assertEqual(report["source"], original["source"])
        self.assertEqual(report["status"], "pending")
        self.assertEqual(report["verification_profile"]["observed_host"], {"memory_bytes": 123})

    def test_cleanup_uses_privilege_and_survives_expired_work_deadline(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "Result=timeout\nControlGroup=\n", "")

        with mock.patch.object(verifier, "_SYSTEMD_MANAGER", "system"), \
             mock.patch.object(verifier, "_EXECUTION_DEADLINE", 0), \
             mock.patch.object(verifier.shutil, "which", side_effect=lambda name: "/usr/bin/" + name), \
             mock.patch.object(verifier.subprocess, "run", side_effect=run):
            outcome = verifier.systemd_unit_outcome(
                "palomar-" + "a" * 24, cwd=Path.cwd(), environment={}
            )
        self.assertEqual(outcome["Result"], "timeout")
        self.assertEqual(calls[1][:6], ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "kill",
                                      "--signal=KILL", "--kill-whom=all"])
        self.assertEqual(calls[2][3], "stop")
        self.assertEqual(calls[3][3], "reset-failed")


@unittest.skipUnless(os.environ.get("PALOMAR_TEST_LANDRUN"), "requires real Landrun/systemd")
class RealResourceBoundaryTests(unittest.TestCase):
    def run_phase(self, code, *, memory="256M", timeout=30, expected=None,
                  extra_properties=()):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            work = Path(directory).resolve()
            scratch = work / "scratch"
            scratch.mkdir()
            metrics = work / "metrics.jsonl"
            python = Path(sys.executable).resolve()
            landrun = Path(os.environ["PALOMAR_TEST_LANDRUN"]).resolve()
            executable = [python.parent.parent, python, landrun]
            for raw in ("/usr", "/bin", "/lib", "/lib64", "/nix/store", "/run/current-system/sw"):
                path = Path(raw)
                if path.exists():
                    executable.append(path.resolve())
            environment = {**os.environ, "TMPDIR": str(scratch)}
            with mock.patch.object(verifier, "_SYSTEMD_MANAGER", None), \
                 mock.patch.object(verifier, "_EXECUTION_DEADLINE", None), \
                 mock.patch.object(verifier, "_RESOURCE_METRICS_PATH", metrics), \
                 mock.patch.object(verifier, "_RESOURCE_DISK_PATH", work), \
                 mock.patch.object(verifier, "systemd_command",
                                   wraps=verifier.systemd_command) as command_spy:
                def invoke():
                    return verifier.sandboxed_run(
                        [str(python), "-c", code], cwd=work, environment=environment,
                        landrun=landrun, writable_directories=[scratch],
                        readable_paths=[work, *verifier.system_readable_paths()],
                        executable_paths=sorted(set(executable)),
                        tools=verifier.tool_snapshot([python, landrun]), timeout=timeout,
                        # A tiny, deterministic fixture ceiling; production swap policy is unchanged.
                        resource_properties=(f"MemoryMax={memory}", "MemoryHigh=infinity",
                                             "MemorySwapMax=0", "TimeoutStopSec=2s",
                                             *extra_properties),
                    )
                if expected:
                    with self.assertRaises(expected) as raised:
                        invoke()
                    error = raised.exception
                else:
                    self.assertEqual(invoke().returncode, 0)
                    error = None
                unit = command_spy.call_args.kwargs["unit_name"] + ".service"
                manager = ["systemctl"]
                if verifier._SYSTEMD_MANAGER == "user":
                    manager.append("--user")
                active = subprocess.run(
                    [*manager, "is-active", unit], capture_output=True, text=True, timeout=10,
                )
                self.assertNotEqual(active.stdout.strip(), "active", unit)
                self.assertNotEqual(active.stdout.strip(), "deactivating", unit)
            self.assertTrue(metrics.exists(), f"No worker telemetry; phase error: {error}")
            records = [json.loads(line) for line in metrics.read_text().splitlines()]
            # The payload has write access only to scratch, never the trusted metrics.
            return records, error

    def test_success_records_workload_rss_not_launcher_rss(self):
        records, _ = self.run_phase(
            "data = bytearray(b'x' * (64 * 1024 * 1024)); assert data[-1] == 120"
        )
        self.assertGreater(records[0]["max_rss_kib"], 60 * 1024)
        self.assertEqual(records[0]["returncode"], 0)

    def test_real_cgroup_oom_survives_loss_of_the_inner_observer(self):
        records, error = self.run_phase(
            "blocks = []\nwhile True: blocks.append(bytearray(b'x' * (8 * 1024 * 1024)))",
            memory="64M", expected=verifier.ResourceExhausted,
        )
        self.assertEqual(error.code, "provider.resource_exhausted")
        self.assertTrue(any(row.get("systemd_result") == "oom-kill" for row in records), records)

    def test_real_timeout_is_inconclusive_and_stops_the_unit(self):
        started = time.monotonic()
        records, _ = self.run_phase(
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
            timeout=2, extra_properties=("RuntimeMaxSec=60s", "TimeoutStopSec=60s"),
            expected=(subprocess.TimeoutExpired, verifier.ResourceExhausted),
        )
        self.assertLess(time.monotonic() - started, 30)
        self.assertTrue(any(":cgroup" in row["phase"] for row in records), records)

    def test_deliberate_exit_137_is_not_oom(self):
        records, error = self.run_phase("raise SystemExit(137)", expected=verifier.VerificationError)
        self.assertNotIsInstance(error, verifier.ResourceExhausted)
        self.assertTrue(any(row.get("systemd_result") == "exit-code" for row in records), records)
