#!/usr/bin/env python3
"""Load and validate Palomar's single reproducible verification envelope."""

from __future__ import annotations

import json
import platform
import shutil
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = ROOT / "verification-profile.json"


class VerificationProfileError(RuntimeError):
    pass


def load_profile() -> dict[str, Any]:
    value = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "id", "runner", "limits", "trusted_tools", "cache_policy"
    }:
        raise VerificationProfileError("verification profile has an invalid top-level shape")
    if value["schema_version"] != 1 or value["id"] != "palomar-standard-v1":
        raise VerificationProfileError("verification profile identity is unsupported")
    runner = value["runner"]
    if not isinstance(runner, dict) or set(runner) != {
        "provider", "label", "architecture"
    }:
        raise VerificationProfileError("verification profile runner is invalid")
    limits = value["limits"]
    expected_limits = {
        "job_timeout_minutes",
        "execution_budget_seconds",
        "memory_high_bytes",
        "memory_max_bytes",
        "minimum_workspace_free_bytes",
        "tasks_max",
        "open_files_max",
        "file_size_max_bytes",
        "lake_jobs",
    }
    if not isinstance(limits, dict) or set(limits) != expected_limits:
        raise VerificationProfileError("verification profile limits are invalid")
    if any(type(limits[name]) is not int or limits[name] <= 0 for name in expected_limits):
        raise VerificationProfileError("verification profile limits must be positive integers")
    if limits["memory_high_bytes"] >= limits["memory_max_bytes"]:
        raise VerificationProfileError("memory_high_bytes must be below memory_max_bytes")
    if limits["execution_budget_seconds"] > limits["job_timeout_minutes"] * 60:
        raise VerificationProfileError("execution budget exceeds the job timeout")
    tools = value["trusted_tools"]
    expected_tools = {
        "comparator_commit", "landrun_commit", "nanoda_commit", "lean4export"
    }
    if not isinstance(tools, dict) or set(tools) != expected_tools:
        raise VerificationProfileError("verification profile trusted tools are invalid")
    for name in ("comparator_commit", "landrun_commit", "nanoda_commit"):
        commit = tools[name]
        if not isinstance(commit, str) or len(commit) != 40 or any(
            character not in "0123456789abcdef" for character in commit
        ):
            raise VerificationProfileError(f"verification profile {name} is not a commit")
    return value


def host_memory_bytes() -> int:
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) * 1024
    raise VerificationProfileError("could not read host memory capacity")


def check_host(profile: dict[str, Any], disk_path: Path) -> dict[str, int | str]:
    runner = profile["runner"]
    limits = profile["limits"]
    architecture = platform.machine()
    accepted_architectures = {"x86_64": {"x86_64", "amd64"}}
    if architecture.lower() not in accepted_architectures.get(runner["architecture"], set()):
        raise VerificationProfileError(
            f"runner architecture {architecture!r} does not satisfy {runner['architecture']}"
        )
    memory = host_memory_bytes()
    if memory < limits["memory_max_bytes"]:
        raise VerificationProfileError(
            f"runner has {memory} memory bytes; profile requires {limits['memory_max_bytes']}"
        )
    workspace = shutil.disk_usage(disk_path).free
    if workspace < limits["minimum_workspace_free_bytes"]:
        raise VerificationProfileError(
            f"runner has {workspace} free workspace bytes; profile requires "
            f"{limits['minimum_workspace_free_bytes']}"
        )
    return {
        "architecture": architecture,
        "memory_bytes": memory,
        "workspace_free_bytes": workspace,
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--disk-path", type=Path, required=True)
    args = parser.parse_args()
    profile = load_profile()
    observed = check_host(profile, args.disk_path)
    print(json.dumps({"profile": profile["id"], "observed": observed}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
