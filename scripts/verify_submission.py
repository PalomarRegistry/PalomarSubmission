#!/usr/bin/env python3
"""Prepare and mechanically verify one Palomar submission."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

ROOT = Path(__file__).resolve().parent.parent
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts import submission_contract  # noqa: E402
from scripts.verification_errors import (  # noqa: E402
    FormalizationValidationError,
    VerificationError,
)

MAX_SOURCE_BYTES = 500 * 1024 * 1024
MAX_LICENSE_BYTES = 1024 * 1024
MAX_CHALLENGE_BYTES = 100 * 1024
MAX_CHALLENGE_LINES = 1000
MAX_CONFIGURATION_BYTES = 1024 * 1024
STANDARD_AXIOMS = {"propext", "Quot.sound", "Classical.choice"}
COMPARATOR_REQUIRED_KEYS = {
    "challenge_module",
    "solution_module",
    "theorem_names",
    "permitted_axioms",
}
COMPARATOR_ALLOWED_KEYS = COMPARATOR_REQUIRED_KEYS | {"definition_names", "enable_nanoda"}
COMPILED_ARTIFACT_SUFFIXES = {
    ".a",
    ".bc",
    ".dll",
    ".dylib",
    ".ilean",
    ".ir",
    ".o",
    ".obj",
    ".olean",
    ".so",
    ".trace",
}
# The module system's sidecars are double-suffixed, so `Path.suffix` reports
# `.private`/`.server` and the table above cannot match them on its own.
COMPILED_ARTIFACT_NAME_SUFFIXES = (".olean.private", ".olean.server")
# A Lean toolchain, as a comparable version. Release candidates sort before the
# release they lead to, so v4.31.0-rc2 < v4.31.0, and anything that does not
# parse is refused rather than guessed at.
TOOLCHAIN_RE = re.compile(
    r"^leanprover/lean4:v(?P<major>[0-9]+)\.(?P<minor>[0-9]+)\.(?P<patch>[0-9]+)"
    r"(?:-rc(?P<rc>[0-9]+))?$"
)
VERSION_RE = re.compile(
    r"^v(?P<major>[0-9]+)\.(?P<minor>[0-9]+)\.(?P<patch>[0-9]+)"
    r"(?:-rc(?P<rc>[0-9]+))?$"
)


def parse_lean_version(value: str, pattern: re.Pattern[str]) -> tuple[int, int, int, int, int]:
    match = pattern.fullmatch(value.strip())
    if not match:
        raise VerificationError(
            f"unsupported Lean toolchain: {value!r}", code="toolchain.invalid"
        )
    rc = match.group("rc")
    # The fourth element orders a release candidate before its release.
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        0 if rc else 1,
        int(rc) if rc else 0,
    )


def toolchain_release_tag(toolchain: str) -> str:
    """The tag in Palomar's tooling repositories that matches this toolchain.

    The version is derived rather than looked up in a table that has to be
    edited for every release and is stale the moment it is not. Renderer policy
    may subsequently fall back from a missing stable Verso patch tag to the
    release line's patch-zero tag.
    """
    match = TOOLCHAIN_RE.fullmatch(toolchain.strip())
    if not match:
        raise VerificationError(f"unsupported Lean toolchain: {toolchain!r}")
    return "v" + toolchain.strip().split(":v", 1)[1]


def supported_toolchain(toolchain: str) -> str:
    """Refuse a toolchain below the floor Palomar's tooling supports."""
    settings = json.loads((ROOT / "toolchains.json").read_text(encoding="utf-8"))
    if not isinstance(settings, dict) or set(settings) != {"schema_version", "minimum"}:
        raise VerificationError(
            "toolchains.json must contain exactly schema_version and minimum",
            code="palomar.toolchain_policy_invalid",
            owner="palomar",
            next_action="Do not change the repository; Palomar must repair its toolchain policy.",
        )
    if type(settings["schema_version"]) is not int or settings["schema_version"] != 2:
        raise VerificationError(
            "toolchains.json does not use schema version 2",
            code="palomar.toolchain_policy_invalid",
            owner="palomar",
            next_action="Do not change the repository; Palomar must repair its toolchain policy.",
        )
    minimum = settings.get("minimum")
    if not isinstance(minimum, str) or not VERSION_RE.fullmatch(minimum):
        raise VerificationError(
            "toolchains.json does not record a valid minimum",
            code="palomar.toolchain_policy_invalid",
            owner="palomar",
            next_action="Do not change the repository; Palomar must repair its toolchain policy.",
        )
    if parse_lean_version(toolchain, TOOLCHAIN_RE) < parse_lean_version(minimum, VERSION_RE):
        raise VerificationError(
            f"Lean toolchain {toolchain} is older than the minimum Palomar supports ({minimum})",
            code="toolchain.unsupported",
        )
    return toolchain_release_tag(toolchain)


def resolve_release_commit(repository: str, tag: str) -> str:
    """The commit a tooling release points at.

    The tag is resolved once and the commit it named is recorded in the
    mechanical report, so a record always says exactly which revision of
    Palomar's own tooling checked it, whatever the tag says afterwards.
    """
    proc = subprocess.run(
        ["git", "ls-remote", f"https://github.com/{repository}", f"refs/tags/{tag}^{{}}",
         f"refs/tags/{tag}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise VerificationError(
            f"could not read {repository} releases",
            code="provider.release_lookup_failed",
            owner="provider",
            next_action="Do not change the repository. Retry the same commit later.",
            retryable=True,
        )
    commits = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and submission_contract.SHA_RE.fullmatch(parts[0]):
            commits[parts[1]] = parts[0]
    # An annotated tag resolves through its peeled ref; prefer that.
    commit = commits.get(f"refs/tags/{tag}^{{}}") or commits.get(f"refs/tags/{tag}")
    if not commit:
        raise VerificationError(
            f"{repository} has published no {tag} release",
            code="palomar.toolchain_release_missing",
            owner="palomar",
            next_action=(
                "Do not change the repository. Palomar must add support for this Lean release."
            ),
        )
    return commit


LICENSE_FILE_RE = re.compile(
    r"^(?:licen[cs]e|copying|unlicense|ofl)(?:\.(?:md|markdown|txt))?$",
    re.IGNORECASE,
)
MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_']*(?:\.[A-Za-z_][A-Za-z0-9_']*)*$")
OFFICIAL_REF_RE = re.compile(r"^refs/heads/[A-Za-z0-9._/-]+$")
SANDBOX_ENVIRONMENT = (
    "PATH",
    "HOME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LEAN_PATH",
    "LEAN_SRC_PATH",
    "LEAN_ABORT_ON_PANIC",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_TERMINAL_PROMPT",
    "MATHLIB_CACHE_DIR",
    "MATHLIB_CACHE_GET_URL",
    "LAKE_PKG_URL_MAP",
    "COMPARATOR_LANDRUN",
    "COMPARATOR_LEAN4EXPORT",
    "COMPARATOR_NANODA",
    "PALOMAR_LANDRUN_REAL",
)


class LicenseValidationError(VerificationError):
    """The submitted repository does not satisfy the licence policy."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            code="license.mismatch",
            field="project.license",
            repairable=True,
            next_action=(
                "Make project.license agree with the repository license file, commit the "
                "change, and make a new submission."
            ),
        )


class LicenseDetectorError(VerificationError):
    """The trusted SPDX detector failed rather than rejecting submitted terms."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            code="palomar.license_detector_failed",
            owner="palomar",
            next_action=(
                "Do not change the repository. Retry the same commit later; report the "
                "workflow URL if the problem recurs."
            ),
            retryable=True,
        )


class ResourceExhausted(VerificationError):
    """The available worker could not complete a verification phase."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            code="provider.resource_exhausted",
            owner="provider",
            next_action="Do not change the repository. Retry the same commit later.",
            retryable=True,
        )


DIAGNOSTICS_SCHEMA_VERSION = 1
MAX_DIAGNOSTICS = 50
PALOMAR_OWNED_STAGES = frozenset(
    {
        "setup",
        "confinement-initial",
        "confinement-final",
        "trusted-cache",
        "trusted-roots",
        "resource-exhausted",
    }
)


def report_diagnostic(
    report: dict[str, Any],
    error: BaseException,
    *,
    stage: str | None = None,
    owner: str | None = None,
) -> None:
    """Append bounded public diagnostics without changing successful reports."""
    current_stage = stage or str(report.get("stage") or "unknown")
    if isinstance(error, FormalizationValidationError):
        for issue in error.issues:
            report_diagnostic(report, issue, stage=current_stage)
        return
    if isinstance(error, VerificationError):
        diagnostic = error.diagnostic(current_stage)
        reclassified_to_palomar = False
        if owner is not None:
            diagnostic["owner"] = owner
            reclassified_to_palomar = owner == "palomar" and error.owner == "submitter"
        elif error.owner == "submitter" and current_stage in PALOMAR_OWNED_STAGES:
            diagnostic["owner"] = "palomar"
            reclassified_to_palomar = True
        if reclassified_to_palomar:
            diagnostic["retryable"] = True
            diagnostic["repairable"] = False
            diagnostic["next_action"] = (
                "Do not change the repository. Retry the same commit later; report the "
                "workflow URL if the problem recurs."
            )
    else:
        diagnostic = VerificationError(
            "Palomar could not complete this check.",
            code="palomar.internal_error",
            owner="palomar",
            next_action=(
                "Do not change the repository. Retry the same commit later; report the "
                "workflow URL if the problem recurs."
            ),
            retryable=True,
        ).diagnostic(current_stage)
        diagnostic["explanation"] = f"{type(error).__name__}: {str(error)[:1_500]}"
    diagnostics = report.setdefault("diagnostics", [])
    if not isinstance(diagnostics, list) or len(diagnostics) >= MAX_DIAGNOSTICS:
        return
    key = (diagnostic["code"], diagnostic["summary"], diagnostic.get("field"))
    if any(
        isinstance(item, dict)
        and (item.get("code"), item.get("summary"), item.get("field")) == key
        for item in diagnostics
    ):
        return
    report["diagnostics_schema_version"] = DIAGNOSTICS_SCHEMA_VERSION
    report["formalization_profile_version"] = submission_contract.FORMALIZATION_PROFILE_VERSION
    diagnostics.append(diagnostic)


MAX_CAPTURE_BYTES = 8 * 1024 * 1024
EXECUTION_BUDGET_SECONDS = 12 * 60 * 60
PERMISSIVE_RESOURCE_PROPERTIES = (
    "MemoryHigh=95%",
    "MemoryMax=98%",
    "TasksMax=32768",
    "LimitNOFILE=1048576",
    f"LimitFSIZE={1024**4}",
)
_EXECUTION_DEADLINE: float | None = None
_MONOTONIC = time.monotonic
_WALL_TIME = time.time
_SYSTEMD_MANAGER: str | None = None
_RESOURCE_METRICS_PATH: Path | None = None
_RESOURCE_DISK_PATH: Path | None = None


def install_execution_deadline(
    started_at_epoch: str | float | None = None,
    budget_seconds: int = EXECUTION_BUDGET_SECONDS,
) -> float | None:
    """Arm the shared deadline, including trusted CI setup time when supplied."""
    global _EXECUTION_DEADLINE

    previous = _EXECUTION_DEADLINE
    if budget_seconds < 60:
        raise VerificationError("verification execution budget must be at least one minute")
    elapsed = 0.0
    if started_at_epoch is not None:
        try:
            started_at = float(started_at_epoch)
        except (TypeError, ValueError) as error:
            raise VerificationError("invalid verification job start time") from error
        if not math.isfinite(started_at) or started_at <= 0:
            raise VerificationError("invalid verification job start time")
        elapsed = max(0.0, _WALL_TIME() - started_at)
    remaining = max(0.0, budget_seconds - elapsed)
    _EXECUTION_DEADLINE = _MONOTONIC() + remaining
    return previous


def _deadline_timeout(requested: int, command: list[str]) -> int:
    """Cap one phase by the verifier-wide wall-clock deadline when active."""
    if _EXECUTION_DEADLINE is None:
        return requested
    remaining = int(_EXECUTION_DEADLINE - _MONOTONIC())
    if remaining < 1:
        raise subprocess.TimeoutExpired(command, EXECUTION_BUDGET_SECONDS)
    return min(requested, remaining)


def _bounded_stream_reader(stream: Any, destination: bytearray) -> None:
    omitted = 0
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            destination.extend(chunk)
            if len(destination) > MAX_CAPTURE_BYTES:
                excess = len(destination) - MAX_CAPTURE_BYTES
                del destination[:excess]
                omitted += excess
    finally:
        stream.close()
    if omitted:
        marker = f"<output truncated; omitted {omitted} bytes>\n".encode()
        destination[:0] = marker


def now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def resource_metrics(path: Path) -> list[dict[str, Any]]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024 * 1024:
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines()[:1000]:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: int = 600,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    timeout = _deadline_timeout(timeout, command)
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None and proc.stderr is not None
    stdout_bytes = bytearray()
    stderr_bytes = bytearray()
    readers = [
        threading.Thread(target=_bounded_stream_reader, args=(proc.stdout, stdout_bytes), daemon=True),
        threading.Thread(target=_bounded_stream_reader, args=(proc.stderr, stderr_bytes), daemon=True),
    ]
    for reader in readers:
        reader.start()
    if input_text is not None:
        assert proc.stdin is not None
        proc.stdin.write(input_text.encode("utf-8"))
        proc.stdin.close()
    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        for reader in readers:
            reader.join()
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr) from None
    for reader in readers:
        reader.join()
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    completed = subprocess.CompletedProcess(command, returncode, stdout, stderr)
    if check and completed.returncode:
        streams = []
        if completed.stdout.strip():
            streams.append(f"stdout:\n{completed.stdout.strip()}")
        if completed.stderr.strip():
            streams.append(f"stderr:\n{completed.stderr.strip()}")
        detail = "\n".join(streams)[-8000:]
        raise VerificationError(
            f"{' '.join(command[:3])} failed ({completed.returncode}): {detail}"
        )
    return completed


def normalized_repository_path(value: str, field: str) -> pathlib.PurePosixPath:
    """Validate one user-supplied repository-relative POSIX path."""
    raw = value.strip()
    segments = raw.split("/")
    if (
        not raw
        or raw.startswith("/")
        or "\\" in raw
        or "?" in raw
        or "#" in raw
        or any(not segment or segment in {".", ".."} for segment in segments)
        or any(segment.lower() in {".git", ".lake"} for segment in segments)
        or ":" in segments[0]
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
    ):
        raise VerificationError(f"{field} must be a safe repository-relative POSIX path")
    return pathlib.PurePosixPath(*segments)


def joined_repository_path(
    project_path: pathlib.PurePosixPath | None, filename: str
) -> pathlib.PurePosixPath:
    return (project_path / filename) if project_path is not None else pathlib.PurePosixPath(filename)


def resolve_repository_path(
    checkout: Path,
    relative: pathlib.PurePosixPath,
    field: str,
    *,
    kind: str,
) -> Path:
    """Resolve a validated repository path without following submitted symlinks."""
    candidate = checkout
    for segment in relative.parts:
        candidate /= segment
        if candidate.is_symlink():
            raise VerificationError(f"{field} contains a symlinked path component")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(checkout.resolve())
    except ValueError as error:
        raise VerificationError(f"{field} escapes the pinned repository checkout") from error
    if kind == "file" and not resolved.is_file():
        raise VerificationError(f"{field} is not a regular file: {relative.as_posix()}")
    if kind == "directory" and not resolved.is_dir():
        raise VerificationError(f"{field} is not a directory: {relative.as_posix()}")
    return resolved


def repository_relative_path(checkout: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(checkout.resolve()).as_posix()
    except ValueError as error:
        raise VerificationError("resolved project file escapes the pinned repository checkout") from error


def project_tree_url(repository_url: str, commit: str, project_path: str | None) -> str:
    base = f"{repository_url}/tree/{commit}"
    if not project_path:
        return base
    encoded = "/".join(quote(segment, safe="") for segment in project_path.split("/"))
    return f"{base}/{encoded}"


def module_source_suffix(module: str) -> pathlib.PurePosixPath:
    if not isinstance(module, str) or not MODULE_RE.fullmatch(module):
        raise VerificationError(f"unsafe dotted Lean module name: {module!r}")
    return pathlib.PurePosixPath(*module.split(".")).with_suffix(".lean")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def repository_license_file(source: Path) -> Path:
    candidates = sorted(
        (path for path in source.iterdir() if LICENSE_FILE_RE.fullmatch(path.name)),
        key=lambda path: (path.name.casefold(), path.name),
    )
    if not candidates:
        raise LicenseValidationError(
            "repository root has no conventional licence file "
            "(LICENSE, LICENCE, COPYING, UNLICENSE, or OFL)"
        )
    if len(candidates) != 1:
        names = ", ".join(path.name for path in candidates)
        raise LicenseValidationError(
            f"repository root must contain exactly one conventional licence file; found: {names}"
        )
    path = candidates[0]
    if path.is_symlink() or not path.is_file():
        raise LicenseValidationError(f"repository licence path is not a regular root file: {path.name}")
    if path.stat().st_size > MAX_LICENSE_BYTES:
        raise LicenseValidationError("repository licence file exceeds the 1 MiB hard cap")
    try:
        contents = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise LicenseValidationError("repository licence file must be UTF-8 text") from error
    if not contents.strip():
        raise LicenseValidationError("repository licence file must not be empty")
    return path


def detect_spdx_identifier(path: Path, bundle: Path) -> str:
    if not bundle.is_file():
        raise LicenseDetectorError(f"trusted Bundler executable is unavailable: {bundle}")
    environment = os.environ.copy()
    environment["BUNDLE_GEMFILE"] = str(ROOT / "Gemfile")
    try:
        completed = run(
            [
                str(bundle),
                "exec",
                "ruby",
                str(ROOT / "scripts" / "detect_license.rb"),
                str(path),
            ],
            cwd=ROOT,
            env=environment,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LicenseDetectorError("trusted SPDX licence detector could not run") from error
    if completed.returncode != 0:
        detail = completed.stderr.strip()[-2_000:]
        suffix = f": {detail}" if detail else ""
        raise LicenseDetectorError(
            f"trusted SPDX licence detector failed with exit {completed.returncode}{suffix}"
        )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise LicenseDetectorError("trusted SPDX licence detector returned malformed JSON") from error
    if not isinstance(result, dict):
        raise LicenseDetectorError("trusted SPDX licence detector returned a non-object result")
    licenses = result.get("licenses")
    matched_files = result.get("matched_files")
    if not isinstance(licenses, list) or not isinstance(matched_files, list):
        raise LicenseDetectorError("trusted SPDX licence detector omitted required result fields")
    identifiers = [
        item.get("spdx_id")
        for item in licenses
        if isinstance(item, dict) and isinstance(item.get("spdx_id"), str)
    ]
    matches = [
        item.get("matched_license")
        for item in matched_files
        if isinstance(item, dict) and isinstance(item.get("matched_license"), str)
    ]
    if len(identifiers) != 1 or len(matches) != 1:
        raise LicenseValidationError(
            "repository licence file does not have one unambiguous standard SPDX match"
        )
    identifier = identifiers[0]
    if identifier in {"NONE", "NOASSERTION"} or matches[0] != identifier:
        raise LicenseValidationError(
            "repository licence file does not have one unambiguous standard SPDX match"
        )
    return identifier


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def workflow_output(**values: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            if "\n" in value:
                raise VerificationError(f"newline in workflow output {key}")
            handle.write(f"{key}={value}\n")


def clone_commit(url: str, commit: str, destination: Path) -> None:
    destination.mkdir(parents=True)
    git_env = os.environ.copy()
    git_env.update(
        {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
        }
    )
    git = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "protocol.file.allow=never",
        "-C",
        str(destination),
    ]
    run(["git", "-c", "core.hooksPath=/dev/null", "init", "-q", str(destination)], env=git_env)
    run([*git, "remote", "add", "origin", url], env=git_env)
    run(
        [
            *git,
            "fetch",
            "--depth=1",
            "--no-tags",
            "origin",
            commit,
        ],
        env=git_env,
        timeout=600,
    )
    run([*git, "checkout", "-q", "--detach", "FETCH_HEAD"], env=git_env)
    resolved = run([*git, "rev-parse", "HEAD"], env=git_env).stdout.strip()
    if resolved != commit:
        raise VerificationError(f"fetched {resolved}, expected {commit}")
    run([*git, "remote", "set-url", "--push", "origin", "no_push"], env=git_env)


def validate_preservable_git_checkout(
    checkout: Path,
    label: str,
    *,
    allow_inert_submodules: bool = False,
) -> None:
    """Reject Git shapes whose contents a GitHub fork would not preserve.

    A native fork preserves a submodule's gitlink but not the referenced
    repository. Submitted and substantive sources therefore may not use them.
    Dependency checkouts are different: Palomar never initializes their
    submodules, so an inert historical gitlink is part of the preserved Git
    tree but not part of the source consumed by the build.

    Git LFS entries always preserve only pointers into storage owned by the
    original repository, so they remain disallowed in every checkout.
    """
    git = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-C",
        str(checkout),
    ]
    if not allow_inert_submodules:
        index = run([*git, "ls-files", "--stage", "-z"]).stdout
        for record in index.split("\0"):
            if record and record.split(None, 1)[0] == "160000":
                path = record.split("\t", 1)[-1]
                raise VerificationError(
                    f"{label} contains Git submodule {path!r}; submodules are not preservable"
                )

    tracked = run([*git, "ls-files", "-z"]).stdout
    if not tracked:
        return
    attributes = run(
        [*git, "check-attr", "--cached", "-z", "filter", "--stdin"],
        input_text=tracked,
    ).stdout.split("\0")
    for position in range(0, len(attributes) - 2, 3):
        path, attribute, value = attributes[position : position + 3]
        if attribute == "filter" and value == "lfs":
            raise VerificationError(
                f"{label} tracks {path!r} with Git LFS; LFS objects are not preservable"
            )


def tree_size(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        if ".git" in path.parts or path.is_symlink() or not path.is_file():
            continue
        total += path.stat().st_size
        if total > MAX_SOURCE_BYTES:
            break
    return total


def validate_preservable_remote_source(
    work: Path, source: dict[str, str], label: str
) -> None:
    """Fetch and inspect a recorded source that is not part of the proof build."""
    with tempfile.TemporaryDirectory(prefix="palomar-preservation-", dir=work) as directory:
        checkout = Path(directory) / "source"
        clone_commit(source["repository_url"], source["commit"], checkout)
        validate_preservable_git_checkout(checkout, label)
        if tree_size(checkout) > MAX_SOURCE_BYTES:
            raise VerificationError(f"{label} exceeds the 500 MiB cap")


def strip_lean_comments(text: str) -> str:
    """Replace nested Lean comments with whitespace while preserving line boundaries."""
    result: list[str] = []
    index = 0
    block_depth = 0
    in_string = False
    escaped = False
    while index < len(text):
        pair = text[index : index + 2]
        character = text[index]
        if block_depth:
            if pair == "/-":
                block_depth += 1
                result.extend("  ")
                index += 2
            elif pair == "-/":
                block_depth -= 1
                result.extend("  ")
                index += 2
            else:
                result.append("\n" if character == "\n" else " ")
                index += 1
            continue
        if not in_string and pair == "--":
            end = text.find("\n", index)
            if end == -1:
                result.extend(" " * (len(text) - index))
                break
            result.extend(" " * (end - index))
            result.append("\n")
            index = end + 1
            continue
        if not in_string and pair == "/-":
            block_depth = 1
            result.extend("  ")
            index += 2
            continue
        result.append(character)
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        index += 1
    if block_depth:
        raise VerificationError("unterminated Lean block comment")
    return "".join(result)


def unique_comparator_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build one JSON object without accepting last-key-wins ambiguity."""
    result: dict[str, Any] = {}
    duplicates: set[str] = set()
    for key, value in pairs:
        if key in result:
            duplicates.add(key)
        result[key] = value
    if duplicates:
        raise VerificationError(
            f"Comparator configuration has duplicate keys: {', '.join(sorted(duplicates))}",
            code="comparator.duplicate_key",
        )
    return result


def load_comparator_config(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise VerificationError("Comparator configuration is not a regular file")
    if path.stat().st_size > MAX_CONFIGURATION_BYTES:
        raise VerificationError(
            "Comparator configuration exceeds the 1 MiB hard cap",
            code="comparator.too_large",
        )
    try:
        config = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=unique_comparator_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise VerificationError(
            "Comparator configuration must contain valid UTF-8 JSON"
        ) from error
    if not isinstance(config, dict):
        raise VerificationError("Comparator configuration must contain one JSON object")
    missing = COMPARATOR_REQUIRED_KEYS - config.keys()
    if missing:
        raise VerificationError(
            f"comparator.json missing: {', '.join(sorted(missing))}",
            code="comparator.missing_key",
        )
    # Comparator exposes this switch for other callers, but it is not an
    # authoring requirement here. Palomar always enables the independent
    # replay in its own protected copy below, regardless of what a repository
    # says or whether it carries the compatibility field at all.
    unknown = config.keys() - COMPARATOR_ALLOWED_KEYS
    if unknown:
        raise VerificationError(
            f"comparator.json has unknown keys: {', '.join(sorted(unknown))}",
            code="comparator.unknown_key",
        )
    module_source_suffix(config["challenge_module"])
    module_source_suffix(config["solution_module"])
    if config["challenge_module"] == config["solution_module"]:
        raise VerificationError("Comparator Challenge and Solution modules must be distinct")
    theorem_names = config["theorem_names"]
    definition_names = config.get("definition_names", [])
    if not isinstance(theorem_names, list) or not theorem_names:
        raise VerificationError("comparator theorem_names must be a nonempty array")
    if not all(isinstance(item, str) and item for item in theorem_names + definition_names):
        raise VerificationError("comparator declaration names must be nonempty strings")
    axioms = config["permitted_axioms"]
    if not isinstance(axioms, list) or not set(axioms) <= STANDARD_AXIOMS:
        raise VerificationError(
            "comparator permitted_axioms exceed Palomar's standard allowlist",
            code="comparator.invalid_axioms",
        )
    return config


def protected_comparator_config(source: Path, destination: Path) -> Path:
    """Write Palomar's trusted config with the independent replay forced on."""
    config = load_comparator_config(source)
    config["enable_nanoda"] = True
    write_json(destination, config)
    # Validate the bytes Comparator will actually consume, not only the
    # earlier submitted object. The explicit assertion is defense in depth
    # against a future serializer or loader change weakening the invariant.
    protected = load_comparator_config(destination)
    if protected.get("enable_nanoda") is not True:
        raise VerificationError("protected Comparator configuration did not enable NanoDa")
    return destination.resolve(strict=True)


def prepare(args: argparse.Namespace) -> int:
    global _EXECUTION_DEADLINE

    output = Path(args.output).resolve()
    work = Path(args.work_dir).resolve()
    previous_deadline = _EXECUTION_DEADLINE
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "error",
        "stage": "intake",
        "phase": "preparation",
        "checked_at": now(),
        "errors": [],
        "warnings": [],
    }
    try:
        install_execution_deadline(os.environ.get("PALOMAR_JOB_STARTED_AT"))
        event = json.loads(Path(args.event).read_text(encoding="utf-8"))
        values, submission_id = submission_contract.submission_request(event)
        repository, url = submission_contract.normalize_repository(
            values.get("repository_url", "")
        )
        commit = values.get("commit_sha", "").strip().lower()
        if not submission_contract.SHA_RE.fullmatch(commit):
            raise VerificationError("Commit SHA must be 40 lowercase hexadecimal characters")
        existing_id = values.get("existing_id", "").strip().upper()
        if existing_id and not submission_contract.PALOMAR_ID_RE.fullmatch(existing_id):
            raise VerificationError(
                "Existing Palomar ID must have the form PALOMAR-2026-07-29-000123"
            )
        raw_relationship = values.get("authorization_relationship", "").strip()
        if raw_relationship:
            authorization_relationship = submission_contract.AUTHORIZATION_RELATIONSHIPS.get(
                raw_relationship
            )
            if authorization_relationship is None:
                raise VerificationError(
                    "Relationship to the substantive formalization is not recognized"
                )
        else:
            raise VerificationError(
                "Relationship to the substantive formalization must be declared"
            )
        authorization: dict[str, str] = {
            "relationship": authorization_relationship,
        }
        authorization_evidence = values.get("authorization_evidence", "").strip()
        if len(authorization_evidence) > 4_000:
            raise VerificationError("Authorization evidence exceeds 4,000 characters")
        if authorization_evidence:
            authorization["evidence"] = authorization_evidence

        raw_project_path = values.get("project_path", "").strip()
        project_relative = (
            normalized_repository_path(raw_project_path, "Project path")
            if raw_project_path
            else None
        )
        project_path_value = project_relative.as_posix() if project_relative is not None else None

        report.update(
            {
                "submission": {
                    "submission_id": submission_id,
                    "authorization": authorization,
                    "requested_paths": {
                        "project_path": raw_project_path,
                        "comparator_config_path": values.get(
                            "comparator_config_path", ""
                        ).strip(),
                        "formalization_metadata_path": values.get(
                            "formalization_metadata_path", ""
                        ).strip(),
                    },
                },
                "source": {
                    "repository": repository,
                    "repository_url": url,
                    "commit": commit,
                    "tree_url": project_tree_url(url, commit, project_path_value),
                },
                "existing_id": existing_id or None,
            }
        )
        source = work / "source"
        clone_commit(url, commit, source)
        validate_preservable_git_checkout(source, "submitted source")
        size = tree_size(source)
        if size > MAX_SOURCE_BYTES:
            raise VerificationError("checked-out source exceeds the 500 MiB cap")
        report["source"]["bytes"] = size

        project = (
            resolve_repository_path(source, project_relative, "Project path", kind="directory")
            if project_relative is not None
            else source.resolve()
        )
        if project_path_value is not None:
            report["source"]["project_path"] = project_path_value

        lakefiles = [
            path
            for name in ("lakefile.toml", "lakefile.lean")
            if (path := project / name).exists() or path.is_symlink()
        ]
        if len(lakefiles) != 1:
            raise VerificationError(
                "project directory must contain exactly one of lakefile.toml or lakefile.lean"
            )
        lakefile = lakefiles[0]
        if lakefile.is_symlink() or not lakefile.is_file():
            raise VerificationError("selected Lakefile is not a regular file")
        if lakefile.stat().st_size > MAX_CONFIGURATION_BYTES:
            raise VerificationError("Lakefile exceeds the 1 MiB hard cap")
        if lakefile.name == "lakefile.toml":
            tomllib.loads(lakefile.read_text(encoding="utf-8"))

        raw_metadata_path = values.get("formalization_metadata_path", "").strip()
        metadata_relative = (
            normalized_repository_path(raw_metadata_path, "Formalization metadata path")
            if raw_metadata_path
            else joined_repository_path(project_relative, "formalization.yaml")
        )
        if metadata_relative.name != "formalization.yaml":
            raise VerificationError("Formalization metadata path must name formalization.yaml")
        metadata_path = resolve_repository_path(
            source, metadata_relative, "Formalization metadata path", kind="file"
        )
        preflight_issues: list[tuple[str, BaseException]] = []

        def add_issue(stage: str, error: BaseException) -> None:
            if isinstance(error, FormalizationValidationError):
                preflight_issues.extend((stage, issue) for issue in error.issues)
            else:
                preflight_issues.append((stage, error))

        formalization: dict[str, Any] | None = None
        provenance: dict[str, Any] | None = None
        try:
            formalization = submission_contract.load_formalization_metadata(metadata_path)
            provenance = submission_contract.normalized_provenance(formalization)
            substantive = provenance.get("substantive_formalization")
            if isinstance(substantive, dict):
                validate_preservable_remote_source(
                    work,
                    substantive,
                    "substantive formalization source",
                )
        except Exception as error:  # independent preflight group
            if isinstance(error, FormalizationValidationError) and error.repair_draft is not None:
                report["formalization_repair_draft"] = error.repair_draft
            add_issue("formalization", error)

        license_record: dict[str, Any] | None = None
        try:
            license_path = repository_license_file(source)
            detected_license = detect_spdx_identifier(
                license_path, Path(args.licensee).resolve()
            )
            declared_license = (
                formalization["project"]["license"].strip()
                if formalization is not None
                else None
            )
            license_record = {
                "path": license_path.name,
                "sha256": sha256(license_path),
                "declared_identifier": declared_license,
                "detected_identifier": detected_license,
            }
            if declared_license is not None and declared_license != detected_license:
                raise LicenseValidationError(
                    "formalization.yaml field project.license "
                    f"declares {declared_license!r}, but {license_path.name} matches "
                    f"{detected_license!r}"
                )
        except Exception as error:  # independent preflight group
            add_issue("license", error)

        toolchain_path: Path | None = None
        toolchain: str | None = None
        export_commit: str | None = None
        try:
            project_toolchain = project / "lean-toolchain"
            root_toolchain = source / "lean-toolchain"
            toolchain_path = project_toolchain if project_toolchain.exists() else root_toolchain
            if toolchain_path.is_symlink() or not toolchain_path.is_file():
                raise VerificationError(
                    "lean-toolchain must be a regular file in the project or repository root",
                    code="toolchain.missing",
                    path="lean-toolchain",
                    next_action=(
                        "Add a regular lean-toolchain file to the project or repository root, "
                        "commit it, and make a new submission."
                    ),
                )
            toolchain = toolchain_path.read_text(encoding="utf-8").strip()
            # Verification deliberately requires lean4export's exact release
            # tag. Only the post-acceptance Verso renderer has a stable-patch
            # fallback policy.
            export_commit = resolve_release_commit(
                "leanprover/lean4export", supported_toolchain(toolchain)
            )
        except Exception as error:  # independent preflight group
            add_issue("toolchain", error)

        config_relative: Path | None = None
        config_path: Path | None = None
        config: dict[str, Any] | None = None
        manifest_path = project / "lake-manifest.json"
        try:
            raw_config_path = values.get("comparator_config_path", "").strip()
            if not raw_config_path:
                raise VerificationError(
                    "Comparator configuration path must be supplied explicitly for this submission",
                    code="comparator.path_missing",
                    next_action=(
                        "Enter the repository-relative comparator.json path and submit again."
                    ),
                )
            config_relative = normalized_repository_path(
                raw_config_path, "Comparator configuration path"
            )
            if config_relative.suffix.lower() != ".json":
                raise VerificationError(
                    "Comparator configuration path must name a .json file",
                    code="comparator.path_invalid",
                )
            config_path = resolve_repository_path(
                source, config_relative, "Comparator configuration path", kind="file"
            )
            try:
                config_path.relative_to(project)
            except ValueError as error:
                raise VerificationError(
                    "Comparator configuration path must be inside the selected project",
                    code="comparator.path_outside_project",
                ) from error
            config = load_comparator_config(config_path)
            if manifest_path.is_symlink():
                raise VerificationError("project lake-manifest.json must not be a symlink")
            if lakefile.name == "lakefile.lean" and not manifest_path.is_file():
                raise VerificationError(
                    "lakefile.lean projects require a committed lake-manifest.json"
                )
        except Exception as error:  # independent preflight group
            add_issue("comparator-config", error)

        if preflight_issues:
            report["stage"] = "preflight"
            for issue_stage, issue in preflight_issues:
                report["errors"].append(str(issue))
                report_diagnostic(report, issue, stage=issue_stage)
            owners = {
                item.get("owner")
                for item in report.get("diagnostics", [])
                if isinstance(item, dict)
            }
            report["status"] = "error" if owners - {"submitter"} else "fail"
            if license_record is not None:
                report["license"] = license_record
            write_json(output, report)
            workflow_output(ready="false", lean4export_commit="", lean_toolchain="")
            return 0

        assert formalization is not None and provenance is not None
        assert license_record is not None
        assert toolchain_path is not None and toolchain is not None and export_commit is not None
        assert config_relative is not None and config_path is not None and config is not None
        report["license"] = license_record
        lakefile_relative = repository_relative_path(source, lakefile)
        toolchain_relative = repository_relative_path(source, toolchain_path)
        report.update(
            {
                "lean_toolchain": toolchain,
                "lean_toolchain_path": toolchain_relative,
                "lean4export_commit": export_commit,
                "challenge": {
                    "module": config["challenge_module"],
                },
                "solution": {
                    "module": config["solution_module"],
                },
                "formalization": {
                    "path": metadata_relative.as_posix(),
                    "sha256": sha256(metadata_path),
                    "project_name": formalization["project"]["name"].strip(),
                    "source_count": len(provenance["mathematical_sources"]),
                    "automation_method_count": len(formalization["automation"]["methods"]),
                    "review_status": formalization["review"]["status"].strip(),
                },
                "classification": {
                    "arxiv": [
                        {
                            "code": code,
                            "name": submission_contract.ARXIV_CATEGORY_NAMES[code],
                        }
                        for code in formalization["classification"]["arxiv"]
                    ],
                    "msc2020": [
                        {
                            "code": code,
                            "name": submission_contract.MSC2020_NAMES[code],
                        }
                        for code in formalization["classification"].get("msc2020", [])
                    ],
                },
                "provenance": provenance,
                "lakefile": {
                    "path": lakefile_relative,
                    "sha256": sha256(lakefile),
                    "format": "lean" if lakefile.name == "lakefile.lean" else "toml",
                },
                "comparator": {
                    "path": config_relative.as_posix(),
                    "sha256": sha256(config_path),
                    "challenge_module": config["challenge_module"],
                    "solution_module": config["solution_module"],
                    "theorem_names": config["theorem_names"],
                    "definition_names": config.get("definition_names", []),
                    "permitted_axioms": config["permitted_axioms"],
                },
            }
        )
        if manifest_path.is_file():
            report["lake_manifest"] = {
                "path": repository_relative_path(source, manifest_path),
                "sha256": sha256(manifest_path),
            }
        write_json(work / "metadata.json", report)
        report["status"] = "pending"
        report["stage"] = "prepared"
        write_json(output, report)
        workflow_output(ready="true", lean4export_commit=export_commit, lean_toolchain=toolchain)
    except LicenseValidationError as error:
        report["status"] = "fail"
        report["stage"] = "license"
        report["errors"].append(str(error))
        report_diagnostic(report, error, stage="license")
        write_json(output, report)
        workflow_output(ready="false", lean4export_commit="", lean_toolchain="")
    except FormalizationValidationError as error:
        report["status"] = "fail"
        report["stage"] = "formalization"
        report["errors"].extend(str(issue) for issue in error.issues)
        report_diagnostic(report, error, stage="formalization")
        write_json(output, report)
        workflow_output(ready="false", lean4export_commit="", lean_toolchain="")
    except Exception as error:  # noqa: BLE001 -- all intake failures become a bounded report
        report["errors"].append(str(error))
        report["status"] = "fail" if isinstance(error, VerificationError) else "error"
        report_diagnostic(report, error)
        write_json(output, report)
        workflow_output(ready="false", lean4export_commit="", lean_toolchain="")
    finally:
        _EXECUTION_DEADLINE = previous_deadline
    return 0


def github_repository(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.hostname and parsed.hostname.lower() != "github.com":
        return None
    if parsed.scheme in {"http", "https", "git"}:
        path = parsed.path
    elif url.startswith("git@github.com:"):
        path = url.split(":", 1)[1]
    else:
        return None
    path = path.strip("/")
    path = path.removesuffix(".git")
    parts = path.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return f"{parts[0]}/{parts[1]}"


def manifest_packages(source: Path) -> list[dict[str, str]]:
    path = source / "lake-manifest.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    packages = []
    for package in data.get("packages", []):
        package_type = package.get("type")
        url = package.get("url")
        if package_type == "git":
            repository = github_repository(url) if isinstance(url, str) else None
            if repository is None:
                raise VerificationError(
                    f"Git package {str(package.get('name') or '')!r} must be hosted on GitHub"
                )
            revision = str(package.get("rev") or package.get("inputRev") or "")
            if not submission_contract.SHA_RE.fullmatch(revision):
                raise VerificationError(
                    f"Git package {str(package.get('name') or '')!r} is not pinned to a full commit"
                )
        elif package_type == "path":
            directory = str(package.get("dir") or "")
            repository = f"path:{directory}"
            url = repository
            revision = "source-tree"
        else:
            repository = f"{package_type or 'unknown'}:{package.get('name') or ''}"
            url = repository
            revision = str(package.get("rev") or package.get("inputRev") or "unknown")
        packages.append(
            {
                "name": str(package.get("name") or ""),
                "repository": repository,
                "url": url,
                "revision": revision,
            }
        )
    return packages


def contained_path_dependency(
    project: Path, raw_value: str, checkout: Path, name: str
) -> Path:
    """Resolve a Lake path dependency without following submitted symlinks."""
    relative = pathlib.PurePosixPath(raw_value)
    if (
        not raw_value
        or raw_value != relative.as_posix()
        or relative.is_absolute()
        or "\\" in raw_value
        or any(ord(character) < 32 or ord(character) == 127 for character in raw_value)
    ):
        raise VerificationError(f"path package {name!r} has an unsafe directory")
    boundary = checkout.resolve()
    candidate = project.resolve()
    for segment in relative.parts:
        candidate /= segment
        if candidate.is_symlink():
            raise VerificationError(f"path package {name!r} contains a symlinked component")
        try:
            candidate.resolve().relative_to(boundary)
        except ValueError as error:
            raise VerificationError(
                f"path package {name!r} escapes the repository checkout"
            ) from error
    resolved = candidate.resolve()
    if any(part == ".lake" for part in resolved.relative_to(boundary).parts):
        raise VerificationError(f"path package {name!r} may not live under .lake")
    if resolved == project.resolve() or not resolved.is_dir():
        raise VerificationError(f"path package {name!r} is not a distinct regular directory")
    return resolved


def ensure_lake_manifest(project: Path, checkout: Path) -> bool:
    """Create a trusted manifest for a simple TOML path-dependency workspace.

    Nested Comparator workspaces often depend on the repository's main project
    by path but omit their own generated manifest.  Reuse only already-pinned,
    committed manifests from contained path targets; never run submitted Lake
    configuration or resolve a moving Git reference to manufacture the lock.
    Returns true when a manifest was generated.
    """
    manifest = project / "lake-manifest.json"
    if manifest.is_symlink():
        raise VerificationError("project lake-manifest.json must not be a symlink")
    if manifest.is_file():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("packages", []), list):
            raise VerificationError("lake-manifest.json must contain one manifest object")
        return False
    if manifest.exists():
        raise VerificationError("project lake-manifest.json must be a regular file")

    # Only a declarative lakefile.toml can be read without running it, so it is
    # the only shape whose dependencies can be established without a manifest.
    lakefile = project / "lakefile.toml"
    if lakefile.is_symlink() or not lakefile.is_file():
        raise VerificationError(
            "a project with no committed lake-manifest.json must configure Lake with "
            "a regular lakefile.toml"
        )
    try:
        configuration = tomllib.loads(lakefile.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise VerificationError(f"invalid lakefile.toml: {error}") from error
    requirements = configuration.get("require", [])
    if not isinstance(requirements, list):
        raise VerificationError("lakefile.toml require entries must be an array")

    boundary = checkout.resolve()
    packages: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    packages_directories: set[Path] = set()

    def add(package: dict[str, Any], *, inherited: bool) -> None:
        name = package.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise VerificationError(f"invalid Lake package name: {name!r}")
        if name in seen_names:
            raise VerificationError(f"duplicate Lake package name: {name!r}")
        seen_names.add(name)
        candidate = dict(package)
        candidate["inherited"] = inherited
        packages.append(candidate)

    for requirement in requirements:
        if not isinstance(requirement, dict):
            raise VerificationError("lakefile.toml require entries must be objects")
        name = requirement.get("name")
        raw_path = requirement.get("path")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise VerificationError(f"invalid direct Lake package name: {name!r}")
        if not isinstance(raw_path, str):
            raise VerificationError(
                "a TOML project without lake-manifest.json may use only contained path "
                "dependencies whose targets have committed manifests"
            )
        target = contained_path_dependency(project, raw_path, boundary, name)
        target_lakefiles = [
            target / filename
            for filename in ("lakefile.toml", "lakefile.lean")
            if (target / filename).exists() or (target / filename).is_symlink()
        ]
        if len(target_lakefiles) != 1 or target_lakefiles[0].is_symlink():
            raise VerificationError(f"path package {name!r} has no unique regular Lakefile")
        target_manifest = target / "lake-manifest.json"
        if target_manifest.is_symlink() or not target_manifest.is_file():
            raise VerificationError(
                f"path package {name!r} must have a committed lake-manifest.json"
            )
        try:
            target_data = json.loads(target_manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise VerificationError(f"path package {name!r} has an invalid manifest: {error}") from error
        target_packages = target_data.get("packages") if isinstance(target_data, dict) else None
        if not isinstance(target_packages, list):
            raise VerificationError(f"path package {name!r} has an invalid manifest")
        target_packages_dir = manifest_packages_directory(target, checkout=boundary)
        packages_directories.add(target_packages_dir)
        add(
            {
                "type": "path",
                "scope": "",
                "name": name,
                "manifestFile": "lake-manifest.json",
                "dir": raw_path,
                "configFile": target_lakefiles[0].name,
            },
            inherited=False,
        )
        for package in target_packages:
            if not isinstance(package, dict) or package.get("type") == "path":
                raise VerificationError(
                    f"path package {name!r} manifest has an unsupported nested path dependency"
                )
            add(package, inherited=True)

    if len(packages_directories) > 1:
        raise VerificationError(
            "path dependencies use different packages directories; commit this project's manifest"
        )
    packages_dir = next(iter(packages_directories), project / ".lake" / "packages")
    packages_dir_value = Path(os.path.relpath(packages_dir, project)).as_posix()
    generated = {
        "version": "1.2.0",
        "packagesDir": packages_dir_value,
        "packages": packages,
        "name": str(configuration.get("name") or project.name),
        "lakeDir": ".lake",
        "fixedToolchain": False,
    }
    write_json(manifest, generated)
    return True


def manifest_packages_directory(source: Path, *, checkout: Path) -> Path:
    """Resolve Lake's packages directory within the verifier-owned checkout."""
    manifest = source / "lake-manifest.json"
    directory = ".lake/packages"
    if manifest.is_file() and not manifest.is_symlink():
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise VerificationError("lake-manifest.json must contain one JSON object")
        directory = str(data.get("packagesDir") or directory)
    raw = Path(directory)
    if raw.is_absolute() or "\\" in directory or any(
        ord(character) < 32 or ord(character) == 127 for character in directory
    ):
        raise VerificationError("Lake manifest packagesDir must be a safe relative path")
    resolved = (source / raw).resolve()
    boundary = checkout.resolve()
    try:
        resolved.relative_to(boundary)
    except ValueError as error:
        raise VerificationError("Lake manifest packagesDir escapes the repository checkout") from error
    if resolved.name != "packages" or resolved.parent.name != ".lake":
        raise VerificationError("Lake manifest packagesDir must name a contained .lake/packages directory")
    return resolved


def recorded_project_dependencies(
    source: Path, checkout: Path, packages: list[dict[str, str]]
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for package in packages:
        if package["url"].startswith("path:"):
            target = package_checkout(source, package, checkout=checkout)
            relative = target.relative_to(checkout.resolve()).as_posix()
            records.append({"name": package["name"], "path": relative or "."})
        else:
            records.append(
                {
                    "name": package["name"],
                    "repository": package["repository"],
                    "url": package["url"],
                    "revision": package["revision"],
                }
            )
    return records



def allowed_roots() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Load canonical allowlisted roots and a case-insensitive alias index."""
    config = json.loads((ROOT / "allowed-challenge-repositories.json").read_text(encoding="utf-8"))
    roots = config.get("roots")
    if not isinstance(roots, list):
        raise VerificationError("allowlisted roots configuration is malformed")
    aliases: dict[str, dict[str, Any]] = {}
    for root in roots:
        if not isinstance(root, dict):
            raise VerificationError("allowlisted root entry is malformed")
        repository = root.get("repository")
        official_ref = root.get("official_ref")
        accepted_revisions = root.get("accepted_revisions", [])
        trust_level = root.get("trust_level")
        repository_aliases = root.get("repository_aliases", [])
        if (
            not isinstance(repository, str)
            or github_repository(f"https://github.com/{repository}") != repository
            or not isinstance(official_ref, str)
            or not OFFICIAL_REF_RE.fullmatch(official_ref)
            or not isinstance(accepted_revisions, list)
            or not all(
                isinstance(revision, str)
                and submission_contract.SHA_RE.fullmatch(revision)
                for revision in accepted_revisions
            )
            or len(set(accepted_revisions)) != len(accepted_revisions)
            or trust_level not in {"high", "qualified"}
            or root.get("include_pinned_manifest_closure") is not True
            or not isinstance(repository_aliases, list)
            or not all(isinstance(alias, str) for alias in repository_aliases)
        ):
            raise VerificationError(f"invalid allowlisted root configuration: {repository!r}")
        for alias in [repository, *repository_aliases]:
            if github_repository(f"https://github.com/{alias}") != alias:
                raise VerificationError(f"invalid repository alias in allowlist: {alias!r}")
            normalized = alias.lower()
            if normalized in aliases:
                raise VerificationError(f"duplicate repository alias in allowlist: {alias}")
            aliases[normalized] = root
    return roots, aliases


def canonical_repository(repository: str, aliases: dict[str, dict[str, Any]]) -> str:
    root = aliases.get(repository.lower())
    return str(root["repository"]) if root else repository


def git_environment(source: Path, base_env: dict[str, str]) -> dict[str, str]:
    return {
        "HOME": str(source / ".lake" / "config" / "git-home"),
        "PATH": base_env["PATH"],
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LFS_SKIP_SMUDGE": "1",
    }


def verify_official_revision(
    package_dir: Path,
    *,
    repository: str,
    revision: str,
    official_ref: str,
    accepted_revisions: list[str] | None = None,
    git_env: dict[str, str],
) -> None:
    """Require a package commit to occur in the canonical repository's official history."""
    if revision in (accepted_revisions or []):
        return
    fetched_ref = "refs/remotes/palomar-official/head"
    git = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "protocol.file.allow=never",
        "-C",
        str(package_dir),
    ]
    run([*git, "remote", "add", "palomar-official", f"https://github.com/{repository}"], env=git_env)
    run(
        [
            *git,
            "fetch",
            "--quiet",
            "--filter=tree:0",
            "--no-tags",
            "palomar-official",
            f"+{official_ref}:{fetched_ref}",
        ],
        env=git_env,
        timeout=EXECUTION_BUDGET_SECONDS,
    )
    ancestry = run(
        [*git, "merge-base", "--is-ancestor", revision, fetched_ref],
        env=git_env,
        check=False,
    )
    if ancestry.returncode == 1:
        raise VerificationError(
            f"{repository} revision {revision} is not an ancestor of canonical {official_ref}"
        )
    if ancestry.returncode:
        raise VerificationError(f"could not establish official ancestry for {repository}")


def package_allowlist(
    source: Path,
    packages: list[dict[str, str]],
    *,
    checkout: Path,
    base_env: dict[str, str],
) -> dict[str, tuple[str, str]]:
    """Verify roots and their exact official manifest closures, then map trusted package names."""
    roots, aliases = allowed_roots()
    by_name: dict[str, dict[str, str]] = {}
    for package in packages:
        name = package["name"]
        if not name or name in by_name:
            raise VerificationError(f"duplicate or empty package name in Lake manifest: {name!r}")
        by_name[name] = package

    allowed: dict[str, tuple[str, str]] = {}
    # Mathlib wins overlap so its infrastructure remains a high-trust surface.
    roots = sorted(roots, key=lambda root: root["trust_level"] != "high")
    git_env = git_environment(source, base_env)
    for root in roots:
        display = str(root["repository"])
        level = str(root["trust_level"])
        matching = [
            package
            for package in packages
            if canonical_repository(str(package["repository"]), aliases).lower() == display.lower()
        ]
        if len(matching) > 1:
            names = ", ".join(repr(package["name"]) for package in matching)
            raise VerificationError(
                f"Lake manifest assigns multiple package names to allowlisted "
                f"repository {display}: {names}"
            )
        for root_package in matching:
            package_dir = package_checkout(source, root_package, checkout=checkout)
            verify_official_revision(
                package_dir,
                repository=display,
                revision=root_package["revision"],
                official_ref=str(root["official_ref"]),
                accepted_revisions=list(root.get("accepted_revisions", [])),
                git_env=git_env,
            )
            nested = manifest_packages(package_dir)
            closure = {root_package["name"]}
            for expected in nested:
                name = expected["name"]
                actual = by_name.get(name)
                if actual is None:
                    raise VerificationError(
                        f"submitted manifest omits {display}'s pinned dependency {name!r}"
                    )
                expected_repository = canonical_repository(str(expected["repository"]), aliases)
                actual_repository = canonical_repository(str(actual["repository"]), aliases)
                if (
                    actual_repository.lower() != expected_repository.lower()
                    or actual["revision"] != expected["revision"]
                ):
                    raise VerificationError(
                        f"submitted manifest substitutes {display}'s pinned dependency {name!r}"
                    )
                closure.add(name)
            for name in closure:
                allowed.setdefault(name, (display, level))
    return allowed


def package_checkout(source: Path, package: dict[str, str], *, checkout: Path) -> Path:
    """Return the already-materialized checkout for one manifest package."""
    if package["url"].startswith("path:"):
        return contained_path_dependency(
            source,
            package["url"].removeprefix("path:"),
            checkout,
            package["name"],
        )
    return (
        manifest_packages_directory(source, checkout=checkout) / package["name"]
    ).resolve()


def trusted_package_url_map(
    packages: list[dict[str, str]], authoritative_packages: list[dict[str, str]]
) -> str:
    """Pin trusted Lake dependency remotes to the authenticated root manifest."""
    _roots, aliases = allowed_roots()
    by_name = {package["name"]: package for package in packages}
    urls: dict[str, str] = {}
    for expected in sorted(authoritative_packages, key=lambda package: package["name"]):
        name = expected["name"]
        actual = by_name.get(name)
        if actual is None:
            raise VerificationError(f"trusted package {name!r} is absent from the manifest")
        expected_url = expected["url"]
        if actual["url"].startswith("path:") or expected_url.startswith("path:"):
            raise VerificationError(f"trusted package {name!r} may not use a path dependency")
        parsed_expected = urlparse(expected_url)
        if (
            parsed_expected.scheme != "https"
            or parsed_expected.netloc.lower() != "github.com"
            or parsed_expected.username
            or parsed_expected.password
            or parsed_expected.query
            or parsed_expected.fragment
        ):
            raise VerificationError(
                f"trusted package {name!r} authenticated URL is not a credential-free "
                "HTTPS GitHub remote"
            )
        actual_repository = github_repository(actual["url"])
        expected_repository = github_repository(expected_url)
        if (
            actual_repository is None
            or expected_repository is None
            or canonical_repository(actual_repository, aliases).lower()
            != canonical_repository(expected_repository, aliases).lower()
        ):
            raise VerificationError(
                f"trusted package {name!r} URL does not match its authenticated repository"
            )
        if actual.get("revision") != expected.get("revision"):
            raise VerificationError(
                f"trusted package {name!r} revision does not match its verified manifest"
            )
        urls[name] = expected_url
    return json.dumps(urls, sort_keys=True, separators=(",", ":"))


def package_lake_directories(
    source: Path, name: str, *, checkout: Path
) -> tuple[Path, Path]:
    package = next((item for item in manifest_packages(source) if item["name"] == name), None)
    if package is None:
        raise VerificationError(f"trusted package {name!r} is absent from the manifest")
    boundary = checkout.resolve()
    package_dir = package_checkout(source, package, checkout=boundary)
    return _validated_package_lake_directories(package_dir, name, boundary)


def _validated_package_lake_directories(
    package_dir: Path, name: str, boundary: Path
) -> tuple[Path, Path]:
    """Resolve one package's writable Lake leaves inside an explicit boundary."""
    result: list[Path] = []
    for leaf in ("build", "config"):
        candidate = package_dir / ".lake" / leaf
        try:
            relative = pathlib.PurePosixPath(candidate.relative_to(boundary).as_posix())
        except ValueError as error:
            raise VerificationError(
                f"trusted package {name!r} Lake directory escapes the repository checkout"
            ) from error
        result.append(
            resolve_repository_path(
                boundary,
                relative,
                f"trusted package {name!r} Lake {leaf} directory",
                kind="directory",
            )
        )
    return result[0], result[1]


def trusted_lake_directories(
    source: Path, names: Any, *, checkout: Path
) -> list[Path]:
    result: list[Path] = []
    for name in names:
        for directory in package_lake_directories(source, name, checkout=checkout):
            if not directory.is_dir():
                raise VerificationError(f"trusted package Lake directory is missing: {directory}")
            result.append(directory)
    return sorted(set(result))


def reset_trusted_lake_state(
    source: Path,
    names: Any,
    *,
    packages: list[dict[str, str]],
    checkout: Path,
) -> list[Path]:
    """Validate, then recreate each trusted package's writable Lake state."""
    requested = set(names)
    boundary = checkout.resolve()
    packages_directory = manifest_packages_directory(source, checkout=boundary)
    package_directories: list[Path] = []
    selected: set[str] = set()
    for package in packages:
        name = package["name"]
        if name not in requested:
            continue
        if name in selected:
            raise VerificationError(f"duplicate trusted package name: {name!r}")
        selected.add(name)
        if package["url"].startswith("path:"):
            raise VerificationError(f"trusted package {name!r} may not use a path dependency")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name in {".", ".."}:
            raise VerificationError(f"unsafe trusted package name: {name!r}")
        package_dir = (packages_directory / name).resolve()
        # Validate every target before deleting any state. A redirected or
        # malformed package therefore fails without touching another package.
        _validated_package_lake_directories(package_dir, name, boundary)
        package_directories.append(package_dir)
    missing = requested - selected
    if missing:
        name = min(missing)
        raise VerificationError(f"trusted package {name!r} is absent from the manifest")

    writable: list[Path] = []
    for package_dir in package_directories:
        writable.extend(remove_untrusted_lake_state(package_dir))
    return writable


def nested_package_links(
    source: Path,
    root_package: Path,
    *,
    checkout: Path,
    allowed_names: set[str],
) -> Path:
    """Give a trusted root its exact flattened closure without running Lake update."""
    nested = root_package / ".lake" / "packages"
    if nested.exists() or nested.is_symlink():
        raise VerificationError(f"trusted package directory was not freshly prepared: {nested}")
    nested.mkdir()
    by_name = {package["name"]: package for package in manifest_packages(source)}
    for expected in manifest_packages(root_package):
        name = expected["name"]
        if name not in allowed_names:
            raise VerificationError(
                f"trusted root dependency {name!r} is outside its verified manifest closure"
            )
        actual = by_name.get(name)
        if actual is None:
            raise VerificationError(f"trusted root dependency {name!r} is not materialized")
        if (
            actual["repository"].lower() != expected["repository"].lower()
            or actual["revision"] != expected["revision"]
        ):
            raise VerificationError(
                f"trusted root dependency {name!r} does not match its pinned manifest"
            )
        target = package_checkout(source, actual, checkout=checkout)
        if not target.is_dir() or target.is_symlink():
            raise VerificationError(f"trusted root dependency is not a real checkout: {target}")
        (nested / name).symlink_to(target, target_is_directory=True)
    return nested.resolve()


def build_allowlisted_roots(
    source: Path,
    *,
    checkout: Path,
    packages: list[dict[str, str]],
    allowlist: dict[str, tuple[str, str]],
    base_env: dict[str, str],
    lake: Path,
    landrun: Path,
    readable_paths: list[Path],
    executable_paths: list[Path],
    tools: dict[Path, str],
) -> None:
    """Build non-Mathlib trusted roots from freshly reset Lake state."""
    roots, aliases = allowed_roots()
    by_name = {package["name"]: package for package in packages}
    for root in sorted(roots, key=lambda item: item["trust_level"] != "high"):
        repository = str(root["repository"])
        if repository.lower() == "leanprover-community/mathlib4":
            # Mathlib and its official closure come from its explicitly trusted cache.
            continue
        root_package = next(
            (
                package
                for package in packages
                if canonical_repository(package["repository"], aliases).lower() == repository.lower()
            ),
            None,
        )
        if root_package is None:
            continue
        root_dir = package_checkout(source, root_package, checkout=checkout)
        closure = {
            root_package["name"],
            *(dependency["name"] for dependency in manifest_packages(root_dir)),
        }
        if not closure <= allowlist.keys() or not closure <= by_name.keys():
            raise VerificationError(f"trusted root {repository} has an incomplete closure")
        owned_names = {
            name for name, (owner, _level) in allowlist.items() if owner == repository
        }
        if root_package["name"] not in owned_names:
            raise VerificationError(f"trusted root {repository} has no unique package role")
        trusted_directories = reset_trusted_lake_state(
            source,
            owned_names,
            packages=packages,
            checkout=checkout,
        )
        nested = nested_package_links(
            source, root_dir, checkout=checkout, allowed_names=closure
        )
        build_env = base_env.copy()
        build_env["LAKE_PKG_URL_MAP"] = trusted_package_url_map(
            packages, manifest_packages(root_dir)
        )
        home = root_dir / ".lake" / "config" / "home"
        temporary = root_dir / ".lake" / "config" / "tmp"
        home.mkdir(exist_ok=True)
        temporary.mkdir(exist_ok=True)
        build_env.update(
            {"HOME": str(home.resolve()), "TMPDIR": str(temporary.resolve()), "LEAN_ABORT_ON_PANIC": "1"}
        )
        writable = validate_writable_directories(checkout, trusted_directories)
        try:
            sandboxed_run(
                [str(lake), "build"],
                cwd=root_dir,
                environment=build_env,
                landrun=landrun,
                writable_directories=writable,
                readable_paths=readable_paths,
                executable_paths=executable_paths,
                tools=tools,
                timeout=EXECUTION_BUDGET_SECONDS,
            )
        finally:
            if nested.is_dir():
                shutil.rmtree(nested)



def source_package(path: Path, source: Path, *, checkout: Path) -> str | None:
    packages = manifest_packages_directory(source, checkout=checkout)
    try:
        relative = path.resolve().relative_to(packages)
    except ValueError:
        return None
    return relative.parts[0] if relative.parts else None


def remove_untrusted_lake_state(package_dir: Path) -> tuple[Path, Path]:
    """Discard submitted Lake state and create the only two writable subdirectories."""
    dot_lake = package_dir / ".lake"
    if dot_lake.is_symlink() or (dot_lake.exists() and not dot_lake.is_dir()):
        dot_lake.unlink()
    elif dot_lake.is_dir():
        shutil.rmtree(dot_lake)
    build = dot_lake / "build"
    config = dot_lake / "config"
    build.mkdir(parents=True)
    config.mkdir()
    return build.resolve(), config.resolve()


def reject_committed_build_artifacts(package_dir: Path) -> None:
    """Reject prebuilt Lean/native output outside the fresh ``.lake`` tree."""
    root = package_dir.resolve()
    for current, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if name not in {".git", ".lake"}]
        for filename in filenames:
            path = Path(current) / filename
            lowered = path.name.lower()
            if path.suffix.lower() in COMPILED_ARTIFACT_SUFFIXES or lowered.endswith(
                COMPILED_ARTIFACT_NAME_SUFFIXES
            ):
                raise VerificationError(
                    "committed build artifact is not permitted outside fresh .lake state: "
                    f"{path}"
                )


def purge_untrusted_lake_state(checkout: Path) -> None:
    """Remove every submitted ``.lake`` tree before the checkout becomes readable."""
    root = checkout.resolve()
    candidates = sorted(
        (path for path in root.rglob(".lake") if ".git" not in path.relative_to(root).parts),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in candidates:
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)


def validate_writable_directories(
    checkout: Path, directories: list[Path]
) -> list[Path]:
    """Validate sandbox writes against the verifier-owned checkout boundary."""
    root = checkout.resolve()
    result: list[Path] = []
    for directory in directories:
        if directory.is_symlink() or not directory.is_dir():
            raise VerificationError(f"sandbox writable path is not a real directory: {directory}")
        resolved = directory.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise VerificationError(f"sandbox writable path escapes the source tree: {directory}") from error
        if resolved not in result:
            result.append(resolved)
    return result


def require_protected_paths(protected_paths: list[Path], writable_directories: list[Path]) -> None:
    for protected in protected_paths:
        resolved = protected.resolve()
        for writable in writable_directories:
            try:
                resolved.relative_to(writable)
            except ValueError:
                continue
            raise VerificationError(f"protected verifier path is sandbox-writable: {protected}")


def path_is_within(path: Path, directories: list[Path]) -> bool:
    for directory in directories:
        try:
            path.resolve().relative_to(directory.resolve())
        except ValueError:
            continue
        return True
    return False


def system_readable_paths() -> list[Path]:
    """Return the narrow host configuration needed by ordinary HTTPS clients."""
    candidates = (
        Path("/etc/ssl/certs"),
        Path("/etc/pki"),
        Path("/etc/resolv.conf"),
        Path("/etc/hosts"),
        Path("/etc/nsswitch.conf"),
        Path("/etc/gai.conf"),
        Path("/etc/host.conf"),
        Path("/etc/ld.so.cache"),
    )
    result: set[Path] = set()
    for path in candidates:
        if path.exists():
            result.add(path.absolute())
            result.add(path.resolve())
    return sorted(result)


def landrun_command(
    command: list[str],
    *,
    landrun: Path,
    writable_directories: list[Path],
    writable_files: tuple[Path, ...] | list[Path] = (),
    readable_paths: list[Path] | None = None,
    executable_paths: list[Path],
    environment: dict[str, str],
    readable_directories: tuple[Path, ...] | list[Path] = (),
    unrestricted_network: bool = False,
) -> list[str]:
    """Build the outer, submission-wide Landrun policy."""
    result = [
        str(landrun),
        "--best-effort",
        "--ldd",
        "--add-exec",
    ]
    for device in ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom"):
        if Path(device).exists():
            result.extend(["--rw", device])
    for name in SANDBOX_ENVIRONMENT:
        value = environment.get(name)
        if value is not None:
            if any(character in value for character in ("\0", "\n", "\r")):
                raise VerificationError(f"invalid control character in sandbox environment {name}")
            # Landrun looks the value up in the environment installed on the
            # transient service. Passing JSON values inline would let its
            # string-slice parser split them at commas.
            result.extend(["--env", name])
    for path in sorted({*(readable_paths or []), *readable_directories}):
        result.extend(["--ro", str(path)])
    for directory in sorted(set(writable_directories)):
        result.extend(["--rwx", str(directory)])
    for path in sorted(set(writable_files)):
        result.extend(["--rw", str(path)])
    for path in sorted(set(executable_paths)):
        result.extend(["--rox", str(path)])
    if unrestricted_network:
        result.append("--unrestricted-network")
    return [*result, "--", *command]


def tool_snapshot(paths: list[Path]) -> dict[Path, str]:
    snapshot: dict[Path, str] = {}
    for path in paths:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise VerificationError(f"missing verifier tool: {resolved}")
        snapshot[resolved] = sha256(resolved)
    return snapshot


def verify_tool_snapshot(snapshot: dict[Path, str]) -> None:
    for path, expected in snapshot.items():
        try:
            actual = sha256(path)
        except OSError as error:
            raise VerificationError(f"verifier tool disappeared during execution: {path}") from error
        if actual != expected:
            raise VerificationError(f"verifier tool changed during execution: {path}")


def systemd_command(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: int = 600,
    unrestricted_network: bool = False,
    resource_properties: tuple[str, ...] = (),
) -> list[str]:
    global _SYSTEMD_MANAGER

    runner = shutil.which("systemd-run")
    if not runner:
        raise VerificationError("systemd-run is required for fail-closed confinement")
    common = [
        "--quiet",
        "--collect",
        "--pipe",
        "--wait",
        "--property=RestrictAddressFamilies=~AF_UNIX",
        "--property=LimitNOFILE=524288",
        "--property=NoNewPrivileges=yes",
        "--property=RestrictSUIDSGID=yes",
        "--property=LockPersonality=yes",
        "--property=PrivateDevices=yes",
        "--property=PrivateTmp=yes",
        "--property=ProtectProc=invisible",
        "--property=ProcSubset=pid",
        f"--property=RuntimeMaxSec={max(1, timeout)}s",
        f"--working-directory={cwd}",
    ]
    if not unrestricted_network:
        common.append("--property=PrivateNetwork=yes")
    for property_value in resource_properties:
        if not property_value or "\n" in property_value or "\r" in property_value:
            raise VerificationError("invalid systemd resource property")
        common.append(f"--property={property_value}")
    sudo = shutil.which("sudo")
    true = shutil.which("true")
    if not true:
        raise VerificationError("true is required to probe the systemd confinement manager")

    def manager_command(manager: str, properties: list[str]) -> list[str]:
        if manager == "system":
            if sudo is None:
                raise VerificationError("passwordless sudo disappeared during verification")
            return [
                sudo,
                "-n",
                runner,
                *properties,
                f"--uid={os.getuid()}",
                f"--gid={os.getgid()}",
            ]
        return [runner, "--user", *properties]

    if _SYSTEMD_MANAGER is None:
        # Probe the properties that hosted user managers commonly reject, not
        # merely access to sudo or the bus. The successful choice is stable for
        # this verifier process and avoids repeating transient probe units.
        probe_common = list(common)
        if "--property=PrivateNetwork=yes" not in probe_common:
            probe_common.append("--property=PrivateNetwork=yes")
        candidates = ["system", "user"] if sudo is not None else ["user"]
        for candidate in candidates:
            probe = run(
                [*manager_command(candidate, probe_common), "--", true],
                cwd=cwd,
                env=environment,
                timeout=30,
                check=False,
            )
            if probe.returncode == 0:
                _SYSTEMD_MANAGER = candidate
                break
        if _SYSTEMD_MANAGER is None:
            raise VerificationError(
                "neither passwordless systemd-run nor a user systemd manager can apply "
                "the required confinement properties"
            )

    result = manager_command(_SYSTEMD_MANAGER, common)
    for name in SANDBOX_ENVIRONMENT:
        value = environment.get(name)
        if value is not None:
            if any(character in value for character in ("\0", "\n", "\r")):
                raise VerificationError(f"invalid control character in sandbox environment {name}")
            result.append(f"--setenv={name}={value}")
    return [*result, "--", *command]


def sandboxed_run(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    landrun: Path,
    writable_directories: list[Path],
    writable_files: tuple[Path, ...] | list[Path] = (),
    readable_paths: list[Path] | None = None,
    executable_paths: list[Path],
    tools: dict[Path, str],
    timeout: int = 600,
    check: bool = True,
    unrestricted_network: bool = False,
    resource_properties: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    timeout = _deadline_timeout(timeout, command)
    verify_tool_snapshot(tools)
    confined = landrun_command(
        command,
        landrun=landrun,
        writable_directories=writable_directories,
        writable_files=writable_files,
        readable_paths=readable_paths,
        executable_paths=executable_paths,
        environment=environment,
        readable_directories=[cwd],
        unrestricted_network=unrestricted_network,
    )
    systemd_payload = confined
    if _RESOURCE_METRICS_PATH is not None:
        metrics_wrapper = (ROOT / "scripts" / "measure_resources.py").resolve()
        python = Path(sys.executable).resolve()
        if not metrics_wrapper.is_file():
            raise VerificationError("trusted resource measurement wrapper is missing")
        phase = Path(command[0]).name[:80] or "phase"
        systemd_payload = [
            str(python),
            str(metrics_wrapper),
            "--output",
            str(_RESOURCE_METRICS_PATH),
            "--phase",
            phase,
            "--disk-path",
            str(_RESOURCE_DISK_PATH or cwd),
            "--",
            *confined,
        ]
    proc = run(
        systemd_command(
            systemd_payload,
            cwd=cwd,
            environment=environment,
            timeout=timeout,
            unrestricted_network=unrestricted_network,
            resource_properties=tuple(
                dict.fromkeys((*PERMISSIVE_RESOURCE_PROPERTIES, *resource_properties))
            ),
        ),
        cwd=cwd,
        env=environment,
        timeout=timeout,
        check=False,
    )
    verify_tool_snapshot(tools)
    # Payload output is attacker-controlled and must never manufacture an
    # infrastructure outcome merely by printing an OOM or timeout phrase.
    # Signal-style wrapper exits are the bounded evidence available here;
    # Python-enforced wall-clock expiry is reported by TimeoutExpired.
    resource_signals = {124, 137, 143, 152, 153}
    if proc.returncode in resource_signals:
        raise ResourceExhausted(
            f"worker resource ceiling reached while running {Path(command[0]).name} "
            f"(exit {proc.returncode})"
        )
    if check and proc.returncode:
        detail = (proc.stderr or proc.stdout).strip()[-8000:]
        raise VerificationError(
            f"{' '.join(command[:3])} failed ({proc.returncode}): {detail}"
        )
    return proc


@dataclass(frozen=True)
class _ConfinementProbeResult:
    """Observed positive and negative controls from one filesystem policy."""

    allowed: subprocess.CompletedProcess[str]
    allowed_created: bool
    denied: subprocess.CompletedProcess[str] | None
    denied_created: bool


def _run_filesystem_confinement_probe(
    denied_probe: Path,
    *,
    existing_probe_error: str,
    touch: Path,
    cwd: Path,
    environment: dict[str, str],
    landrun: Path,
    writable_directories: list[Path],
    readable_paths: list[Path],
    executable_paths: list[Path],
    tools: dict[Path, str],
    after_allowed: Callable[[], None] | None = None,
) -> _ConfinementProbeResult:
    """Run one writable positive control and one write-denial control.

    A failed positive control returns without attempting the negative control,
    preserving the security decision order. Every path this function proves
    absent before use is removed in ``finally``, including when the sandbox
    runner itself raises.
    """

    require_protected_paths([denied_probe], writable_directories)
    if denied_probe.exists():
        raise VerificationError(f"{existing_probe_error}: {denied_probe}")
    allowed_probe = writable_directories[0] / ".palomar-landrun-write-probe"
    if allowed_probe.exists():
        raise VerificationError(
            f"filesystem confinement probe path already exists: {allowed_probe}"
        )

    try:
        allowed = sandboxed_run(
            [str(touch), str(allowed_probe)],
            cwd=cwd,
            environment=environment,
            landrun=landrun,
            writable_directories=writable_directories,
            readable_paths=readable_paths,
            executable_paths=executable_paths,
            tools=tools,
            check=False,
        )
        allowed_created = allowed_probe.is_file()
        allowed_probe.unlink(missing_ok=True)
        if allowed.returncode or not allowed_created:
            return _ConfinementProbeResult(allowed, allowed_created, None, False)

        if after_allowed is not None:
            after_allowed()

        denied = sandboxed_run(
            [str(touch), str(denied_probe)],
            cwd=cwd,
            environment=environment,
            landrun=landrun,
            writable_directories=writable_directories,
            readable_paths=readable_paths,
            executable_paths=executable_paths,
            tools=tools,
            check=False,
        )
        return _ConfinementProbeResult(
            allowed,
            allowed_created,
            denied,
            denied_probe.exists(),
        )
    finally:
        allowed_probe.unlink(missing_ok=True)
        denied_probe.unlink(missing_ok=True)


def verify_sandbox_confinement(
    write_probe: Path,
    read_probe: Path,
    *,
    positive_read: Path,
    python: Path,
    touch: Path,
    cwd: Path,
    environment: dict[str, str],
    landrun: Path,
    writable_directories: list[Path],
    protected_write_directories: list[Path] | None = None,
    readable_paths: list[Path],
    executable_paths: list[Path],
    tools: dict[Path, str],
) -> None:
    """Prove the composed outer boundary before untrusted code runs.

    Positive controls: a write inside the first writable directory, a read of
    ``positive_read``, and a nested Landrun domain. Negative controls: a write
    outside the writable set, a write into each ``protected_write_directories``
    entry, a read outside the read allowlist, a read of a live sibling
    process's ``/proc`` environment, and an outbound TCP connection to a
    listener the trusted parent has just accepted on.

    The probes run under the same readable-path policy as the work they stand
    for, so that what they demonstrate is a property of the policy actually
    used. The filesystem assertions hold either way, since a readable path
    grants no right to create or write; matching the real policy makes the
    probes representative rather than merely sound. Every parameter is
    required rather than defaulted, so that a later caller cannot quietly
    probe a configuration nobody runs under.

    Landrun is invoked with ``--best-effort``, which silently drops access
    rights the running kernel's Landlock ABI does not support. These controls
    are what makes that safe: a boundary degraded to the point of permitting a
    denied read, write, or connection fails here, before any candidate Lean or
    Lake configuration executes. Callers must therefore run this under the
    same policy, and in the same network phase, as the work that follows.
    """

    def detail(proc: subprocess.CompletedProcess[str]) -> str:
        message = (proc.stderr or proc.stdout).strip().replace("\n", " ")
        return f" (exit {proc.returncode}: {message[:500]})" if message else ""

    require_protected_paths([write_probe, read_probe], writable_directories)
    if path_is_within(read_probe, [*readable_paths, *executable_paths]):
        raise VerificationError("read-denial probe is inside the sandbox read allowlist")
    for probe in (write_probe, read_probe):
        if probe.exists():
            raise VerificationError(f"confinement probe path already exists: {probe}")

    read_probe.write_text("palomar confidential read sentinel\n", encoding="utf-8")

    def verify_nested_confinement() -> None:
        nested_probe = writable_directories[0] / ".palomar-nested-landrun-probe"
        if nested_probe.exists():
            raise VerificationError(
                f"nested confinement probe path already exists: {nested_probe}"
            )
        try:
            inner = landrun_command(
                [str(touch), str(nested_probe)],
                landrun=landrun,
                writable_directories=writable_directories,
                readable_paths=readable_paths,
                executable_paths=executable_paths,
                environment=environment,
                readable_directories=[cwd],
            )
            nested = sandboxed_run(
                inner,
                cwd=cwd,
                environment=environment,
                landrun=landrun,
                writable_directories=writable_directories,
                readable_paths=readable_paths,
                executable_paths=executable_paths,
                tools=tools,
                check=False,
            )
            nested_created = nested_probe.is_file()
        finally:
            nested_probe.unlink(missing_ok=True)
        if nested.returncode or not nested_created:
            raise VerificationError(
                "nested Landrun confinement is unavailable" + detail(nested)
            )

    result: _ConfinementProbeResult | None = None
    try:
        result = _run_filesystem_confinement_probe(
            write_probe,
            existing_probe_error="confinement probe path already exists",
            touch=touch,
            cwd=cwd,
            environment=environment,
            landrun=landrun,
            writable_directories=writable_directories,
            readable_paths=readable_paths,
            executable_paths=executable_paths,
            tools=tools,
            after_allowed=verify_nested_confinement,
        )
    finally:
        if result is None:
            read_probe.unlink(missing_ok=True)
    if result.allowed.returncode or not result.allowed_created:
        read_probe.unlink(missing_ok=True)
        raise VerificationError(
            "outer sandbox did not permit its writable directory" + detail(result.allowed)
        )

    if result.denied is None:
        read_probe.unlink(missing_ok=True)
        raise VerificationError("outer sandbox confinement probe omitted its negative control")
    if result.denied_created or result.denied.returncode == 0:
        read_probe.unlink(missing_ok=True)
        raise VerificationError(
            "outer sandbox write policy was not enforced" + detail(result.denied)
        )

    protected = protected_write_directories or []
    require_protected_paths(protected, writable_directories)
    if protected:
        marker = ".palomar-frozen-write-probe"
        for directory in protected:
            if not directory.is_dir() or (directory / marker).exists():
                raise VerificationError(f"invalid frozen write probe directory: {directory}")
        frozen_script = (
            "from pathlib import Path; import sys; escaped=False; "
            "\nfor raw in sys.argv[1:]:"
            f"\n p=Path(raw)/'{marker}'"
            "\n try: p.write_text('escape')"
            "\n except OSError: pass"
            "\n else: escaped=True"
            "\nsys.exit(1 if escaped else 0)"
        )
        frozen = sandboxed_run(
            [str(python), "-c", frozen_script, *(str(path) for path in protected)],
            cwd=cwd,
            environment=environment,
            landrun=landrun,
            writable_directories=writable_directories,
            readable_paths=readable_paths,
            executable_paths=executable_paths,
            tools=tools,
            check=False,
        )
        frozen_escape = any((directory / marker).exists() for directory in protected)
        for directory in protected:
            (directory / marker).unlink(missing_ok=True)
        if frozen.returncode or frozen_escape:
            read_probe.unlink(missing_ok=True)
            raise VerificationError("outer sandbox wrote a frozen trusted build" + detail(frozen))

    read_script = "from pathlib import Path; import sys; Path(sys.argv[1]).read_bytes()"
    allowed_read = sandboxed_run(
        [str(python), "-c", read_script, str(positive_read)],
        cwd=cwd,
        environment=environment,
        landrun=landrun,
        writable_directories=writable_directories,
        readable_paths=readable_paths,
        executable_paths=executable_paths,
        tools=tools,
        check=False,
    )
    if allowed_read.returncode:
        read_probe.unlink(missing_ok=True)
        raise VerificationError("outer sandbox denied its readable source" + detail(allowed_read))

    denied_read = sandboxed_run(
        [str(python), "-c", read_script, str(read_probe)],
        cwd=cwd,
        environment=environment,
        landrun=landrun,
        writable_directories=writable_directories,
        readable_paths=readable_paths,
        executable_paths=executable_paths,
        tools=tools,
        check=False,
    )
    read_probe.unlink(missing_ok=True)
    if denied_read.returncode == 0:
        raise VerificationError("outer sandbox read policy was not enforced")

    token = secrets.token_hex(32)
    holder_env = os.environ.copy()
    holder_env["PALOMAR_PROCESS_SENTINEL"] = token
    holder = subprocess.Popen(
        [str(python), "-c", "import time; time.sleep(120)"],
        env=holder_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Popen returns before the child necessarily completes exec; until then
        # /proc can still expose the pre-exec environment. Wait briefly for the
        # sentinel so a scheduler race does not masquerade as failed isolation.
        positive_deadline = time.monotonic() + 2
        while True:
            unconfined = Path(f"/proc/{holder.pid}/environ").read_bytes()
            if token.encode() in unconfined and holder.poll() is None:
                break
            if holder.poll() is not None or time.monotonic() >= positive_deadline:
                raise VerificationError("process-environment probe lacks a live positive control")
            time.sleep(0.01)
        proc_read = sandboxed_run(
            [str(python), "-c", read_script, f"/proc/{holder.pid}/environ"],
            cwd=cwd,
            environment=environment,
            landrun=landrun,
            writable_directories=writable_directories,
            readable_paths=readable_paths,
            executable_paths=executable_paths,
            tools=tools,
            check=False,
        )
    finally:
        holder.terminate()
        try:
            holder.wait(timeout=5)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait()
    if proc_read.returncode == 0:
        raise VerificationError("outer sandbox exposed another process environment")

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    network_script = (
        "import socket,sys; "
        "s=socket.socket(socket.AF_INET,socket.SOCK_STREAM); s.settimeout(2); "
        "sys.exit(0 if s.connect_ex(('127.0.0.1',int(sys.argv[1]))) == 0 else 1)"
    )
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            accepted, _ = listener.accept()
            accepted.close()
        network = sandboxed_run(
            [str(python), "-c", network_script, str(port)],
            cwd=cwd,
            environment=environment,
            landrun=landrun,
            writable_directories=writable_directories,
            readable_paths=readable_paths,
            executable_paths=executable_paths,
            tools=tools,
            check=False,
        )
    finally:
        listener.close()
    if network.returncode == 0:
        raise VerificationError("normal sandbox phase unexpectedly reached the network")


TAG_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+/-]*$")


def revision_release_tags(git: list[str], revision: str, *, env: dict[str, str]) -> list[str]:
    """The remote's tag names that point at exactly this revision.

    Lake builds a GitHub release download URL from ``git describe --tags
    --exact-match``, so a dependency that ships prebuilt assets rather than
    building them (ProofWidgets' widget bundle needs npm) can only be fetched
    when the tag naming its pinned revision exists in the local checkout.
    """
    listing = run(
        [*git, "ls-remote", "--tags", "origin"],
        env=env,
        timeout=EXECUTION_BUDGET_SECONDS,
    ).stdout
    direct: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) != 2 or not submission_contract.SHA_RE.fullmatch(parts[0]):
            continue
        if not parts[1].startswith("refs/tags/"):
            continue
        name = parts[1].removeprefix("refs/tags/")
        if name.endswith("^{}"):
            peeled[name.removesuffix("^{}")] = parts[0]
        else:
            direct[name] = parts[0]
    # An annotated tag names its commit through the peeled ref; a lightweight
    # tag points at the commit directly.
    return sorted(
        name
        for name, target in direct.items()
        if peeled.get(name, target) == revision
        and TAG_NAME_RE.fullmatch(name)
        and ".." not in name
        and "//" not in name
        and not name.endswith(("/", ".lock"))
    )


def materialize_packages(
    source: Path, *, checkout: Path, base_env: dict[str, str]
) -> list[Path]:
    """Materialize the exact Git revisions in the submitted Lake manifest.

    This deliberately does not use ``lake update``: Lake runs package
    post-update hooks, which are unnecessary for fetching and expand the
    pre-verification execution surface.
    """
    boundary = checkout.resolve()
    packages = manifest_packages(source)
    path_directories: dict[str, Path] = {}
    for package in packages:
        if not package["url"].startswith("path:"):
            continue
        name = package["name"]
        raw_value = package["url"].removeprefix("path:")
        package_dir = contained_path_dependency(source, raw_value, boundary, name)
        path_directories[name] = package_dir

    purge_untrusted_lake_state(boundary)
    reject_committed_build_artifacts(boundary)
    packages_dir = manifest_packages_directory(source, checkout=boundary)
    packages_owner = packages_dir.parent.parent
    permitted_owners = {source.resolve(), *path_directories.values()}
    if packages_owner not in permitted_owners:
        raise VerificationError(
            "Lake manifest packagesDir must belong to the project or a contained path dependency"
        )
    writable: list[Path] = []
    for owner in sorted(permitted_owners):
        writable.extend(remove_untrusted_lake_state(owner))
    packages_dir.mkdir(exist_ok=True)
    git_env = git_environment(source, base_env)
    Path(git_env["HOME"]).mkdir(parents=True)
    for package in packages:
        name = package["name"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name in {".", ".."}:
            raise VerificationError(f"unsafe package name in Lake manifest: {name!r}")
        repository_url = package["url"]
        if repository_url.startswith("path:"):
            continue
        revision = package["revision"]
        parsed = urlparse(repository_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise VerificationError(f"Git package {name!r} must use a credential-free HTTPS repository URL")
        if not submission_contract.SHA_RE.fullmatch(revision):
            raise VerificationError(f"Git package {name!r} is not pinned to a full commit")
        package_dir = packages_dir / name
        package_dir.mkdir()
        git = [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "protocol.file.allow=never",
            "-C",
            str(package_dir),
        ]
        run([*git, "init", "--quiet"], env=git_env)
        run([*git, "remote", "add", "origin", repository_url], env=git_env)
        run(
            [*git, "fetch", "--quiet", "--depth=1", "origin", revision],
            env=git_env,
            timeout=EXECUTION_BUDGET_SECONDS,
        )
        # A depth-1 fetch of a bare commit brings no tags, so fetch the ones
        # naming this revision. They carry no history the pinned revision did
        # not already bring, and the revision itself stays the verified one.
        tags = revision_release_tags(git, revision, env=git_env)
        if tags:
            run(
                [
                    *git,
                    "fetch",
                    "--quiet",
                    "--depth=1",
                    "origin",
                    *(f"+refs/tags/{tag}:refs/tags/{tag}" for tag in tags),
                ],
                env=git_env,
                timeout=EXECUTION_BUDGET_SECONDS,
            )
        run([*git, "checkout", "--quiet", "--detach", revision], env=git_env)
        validate_preservable_git_checkout(
            package_dir,
            f"Git package {name!r}",
            allow_inert_submodules=True,
        )
        writable.extend(remove_untrusted_lake_state(package_dir))
    return validate_writable_directories(boundary, writable)


def reject_untrusted_package_artifacts(
    source: Path,
    packages: list[dict[str, str]],
    allowlist: dict[str, tuple[str, str]],
    *,
    checkout: Path,
) -> None:
    """Scan candidate-controlled packages after official closure verification."""
    for package in packages:
        if package["name"] not in allowlist:
            reject_committed_build_artifacts(
                package_checkout(source, package, checkout=checkout)
            )


def mathlib_cache_availability(transcript: str) -> bool | None:
    """Interpret the trusted cache client's completion summary."""
    downloaded = [
        int(count)
        for count in re.findall(r"(?im)^[\r ]*Downloaded:\s*([0-9]+)\s+file", transcript)
    ]
    if "Warning: some files were not found in the cache." in transcript:
        return False
    if downloaded:
        return any(count > 0 for count in downloaded)
    if "No files to download" in transcript:
        return True
    # Older cache clients did not expose a stable availability summary. Do not
    # claim either outcome when the trusted transcript is silent.
    return None


def get_mathlib_cache(
    source: Path,
    *,
    checkout: Path,
    base_env: dict[str, str],
    allowlist: dict[str, tuple[str, str]],
    lake: Path,
    landrun: Path,
    readable_paths: list[Path],
    executable_paths: list[Path],
    tools: dict[Path, str],
    resource_properties: tuple[str, ...] = (),
) -> dict[str, bool | None]:
    """Run the trusted cache client and report whether it supplied the closure."""
    packages = manifest_packages(source)
    _roots, aliases = allowed_roots()
    mathlib = next(
        (
            package
            for package in packages
            if canonical_repository(str(package["repository"]), aliases).lower()
            == "leanprover-community/mathlib4"
        ),
        None,
    )
    if mathlib is None:
        return {"required": False, "available": None}
    package_dir = package_checkout(source, mathlib, checkout=checkout)
    if not (package_dir / ".git").is_dir():
        raise VerificationError("official Mathlib dependency is not a Git checkout")
    git_env = git_environment(source, base_env)
    head = run(
        [
            "git",
            "-C",
            str(package_dir),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "rev-parse",
            "HEAD",
        ],
        env=git_env,
    ).stdout.strip()
    if head != mathlib["revision"]:
        raise VerificationError("Mathlib checkout does not match lake-manifest.json")
    origin = run(["git", "-C", str(package_dir), "remote", "get-url", "origin"], env=git_env).stdout.strip()
    if (github_repository(origin) or "").lower() != "leanprover-community/mathlib4":
        raise VerificationError("Mathlib checkout has an unexpected origin")
    changes = run(
        [
            "git",
            "-C",
            str(package_dir),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ],
        env=git_env,
    ).stdout.strip()
    if changes:
        raise VerificationError("Mathlib source was modified while configuring the workspace")

    # Run Mathlib as the workspace root so candidate Lake configuration never
    # executes during the network-enabled phase. Symlinks expose only Mathlib's
    # independently verified official closure, so trusted cache output is
    # written into the exact flattened checkouts the candidate will later read.
    closure = {
        mathlib["name"],
        *(dependency["name"] for dependency in manifest_packages(package_dir)),
    }
    if not closure <= allowlist.keys():
        raise VerificationError("Mathlib cache closure is outside the verified allowlist")
    # Candidate configuration ran with these directories writable. Recreate
    # every verified package's .lake tree immediately before the first
    # networked Lake command, then expose only fresh build/config directories.
    trusted_directories = reset_trusted_lake_state(
        source,
        closure,
        packages=packages,
        checkout=checkout,
    )

    nested_packages = nested_package_links(
        source, package_dir, checkout=checkout, allowed_names=closure
    )
    cache_writable = validate_writable_directories(checkout, trusted_directories)
    replay_writable_files: list[Path] = []
    proofwidgets = next((package for package in packages if package["name"] == "proofwidgets"), None)
    if proofwidgets and proofwidgets["name"] in closure:
        # ProofWidgets' official Lake target records this generated replay
        # marker beside the pinned lockfile. Grant only this exact file; a
        # qualified root must never write its source tree or compiled output.
        lock_hash = (
            package_checkout(source, proofwidgets, checkout=checkout)
            / "widget"
            / "package-lock.json.hash"
        )
        lock_hash.touch(exist_ok=True)
        if lock_hash.is_symlink() or not lock_hash.is_file():
            raise VerificationError("ProofWidgets replay marker is not a regular file")
        replay_writable_files.append(lock_hash.resolve())
    cache_env = base_env.copy()
    cache_env["LAKE_PKG_URL_MAP"] = trusted_package_url_map(
        packages, manifest_packages(package_dir)
    )
    home = package_dir / ".lake" / "config" / "home"
    temporary = package_dir / ".lake" / "config" / "tmp"
    home.mkdir(exist_ok=True)
    temporary.mkdir(exist_ok=True)
    cache_env.update(
        {"HOME": str(home.resolve()), "TMPDIR": str(temporary.resolve()), "LEAN_ABORT_ON_PANIC": "1"}
    )
    try:
        cache_result = sandboxed_run(
            [str(lake), "exe", "cache", "get"],
            cwd=package_dir,
            environment=cache_env,
            landrun=landrun,
            writable_directories=cache_writable,
            readable_paths=readable_paths,
            executable_paths=executable_paths,
            tools=tools,
            timeout=EXECUTION_BUDGET_SECONDS,
            unrestricted_network=True,
            resource_properties=resource_properties,
        )
        cache_transcript = f"{cache_result.stdout}\n{cache_result.stderr}"
        cache_available = mathlib_cache_availability(cache_transcript)
        # Replay the trusted cache while the high-trust Mathlib closure
        # is still the only writable package surface. Lake records local hash
        # metadata during replay; creating it here prevents a qualified root
        # from later needing write access to Mathlib or its dependencies.
        sandboxed_run(
            [str(lake), "build"],
            cwd=package_dir,
            environment=cache_env,
            landrun=landrun,
            writable_directories=cache_writable,
            writable_files=replay_writable_files,
            readable_paths=readable_paths,
            executable_paths=executable_paths,
            tools=tools,
            timeout=EXECUTION_BUDGET_SECONDS,
        )
        return {"required": True, "available": cache_available}
    finally:
        if nested_packages.is_symlink():
            nested_packages.unlink()
        elif nested_packages.is_dir():
            shutil.rmtree(nested_packages)


def source_matches_checkout(path: Path, package_dir: Path) -> bool:
    try:
        relative = path.resolve().relative_to(package_dir.resolve()).as_posix()
    except ValueError:
        return False
    git = [
        "git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "protocol.file.allow=never",
        "-C",
        str(package_dir),
    ]
    env = os.environ.copy()
    env.update(
        {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    expected = run([*git, "rev-parse", f"HEAD:{relative}"], env=env, check=False)
    actual = run([*git, "hash-object", "--", str(path.resolve())], env=env, check=False)
    return (
        expected.returncode == 0
        and actual.returncode == 0
        and expected.stdout.strip() == actual.stdout.strip()
    )


@dataclass(frozen=True)
class LeanHeader:
    """A Lean source header, as the compiler's own parser reports it."""

    is_module: bool
    imports: tuple[str, ...]


def parse_lean_header(stdout: str) -> LeanHeader:
    """Read one `lean --deps-json` report.

    The compiler injects `Init` into every header that does not say `prelude`,
    so those entries are dropped: this reports the modules an author wrote. A
    Challenge that imports `Init` by hand is therefore not credited with it,
    which costs nothing that a reviewer reads.
    """
    lines = [line for line in stdout.splitlines() if line.strip()]
    try:
        payload = json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError) as error:
        raise VerificationError("Lean did not report a parsable header") from error
    entries = payload.get("imports") if isinstance(payload, dict) else None
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
        raise VerificationError("Lean reported no single header")
    reported = entries[0]
    errors = reported.get("errors") or []
    if errors:
        raise VerificationError(f"Lean rejected the header: {str(errors[0])[:500]}")
    result = reported.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("isModule"), bool):
        raise VerificationError("Lean did not report whether the source is a module")
    reported_imports = result.get("imports")
    if not isinstance(reported_imports, list):
        raise VerificationError("Lean did not report the header imports")
    modules: list[str] = []
    for entry in reported_imports:
        if not isinstance(entry, dict) or not isinstance(entry.get("module"), str):
            raise VerificationError("Lean reported an unreadable header import")
        module = entry["module"]
        if module != "Init":
            modules.append(module)
    return LeanHeader(is_module=result["isModule"], imports=tuple(sorted(set(modules))))


def lean_header(
    source: Path,
    *,
    lean_source: Path,
    lean: Path,
    environment: dict[str, str],
    landrun: Path,
    writable_directories: list[Path],
    readable_paths: list[Path],
    executable_paths: list[Path],
    tools: dict[Path, str],
) -> LeanHeader:
    """Ask Lean to read a source header.

    ``--deps-json`` runs the compiler's own parser, the one Lake uses to decide
    which artifacts a module produces and which modules it imports, so
    Palomar's reading cannot drift from Lean's. The header is parsed, never
    elaborated, so this reads the candidate's source without running it.
    """
    proc = sandboxed_run(
        [str(lean), "--deps-json", str(lean_source)],
        cwd=source,
        environment=environment,
        landrun=landrun,
        writable_directories=writable_directories,
        readable_paths=readable_paths,
        executable_paths=executable_paths,
        tools=tools,
    )
    return parse_lean_header(proc.stdout)


def lean_source_dependencies(
    source: Path,
    *,
    challenge_source: Path,
    lean: Path,
    environment: dict[str, str],
    landrun: Path,
    writable_directories: list[Path],
    readable_paths: list[Path],
    executable_paths: list[Path],
    tools: dict[Path, str],
) -> list[Path]:
    proc = sandboxed_run(
        [str(lean), "--src-deps", str(challenge_source)],
        cwd=source,
        environment=environment,
        landrun=landrun,
        writable_directories=writable_directories,
        readable_paths=readable_paths,
        executable_paths=executable_paths,
        tools=tools,
    )
    dependencies: set[Path] = set()
    for line in proc.stdout.splitlines():
        value = line.strip()
        if not value:
            continue
        path = Path(value)
        if not path.is_absolute():
            raise VerificationError(f"Lean reported a non-absolute source dependency: {value!r}")
        resolved = path.resolve()
        if resolved.suffix != ".lean" or not resolved.is_file():
            raise VerificationError(f"Lean reported an invalid source dependency: {value!r}")
        dependencies.add(resolved)
    return sorted(dependencies)


def audit_challenge_sources(
    source: Path,
    *,
    checkout: Path,
    dependency_sources: list[Path],
    lean_prefix: Path,
    allowlist: dict[str, tuple[str, str]],
    writable_directories: list[Path],
) -> dict[str, Any]:
    packages = manifest_packages(source)
    by_name = {package["name"]: package for package in packages}
    untrusted: list[str] = []
    dependencies: dict[tuple[str, str], None] = {}
    qualified_allowlisted = False
    toolchain_prefix = lean_prefix.resolve()

    for resolved in dependency_sources:
        try:
            resolved.relative_to(toolchain_prefix)
            continue
        except ValueError:
            pass
        if path_is_within(resolved, writable_directories):
            untrusted.append(str(resolved))
            continue
        package_name = source_package(resolved, source, checkout=checkout)
        if package_name is None:
            # Any candidate-local helper import expands the unaudited statement
            # surface and is forbidden in v1.
            untrusted.append(str(resolved))
            continue
        package = by_name.get(package_name)
        if not package:
            untrusted.append(str(resolved))
            continue
        package_dir = package_checkout(source, package, checkout=checkout)
        if not source_matches_checkout(resolved, package_dir):
            untrusted.append(str(resolved))
            continue
        if package_name in allowlist:
            repository, level = allowlist[package_name]
            dependencies[(repository, "allowlisted")] = None
            qualified_allowlisted = qualified_allowlisted or level == "qualified"
            continue
        # A repository is not importable merely because Palomar has already
        # accepted a record from it.
        untrusted.append(str(resolved))

    serialized_dependencies = [
        {"repository": repository, "provenance": provenance}
        for repository, provenance in sorted(dependencies)
    ]
    return {
        "source_count": len(dependency_sources),
        "dependencies": serialized_dependencies,
        "untrusted_sources": untrusted[:100],
        "trust_level": "qualified" if qualified_allowlisted else "high",
    }


def lake_environment_value(
    name: str,
    *,
    source: Path,
    lake: Path,
    printenv: Path,
    environment: dict[str, str],
    landrun: Path,
    writable_directories: list[Path],
    readable_paths: list[Path],
    executable_paths: list[Path],
    tools: dict[Path, str],
    allowed_roots: list[Path],
) -> str:
    proc = sandboxed_run(
        [str(lake), "env", str(printenv), name],
        cwd=source,
        environment=environment,
        landrun=landrun,
        writable_directories=writable_directories,
        readable_paths=readable_paths,
        executable_paths=executable_paths,
        tools=tools,
    )
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        raise VerificationError(f"Lake did not report {name}")
    value = lines[-1]
    paths = value.split(os.pathsep)
    if not paths or any(not path or not Path(path).is_absolute() for path in paths):
        raise VerificationError(f"Lake reported an invalid {name}")
    resolved = [Path(path).resolve() for path in paths]
    if any(not path_is_within(path, allowed_roots) for path in resolved):
        raise VerificationError(f"Lake reported {name} outside the materialized project")
    return os.pathsep.join(str(path) for path in resolved)


def resolve_module_source(
    module: str,
    *,
    project: Path,
    lean_source_path: str,
) -> Path:
    """Resolve a configured module using Lake's ordered source roots."""
    suffix = module_source_suffix(module)
    for root_value in lean_source_path.split(os.pathsep):
        if not root_value:
            continue
        candidate = Path(root_value).joinpath(*suffix.parts)
        if not candidate.exists() and not candidate.is_symlink():
            continue
        if candidate.is_symlink() or not candidate.is_file():
            raise VerificationError(f"configured module {module!r} is not a regular source file")
        resolved = candidate.resolve()
        try:
            resolved.relative_to(project.resolve())
        except ValueError as error:
            raise VerificationError(
                f"configured module {module!r} resolves outside the selected project"
            ) from error
        return resolved
    raise VerificationError(f"configured module {module!r} has no source file in Lake's source path")


COMPARATOR_FAILURE_MARKERS = ("uncaught exception", "error:", "error]", "failed")


def comparator_failure_excerpt(log: str, *, limit: int = 10) -> str:
    """The lines of a Comparator log that say why it stopped.

    A submitter should not have to read a build log of several thousand lines
    to find the one line that matters, so the lines that name a failure are
    carried into the report. The whole tail is kept separately for an operator.
    """
    lines = [line.rstrip() for line in log.splitlines() if line.strip()]
    marked = [
        line for line in lines
        if any(marker in line.lower() for marker in COMPARATOR_FAILURE_MARKERS)
    ]
    chosen = (marked or lines)[-limit:]
    return "\n".join(line[:400] for line in chosen)


def comparator_failure(returncode: int, log: str, *, canonical_root: Path) -> VerificationError:
    """Say what a nonzero Comparator exit means, and whose problem it is.

    Comparator judges a submission, but it can also fail without judging one.
    The protected Challenge module is Palomar's own artifact, so a run that
    could not read it has established nothing about the submission and must not
    be reported as a rejection of it.
    """
    excerpt = comparator_failure_excerpt(log)
    if "landrun adapter:" in log:
        return VerificationError(
            "Comparator sandbox adapter failed",
            code="palomar.comparator_sandbox_failed",
            owner="palomar",
            detail=excerpt,
            next_action=(
                "Do not change the repository. Retry the same commit later; report the "
                "workflow URL if the problem recurs."
            ),
            retryable=True,
        )
    if str(canonical_root) in log:
        return VerificationError(
            "Comparator could not read the Challenge module that Palomar compiled",
            code="palomar.canonical_challenge_unreadable",
            owner="palomar",
            detail=excerpt,
            next_action=(
                "Do not change the repository. This is Palomar's to fix; the same commit "
                "can be verified again once it is."
            ),
            retryable=True,
        )
    return VerificationError(
        f"Comparator rejected the project (exit {returncode})",
        code="comparator.rejected",
        detail=excerpt,
        next_action=(
            "Correct the Lean or Comparator failure quoted above, commit it, and make "
            "a new submission."
        ),
    )


def protected_lean_path(
    canonical_olean: Path,
    trusted_lean_paths: list[Path],
    candidate_lean_path: str,
    *,
    protected_root: Path | None = None,
) -> str:
    """Resolve protected statement imports before any candidate build output."""
    candidate_paths = [Path(value) for value in candidate_lean_path.split(os.pathsep) if value]
    ordered = [protected_root or canonical_olean.parent, *trusted_lean_paths, *candidate_paths]
    return os.pathsep.join(str(path) for path in dict.fromkeys(ordered))


def canonical_challenge_artifacts(olean: Path, *, module_system: bool) -> tuple[Path, ...]:
    """Every file the trusted Challenge compilation is expected to publish.

    A module-system source compiles to a public module plus private, server and
    IR sidecars, and importing it fails unless all four travel together. A
    pre-module-system source compiles to the single module. Deriving the set
    from the source rather than from whatever the sandbox happens to contain
    keeps hostile elaboration from adding a file to the protected directory.
    """
    if not module_system:
        return (olean,)
    return (
        olean,
        olean.parent / f"{olean.name}.private",
        olean.parent / f"{olean.name}.server",
        olean.parent / f"{olean.stem}.ir",
    )


def compile_canonical_challenge(
    work: Path,
    source: Path,
    *,
    checkout: Path,
    challenge_source: Path | None = None,
    challenge_module: str = "Challenge",
    lean: Path,
    lean_prefix: Path,
    allowlist: dict[str, tuple[str, str]],
    environment: dict[str, str],
    landrun: Path,
    readable_paths: list[Path],
    executable_paths: list[Path],
    tools: dict[Path, str],
) -> tuple[Path, list[Path], list[Path]]:
    """Compile Challenge directly against frozen trusted dependencies.

    Candidate Lake configuration never participates in this compilation. The
    resulting module is prepended to Comparator's LEAN_PATH so its Challenge
    export cannot be replaced by candidate build output.
    """
    output_dir = work / "canonical-challenge"
    scratch = work / "canonical-challenge-scratch"
    for directory in (output_dir, scratch):
        if directory.exists() or directory.is_symlink():
            raise VerificationError(f"canonical Challenge path is not fresh: {directory}")
    scratch.mkdir()
    home = scratch / "home"
    temporary = scratch / "tmp"
    home.mkdir()
    temporary.mkdir()
    challenge_source = challenge_source or (source / "Challenge.lean")

    packages_by_name = {package["name"]: package for package in manifest_packages(source)}
    trusted_names = sorted(
        allowlist,
        key=lambda name: (allowlist[name][1] != "high", name.lower()),
    )
    trusted_packages = {
        name: package_checkout(source, packages_by_name[name], checkout=checkout)
        for name in trusted_names
    }
    lean_paths: list[Path] = []
    core_path = lean_prefix / "lib" / "lean"
    if core_path.is_dir():
        lean_paths.append(core_path.resolve())
    for package_dir in trusted_packages.values():
        path = package_dir / ".lake" / "build" / "lib" / "lean"
        if path.is_dir():
            lean_paths.append(path.resolve())
    lean_paths = list(dict.fromkeys(lean_paths))
    canonical_readable_paths = list(readable_paths)
    challenge_root = challenge_source.resolve()
    for _segment in module_source_suffix(challenge_module).parts:
        challenge_root = challenge_root.parent
    source_paths = [
        source.resolve(),
        challenge_root,
        *trusted_packages.values(),
    ]
    lake_source = lean_prefix / "src" / "lean" / "lake"
    if lake_source.is_dir():
        source_paths.append(lake_source.resolve())

    canonical_env = environment.copy()
    canonical_env.update(
        {
            "HOME": str(home.resolve()),
            "TMPDIR": str(temporary.resolve()),
            "LEAN_PATH": os.pathsep.join(str(path) for path in dict.fromkeys(lean_paths)),
            "LEAN_SRC_PATH": os.pathsep.join(str(path) for path in sorted(set(source_paths))),
        }
    )
    module_suffix = module_source_suffix(challenge_module).with_suffix(".olean")
    compiled_olean = scratch.joinpath(*module_suffix.parts)
    compiled_olean.parent.mkdir(parents=True, exist_ok=True)
    sandboxed_run(
        [str(lean), "-o", str(compiled_olean), str(challenge_source)],
        cwd=source,
        environment=canonical_env,
        landrun=landrun,
        writable_directories=[scratch.resolve()],
        readable_paths=canonical_readable_paths,
        executable_paths=executable_paths,
        tools=tools,
        timeout=EXECUTION_BUDGET_SECONDS,
    )
    if compiled_olean.is_symlink() or not compiled_olean.is_file():
        raise VerificationError("trusted Challenge compilation produced no module")
    module_system = lean_header(
        source,
        lean_source=challenge_source,
        lean=lean,
        environment=canonical_env,
        landrun=landrun,
        writable_directories=[scratch.resolve()],
        readable_paths=canonical_readable_paths,
        executable_paths=executable_paths,
        tools=tools,
    ).is_module
    compiled_artifacts = canonical_challenge_artifacts(
        compiled_olean, module_system=module_system
    )
    for artifact in compiled_artifacts:
        if artifact.is_symlink() or not artifact.is_file():
            raise VerificationError(
                f"trusted Challenge compilation did not publish {artifact.name}"
            )
    # A source that Lean treats as a module but the header check does not would
    # otherwise lose its sidecars silently, and a source that is not a module
    # has no business carrying them.
    surplus = [
        artifact
        for artifact in canonical_challenge_artifacts(compiled_olean, module_system=True)
        if artifact not in compiled_artifacts and (artifact.exists() or artifact.is_symlink())
    ]
    if surplus:
        raise VerificationError(
            "trusted Challenge compilation published unexpected module artifacts: "
            + ", ".join(sorted(artifact.name for artifact in surplus))
        )
    compiled_artifacts = tuple(artifact.resolve() for artifact in compiled_artifacts)
    compiled_olean = compiled_olean.resolve()
    for artifact in compiled_artifacts:
        tools[artifact] = sha256(artifact)
    dependencies = lean_source_dependencies(
        source,
        challenge_source=challenge_source,
        lean=lean,
        environment=canonical_env,
        landrun=landrun,
        writable_directories=[scratch.resolve()],
        readable_paths=canonical_readable_paths,
        executable_paths=executable_paths,
        tools=tools,
    )
    # Challenge elaboration is hostile and may write other files in `scratch`.
    # Only the single snapshotted module crosses into the protected search path,
    # which is created by the verifier after all hostile canonical-build passes.
    output_dir.mkdir()
    canonical_olean = output_dir.joinpath(*module_suffix.parts)
    canonical_olean.parent.mkdir(parents=True, exist_ok=True)
    canonical_artifacts = canonical_challenge_artifacts(
        canonical_olean, module_system=module_system
    )
    for compiled, canonical in zip(compiled_artifacts, canonical_artifacts, strict=True):
        shutil.copyfile(compiled, canonical)
        if canonical.is_symlink() or not canonical.is_file():
            raise VerificationError(f"protected Challenge artifact {canonical.name} is not a regular file")
    protected_files = {
        path.resolve()
        for path in output_dir.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if protected_files != {artifact.resolve() for artifact in canonical_artifacts}:
        raise VerificationError("protected Challenge directory contains unexpected files")
    canonical_olean = canonical_olean.resolve()
    for artifact in canonical_artifacts:
        tools[artifact.resolve()] = sha256(artifact)
    return canonical_olean, dependencies, lean_paths


def execute(args: argparse.Namespace) -> int:
    global _EXECUTION_DEADLINE, _RESOURCE_DISK_PATH, _RESOURCE_METRICS_PATH

    output = Path(args.output).resolve()
    work = Path(args.work_dir).resolve()
    report = json.loads(output.read_text(encoding="utf-8"))
    if report.get("status") != "pending":
        return 0
    previous_deadline = _EXECUTION_DEADLINE
    previous_metrics_path = _RESOURCE_METRICS_PATH
    previous_disk_path = _RESOURCE_DISK_PATH
    checkout = work / "source"
    raw_project_path = report.get("source", {}).get("project_path")
    project_relative = (
        normalized_repository_path(raw_project_path, "Mechanical project path")
        if isinstance(raw_project_path, str) and raw_project_path
        else None
    )
    source = (
        resolve_repository_path(
            checkout, project_relative, "Mechanical project path", kind="directory"
        )
        if project_relative is not None
        else checkout.resolve()
    )
    metrics_path = work / "resource-metrics.jsonl"
    report.update(
        {
            "status": "error",
            "stage": "setup",
            "phase": "verification",
            "comparator_commit": args.comparator_commit,
            "landrun_commit": args.landrun_commit,
            "nanoda_commit": args.nanoda_commit,
            "workflow_url": args.workflow_url,
        }
    )
    tools: dict[Path, str] = {}

    def guarded_write() -> None:
        report["resource_usage"] = resource_metrics(metrics_path)
        if tools:
            try:
                verify_tool_snapshot(tools)
            except VerificationError as error:
                report["status"] = "error"
                message = str(error)
                if message not in report["errors"]:
                    report["errors"].append(message)
                report_diagnostic(report, error, owner="palomar")
        write_json(output, report)

    try:
        if checkout.is_symlink() or not checkout.is_dir():
            raise VerificationError("verifier-owned source checkout is not a real directory")
        git_metadata = checkout / ".git"
        if git_metadata.is_symlink() or not git_metadata.is_dir():
            raise VerificationError(
                "verifier-owned source checkout has no real Git metadata directory"
            )
        if metrics_path.is_symlink() or (metrics_path.exists() and not metrics_path.is_file()):
            raise VerificationError("resource metrics path is not a regular file")
        metrics_path.unlink(missing_ok=True)
        _RESOURCE_METRICS_PATH = metrics_path
        _RESOURCE_DISK_PATH = checkout
        install_execution_deadline(
            os.environ.get("PALOMAR_JOB_STARTED_AT"),
            getattr(args, "execution_budget_seconds", EXECUTION_BUDGET_SECONDS),
        )
        comparator = Path(args.comparator).resolve()
        lean4export = Path(args.lean4export).resolve()
        landrun = Path(args.landrun).resolve()
        nanoda = Path(args.nanoda).resolve()
        adapter = (ROOT / "scripts" / "landrun_passthrough.py").resolve()
        metrics_wrapper = (ROOT / "scripts" / "measure_resources.py").resolve()
        verifier = Path(__file__).resolve()
        for tool in (comparator, lean4export, landrun, nanoda, adapter, metrics_wrapper, verifier):
            if not tool.is_file():
                raise VerificationError(f"missing verifier tool: {tool}")
        env = os.environ.copy()
        env.pop("LAKE_PKG_URL_MAP", None)
        env.update(
            {
                "COMPARATOR_LANDRUN": str(adapter),
                "COMPARATOR_LEAN4EXPORT": str(lean4export),
                "COMPARATOR_NANODA": str(nanoda),
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "PALOMAR_LANDRUN_REAL": str(landrun),
                "LEAN_ABORT_ON_PANIC": "1",
            }
        )
        lean_command = shutil.which("lean", path=env["PATH"])
        if not lean_command:
            raise VerificationError("trusted Lean executable is unavailable")
        lean_prefix = Path(
            run([lean_command, "--print-prefix"], cwd=source, env=env).stdout.strip()
        ).resolve()
        if not lean_prefix.is_dir():
            raise VerificationError("trusted Lean prefix is unavailable")
        env["PATH"] = f"{lean_prefix / 'bin'}:{env['PATH']}"
        lake_command = shutil.which("lake", path=env["PATH"])
        printenv_command = shutil.which("printenv", path=env["PATH"])
        touch_command = shutil.which("touch", path=env["PATH"])
        if not lake_command or not printenv_command or not touch_command:
            raise VerificationError("trusted Lake environment tools are unavailable")
        lake = Path(lake_command).resolve(strict=True)
        printenv = Path(printenv_command).absolute()
        lean = (lean_prefix / "bin" / "lean").resolve(strict=True)
        python = Path(sys.executable).resolve(strict=True)
        touch = Path(touch_command).absolute()
        try:
            lake.relative_to(lean_prefix)
        except ValueError as error:
            raise VerificationError("Lake executable is outside the selected Lean toolchain") from error

        report["stage"] = "candidate-setup"
        if ensure_lake_manifest(source, checkout):
            report["warnings"].append(
                "Generated a trusted Lake manifest from contained path-dependency manifests"
            )
        writable_directories = materialize_packages(source, checkout=checkout, base_env=env)
        sandbox_home = source / ".lake" / "config" / "home"
        sandbox_tmp = source / ".lake" / "config" / "tmp"
        sandbox_home.mkdir()
        sandbox_tmp.mkdir()
        env["HOME"] = str(sandbox_home.resolve())
        env["TMPDIR"] = str(sandbox_tmp.resolve())

        python_prefix = Path(sys.executable).resolve().parent.parent
        executable_paths = [
            lean_prefix,
            python_prefix,
            comparator,
            lean4export,
            landrun,
            nanoda,
            adapter,
            metrics_wrapper,
            lake,
            lean,
            python,
            printenv,
            touch,
        ]
        for system_path in (
            Path("/usr"),
            Path("/bin"),
            Path("/lib"),
            Path("/lib64"),
            Path("/run/current-system/sw"),
            Path("/nix/store"),
        ):
            if system_path.exists():
                executable_paths.append(system_path.resolve())
        executable_paths = sorted(set(executable_paths))
        comparator_path = resolve_repository_path(
            checkout,
            normalized_repository_path(
                str(report.get("comparator", {}).get("path") or ""),
                "Mechanical Comparator configuration path",
            ),
            "Mechanical Comparator configuration path",
            kind="file",
        )
        lakefile_path = resolve_repository_path(
            checkout,
            normalized_repository_path(
                str(report.get("lakefile", {}).get("path") or ""),
                "Mechanical Lakefile path",
            ),
            "Mechanical Lakefile path",
            kind="file",
        )
        comparator_config = protected_comparator_config(
            comparator_path, work / "protected-comparator.json"
        )
        report["stage"] = "setup"
        readable_paths = sorted(
            {checkout.resolve(), comparator_config, *system_readable_paths()}
        )
        require_protected_paths(
            [
                output,
                comparator_config,
                comparator,
                lean4export,
                landrun,
                nanoda,
                adapter,
                metrics_wrapper,
                verifier,
                lake,
                lean,
                python,
                printenv,
                touch,
                lean_prefix,
            ],
            writable_directories,
        )
        tools = tool_snapshot(
            [
                comparator,
                lean4export,
                landrun,
                nanoda,
                comparator_config,
                adapter,
                metrics_wrapper,
                verifier,
                lake,
                lean,
                python,
                printenv,
                touch,
            ]
        )
        report["stage"] = "confinement-initial"
        # `--best-effort` is accepted only after the composed outer boundary
        # proves that its positive and negative controls work on this runner.
        # No candidate-controlled Lean or Lake code executes before this probe.
        verify_sandbox_confinement(
            work / "landrun-initial-write-denial-probe",
            work / "landrun-initial-read-denial-probe",
            positive_read=lakefile_path,
            python=python,
            touch=touch,
            cwd=source,
            environment=env,
            landrun=landrun,
            writable_directories=writable_directories,
            readable_paths=readable_paths,
            executable_paths=executable_paths,
            tools=tools,
        )
        report["stage"] = "module-resolution"
        candidate_source_path = lake_environment_value(
            "LEAN_SRC_PATH",
            source=source,
            lake=lake,
            printenv=printenv,
            environment=env,
            landrun=landrun,
            writable_directories=writable_directories,
            readable_paths=readable_paths,
            executable_paths=executable_paths,
            tools=tools,
            allowed_roots=[checkout, lean_prefix],
        )
        challenge_source = resolve_module_source(
            report["comparator"]["challenge_module"],
            project=source,
            lean_source_path=candidate_source_path,
        )
        solution_source = resolve_module_source(
            report["comparator"]["solution_module"],
            project=source,
            lean_source_path=candidate_source_path,
        )
        challenge_bytes = challenge_source.stat().st_size
        if challenge_bytes > MAX_CHALLENGE_BYTES:
            raise VerificationError("configured Challenge source exceeds the 100 KiB hard cap")
        challenge_text = challenge_source.read_text(encoding="utf-8")
        challenge_lines = len(challenge_text.splitlines())
        if challenge_lines > MAX_CHALLENGE_LINES:
            raise VerificationError("configured Challenge source exceeds the 1,000-line hard cap")
        if challenge_bytes > 32 * 1024 or challenge_lines > 300:
            report["warnings"].append(
                "configured Challenge source exceeds the preferred 32 KiB / 300-line review surface"
            )
        challenge_header = lean_header(
            source,
            lean_source=challenge_source,
            lean=lean,
            environment=env,
            landrun=landrun,
            writable_directories=writable_directories,
            readable_paths=readable_paths,
            executable_paths=executable_paths,
            tools=tools,
        )
        report["challenge"].update(
            {
                "path": repository_relative_path(checkout, challenge_source),
                "bytes": challenge_bytes,
                "lines": challenge_lines,
                "direct_imports": list(challenge_header.imports),
                "sha256": sha256(challenge_source),
            }
        )
        report["solution"].update(
            {
                "path": repository_relative_path(checkout, solution_source),
                "sha256": sha256(solution_source),
            }
        )
        report["stage"] = "dependency-provenance"
        packages = manifest_packages(source)
        allowlist = package_allowlist(
            source, packages, checkout=checkout, base_env=env
        )
        reject_untrusted_package_artifacts(
            source, packages, allowlist, checkout=checkout
        )
        report["stage"] = "trusted-cache"
        report["mathlib_cache"] = get_mathlib_cache(
            source,
            checkout=checkout,
            base_env=env,
            allowlist=allowlist,
            lake=lake,
            landrun=landrun,
            readable_paths=readable_paths,
            executable_paths=executable_paths,
            tools=tools,
        )
        report["stage"] = "trusted-roots"
        build_allowlisted_roots(
            source,
            checkout=checkout,
            packages=packages,
            allowlist=allowlist,
            base_env=env,
            lake=lake,
            landrun=landrun,
            readable_paths=readable_paths,
            executable_paths=executable_paths,
            tools=tools,
        )

        trusted_names = set(allowlist)
        trusted_directories = set(
            trusted_lake_directories(source, trusted_names, checkout=checkout)
        )
        trusted_build_directories = {
            package_lake_directories(source, name, checkout=checkout)[0]
            for name in trusted_names
        }
        candidate_writable = [
            directory for directory in writable_directories if directory not in trusted_directories
        ]
        executable_paths = sorted({*executable_paths, *trusted_build_directories})
        report["stage"] = "canonical-challenge"
        canonical_olean, dependency_sources, trusted_lean_paths = compile_canonical_challenge(
            work,
            source,
            checkout=checkout,
            challenge_source=challenge_source,
            challenge_module=report["comparator"]["challenge_module"],
            lean=lean,
            lean_prefix=lean_prefix,
            allowlist=allowlist,
            environment=env,
            landrun=landrun,
            readable_paths=readable_paths,
            executable_paths=executable_paths,
            tools=tools,
        )
        canonical_root = (work / "canonical-challenge").resolve()
        readable_paths = sorted({*readable_paths, canonical_root})
        # The whole protected directory, so every module-system sidecar beside
        # the Challenge olean is covered, not just the olean itself.
        require_protected_paths([canonical_root, canonical_olean], candidate_writable)
        report["stage"] = "challenge-provenance"
        audit = audit_challenge_sources(
            source,
            checkout=checkout,
            dependency_sources=dependency_sources,
            lean_prefix=lean_prefix,
            allowlist=allowlist,
            writable_directories=candidate_writable,
        )
        report["project_dependencies"] = recorded_project_dependencies(
            source, checkout, packages
        )
        report["challenge"].update(
            {
                "transitive_source_count": audit["source_count"],
                "dependencies": audit["dependencies"],
                "trust_level": audit["trust_level"],
                "untrusted_sources": audit["untrusted_sources"],
                "canonical_olean_sha256": sha256(canonical_olean),
            }
        )
        if audit["untrusted_sources"]:
            report["status"] = "fail"
            report["stage"] = "challenge-provenance"
            error = VerificationError(
                "configured Challenge transitively imports sources outside the allowlist",
                code="challenge.untrusted_imports",
                next_action=(
                    "Remove the untrusted Challenge imports or move the trusted statement into "
                    "the submitted project, then make a new submission."
                ),
            )
            report["errors"].append(str(error))
            report_diagnostic(report, error)
            report["checked_at"] = now()
            guarded_write()
            return 0

        report["stage"] = "confinement-final"
        verify_sandbox_confinement(
            work / "landrun-write-denial-probe",
            work / "landrun-read-denial-probe",
            positive_read=challenge_source,
            python=python,
            touch=touch,
            cwd=source,
            environment=env,
            landrun=landrun,
            writable_directories=candidate_writable,
            protected_write_directories=sorted(trusted_build_directories),
            readable_paths=readable_paths,
            executable_paths=executable_paths,
            tools=tools,
        )

        report["stage"] = "candidate-configuration"
        env["LEAN_PATH"] = lake_environment_value(
            "LEAN_PATH",
            source=source,
            lake=lake,
            printenv=printenv,
            environment=env,
            landrun=landrun,
            writable_directories=candidate_writable,
            readable_paths=readable_paths,
            executable_paths=executable_paths,
            tools=tools,
            allowed_roots=[checkout, lean_prefix],
        )
        env["LEAN_SRC_PATH"] = lake_environment_value(
            "LEAN_SRC_PATH",
            source=source,
            lake=lake,
            printenv=printenv,
            environment=env,
            landrun=landrun,
            writable_directories=candidate_writable,
            readable_paths=readable_paths,
            executable_paths=executable_paths,
            tools=tools,
            allowed_roots=[checkout, lean_prefix],
        )
        env["LEAN_PATH"] = protected_lean_path(
            canonical_olean,
            trusted_lean_paths,
            env["LEAN_PATH"],
            protected_root=canonical_root,
        )
        report["stage"] = "comparator"
        proc = sandboxed_run(
            [str(comparator), str(comparator_config)],
            cwd=source,
            environment=env,
            landrun=landrun,
            writable_directories=candidate_writable,
            readable_paths=readable_paths,
            executable_paths=executable_paths,
            tools=tools,
            timeout=EXECUTION_BUDGET_SECONDS,
            check=False,
        )
        log = (proc.stdout + "\n" + proc.stderr).strip()
        report["comparator_log_tail"] = log[-20000:]
        if proc.returncode:
            error = comparator_failure(proc.returncode, log, canonical_root=canonical_root)
            report["status"] = "error" if error.owner == "palomar" else "fail"
            report["errors"].append(str(error))
            report_diagnostic(report, error, stage="comparator")
            report["stage"] = "comparator"
            guarded_write()
            return 0

        report["status"] = "pass"
        report["stage"] = "complete"
        report["checked_at"] = now()
        guarded_write()
    except (subprocess.TimeoutExpired, ResourceExhausted) as error:
        report["status"] = "error"
        report["stage"] = "resource-exhausted"
        report["error_kind"] = "infrastructure/resource-exhausted"
        report["retryable"] = True
        if isinstance(error, subprocess.TimeoutExpired):
            bounded = ResourceExhausted(
                "worker wall-clock capacity was exhausted; retry on a longer-running worker"
            )
        else:
            bounded = error
        report["errors"].append(str(bounded))
        report_diagnostic(report, bounded, stage="resource-exhausted")
        guarded_write()
    except Exception as error:  # noqa: BLE001 -- all verifier failures become a bounded report
        submitter_failure = (
            isinstance(error, VerificationError)
            and error.owner == "submitter"
            and str(report.get("stage") or "") not in PALOMAR_OWNED_STAGES
        )
        report["status"] = "fail" if submitter_failure else "error"
        report["errors"].append(str(error))
        report_diagnostic(report, error)
        guarded_write()
    finally:
        _EXECUTION_DEADLINE = previous_deadline
        _RESOURCE_METRICS_PATH = previous_metrics_path
        _RESOURCE_DISK_PATH = previous_disk_path
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    commands = result.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--event", required=True)
    prepare_parser.add_argument("--work-dir", required=True)
    prepare_parser.add_argument("--output", required=True)
    prepare_parser.add_argument("--licensee", required=True)
    prepare_parser.set_defaults(func=prepare)
    execute_parser = commands.add_parser("execute")
    execute_parser.add_argument("--work-dir", required=True)
    execute_parser.add_argument("--output", required=True)
    execute_parser.add_argument("--comparator", required=True)
    execute_parser.add_argument("--lean4export", required=True)
    execute_parser.add_argument("--landrun", required=True)
    execute_parser.add_argument("--nanoda", required=True)
    execute_parser.add_argument("--comparator-commit", required=True)
    execute_parser.add_argument("--landrun-commit", required=True)
    execute_parser.add_argument("--nanoda-commit", required=True)
    execute_parser.add_argument("--workflow-url", required=True)
    execute_parser.add_argument(
        "--execution-budget-seconds",
        type=int,
        default=EXECUTION_BUDGET_SECONDS,
        help="trusted worker wall-clock capacity (default: 12 hours)",
    )
    execute_parser.set_defaults(func=execute)
    return result


def main() -> int:
    args = parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
