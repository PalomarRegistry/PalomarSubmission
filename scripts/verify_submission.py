#!/usr/bin/env python3
"""Prepare and mechanically verify one issue-based Palomar submission."""

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
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = (
    "lean-toolchain",
    "lakefile.toml",
    "formalization.yaml",
    "Challenge.lean",
    "Solution.lean",
    "comparator.json",
)
MAX_SOURCE_BYTES = 500 * 1024 * 1024
MAX_CHALLENGE_BYTES = 100 * 1024
MAX_CHALLENGE_LINES = 1000
MAX_CONFIGURATION_BYTES = 1024 * 1024
STANDARD_AXIOMS = {"propext", "Quot.sound", "Classical.choice"}
COMPILED_ARTIFACT_SUFFIXES = {
    ".a",
    ".bc",
    ".dll",
    ".dylib",
    ".ilean",
    ".o",
    ".obj",
    ".olean",
    ".so",
    ".trace",
}
SECTION_KEYS = {
    "Repository URL": "repository_url",
    "Commit SHA": "commit_sha",
    "Existing Palomar ID (updates only)": "existing_id",
    "Additional context (optional)": "context",
}
GITHUB_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PALOMAR_ID_RE = re.compile(r"^PALOMAR-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}$")
IMPORT_RE = re.compile(r"^\s*(?:public\s+)?import\s+(.+?)\s*$")
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
    "PALOMAR_LANDRUN_REAL",
)


class VerificationError(RuntimeError):
    pass


class ResourceExhausted(VerificationError):
    """The available worker could not complete a verification phase."""


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
    timeout: int = 600,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    timeout = _deadline_timeout(timeout, command)
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
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


def parse_issue_body(body: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    matches = list(re.finditer(r"^###\s+(.+?)\s*$", body, re.MULTILINE))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        value = body[match.end() : end].strip()
        if value == "_No response_":
            value = ""
        key = SECTION_KEYS.get(match.group(1).strip())
        if key:
            if key in sections:
                raise VerificationError(f"duplicate recognized issue section: {match.group(1).strip()}")
            sections[key] = value
    return sections


def normalize_repository(url: str) -> tuple[str, str]:
    match = GITHUB_RE.fullmatch(url.strip())
    if not match:
        raise VerificationError("Repository URL must be https://github.com/owner/repo")
    owner = match.group("owner")
    repo = match.group("repo")
    if owner in {".", ".."} or repo in {".", ".."}:
        raise VerificationError("invalid GitHub repository path")
    return f"{owner}/{repo}", f"https://github.com/{owner}/{repo}"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def tree_size(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        if ".git" in path.parts or path.is_symlink() or not path.is_file():
            continue
        total += path.stat().st_size
        if total > MAX_SOURCE_BYTES:
            break
    return total


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


def direct_imports(text: str) -> list[str]:
    imports: list[str] = []
    for line in strip_lean_comments(text).splitlines():
        match = IMPORT_RE.match(line)
        if not match:
            continue
        imports.extend(token for token in match.group(1).split() if token)
    return sorted(set(imports))


def load_comparator_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "challenge_module",
        "solution_module",
        "theorem_names",
        "permitted_axioms",
        "enable_nanoda",
    }
    missing = required - config.keys()
    if missing:
        raise VerificationError(f"comparator.json missing: {', '.join(sorted(missing))}")
    allowed = required | {"definition_names"}
    unknown = config.keys() - allowed
    if unknown:
        raise VerificationError(f"comparator.json has unknown keys: {', '.join(sorted(unknown))}")
    if config["challenge_module"] != "Challenge" or config["solution_module"] != "Solution":
        raise VerificationError("comparator modules must be Challenge and Solution")
    theorem_names = config["theorem_names"]
    definition_names = config.get("definition_names", [])
    if not isinstance(theorem_names, list) or not theorem_names:
        raise VerificationError("comparator theorem_names must be a nonempty array")
    if not all(isinstance(item, str) and item for item in theorem_names + definition_names):
        raise VerificationError("comparator declaration names must be nonempty strings")
    axioms = config["permitted_axioms"]
    if not isinstance(axioms, list) or not set(axioms) <= STANDARD_AXIOMS:
        raise VerificationError("comparator permitted_axioms exceed Palomar's standard allowlist")
    if config["enable_nanoda"] is not False:
        raise VerificationError("enable_nanoda must be false in the v1 runner")
    return config


def prepare(args: argparse.Namespace) -> int:
    output = Path(args.output).resolve()
    work = Path(args.work_dir).resolve()
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "error",
        "stage": "intake",
        "checked_at": now(),
        "errors": [],
        "warnings": [],
    }
    try:
        event = json.loads(Path(args.event).read_text(encoding="utf-8"))
        issue = event["issue"]
        values = parse_issue_body(issue.get("body") or "")
        repository, url = normalize_repository(values.get("repository_url", ""))
        commit = values.get("commit_sha", "").strip().lower()
        if not SHA_RE.fullmatch(commit):
            raise VerificationError("Commit SHA must be 40 lowercase hexadecimal characters")
        existing_id = values.get("existing_id", "").strip().upper()
        if existing_id and not PALOMAR_ID_RE.fullmatch(existing_id):
            raise VerificationError(
                "Existing Palomar ID must have the form PALOMAR-2026-07-29-000123"
            )

        report.update(
            {
                "issue": {
                    "number": int(issue["number"]),
                    "url": issue["html_url"],
                    "submitter": issue["user"]["login"],
                },
                "source": {
                    "repository": repository,
                    "repository_url": url,
                    "commit": commit,
                    "tree_url": f"{url}/tree/{commit}",
                },
                "existing_id": existing_id or None,
            }
        )
        source = work / "source"
        clone_commit(url, commit, source)
        size = tree_size(source)
        if size > MAX_SOURCE_BYTES:
            raise VerificationError("checked-out source exceeds the 500 MiB cap")
        report["source"]["bytes"] = size

        for name in REQUIRED:
            path = source / name
            if path.is_symlink() or not path.is_file():
                raise VerificationError(f"required regular root file is missing: {name}")
        if (source / "lakefile.lean").exists():
            raise VerificationError("lakefile.lean is not supported; provide lakefile.toml")
        lakefile = source / "lakefile.toml"
        if lakefile.stat().st_size > MAX_CONFIGURATION_BYTES:
            raise VerificationError("lakefile.toml exceeds the 1 MiB hard cap")
        tomllib.loads(lakefile.read_text(encoding="utf-8"))

        toolchain = (source / "lean-toolchain").read_text(encoding="utf-8").strip()
        mapping = json.loads((ROOT / "toolchains.json").read_text(encoding="utf-8"))
        export_commit = mapping["lean4export"].get(toolchain)
        if not export_commit:
            raise VerificationError(f"unsupported Lean toolchain: {toolchain}")

        challenge = source / "Challenge.lean"
        challenge_bytes = challenge.stat().st_size
        if challenge_bytes > MAX_CHALLENGE_BYTES:
            raise VerificationError("Challenge.lean exceeds the 100 KiB hard cap")
        challenge_text = challenge.read_text(encoding="utf-8")
        challenge_lines = len(challenge_text.splitlines())
        if challenge_lines > MAX_CHALLENGE_LINES:
            raise VerificationError("Challenge.lean exceeds the 1,000-line hard cap")
        if challenge_bytes > 32 * 1024 or challenge_lines > 300:
            report["warnings"].append("Challenge.lean exceeds the preferred 32 KiB / 300-line review surface")

        config = load_comparator_config(source / "comparator.json")
        report.update(
            {
                "lean_toolchain": toolchain,
                "lean4export_commit": export_commit,
                "challenge": {
                    "path": "Challenge.lean",
                    "bytes": challenge_bytes,
                    "lines": challenge_lines,
                    "direct_imports": direct_imports(challenge_text),
                    "sha256": sha256(challenge),
                },
                "solution": {
                    "path": "Solution.lean",
                    "sha256": sha256(source / "Solution.lean"),
                },
                "formalization_sha256": sha256(source / "formalization.yaml"),
                "comparator_config_sha256": sha256(source / "comparator.json"),
                "comparator": {
                    "theorem_names": config["theorem_names"],
                    "definition_names": config.get("definition_names", []),
                    "permitted_axioms": config["permitted_axioms"],
                },
            }
        )
        write_json(work / "metadata.json", report)
        report["status"] = "pending"
        report["stage"] = "prepared"
        write_json(output, report)
        workflow_output(ready="true", lean4export_commit=export_commit, lean_toolchain=toolchain)
    except Exception as error:  # noqa: BLE001 -- all intake failures become a bounded report
        report["errors"].append(str(error))
        write_json(output, report)
        workflow_output(ready="false", lean4export_commit="", lean_toolchain="")
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
        if package_type == "git" and isinstance(url, str):
            repository = github_repository(url) or url
            revision = str(package.get("rev") or package.get("inputRev") or "")
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


def indexed_versions(database: Path) -> dict[tuple[str, str], dict[str, Any]]:
    """Return one stable, versioned certificate for every accepted source snapshot.

    Multiple Palomar records can legitimately cite the same repository commit.
    Selecting the earliest accepted record is deterministic and remains stable as
    later records are appended to the database.
    """
    candidates: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for path in (database / "entries").glob("*.json"):
        entry = json.loads(path.read_text(encoding="utf-8"))
        source = entry.get("source", {})
        repository = source.get("repository")
        commit = source.get("commit")
        identifier = entry.get("id")
        version = entry.get("version")
        accepted_at = entry.get("accepted_at")
        if (
            isinstance(repository, str)
            and isinstance(commit, str)
            and SHA_RE.fullmatch(commit)
            and isinstance(identifier, str)
            and PALOMAR_ID_RE.fullmatch(identifier)
            and isinstance(version, int)
            and not isinstance(version, bool)
            and version >= 1
            and isinstance(accepted_at, str)
        ):
            candidates.setdefault((repository.lower(), commit), []).append(
                {
                    "repository": repository,
                    "revision": commit,
                    "palomar_id": identifier,
                    "palomar_version": version,
                    "accepted_at": accepted_at,
                }
            )
    return {
        key: min(
            records,
            key=lambda item: (
                item["accepted_at"],
                item["palomar_id"],
                item["palomar_version"],
            ),
        )
        for key, records in candidates.items()
    }


def indexed_packages(
    source: Path,
    database: Path,
    packages: list[dict[str, str]],
) -> dict[str, dict[str, Any]]:
    """Bind materialized Git packages to exact versioned Palomar records."""
    indexed = indexed_versions(database)
    result: dict[str, dict[str, Any]] = {}
    for package in packages:
        repository = package["repository"]
        revision = package["revision"]
        record = indexed.get((repository.lower(), revision))
        if record is None:
            continue
        if package["url"].startswith("path:"):
            raise VerificationError(
                f"Palomar-indexed package {package['name']!r} may not use a path dependency"
            )
        checkout = package_checkout(source, package)
        git_env = os.environ.copy()
        git_env.update(
            {
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        head = run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(checkout),
                "rev-parse",
                "HEAD",
            ],
            env=git_env,
        ).stdout.strip()
        if head != revision:
            raise VerificationError(
                f"Palomar-indexed package {package['name']!r} checkout does not match its commit"
            )
        result[package["name"]] = record
    return result


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
                isinstance(revision, str) and SHA_RE.fullmatch(revision) for revision in accepted_revisions
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
        for root_package in matching:
            package_dir = source / ".lake" / "packages" / root_package["name"]
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


def package_checkout(source: Path, package: dict[str, str]) -> Path:
    """Return the already-materialized checkout for one manifest package."""
    if package["url"].startswith("path:"):
        return (source / package["url"].removeprefix("path:")).resolve()
    return (source / ".lake" / "packages" / package["name"]).resolve()


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


def package_lake_directories(source: Path, name: str) -> tuple[Path, Path]:
    package = next((item for item in manifest_packages(source) if item["name"] == name), None)
    if package is None:
        raise VerificationError(f"trusted package {name!r} is absent from the manifest")
    checkout = package_checkout(source, package)
    return (checkout / ".lake" / "build").resolve(), (checkout / ".lake" / "config").resolve()


def trusted_lake_directories(source: Path, names: Any) -> list[Path]:
    result: list[Path] = []
    for name in names:
        for directory in package_lake_directories(source, name):
            if not directory.is_dir():
                raise VerificationError(f"trusted package Lake directory is missing: {directory}")
            result.append(directory)
    return sorted(set(result))


def nested_package_links(
    source: Path,
    root_package: Path,
    *,
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
        target = package_checkout(source, actual)
        if not target.is_dir() or target.is_symlink():
            raise VerificationError(f"trusted root dependency is not a real checkout: {target}")
        (nested / name).symlink_to(target, target_is_directory=True)
    return nested.resolve()


def build_allowlisted_roots(
    source: Path,
    *,
    packages: list[dict[str, str]],
    allowlist: dict[str, tuple[str, str]],
    base_env: dict[str, str],
    lake: Path,
    landrun: Path,
    readable_paths: list[Path],
    executable_paths: list[Path],
    tools: dict[Path, str],
) -> None:
    """Build non-Mathlib trusted roots before hostile Lake configuration can run."""
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
        root_dir = package_checkout(source, root_package)
        closure = {
            root_package["name"],
            *(dependency["name"] for dependency in manifest_packages(root_dir)),
        }
        if not closure <= allowlist.keys() or not closure <= by_name.keys():
            raise VerificationError(f"trusted root {repository} has an incomplete closure")
        nested = nested_package_links(source, root_dir, allowed_names=closure)
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
        owned_names = {name for name, (owner, _level) in allowlist.items() if owner == repository}
        writable = validate_writable_directories(
            source,
            [
                *(directory for name in owned_names for directory in package_lake_directories(source, name)),
                nested,
            ],
        )
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


def build_indexed_roots(
    work: Path,
    source: Path,
    *,
    packages: list[dict[str, str]],
    indexed: dict[str, dict[str, Any]],
    allowlist: dict[str, tuple[str, str]],
    base_env: dict[str, str],
    lean: Path,
    lean_prefix: Path,
    landrun: Path,
    readable_paths: list[Path],
    executable_paths: list[Path],
    tools: dict[Path, str],
) -> tuple[Path | None, list[Path]]:
    """Compile the imported indexed source closure into verifier-owned output.

    Indexed Lake configuration is not a source-to-object authority: a qualified
    project may select another source directory, run elaborator code, or write a
    deceptive build artifact. Resolve imported modules to unique tracked source
    files, follow their imports, and invoke trusted Lean directly. Only the
    verifier-owned output returned here enters the canonical Challenge path.
    """
    by_name = {package["name"]: package for package in packages}
    indexed_directories = {
        name: package_checkout(source, by_name[name]) for name in sorted(indexed)
    }
    for name, directory in indexed_directories.items():
        if not directory.is_dir() or not source_matches_checkout(
            directory / "lake-manifest.json", directory
        ):
            raise VerificationError(
                f"Palomar-indexed package {name!r} lacks its tracked pinned manifest"
            )

    output = work / "indexed-olean"
    scratch = work / "indexed-compile-scratch"
    for directory in (output, scratch):
        if directory.exists() or directory.is_symlink():
            raise VerificationError(f"indexed compilation path is not fresh: {directory}")
        directory.mkdir()
    home = scratch / "home"
    temporary = scratch / "tmp"
    home.mkdir()
    temporary.mkdir()

    allowlisted_paths: list[Path] = []
    for name in sorted(allowlist):
        path = package_lake_directories(source, name)[0] / "lib" / "lean"
        if path.is_dir():
            allowlisted_paths.append(path.resolve())
    core_path = (lean_prefix / "lib" / "lean").resolve()
    trusted_lean_paths = [
        *([core_path] if core_path.is_dir() else []),
        *allowlisted_paths,
    ]
    compile_env = base_env.copy()
    compile_env.update(
        {
            "HOME": str(home.resolve()),
            "TMPDIR": str(temporary.resolve()),
            "LEAN_ABORT_ON_PANIC": "1",
            "LEAN_PATH": os.pathsep.join(
                str(path) for path in [*trusted_lean_paths, output.resolve()]
            ),
        }
    )

    def module_suffix(module: str) -> pathlib.PurePosixPath:
        parts = module.split(".")
        if (
            not parts
            or any(not part or part in {".", ".."} or "/" in part or "\\" in part for part in parts)
        ):
            raise VerificationError(f"unsafe imported Lean module name: {module!r}")
        return pathlib.PurePosixPath(*parts).with_suffix(".lean")

    resolved_sources: dict[str, tuple[Path, Path] | None] = {}

    def source_for(module: str) -> tuple[Path, Path] | None:
        if module in resolved_sources:
            return resolved_sources[module]
        suffix = module_suffix(module)
        matches: list[tuple[Path, Path]] = []
        for package_dir in indexed_directories.values():
            for candidate in package_dir.rglob(suffix.name):
                relative = candidate.relative_to(package_dir)
                if relative.parts and relative.parts[0] in {".git", ".lake"}:
                    continue
                if tuple(relative.parts[-len(suffix.parts) :]) != suffix.parts:
                    continue
                if (
                    candidate.is_symlink()
                    or not candidate.is_file()
                    or not source_matches_checkout(candidate, package_dir)
                ):
                    raise VerificationError(
                        f"indexed module {module!r} is not a tracked source file"
                    )
                root = candidate.resolve()
                for _part in suffix.parts:
                    root = root.parent
                matches.append((candidate.resolve(), root))
        if len(matches) > 1:
            raise VerificationError(f"indexed module {module!r} resolves ambiguously")
        if matches and any(
            (root / suffix).with_suffix(".olean").is_file()
            for root in trusted_lean_paths
        ):
            raise VerificationError(
                f"indexed module {module!r} shadows a Lean core or allowlisted module"
            )
        resolved_sources[module] = matches[0] if matches else None
        return resolved_sources[module]

    compiled: dict[str, Path] = {}
    visiting: set[str] = set()
    source_roots: set[Path] = set()

    def compile_module(module: str) -> None:
        if module in compiled:
            return
        resolved = source_for(module)
        if resolved is None:
            return  # Lean core or an independently built allowlisted module.
        if module in visiting:
            raise VerificationError(f"indexed module import cycle reaches {module!r}")
        visiting.add(module)
        source_file, source_root = resolved
        for imported in direct_imports(source_file.read_text(encoding="utf-8")):
            compile_module(imported)
        visiting.remove(module)
        target = output.joinpath(*module.split(".")).with_suffix(".olean")
        if target.exists() or target.is_symlink():
            raise VerificationError(f"indexed compiler output was pre-created: {target}")
        module_work = scratch / "modules" / hashlib.sha256(module.encode()).hexdigest()
        if module_work.exists() or module_work.is_symlink():
            raise VerificationError(f"indexed module work path was pre-created: {module_work}")
        untrusted_target = module_work.joinpath(*module.split(".")).with_suffix(".olean")
        untrusted_target.parent.mkdir(parents=True)
        sandboxed_run(
            [str(lean), "-o", str(untrusted_target), str(source_file)],
            cwd=source_file.parent,
            environment=compile_env,
            landrun=landrun,
            writable_directories=[home.resolve(), temporary.resolve(), module_work.resolve()],
            readable_paths=[*readable_paths, output.resolve()],
            executable_paths=[*executable_paths, *trusted_lean_paths],
            tools=tools,
            timeout=EXECUTION_BUDGET_SECONDS,
        )
        if untrusted_target.is_symlink() or not untrusted_target.is_file():
            raise VerificationError(f"trusted indexed compilation produced no module: {module}")
        actual = {
            path.resolve()
            for path in module_work.rglob("*")
            if path.is_file() or path.is_symlink()
        }
        if actual != {untrusted_target.resolve()}:
            raise VerificationError("indexed elaboration wrote unexpected protected output")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(untrusted_target, target)
        if target.is_symlink() or not target.is_file():
            raise VerificationError(f"protected indexed module is not regular: {module}")
        compiled[module] = target.resolve()
        source_roots.add(source_root)

    for imported in direct_imports((source / "Challenge.lean").read_text(encoding="utf-8")):
        compile_module(imported)
    if not compiled:
        shutil.rmtree(output)
        return None, []
    tools.update({path: sha256(path) for path in compiled.values()})
    return output.resolve(), sorted(source_roots)


def source_package(path: Path, source: Path) -> str | None:
    packages = (source / ".lake" / "packages").resolve()
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
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if not relative.parts or relative.parts[0] in {".git", ".lake"}:
            continue
        if path.suffix.lower() in COMPILED_ARTIFACT_SUFFIXES:
            raise VerificationError(
                f"committed build artifact is not permitted outside fresh .lake state: {path}"
            )


def validate_writable_directories(source: Path, directories: list[Path]) -> list[Path]:
    root = source.resolve()
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
    allowed_probe = writable_directories[0] / ".palomar-landrun-write-probe"
    if allowed_probe.exists():
        raise VerificationError(f"filesystem confinement probe path already exists: {allowed_probe}")
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
    if allowed_created:
        allowed_probe.unlink()
    if allowed.returncode or not allowed_created:
        read_probe.unlink(missing_ok=True)
        raise VerificationError("outer sandbox did not permit its writable directory" + detail(allowed))

    nested_probe = writable_directories[0] / ".palomar-nested-landrun-probe"
    if nested_probe.exists():
        read_probe.unlink(missing_ok=True)
        raise VerificationError(f"nested confinement probe path already exists: {nested_probe}")
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
    if nested_created:
        nested_probe.unlink()
    if nested.returncode or not nested_created:
        read_probe.unlink(missing_ok=True)
        raise VerificationError("nested Landrun confinement is unavailable" + detail(nested))

    denied = sandboxed_run(
        [str(touch), str(write_probe)],
        cwd=cwd,
        environment=environment,
        landrun=landrun,
        writable_directories=writable_directories,
        readable_paths=readable_paths,
        executable_paths=executable_paths,
        tools=tools,
        check=False,
    )
    escaped = write_probe.exists()
    if escaped:
        write_probe.unlink()
    if escaped or denied.returncode == 0:
        read_probe.unlink(missing_ok=True)
        raise VerificationError("outer sandbox write policy was not enforced" + detail(denied))

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


def verify_filesystem_confinement(
    probe: Path,
    *,
    touch: Path,
    cwd: Path,
    environment: dict[str, str],
    landrun: Path,
    writable_directories: list[Path],
    executable_paths: list[Path],
    tools: dict[Path, str],
) -> None:
    """Exercise the renderer's narrower write-only confinement contract."""

    def failure_detail(proc: subprocess.CompletedProcess[str]) -> str:
        detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")
        if len(detail) > 500:
            detail = f"{detail[:497]}..."
        suffix = f" (exit {proc.returncode}"
        if detail:
            suffix += f": {detail}"
        return f"{suffix})"

    require_protected_paths([probe], writable_directories)
    if probe.exists():
        raise VerificationError(f"filesystem confinement probe path already exists: {probe}")
    allowed_probe = writable_directories[0] / ".palomar-landrun-write-probe"
    if allowed_probe.exists():
        raise VerificationError(f"filesystem confinement probe path already exists: {allowed_probe}")
    allowed = sandboxed_run(
        [str(touch), str(allowed_probe)],
        cwd=cwd,
        environment=environment,
        landrun=landrun,
        writable_directories=writable_directories,
        executable_paths=executable_paths,
        tools=tools,
        check=False,
    )
    allowed_created = allowed_probe.is_file()
    if allowed_created:
        allowed_probe.unlink()
    if allowed.returncode or not allowed_created:
        raise VerificationError(
            "outer Landrun did not permit its writable directory" + failure_detail(allowed)
        )

    denied = sandboxed_run(
        [str(touch), str(probe)],
        cwd=cwd,
        environment=environment,
        landrun=landrun,
        writable_directories=writable_directories,
        executable_paths=executable_paths,
        tools=tools,
        check=False,
    )
    escaped = probe.exists()
    if escaped:
        probe.unlink()
    if escaped or denied.returncode == 0:
        raise VerificationError(
            "outer Landrun filesystem policy was not enforced" + failure_detail(denied)
        )


def materialize_packages(source: Path, *, base_env: dict[str, str]) -> list[Path]:
    """Materialize the exact Git revisions in the submitted Lake manifest.

    This deliberately does not use ``lake update``: Lake runs package
    post-update hooks, which are unnecessary for fetching and expand the
    pre-verification execution surface.
    """
    root_build, root_config = remove_untrusted_lake_state(source)
    dot_lake = source / ".lake"
    packages_dir = dot_lake / "packages"
    packages_dir.mkdir()
    git_env = git_environment(source, base_env)
    Path(git_env["HOME"]).mkdir(parents=True)
    writable = [root_build, root_config]
    for package in manifest_packages(source):
        name = package["name"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise VerificationError(f"unsafe package name in Lake manifest: {name!r}")
        repository_url = package["url"]
        if repository_url.startswith("path:"):
            relative = Path(repository_url.removeprefix("path:"))
            if relative.is_absolute():
                raise VerificationError(f"path package {name!r} must be relative to the source root")
            package_dir = (source / relative).resolve()
            lake_root = (source / ".lake").resolve()
            try:
                package_dir.relative_to(source.resolve())
            except ValueError as error:
                raise VerificationError(f"path package {name!r} escapes the source tree") from error
            if package_dir == source.resolve() or not package_dir.is_dir():
                raise VerificationError(f"path package {name!r} is not a distinct source directory")
            if package_dir == lake_root or lake_root in package_dir.parents:
                raise VerificationError(f"path package {name!r} may not live under .lake")
            writable.extend(remove_untrusted_lake_state(package_dir))
            reject_committed_build_artifacts(package_dir)
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
        if not SHA_RE.fullmatch(revision):
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
        run([*git, "checkout", "--quiet", "--detach", revision], env=git_env)
        writable.extend(remove_untrusted_lake_state(package_dir))
    reject_committed_build_artifacts(source)
    return validate_writable_directories(source, writable)


def reject_untrusted_package_artifacts(
    source: Path,
    packages: list[dict[str, str]],
    allowlist: dict[str, tuple[str, str]],
) -> None:
    """Scan candidate-controlled packages after official closure verification."""
    for package in packages:
        if package["name"] not in allowlist:
            reject_committed_build_artifacts(package_checkout(source, package))


def get_mathlib_cache(
    source: Path,
    *,
    base_env: dict[str, str],
    allowlist: dict[str, tuple[str, str]],
    lake: Path,
    landrun: Path,
    readable_paths: list[Path],
    executable_paths: list[Path],
    tools: dict[Path, str],
    resource_properties: tuple[str, ...] = (),
) -> None:
    """Run the cache client only from a clean, pinned official Mathlib checkout."""
    packages = manifest_packages(source)
    mathlib = next(
        (
            package
            for package in packages
            if str(package["repository"]).lower() == "leanprover-community/mathlib4"
        ),
        None,
    )
    if mathlib is None:
        return
    package_dir = source / ".lake" / "packages" / mathlib["name"]
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
    nested_packages = nested_package_links(source, package_dir, allowed_names=closure)
    trusted_directories = [
        directory for name in closure for directory in package_lake_directories(source, name)
    ]
    cache_writable = validate_writable_directories(source, [*trusted_directories, nested_packages])
    replay_writable_files: list[Path] = []
    proofwidgets = next((package for package in packages if package["name"] == "proofwidgets"), None)
    if proofwidgets and proofwidgets["name"] in closure:
        # ProofWidgets' official Lake target records this generated replay
        # marker beside the pinned lockfile. Grant only this exact file; a
        # qualified root must never write its source tree or compiled output.
        lock_hash = package_checkout(source, proofwidgets) / "widget" / "package-lock.json.hash"
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
        sandboxed_run(
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


def lean_source_dependencies(
    source: Path,
    *,
    lean: Path,
    environment: dict[str, str],
    landrun: Path,
    writable_directories: list[Path],
    readable_paths: list[Path],
    executable_paths: list[Path],
    tools: dict[Path, str],
) -> list[Path]:
    proc = sandboxed_run(
        [str(lean), "--src-deps", "Challenge.lean"],
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
    database: Path,
    dependency_sources: list[Path],
    lean_prefix: Path,
    allowlist: dict[str, tuple[str, str]],
    indexed: dict[str, dict[str, Any]],
    writable_directories: list[Path],
) -> dict[str, Any]:
    packages = manifest_packages(source)
    by_name = {package["name"]: package for package in packages}
    untrusted: list[str] = []
    dependencies: dict[tuple[str, str, str | None, int | None, str | None], None] = {}
    review_source_files: list[dict[str, Any]] = []
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
        package_name = source_package(resolved, source)
        if package_name is None:
            # Any candidate-local helper import expands the unaudited statement
            # surface and is forbidden in v1.
            untrusted.append(str(resolved))
            continue
        package = by_name.get(package_name)
        if not package:
            untrusted.append(str(resolved))
            continue
        package_dir = source / ".lake" / "packages" / package_name
        if not source_matches_checkout(resolved, package_dir):
            untrusted.append(str(resolved))
            continue
        if package_name in allowlist:
            repository, level = allowlist[package_name]
            dependencies[(repository, "allowlisted", None, None, None)] = None
            qualified_allowlisted = qualified_allowlisted or level == "qualified"
            continue
        record = indexed.get(package_name)
        if record:
            repository = str(record["repository"])
            revision = str(record["revision"])
            palomar_id = str(record["palomar_id"])
            palomar_version = int(record["palomar_version"])
            if (
                package["repository"].lower() != repository.lower()
                or package["revision"] != revision
            ):
                untrusted.append(str(resolved))
                continue
            dependencies[
                (repository, "palomar-indexed", palomar_id, palomar_version, revision)
            ] = None
            relative = resolved.relative_to(package_dir.resolve()).as_posix()
            review_source_files.append(
                {
                    "repository": repository,
                    "revision": revision,
                    "palomar_id": palomar_id,
                    "palomar_version": palomar_version,
                    "path": relative,
                    "sha256": sha256(resolved),
                }
            )
            continue
        untrusted.append(str(resolved))

    serialized_dependencies = [
        {
            "repository": repository,
            "provenance": provenance,
            **({"palomar_id": palomar_id} if palomar_id else {}),
            **({"palomar_version": palomar_version} if palomar_version else {}),
            **({"revision": revision} if revision else {}),
        }
        for repository, provenance, palomar_id, palomar_version, revision in sorted(dependencies)
    ]
    qualified = qualified_allowlisted or any(
        item["provenance"] == "palomar-indexed" for item in serialized_dependencies
    )
    return {
        "source_count": len(dependency_sources),
        "dependencies": serialized_dependencies,
        "untrusted_sources": untrusted[:100],
        "trust_level": "qualified" if qualified else "high",
        "review_source_files": sorted(
            review_source_files,
            key=lambda item: (item["repository"].lower(), item["revision"], item["path"]),
        ),
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


def protected_lean_path(
    canonical_olean: Path,
    trusted_lean_paths: list[Path],
    candidate_lean_path: str,
) -> str:
    """Resolve protected statement imports before any candidate build output."""
    candidate_paths = [Path(value) for value in candidate_lean_path.split(os.pathsep) if value]
    ordered = [canonical_olean.parent, *trusted_lean_paths, *candidate_paths]
    return os.pathsep.join(str(path) for path in dict.fromkeys(ordered))


def compile_canonical_challenge(
    work: Path,
    source: Path,
    *,
    lean: Path,
    lean_prefix: Path,
    allowlist: dict[str, tuple[str, str]],
    indexed_lean_path: Path | None,
    indexed_source_roots: list[Path],
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

    packages_by_name = {package["name"]: package for package in manifest_packages(source)}
    trusted_names = sorted(
        allowlist,
        key=lambda name: (allowlist[name][1] != "high", name.lower()),
    )
    trusted_packages = {name: package_checkout(source, packages_by_name[name]) for name in trusted_names}
    lean_paths: list[Path] = []
    core_path = lean_prefix / "lib" / "lean"
    if core_path.is_dir():
        lean_paths.append(core_path.resolve())
    for package_dir in trusted_packages.values():
        path = package_dir / ".lake" / "build" / "lib" / "lean"
        if path.is_dir():
            lean_paths.append(path.resolve())
    if indexed_lean_path is not None:
        lean_paths.append(indexed_lean_path.resolve())
    lean_paths = list(dict.fromkeys(lean_paths))
    canonical_readable_paths = list(readable_paths)
    if indexed_lean_path is not None:
        canonical_readable_paths.append(indexed_lean_path.resolve())
    source_paths = [
        source.resolve(),
        *trusted_packages.values(),
        *indexed_source_roots,
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
    compiled_olean = scratch / "Challenge.olean"
    sandboxed_run(
        [str(lean), "-o", str(compiled_olean), "Challenge.lean"],
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
    compiled_olean = compiled_olean.resolve()
    tools[compiled_olean] = sha256(compiled_olean)
    dependencies = lean_source_dependencies(
        source,
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
    canonical_olean = output_dir / "Challenge.olean"
    shutil.copyfile(compiled_olean, canonical_olean)
    if canonical_olean.is_symlink() or not canonical_olean.is_file():
        raise VerificationError("protected Challenge module is not a regular file")
    if [path.name for path in output_dir.iterdir()] != ["Challenge.olean"]:
        raise VerificationError("protected Challenge directory contains unexpected files")
    canonical_olean = canonical_olean.resolve()
    tools[canonical_olean] = sha256(canonical_olean)
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
    source = work / "source"
    metrics_path = work / "resource-metrics.jsonl"
    report.update(
        {
            "status": "error",
            "stage": "setup",
            "comparator_commit": args.comparator_commit,
            "landrun_commit": args.landrun_commit,
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
        write_json(output, report)

    try:
        if metrics_path.is_symlink() or (metrics_path.exists() and not metrics_path.is_file()):
            raise VerificationError("resource metrics path is not a regular file")
        metrics_path.unlink(missing_ok=True)
        _RESOURCE_METRICS_PATH = metrics_path
        _RESOURCE_DISK_PATH = source
        install_execution_deadline(
            os.environ.get("PALOMAR_JOB_STARTED_AT"),
            getattr(args, "execution_budget_seconds", EXECUTION_BUDGET_SECONDS),
        )
        comparator = Path(args.comparator).resolve()
        lean4export = Path(args.lean4export).resolve()
        landrun = Path(args.landrun).resolve()
        adapter = (ROOT / "scripts" / "landrun_passthrough.py").resolve()
        metrics_wrapper = (ROOT / "scripts" / "measure_resources.py").resolve()
        verifier = Path(__file__).resolve()
        for tool in (comparator, lean4export, landrun, adapter, metrics_wrapper, verifier):
            if not tool.is_file():
                raise VerificationError(f"missing verifier tool: {tool}")
        env = os.environ.copy()
        env.pop("LAKE_PKG_URL_MAP", None)
        env.update(
            {
                "COMPARATOR_LANDRUN": str(adapter),
                "COMPARATOR_LEAN4EXPORT": str(lean4export),
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

        writable_directories = materialize_packages(source, base_env=env)
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
        readable_paths = sorted({source.resolve(), *system_readable_paths()})
        require_protected_paths(
            [
                output,
                comparator,
                lean4export,
                landrun,
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
            positive_read=source / "Challenge.lean",
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
        report["stage"] = "dependency-provenance"
        packages = manifest_packages(source)
        allowlist = package_allowlist(source, packages, base_env=env)
        reject_untrusted_package_artifacts(source, packages, allowlist)
        report["stage"] = "trusted-cache"
        get_mathlib_cache(
            source,
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
            packages=packages,
            allowlist=allowlist,
            base_env=env,
            lake=lake,
            landrun=landrun,
            readable_paths=readable_paths,
            executable_paths=executable_paths,
            tools=tools,
        )

        database = Path(args.database).resolve()
        indexed = {
            name: record
            for name, record in indexed_packages(source, database, packages).items()
            if name not in allowlist
        }
        report["stage"] = "indexed-roots"
        indexed_lean_path, indexed_source_roots = build_indexed_roots(
            work,
            source,
            packages=packages,
            indexed=indexed,
            allowlist=allowlist,
            base_env=env,
            lean=lean,
            lean_prefix=lean_prefix,
            landrun=landrun,
            readable_paths=readable_paths,
            executable_paths=executable_paths,
            tools=tools,
        )

        trusted_names = set(allowlist)
        trusted_directories = set(trusted_lake_directories(source, trusted_names))
        trusted_build_directories = {
            package_lake_directories(source, name)[0] for name in trusted_names
        }
        candidate_writable = [
            directory for directory in writable_directories if directory not in trusted_directories
        ]
        if indexed_lean_path is not None:
            require_protected_paths([indexed_lean_path], candidate_writable)
        executable_paths = sorted(
            {
                *executable_paths,
                *trusted_build_directories,
                *([indexed_lean_path] if indexed_lean_path is not None else []),
            }
        )
        report["stage"] = "canonical-challenge"
        canonical_olean, dependency_sources, trusted_lean_paths = compile_canonical_challenge(
            work,
            source,
            lean=lean,
            lean_prefix=lean_prefix,
            allowlist=allowlist,
            indexed_lean_path=indexed_lean_path,
            indexed_source_roots=indexed_source_roots,
            environment=env,
            landrun=landrun,
            readable_paths=readable_paths,
            executable_paths=executable_paths,
            tools=tools,
        )
        readable_paths = sorted({*readable_paths, canonical_olean.parent})
        require_protected_paths([canonical_olean], candidate_writable)
        report["stage"] = "challenge-provenance"
        audit = audit_challenge_sources(
            source,
            database=database,
            dependency_sources=dependency_sources,
            lean_prefix=lean_prefix,
            allowlist=allowlist,
            indexed=indexed,
            writable_directories=candidate_writable,
        )
        report["project_dependencies"] = packages
        report["challenge"].update(
            {
                "transitive_source_count": audit["source_count"],
                "dependencies": audit["dependencies"],
                "trust_level": audit["trust_level"],
                "untrusted_sources": audit["untrusted_sources"],
                "canonical_olean_sha256": sha256(canonical_olean),
                "review_source_files": audit["review_source_files"],
            }
        )
        if audit["untrusted_sources"]:
            report["status"] = "fail"
            report["stage"] = "challenge-provenance"
            report["errors"].append(
                "Challenge.lean transitively imports sources outside the allowlist or Palomar"
            )
            report["checked_at"] = now()
            guarded_write()
            return 0

        report["stage"] = "confinement-final"
        verify_sandbox_confinement(
            work / "landrun-write-denial-probe",
            work / "landrun-read-denial-probe",
            positive_read=source / "Challenge.lean",
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
            allowed_roots=[source, lean_prefix],
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
            allowed_roots=[source, lean_prefix],
        )
        env["LEAN_PATH"] = protected_lean_path(
            canonical_olean,
            trusted_lean_paths,
            env["LEAN_PATH"],
        )
        report["stage"] = "comparator"
        proc = sandboxed_run(
            [str(comparator), "comparator.json"],
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
            if "landrun adapter:" in log:
                report["status"] = "error"
                report["errors"].append("Comparator sandbox adapter failed")
            else:
                report["status"] = "fail"
                report["errors"].append(
                    f"Comparator rejected the project (exit {proc.returncode})"
                )
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
            report["errors"].append(
                "worker wall-clock capacity was exhausted; retry on a longer-running worker"
            )
        else:
            report["errors"].append(str(error))
        guarded_write()
    except Exception as error:  # noqa: BLE001 -- all verifier failures become a bounded report
        report["status"] = "error"
        report["errors"].append(str(error))
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
    prepare_parser.set_defaults(func=prepare)
    execute_parser = commands.add_parser("execute")
    execute_parser.add_argument("--work-dir", required=True)
    execute_parser.add_argument("--output", required=True)
    execute_parser.add_argument("--database", required=True)
    execute_parser.add_argument("--comparator", required=True)
    execute_parser.add_argument("--lean4export", required=True)
    execute_parser.add_argument("--landrun", required=True)
    execute_parser.add_argument("--comparator-commit", required=True)
    execute_parser.add_argument("--landrun-commit", required=True)
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
