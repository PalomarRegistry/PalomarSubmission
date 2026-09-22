#!/usr/bin/env python3
"""Load and validate Palomar's published verification resource policy."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = ROOT / "verification-profile.json"
CATALOGUE_PATH = ROOT / "execution-profiles.json"


class VerificationProfileError(RuntimeError):
    pass


HOSTED_PROFILE = "palomar-standard-v1"
NAMESPACE_LABEL_RE = re.compile(r"^nscloud-ubuntu-24\.04-amd64-\d+x\d+-with-features$")
NAMESPACE_FEATURE_LABEL = "namespace-features:container.privileged=true"
CATALOGUE_LIMITS = {"minimum_host_memory_bytes", "job_timeout_minutes", "execution_budget_seconds"}


def load_catalogue() -> dict[str, Any]:
    """The approved execution profiles beside the hosted one, validated by shape."""
    catalogue = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
    if (
        not isinstance(catalogue, dict)
        or catalogue.get("schema_version") != 1
        or not isinstance(catalogue.get("default"), str)
        or not isinstance(catalogue.get("profiles"), dict)
    ):
        raise VerificationProfileError("approved execution profile catalogue is malformed")
    for identifier, selected in catalogue["profiles"].items():
        if not re.fullmatch(r"palomar-[a-z0-9-]+-v\d+", identifier) or identifier == HOSTED_PROFILE:
            raise VerificationProfileError(f"execution profile id is malformed: {identifier}")
        runner = selected.get("runner") if isinstance(selected, dict) else None
        limits = selected.get("limits") if isinstance(selected, dict) else None
        if (
            set(selected) != {"runner", "limits"}
            or not isinstance(runner, dict)
            or set(runner) != {"provider", "label", "labels", "architecture"}
            or runner["provider"] != "namespace"
            or runner["architecture"] != "x86_64"
            or not NAMESPACE_LABEL_RE.fullmatch(str(runner["label"]))
            or runner["labels"] != [runner["label"], NAMESPACE_FEATURE_LABEL]
            or not isinstance(limits, dict)
            or set(limits) != CATALOGUE_LIMITS
            or any(type(limits[name]) is not int or limits[name] <= 0 for name in CATALOGUE_LIMITS)
            or limits["execution_budget_seconds"] > limits["job_timeout_minutes"] * 60
        ):
            raise VerificationProfileError(f"approved execution profile is malformed: {identifier}")
    if catalogue["default"] != HOSTED_PROFILE and catalogue["default"] not in catalogue["profiles"]:
        raise VerificationProfileError("approved execution profile catalogue default is not approved")
    return catalogue


def default_profile_id() -> str:
    return load_catalogue()["default"]


def load_profile(identifier: str | None = None) -> dict[str, Any]:
    identifier = identifier or os.environ.get("PALOMAR_EXECUTION_PROFILE") or default_profile_id()
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
    if identifier != HOSTED_PROFILE:
        catalogue = load_catalogue()
        selected = catalogue["profiles"].get(identifier)
        if not isinstance(selected, dict):
            raise VerificationProfileError("execution profile is not approved")
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
    args = parser.parse_args()
    profile = load_profile(args.profile)
    if args.resolve:
        runner = profile["runner"]
        labels = runner.get("labels", [runner["label"]])
        outputs = {
            "labels": json.dumps(labels),
            "profile": profile["id"],
            "digest": profile_digest(profile),
            "timeout": str(profile["limits"]["job_timeout_minutes"]),
            # Rendering starts from an accepted submission whose build the
            # budget already bounds; it gets the verification timeout plus a
            # margin. Computed here because workflow expressions cannot add.
            "render_timeout": str(profile["limits"]["job_timeout_minutes"] + 10),
            "budget": str(profile["limits"]["execution_budget_seconds"]),
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
