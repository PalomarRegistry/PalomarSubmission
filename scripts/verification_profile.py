#!/usr/bin/env python3
"""Load and validate Palomar's published verification resource policy."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = ROOT / "verification-profile.json"
CATALOGUE_PATH = ROOT / "execution-profiles.json"


class VerificationProfileError(RuntimeError):
    pass


def load_profile(identifier: str | None = None, *, allow_disabled: bool = False) -> dict[str, Any]:
    identifier = identifier or os.environ.get("PALOMAR_EXECUTION_PROFILE") or "palomar-standard-v1"
    value = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "id",
        "runner",
        "limits",
        "trusted_tools",
        "cache_policy",
    }:
        raise VerificationProfileError("verification profile has an invalid top-level shape")
    if value["schema_version"] != 1 or value["id"] != "palomar-standard-v1":
        raise VerificationProfileError("verification profile identity is unsupported")
    runner = value["runner"]
    if not isinstance(runner, dict) or set(runner) != {"provider", "label", "architecture"}:
        raise VerificationProfileError("verification profile runner is invalid")
    limits = value["limits"]
    expected_limits = {
        "job_timeout_minutes",
        "execution_budget_seconds",
        "memory_high_percent",
        "memory_max_percent",
        "minimum_workspace_free_bytes",
        "tasks_max",
        "open_files_max",
        "file_size_max_bytes",
        "minimum_host_memory_bytes",
    }
    if not isinstance(limits, dict) or set(limits) != expected_limits:
        raise VerificationProfileError("verification profile limits are invalid")
    if any(type(limits[name]) is not int or limits[name] <= 0 for name in expected_limits):
        raise VerificationProfileError("verification profile limits must be positive integers")
    if not (limits["memory_high_percent"] < limits["memory_max_percent"] <= 100):
        raise VerificationProfileError("memory_high_percent must be below memory_max_percent")
    if limits["execution_budget_seconds"] > limits["job_timeout_minutes"] * 60:
        raise VerificationProfileError("execution budget exceeds the job timeout")
    tools = value["trusted_tools"]
    expected_tools = {"comparator_commit", "landrun_commit", "nanoda_commit", "lean4export"}
    if not isinstance(tools, dict) or set(tools) != expected_tools:
        raise VerificationProfileError("verification profile trusted tools are invalid")
    for name in ("comparator_commit", "landrun_commit", "nanoda_commit"):
        commit = tools[name]
        if (
            not isinstance(commit, str)
            or len(commit) != 40
            or any(character not in "0123456789abcdef" for character in commit)
        ):
            raise VerificationProfileError(f"verification profile {name} is not a commit")
    if identifier != "palomar-standard-v1":
        catalogue = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
        selected = catalogue.get("profiles", {}).get(identifier)
        if not isinstance(selected, dict):
            raise VerificationProfileError("execution profile is not approved")
        if (
            catalogue.get("schema_version") != 1
            or catalogue.get("default") != "palomar-standard-v1"
            or set(selected) != {"runner", "limits"}
            or selected.get("limits") != {"minimum_host_memory_bytes": 30064771072}
            or selected.get("runner")
            != {
                "provider": "namespace",
                "architecture": "x86_64",
                "label": "nscloud-ubuntu-24.04-amd64-16x32-with-features",
                "labels": [
                    "nscloud-ubuntu-24.04-amd64-16x32-with-features",
                    "namespace-features:container.privileged=true",
                ],
            }
        ):
            raise VerificationProfileError("approved execution profile catalogue is malformed")
        if not allow_disabled and os.environ.get("PALOMAR_NAMESPACE_ENABLED") != "true":
            raise VerificationProfileError("Namespace is disabled pending confinement qualification")
        value["id"] = identifier
        value["runner"] = selected["runner"]
        value["limits"].update(selected["limits"])
    return value


def profile_digest(profile: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def cgroup_directories() -> list[Path]:
    """The process's cgroup-v2 ancestors, including a namespaced mount root."""
    root = Path("/sys/fs/cgroup")
    try:
        relative = next(
            line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::")
        )
        if ".." in Path(relative).parts:
            return [root]
        leaf = root / relative.lstrip("/")
        return [leaf, *[parent for parent in leaf.parents if parent == root or root in parent.parents]]
    except (OSError, StopIteration):
        return [root]


def effective_memory_bytes() -> int:
    limits = [host_memory_bytes()]
    for directory in cgroup_directories():
        try:
            raw = (directory / "memory.max").read_text().strip()
            if raw.isdigit():
                limits.append(int(raw))
        except OSError:
            continue
    return min(limits)


def effective_cpu_count() -> float:
    cpus = float(len(os.sched_getaffinity(0)))
    for directory in cgroup_directories():
        try:
            quota, period = (directory / "cpu.max").read_text().split()
            if quota.isdigit() and int(period) > 0:
                cpus = min(cpus, int(quota) / int(period))
        except (OSError, ValueError):
            continue
    return cpus


def host_memory_bytes() -> int:
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) * 1024
    raise VerificationProfileError("could not read host memory capacity")


def check_host(profile: dict[str, Any], disk_path: Path) -> dict[str, int | float | str]:
    runner = profile["runner"]
    limits = profile["limits"]
    architecture = platform.machine()
    accepted_architectures = {"x86_64": {"x86_64", "amd64"}}
    if architecture.lower() not in accepted_architectures.get(runner["architecture"], set()):
        raise VerificationProfileError(
            f"runner architecture {architecture!r} does not satisfy {runner['architecture']}"
        )
    cpus = effective_cpu_count()
    if profile["id"] == "palomar-namespace-16x32-v1" and cpus < 16:
        raise VerificationProfileError(f"runner has {cpus} effective CPUs; Namespace profile requires 16")
    memory = effective_memory_bytes()
    if memory < limits["minimum_host_memory_bytes"]:
        raise VerificationProfileError(
            f"runner has {memory} memory bytes; profile requires {limits['minimum_host_memory_bytes']}"
        )
    workspace = shutil.disk_usage(disk_path).free
    if workspace < limits["minimum_workspace_free_bytes"]:
        raise VerificationProfileError(
            f"runner has {workspace} free workspace bytes; profile requires "
            f"{limits['minimum_workspace_free_bytes']}"
        )
    return {
        "architecture": architecture,
        "effective_cpus": cpus,
        "memory_bytes": memory,
        "workspace_free_bytes": workspace,
        "memory_high_bytes": memory * limits["memory_high_percent"] // 100,
        "memory_max_bytes": memory * limits["memory_max_percent"] // 100,
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--disk-path", type=Path)
    parser.add_argument("--profile", default=None)
    parser.add_argument("--resolve", action="store_true")
    parser.add_argument("--allow-disabled", action="store_true")
    args = parser.parse_args()
    profile = load_profile(args.profile, allow_disabled=args.allow_disabled)
    if args.resolve:
        runner = profile["runner"]
        labels = runner.get("labels", [runner["label"]])
        outputs = {
            "labels": json.dumps(labels),
            "profile": profile["id"],
            "digest": profile_digest(profile),
            "timeout": str(profile["limits"]["job_timeout_minutes"]),
        }
        with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
            for name, value in outputs.items():
                handle.write(f"{name}={value}\n")
        return 0
    if args.disk_path is None:
        parser.error("--disk-path is required unless resolving a profile")
    observed = check_host(profile, args.disk_path)
    print(json.dumps({"profile": profile["id"], "observed": observed}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
