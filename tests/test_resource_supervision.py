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
    def test_renderer_helpers_import_without_host_capacity_access(self):
        # The real sanitizer's mount namespace omits /proc and /sys. Importing
        # shared helpers must not try to calculate the outer supervisor's limits.
        result = subprocess.run(
            [sys.executable, "-c", "\n".join((
                "from unittest.mock import patch",
                "with patch('scripts.verification_profile.host_memory_bytes',",
                "           side_effect=FileNotFoundError('/proc/meminfo')):",
                "    import scripts.render_challenge",
            ))],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_supervisor_calculates_limits_from_current_effective_capacity(self):
        with mock.patch.object(verifier, "effective_memory_bytes", return_value=32 * 1024**3):
            properties = verifier.permissive_resource_properties()
        self.assertIn(
            f"MemoryMax={32 * 1024**3 * verifier.VERIFICATION_LIMITS['memory_max_percent'] // 100}",
            properties,
        )

    def test_parent_timeout_does_not_record_an_active_unit_as_successful(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.jsonl"
            verifier.append_resource_outcome(
                path, "worker", {"Result": "success", "ActiveState": "active"},
                supervisor_timeout=True,
            )
            record = json.loads(path.read_text())
        self.assertTrue(record["supervisor_timeout"])
        self.assertIsNone(record["systemd_result"])
        self.assertEqual(record["systemd_result_before_cleanup"], "success")
        self.assertEqual(record["systemd_active_state"], "active")

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


CGROUP_MODE = verifier.SUPERVISOR_KIND == "cgroup"
REAL_BOUNDARY_AVAILABLE = (
    bool(os.environ.get("PALOMAR_BWRAP")) if CGROUP_MODE else bool(os.environ.get("PALOMAR_TEST_LANDRUN"))
)


@unittest.skipUnless(
    REAL_BOUNDARY_AVAILABLE,
    "requires the real supervisor: Landrun/systemd, or PALOMAR_SUPERVISOR=cgroup with PALOMAR_BWRAP",
)
class RealResourceBoundaryTests(unittest.TestCase):
    def run_phase(self, code, *, memory="256M", timeout=30, expected=None,
                  extra_properties=()):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            work = Path(directory).resolve()
            scratch = work / "scratch"
            scratch.mkdir()
            metrics = work / "metrics.jsonl"
            python = Path(sys.executable).resolve()
            # The cgroup supervisor confines with bubblewrap; the landrun argument
            # is then only a tool the phase snapshots.
            landrun = Path(os.environ.get("PALOMAR_TEST_LANDRUN") or verifier.shutil.which("true")).resolve()
            executable = [python.parent.parent, python, landrun]
            for raw in ("/usr", "/bin", "/lib", "/lib64", "/nix/store", "/run/current-system/sw"):
                path = Path(raw)
                if path.exists():
                    executable.append(path.resolve())
            environment = {**os.environ, "TMPDIR": str(scratch)}
            spied = "supervisor_command" if CGROUP_MODE else "systemd_command"
            with mock.patch.object(verifier, "_SYSTEMD_MANAGER", None), \
                 mock.patch.object(verifier, "_EXECUTION_DEADLINE", None), \
                 mock.patch.object(verifier, "_RESOURCE_METRICS_PATH", metrics), \
                 mock.patch.object(verifier, "_RESOURCE_DISK_PATH", work), \
                 mock.patch.object(verifier, spied, wraps=getattr(verifier, spied)) as command_spy:
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
                unit_name = command_spy.call_args.kwargs["unit_name"]
                if CGROUP_MODE:
                    # KillMode=control-group: the phase cgroup is gone once the phase is over.
                    leftovers = [p for p in Path("/sys/fs/cgroup").rglob(unit_name)]
                    self.assertEqual(leftovers, [], unit_name)
                else:
                    unit = unit_name + ".service"
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
            expected=subprocess.TimeoutExpired,
        )
        self.assertLess(time.monotonic() - started, 30)
        timeout_records = [row for row in records if row.get("supervisor_timeout")]
        self.assertTrue(timeout_records, records)
        self.assertTrue(all(row["systemd_result"] is None for row in timeout_records))

    def test_deliberate_exit_137_is_not_oom(self):
        records, error = self.run_phase("raise SystemExit(137)", expected=verifier.VerificationError)
        self.assertNotIsInstance(error, verifier.ResourceExhausted)
        self.assertTrue(any(row.get("systemd_result") == "exit-code" for row in records), records)


class CgroupSupervisorTranslationTests(unittest.TestCase):
    def test_translates_the_properties_the_phases_use(self):
        limits = verifier.translate_resource_properties((
            "MemoryMax=64M", "MemoryHigh=infinity", "MemorySwapMax=0", "TasksMax=512",
            "LimitNOFILE=1024", "LimitFSIZE=2048", "RuntimeMaxSec=60s", "TimeoutStopSec=2s",
            "CPUQuota=200%",
        ))
        self.assertEqual(limits.cgroup, {
            "memory.oom.group": "1", "memory.max": str(64 * 1024**2), "memory.high": "max",
            "memory.swap.max": "0", "pids.max": "512", "cpu.max": "200000 100000",
        })
        self.assertEqual(limits.rlimits, {"NOFILE": 1024, "FSIZE": 2048})
        self.assertEqual((limits.deadline, limits.grace), (60, 2))

    def test_production_properties_translate(self):
        with mock.patch.object(verifier, "effective_memory_bytes", return_value=32 * 1024**3):
            limits = verifier.translate_resource_properties(verifier.permissive_resource_properties())
        self.assertIn("memory.max", limits.cgroup)
        self.assertIn("pids.max", limits.cgroup)
        self.assertEqual(set(limits.rlimits), {"NOFILE", "FSIZE"})

    def test_rejects_address_space_and_unknown_properties(self):
        for item in ("LimitAS=1000000", "PrivateNetwork=yes", "MemoryMax=50%", "MemoryMax=",
                     "CPUQuota=2", "RuntimeMaxSec=1d"):
            with self.subTest(item=item), self.assertRaises(verifier.VerificationError):
                verifier.translate_resource_properties((item,))


class CgroupSupervisorCommandTests(unittest.TestCase):
    def test_babysitter_argv_carries_limits_deadline_and_sandbox_environment(self):
        bootstrap = ["/usr/bin/systemd-run", "--user", "--scope", "--"]
        with mock.patch.object(verifier, "supervisor_bootstrap", return_value=bootstrap):
            argv = verifier.supervisor_command(
                ["/bin/true"], cwd=Path("/work"),
                environment={"PATH": "/usr/bin", "SECRET": "x", "HOME": "/work/home"},
                timeout=120, resource_properties=("MemoryMax=64M", "TasksMax=8", "LimitNOFILE=64"),
                unit_name="palomar-" + "a" * 24, status_path=Path("/s/status.json"),
                liveness_path=Path("/s/liveness"),
            )
        self.assertEqual(argv[:4], ["/usr/bin/systemd-run", "--user", "--scope", "--"])
        self.assertTrue(argv[5].endswith("scripts/supervise_cgroup.py"))
        self.assertEqual(argv[argv.index("--deadline") + 1], "120")
        self.assertEqual(argv[argv.index("--name") + 1], "palomar-" + "a" * 24)
        self.assertIn("--limit", argv)
        self.assertIn("memory.max=67108864", argv)
        self.assertIn("pids.max=8", argv)
        self.assertIn("memory.oom.group=1", argv)
        self.assertIn("NOFILE=64", argv)
        self.assertIn("PATH=/usr/bin", argv)
        self.assertIn("HOME=/work/home", argv)
        self.assertNotIn("SECRET=x", argv)
        self.assertEqual(argv[-2:], ["--", "/bin/true"])

    def test_runtime_max_overrides_the_phase_deadline(self):
        with mock.patch.object(verifier, "supervisor_bootstrap", return_value=["/b", "--"]):
            argv = verifier.supervisor_command(
                ["/bin/true"], cwd=Path("/work"), environment={}, timeout=2,
                resource_properties=("RuntimeMaxSec=60s", "TimeoutStopSec=5s"),
                unit_name="palomar-" + "b" * 24, status_path=Path("/s/status.json"),
                liveness_path=Path("/s/liveness"),
            )
        self.assertEqual(argv[argv.index("--deadline") + 1], "60")
        self.assertEqual(argv[argv.index("--grace") + 1], "5")

    def test_rejects_a_foreign_unit_name(self):
        with self.assertRaises(verifier.VerificationError):
            verifier.supervisor_command(
                ["/bin/true"], cwd=Path("/work"), environment={}, unit_name="../../etc",
                status_path=Path("/s/status.json"), liveness_path=Path("/s/liveness"),
            )


class BwrapCommandTests(unittest.TestCase):
    def build(self, **overrides):
        kwargs = dict(
            bwrap=Path("/opt/bwrap"), writable_directories=[Path("/work/.lake")],
            writable_files=[Path("/work/lock.hash")], readable_paths=[Path("/etc/hosts")],
            executable_paths=[Path("/usr")], environment={"PATH": "/usr/bin", "SECRET": "x"},
            readable_directories=[Path("/work")],
        )
        kwargs.update(overrides)
        with mock.patch.object(verifier.shutil, "which", return_value="/usr/bin/env"):
            return verifier.bwrap_command(["/usr/bin/lake", "build"], **kwargs)

    def test_allowlist_root_binds_and_remounts_read_only(self):
        argv = self.build()
        self.assertEqual(argv[:6], ["/usr/bin/env", "-i", "/opt/bwrap", "--tmpfs", "/", "--dev"])
        joined = " ".join(argv)
        self.assertIn("--ro-bind /usr /usr", joined)
        self.assertIn("--ro-bind /work /work", joined)
        self.assertIn("--ro-bind /etc/hosts /etc/hosts", joined)
        self.assertIn("--bind /work/.lake /work/.lake", joined)
        self.assertIn("--bind /work/lock.hash /work/lock.hash", joined)
        # Writable trees are bound after the read-only ones, so the deeper mount wins.
        self.assertLess(argv.index("/work"), argv.index("/work/.lake"))
        self.assertLess(argv.index("/work/.lake"), argv.index("--remount-ro"))
        for flag in ("--unshare-user", "--unshare-pid", "--unshare-net", "--unshare-ipc",
                     "--unshare-uts", "--unshare-cgroup", "--disable-userns", "--die-with-parent",
                     "--new-session"):
            self.assertIn(flag, argv)
        self.assertIn("--setenv PATH /usr/bin", joined)
        self.assertNotIn("SECRET", joined)
        self.assertEqual(argv[-3:], ["--", "/usr/bin/lake", "build"])

    def test_network_and_nested_sandbox_switches(self):
        argv = self.build(unrestricted_network=True, nested_sandbox=True)
        self.assertNotIn("--unshare-net", argv)
        self.assertNotIn("--disable-userns", argv)
        joined = " ".join(argv)
        for point in ("/home", "/root", "/run/user", "/var"):
            self.assertIn(f"--dir {point}", joined)
        self.assertIn("--unshare-user", argv)

    def test_rejects_control_characters_in_the_environment(self):
        with self.assertRaises(verifier.VerificationError):
            self.build(environment={"PATH": "/usr/bin\ninjected"})


class CgroupSupervisorOutcomeTests(unittest.TestCase):
    def outcome(self, report, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / "status.json"
            if report is not None:
                status.write_text(json.dumps(report))
            with mock.patch.object(verifier, "_cleanup_phase_cgroup") as cleanup:
                result = verifier.supervisor_outcome(status, **kwargs)
        return result, cleanup

    def finished(self, **fields):
        report = {
            "state": "finished", "cgroup": "/sys/fs/cgroup/palomar/run-1/palomar-" + "c" * 24,
            "exit_status": 0, "term_signal": None, "deadline_fired": False, "placement_ok": True,
            "memory_events": {"oom": 0, "oom_kill": 0, "oom_group_kill": 0}, "memory_peak": 4096,
            "cpu_stat": {"usage_usec": 1500}, "pids_events": {"max": 0}, "limits_applied": {"cpu.max": False},
        }
        report.update(fields)
        return report

    def test_success_is_read_from_the_status_and_cleans_up(self):
        outcome, cleanup = self.outcome(self.finished())
        self.assertEqual(outcome["Result"], "success")
        self.assertEqual(outcome["ExecMainCode"], "exited")
        self.assertEqual(outcome["MemoryPeak"], "4096")
        self.assertEqual(outcome["CPUUsageNSec"], "1500000")
        self.assertEqual(outcome["ControlGroup"], "/palomar/run-1/palomar-" + "c" * 24)
        self.assertEqual(outcome["supervisor"], "cgroup")
        cleanup.assert_called_once_with(Path("/sys/fs/cgroup/palomar/run-1/palomar-" + "c" * 24))

    def test_oom_wins_over_exit_status(self):
        outcome, _ = self.outcome(self.finished(
            exit_status=None, term_signal=9, memory_events={"oom": 1, "oom_kill": 2, "oom_group_kill": 1},
        ))
        self.assertEqual(outcome["Result"], "oom-kill")
        self.assertEqual(outcome["memory_events"], {"oom": 1, "oom_kill": 2})

    def test_deadline_is_a_timeout_and_plain_exits_are_exit_code(self):
        timed_out = self.finished(exit_status=None, term_signal=15, deadline_fired=True)
        self.assertEqual(self.outcome(timed_out)[0]["Result"], "timeout")
        self.assertEqual(self.outcome(self.finished(exit_status=137))[0]["Result"], "exit-code")
        signalled = self.finished(exit_status=None, term_signal=11)
        self.assertEqual(self.outcome(signalled)[0]["Result"], "signal")

    def test_missing_or_unfinished_status_is_an_infrastructure_diagnostic(self):
        for report in (None, {"state": "started", "cgroup": "/sys/fs/cgroup/x"}, ["not", "an", "object"]):
            with self.subTest(report=report), self.assertRaises(verifier.VerificationError) as raised:
                self.outcome(report)
            self.assertEqual(raised.exception.code, "provider.resource_telemetry_missing")

    def test_placement_failure_is_never_a_candidate_result(self):
        with self.assertRaises(verifier.VerificationError) as raised:
            self.outcome(self.finished(placement_ok=False, placement_error="fail:OSError:x"))
        self.assertEqual(raised.exception.code, "provider.resource_telemetry_missing")

    def test_parent_timeout_reports_no_result_and_still_cleans_up(self):
        started = {"state": "started", "cgroup": "/sys/fs/cgroup/palomar/run-1/palomar-" + "d" * 24}
        outcome, cleanup = self.outcome(started, supervisor_timeout=True)
        self.assertIsNone(outcome["Result"])
        self.assertEqual(outcome["ActiveState"], "active")
        cleanup.assert_called_once()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.jsonl"
            verifier.append_resource_outcome(path, "worker", outcome, supervisor_timeout=True)
            record = json.loads(path.read_text())
        self.assertIsNone(record["systemd_result"])
        self.assertTrue(record["supervisor_timeout"])

    def test_cgroup_paths_outside_the_mount_are_ignored(self):
        outcome, cleanup = self.outcome(self.finished(cgroup="/etc/../sys/fs/cgroup/x"))
        self.assertEqual(outcome["ControlGroup"], "")
        cleanup.assert_not_called()
