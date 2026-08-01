#!/usr/bin/env python3
"""Run one confined phase and append bounded worker-usage telemetry."""

from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--disk-path", type=Path, required=True)
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
    child = subprocess.Popen(command)
    finished = threading.Event()

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
    record = {
        "phase": args.phase,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "user_cpu_seconds": round(usage.ru_utime, 3),
        "system_cpu_seconds": round(usage.ru_stime, 3),
        "max_rss_kib": usage.ru_maxrss,
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
