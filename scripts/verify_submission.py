#!/usr/bin/env python3
"""Prepare and mechanically verify one issue-based Palomar submission."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
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
STANDARD_AXIOMS = {"propext", "Quot.sound", "Classical.choice"}
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
PALOMAR_ID_RE = re.compile(r"^PALOMAR-[0-9]{6}$")
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
    "COMPARATOR_LANDRUN",
    "COMPARATOR_LEAN4EXPORT",
    "PALOMAR_LANDRUN_REAL",
)


class VerificationError(RuntimeError):
    pass


MAX_CAPTURE_BYTES = 8 * 1024 * 1024


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


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 600,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
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
            raise VerificationError("Existing Palomar ID must have the form PALOMAR-000123")

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
        tomllib.loads((source / "lakefile.toml").read_text(encoding="utf-8"))

        toolchain = (source / "lean-toolchain").read_text(encoding="utf-8").strip()
        mapping = json.loads((ROOT / "toolchains.json").read_text(encoding="utf-8"))
        export_commit = mapping["lean4export"].get(toolchain)
        if not export_commit:
            raise VerificationError(f"unsupported Lean toolchain: {toolchain}")

        challenge = source / "Challenge.lean"
        challenge_bytes = challenge.stat().st_size
        challenge_text = challenge.read_text(encoding="utf-8")
        challenge_lines = len(challenge_text.splitlines())
        if challenge_bytes > MAX_CHALLENGE_BYTES:
            raise VerificationError("Challenge.lean exceeds the 100 KiB hard cap")
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


def indexed_versions(database: Path) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for path in (database / "entries").glob("*.json"):
        entry = json.loads(path.read_text(encoding="utf-8"))
        source = entry.get("source", {})
        repository = source.get("repository")
        commit = source.get("commit")
        if repository and commit:
            result[(repository.lower(), commit)] = entry["id"]
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
        trust_level = root.get("trust_level")
        repository_aliases = root.get("repository_aliases", [])
        if (
            not isinstance(repository, str)
            or github_repository(f"https://github.com/{repository}") != repository
            or not isinstance(official_ref, str)
            or not OFFICIAL_REF_RE.fullmatch(official_ref)
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
    git_env: dict[str, str],
) -> None:
    """Require a package commit to occur in the canonical repository's official history."""
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
        timeout=1800,
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


def require_protected_paths(
    protected_paths: list[Path], writable_directories: list[Path]
) -> None:
    for protected in protected_paths:
        resolved = protected.resolve()
        for writable in writable_directories:
            try:
                resolved.relative_to(writable)
            except ValueError:
                continue
            raise VerificationError(f"protected verifier path is sandbox-writable: {protected}")


def landrun_command(
    command: list[str],
    *,
    landrun: Path,
    writable_directories: list[Path],
    executable_paths: list[Path],
    environment: dict[str, str],
    writable_files: tuple[Path, ...] = (),
    readable_directories: tuple[Path, ...] = (),
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
        if name in environment:
            result.extend(["--env", name])
    for directory in sorted(set(readable_directories)):
        result.extend(["--ro", str(directory)])
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
    unrestricted_network: bool = False,
    resource_properties: tuple[str, ...] = (),
) -> list[str]:
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
        f"--working-directory={cwd}",
    ]
    if not unrestricted_network:
        common.append("--property=PrivateNetwork=yes")
    for property_value in resource_properties:
        if not property_value or "\n" in property_value or "\r" in property_value:
            raise VerificationError("invalid systemd resource property")
        common.append(f"--property={property_value}")
    systemctl = shutil.which("systemctl")
    user_manager = (
        systemctl is not None
        and subprocess.run(
            [systemctl, "--user", "show-environment"],
            text=True,
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )
    if user_manager:
        result = [runner, "--user", *common]
    else:
        sudo = shutil.which("sudo")
        if not sudo or run([sudo, "-n", "true"], check=False).returncode:
            raise VerificationError(
                "neither a user systemd manager nor passwordless sudo is available for confinement"
            )
        result = [
            sudo,
            "-n",
            runner,
            *common,
            f"--uid={os.getuid()}",
            f"--gid={os.getgid()}",
        ]
    for name in SANDBOX_ENVIRONMENT:
        value = environment.get(name)
        if value is not None:
            result.append(f"--setenv={name}={value}")
    return result + command


def sandboxed_run(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    landrun: Path,
    writable_directories: list[Path],
    executable_paths: list[Path],
    tools: dict[Path, str],
    writable_files: tuple[Path, ...] = (),
    timeout: int = 600,
    check: bool = True,
    unrestricted_network: bool = False,
    resource_properties: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    verify_tool_snapshot(tools)
    confined = landrun_command(
        command,
        landrun=landrun,
        writable_directories=writable_directories,
        writable_files=writable_files,
        executable_paths=executable_paths,
        environment=environment,
        readable_directories=[cwd],
        unrestricted_network=unrestricted_network,
    )
    proc = run(
        systemd_command(
            confined,
            cwd=cwd,
            environment=environment,
            unrestricted_network=unrestricted_network,
            resource_properties=resource_properties,
        ),
        cwd=cwd,
        env=environment,
        timeout=timeout,
        check=check,
    )
    verify_tool_snapshot(tools)
    return proc


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
            timeout=1800,
        )
        run([*git, "checkout", "--quiet", "--detach", revision], env=git_env)
        writable.extend(remove_untrusted_lake_state(package_dir))
    return validate_writable_directories(source, writable)


def get_mathlib_cache(
    source: Path,
    *,
    base_env: dict[str, str],
    lake: Path,
    landrun: Path,
    writable_directories: list[Path],
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
        ]
    ).stdout.strip()
    if head != mathlib["revision"]:
        raise VerificationError("Mathlib checkout does not match lake-manifest.json")
    origin = run(["git", "-C", str(package_dir), "remote", "get-url", "origin"]).stdout.strip()
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
        ]
    ).stdout.strip()
    if changes:
        raise VerificationError("Mathlib source was modified while configuring the workspace")

    # Run Mathlib as the workspace root so candidate Lake configuration never
    # executes during the network-enabled phase. Its official pinned closure is
    # materialized in a cache-only nested directory that is deleted immediately
    # afterward and is never visible to the candidate workspace.
    nested_packages = package_dir / ".lake" / "packages"
    if nested_packages.exists() or nested_packages.is_symlink():
        raise VerificationError("Mathlib cache package directory was not freshly prepared")
    nested_packages.mkdir()
    cache_writable = validate_writable_directories(
        source, [*writable_directories, nested_packages.resolve()]
    )
    cache_env = base_env.copy()
    cache_env["LEAN_ABORT_ON_PANIC"] = "1"
    try:
        sandboxed_run(
            [str(lake), "exe", "cache", "get"],
            cwd=package_dir,
            environment=cache_env,
            landrun=landrun,
            writable_directories=cache_writable,
            executable_paths=executable_paths,
            tools=tools,
            timeout=1800,
            unrestricted_network=True,
            resource_properties=resource_properties,
        )
    finally:
        if nested_packages.is_symlink():
            nested_packages.unlink()
        elif nested_packages.is_dir():
            shutil.rmtree(nested_packages)


def path_is_within(path: Path, directories: list[Path]) -> bool:
    for directory in directories:
        try:
            path.resolve().relative_to(directory.resolve())
        except ValueError:
            continue
        return True
    return False


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
    expected = run([*git, "rev-parse", f"HEAD:{relative}"], check=False)
    actual = run([*git, "hash-object", "--", str(path.resolve())], check=False)
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
    executable_paths: list[Path],
    tools: dict[Path, str],
) -> list[Path]:
    proc = sandboxed_run(
        [str(lean), "--src-deps", "Challenge.lean"],
        cwd=source,
        environment=environment,
        landrun=landrun,
        writable_directories=writable_directories,
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
    writable_directories: list[Path],
) -> dict[str, Any]:
    packages = manifest_packages(source)
    by_name = {package["name"]: package for package in packages}
    indexed = indexed_versions(database)
    untrusted: list[str] = []
    dependencies: dict[tuple[str, str, str | None], None] = {}
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
            dependencies[(repository, "allowlisted", None)] = None
            qualified_allowlisted = qualified_allowlisted or level == "qualified"
            continue
        repository = str(package["repository"])
        revision = package["revision"]
        palomar_id = indexed.get((repository.lower(), revision))
        if palomar_id:
            dependencies[(repository, "palomar-indexed", palomar_id)] = None
            continue
        untrusted.append(str(resolved))

    serialized_dependencies = [
        {
            "repository": repository,
            "provenance": provenance,
            **({"palomar_id": palomar_id} if palomar_id else {}),
        }
        for repository, provenance, palomar_id in sorted(dependencies)
    ]
    qualified = qualified_allowlisted or any(
        item["provenance"] == "palomar-indexed" for item in serialized_dependencies
    )
    return {
        "source_count": len(dependency_sources),
        "dependencies": serialized_dependencies,
        "untrusted_sources": untrusted[:100],
        "trust_level": "qualified" if qualified else "high",
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
    executable_paths: list[Path],
    tools: dict[Path, str],
) -> str:
    proc = sandboxed_run(
        [str(lake), "env", str(printenv), name],
        cwd=source,
        environment=environment,
        landrun=landrun,
        writable_directories=writable_directories,
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
    return value


def execute(args: argparse.Namespace) -> int:
    output = Path(args.output).resolve()
    work = Path(args.work_dir).resolve()
    report = json.loads(output.read_text(encoding="utf-8"))
    if report.get("status") != "pending":
        return 0
    source = work / "source"
    report.update(
        {
            "status": "error",
            "stage": "comparator",
            "comparator_commit": args.comparator_commit,
            "landrun_commit": args.landrun_commit,
            "workflow_url": args.workflow_url,
        }
    )
    tools: dict[Path, str] = {}

    def guarded_write() -> None:
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
        comparator = Path(args.comparator).resolve()
        lean4export = Path(args.lean4export).resolve()
        landrun = Path(args.landrun).resolve()
        adapter = (ROOT / "scripts" / "landrun_passthrough.py").resolve()
        verifier = Path(__file__).resolve()
        for tool in (comparator, lean4export, landrun, adapter, verifier):
            if not tool.is_file():
                raise VerificationError(f"missing verifier tool: {tool}")
        env = os.environ.copy()
        env.update(
            {
                "COMPARATOR_LANDRUN": str(adapter),
                "COMPARATOR_LEAN4EXPORT": str(lean4export),
                "PALOMAR_LANDRUN_REAL": str(landrun),
                "LEAN_ABORT_ON_PANIC": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
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
            lake,
            lean,
            python,
            printenv,
            touch,
        ]
        for system_path in (Path("/usr"), Path("/bin"), Path("/lib"), Path("/lib64")):
            if system_path.exists():
                executable_paths.append(system_path.resolve())
        executable_paths = sorted(set(executable_paths))
        require_protected_paths(
            [
                output,
                comparator,
                lean4export,
                landrun,
                adapter,
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
                verifier,
                lake,
                lean,
                python,
                printenv,
                touch,
            ]
        )
        verify_filesystem_confinement(
            work / "landrun-denial-probe",
            touch=touch,
            cwd=source,
            environment=env,
            landrun=landrun,
            writable_directories=writable_directories,
            executable_paths=executable_paths,
            tools=tools,
        )

        packages = manifest_packages(source)
        allowlist = package_allowlist(source, packages, base_env=env)
        get_mathlib_cache(
            source,
            base_env=env,
            lake=lake,
            landrun=landrun,
            writable_directories=writable_directories,
            executable_paths=executable_paths,
            tools=tools,
        )
        env["LEAN_PATH"] = lake_environment_value(
            "LEAN_PATH",
            source=source,
            lake=lake,
            printenv=printenv,
            environment=env,
            landrun=landrun,
            writable_directories=writable_directories,
            executable_paths=executable_paths,
            tools=tools,
        )
        env["LEAN_SRC_PATH"] = lake_environment_value(
            "LEAN_SRC_PATH",
            source=source,
            lake=lake,
            printenv=printenv,
            environment=env,
            landrun=landrun,
            writable_directories=writable_directories,
            executable_paths=executable_paths,
            tools=tools,
        )
        proc = sandboxed_run(
            [str(comparator), "comparator.json"],
            cwd=source,
            environment=env,
            landrun=landrun,
            writable_directories=writable_directories,
            executable_paths=executable_paths,
            tools=tools,
            timeout=330 * 60,
            check=False,
        )
        log = (proc.stdout + "\n" + proc.stderr).strip()
        report["comparator_log_tail"] = log[-20000:]
        if proc.returncode:
            report["status"] = "fail"
            report["errors"].append(f"Comparator rejected the project (exit {proc.returncode})")
            report["stage"] = "comparator"
            guarded_write()
            return 0

        dependency_sources = lean_source_dependencies(
            source,
            lean=lean,
            environment=env,
            landrun=landrun,
            writable_directories=writable_directories,
            executable_paths=executable_paths,
            tools=tools,
        )
        audit = audit_challenge_sources(
            source,
            database=Path(args.database).resolve(),
            dependency_sources=dependency_sources,
            lean_prefix=lean_prefix,
            allowlist=allowlist,
            writable_directories=writable_directories,
        )
        report["project_dependencies"] = packages
        report["challenge"].update(
            {
                "transitive_source_count": audit["source_count"],
                "dependencies": audit["dependencies"],
                "trust_level": audit["trust_level"],
                "untrusted_sources": audit["untrusted_sources"],
            }
        )
        if audit["untrusted_sources"]:
            report["status"] = "fail"
            report["stage"] = "challenge-provenance"
            report["errors"].append(
                "Challenge.lean transitively imports sources outside the allowlist or Palomar"
            )
        else:
            report["status"] = "pass"
            report["stage"] = "complete"
        report["checked_at"] = now()
        guarded_write()
    except subprocess.TimeoutExpired:
        report["status"] = "error"
        report["errors"].append("mechanical verification timed out")
        guarded_write()
    except Exception as error:  # noqa: BLE001 -- all verifier failures become a bounded report
        report["status"] = "error"
        report["errors"].append(str(error))
        guarded_write()
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
    execute_parser.set_defaults(func=execute)
    return result


def main() -> int:
    args = parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
