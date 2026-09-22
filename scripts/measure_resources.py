#!/usr/bin/env python3
"""Run one confined phase and append bounded worker-usage telemetry."""

from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path


def process_tree_size(root: int) -> int:
    pending = [root]
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        try:
            children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
        except OSError:
            continue
        pending.extend(int(child) for child in children if child.isdigit())
    return len(seen)


def own_cgroup() -> Path | None:
    """The cgroup v2 directory this observer (and so its workload) lives in."""
    try:
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            if line.startswith("0::"):
                relative = line[3:].strip().lstrip("/")
                if ".." not in Path(relative).parts:
                    return Path("/sys/fs/cgroup") / relative
    except OSError:
        pass
    return None


def cgroup_usage(cgroup: Path | None) -> dict[str, int]:
    """Peak memory and CPU time of the whole phase cgroup, when the kernel exposes them.

    rusage only sees what was waited for; bubblewrap's PID 1 stub does not pass
    its children's figures up, so under the bwrap policy the rusage numbers are
    the launcher's. The cgroup accounts for every process of the phase.
    """
    usage: dict[str, int] = {}
    if cgroup is None:
        return usage
    try:
        peak = (cgroup / "memory.peak").read_text().strip()
        if peak.isdigit():
            usage["memory_peak_bytes"] = int(peak)
    except OSError:
        pass
    try:
        for line in (cgroup / "cpu.stat").read_text().splitlines():
            key, _, raw = line.partition(" ")
            if key in {"user_usec", "system_usec"} and raw.strip().isdigit():
                usage[key] = int(raw)
    except OSError:
        pass
    return usage


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--disk-path", type=Path, required=True)
    parser.add_argument(
        "--cgroup-accounting", action="store_true",
        help="this observer runs in a cgroup dedicated to the phase; prefer its accounting",
    )
    parser.add_argument(
        "--pass-fd", action="append", type=int, default=[],
        help="inherited descriptor the workload must still see (sandbox status, seccomp filter)",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a command is required after --")

    started = time.monotonic()
    initial_free = shutil.disk_usage(args.disk_path).free
    minimum_free = initial_free
    peak_tasks = 1
    child = subprocess.Popen(command, pass_fds=args.pass_fd)
    finished = threading.Event()

    # At the deadline the supervisor terminates the workload, not this
    # observer: forward the signal and keep waiting so the usage record still
    # gets written. A second one, or SIGKILL, ends the observer too.
    def forward(signum, frame):  # noqa: ANN001
        signal.signal(signum, signal.SIG_DFL)
        try:
            child.send_signal(signum)
        except OSError:
            pass

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)

    def sample() -> None:
        nonlocal minimum_free, peak_tasks
        while not finished.wait(1.0):
            peak_tasks = max(peak_tasks, process_tree_size(child.pid))
            try:
                minimum_free = min(minimum_free, shutil.disk_usage(args.disk_path).free)
            except OSError:
                pass

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    returncode = child.wait()
    finished.set()
    sampler.join()
    peak_tasks = max(peak_tasks, process_tree_size(child.pid))
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    accounted = cgroup_usage(own_cgroup()) if args.cgroup_accounting else {}
    record = {
        "phase": args.phase,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "user_cpu_seconds": round(max(usage.ru_utime, accounted.get("user_usec", 0) / 1e6), 3),
        "system_cpu_seconds": round(max(usage.ru_stime, accounted.get("system_usec", 0) / 1e6), 3),
        "max_rss_kib": max(usage.ru_maxrss, accounted.get("memory_peak_bytes", 0) // 1024),
        "memory_source": "cgroup" if "memory_peak_bytes" in accounted else "rusage",
        "peak_tasks_observed": peak_tasks,
        "workspace_disk_peak_bytes": max(0, initial_free - minimum_free),
        "returncode": returncode,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
    return returncode if returncode >= 0 else 128 - returncode


if __name__ == "__main__":
    raise SystemExit(main())
