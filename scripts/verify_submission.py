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
IMPORT_RE = re.compile(r"^\s*(?:public\s+)?import\s+(.+?)\s*(?://.*)?$")


class VerificationError(RuntimeError):
    pass


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
    proc = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if check and proc.returncode:
        detail = (proc.stderr or proc.stdout).strip()[-4000:]
        raise VerificationError(f"{' '.join(command[:3])} failed ({proc.returncode}): {detail}")
    return proc


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
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
    run(["git", "init", "-q", str(destination)])
    run(["git", "-C", str(destination), "remote", "add", "origin", url])
    run(
        [
            "git",
            "-C",
            str(destination),
            "-c",
            "protocol.file.allow=never",
            "fetch",
            "--depth=1",
            "--no-tags",
            "origin",
            commit,
        ],
        timeout=600,
    )
    run(["git", "-C", str(destination), "checkout", "-q", "--detach", "FETCH_HEAD"])
    resolved = run(["git", "-C", str(destination), "rev-parse", "HEAD"]).stdout.strip()
    if resolved != commit:
        raise VerificationError(f"fetched {resolved}, expected {commit}")
    run(["git", "-C", str(destination), "remote", "set-url", "--push", "origin", "no_push"])


def tree_size(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        if ".git" in path.parts or path.is_symlink() or not path.is_file():
            continue
        total += path.stat().st_size
        if total > MAX_SOURCE_BYTES:
            break
    return total


def direct_imports(text: str) -> list[str]:
    imports: list[str] = []
    for line in text.splitlines():
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


def package_allowlist(source: Path, packages: list[dict[str, str]]) -> dict[str, tuple[str, str]]:
    """Map package directory/name to (allowlisted root, trust level)."""
    roots_config = json.loads((ROOT / "allowed-challenge-repositories.json").read_text())
    configured = {
        root["repository"].lower(): (root["repository"], root["trust_level"])
        for root in roots_config["roots"]
    }
    allowed: dict[str, tuple[str, str]] = {}
    # Mathlib wins overlap so its infrastructure remains a high-trust surface.
    roots = sorted(configured.items(), key=lambda item: item[1][1] != "high")
    for normalized, (display, level) in roots:
        matching = [package for package in packages if str(package["repository"]).lower() == normalized]
        for root_package in matching:
            closure = {root_package["name"]}
            package_dir = source / ".lake" / "packages" / root_package["name"]
            nested = manifest_packages(package_dir)
            closure.update(package["name"] for package in nested)
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


def systemd_command(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
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
    for name in (
        "PATH",
        "HOME",
        "LEAN_PATH",
        "LEAN_SRC_PATH",
        "LEAN_ABORT_ON_PANIC",
        "COMPARATOR_LANDRUN",
        "COMPARATOR_LEAN4EXPORT",
        "PALOMAR_LANDRUN_REAL",
    ):
        value = environment.get(name)
        if value is not None:
            result.append(f"--setenv={name}={value}")
    return result + command


def materialize_packages(source: Path, *, base_env: dict[str, str]) -> None:
    """Materialize the exact Git revisions in the submitted Lake manifest.

    This deliberately does not use ``lake update``: Lake runs package
    post-update hooks, which are unnecessary for fetching and expand the
    pre-verification execution surface.
    """
    dot_lake = source / ".lake"
    dot_lake.mkdir(exist_ok=True)
    packages_dir = dot_lake / "packages"
    packages_dir.mkdir(exist_ok=True)
    git_env = {
        "HOME": str(dot_lake),
        "PATH": base_env["PATH"],
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    for package in manifest_packages(source):
        repository_url = package["url"]
        if repository_url.startswith("path:"):
            continue
        name = package["name"]
        revision = package["revision"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise VerificationError(f"unsafe package name in Lake manifest: {name!r}")
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
        git = ["git", "-c", "core.hooksPath=/dev/null", "-C", str(package_dir)]
        run([*git, "init", "--quiet"], env=git_env)
        run([*git, "remote", "add", "origin", repository_url], env=git_env)
        run(
            [*git, "fetch", "--quiet", "--depth=1", "origin", revision],
            env=git_env,
            timeout=1800,
        )
        run([*git, "checkout", "--quiet", "--detach", revision], env=git_env)


def get_mathlib_cache(source: Path, *, base_env: dict[str, str]) -> None:
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
    cache_env = base_env.copy()
    cache_env["LEAN_ABORT_ON_PANIC"] = "1"
    run(
        systemd_command(
            ["lake", "exe", "cache", "get"],
            cwd=package_dir,
            environment=cache_env,
        ),
        cwd=package_dir,
        env=cache_env,
        timeout=1800,
    )


def audit_challenge_sources(
    source: Path,
    *,
    database: Path,
    base_env: dict[str, str],
) -> dict[str, Any]:
    lean_src_path = run(["lake", "env", "printenv", "LEAN_SRC_PATH"], cwd=source, env=base_env).stdout.strip()
    lean_prefix = run(["lake", "env", "lean", "--print-prefix"], cwd=source, env=base_env).stdout.strip()
    search_roots = [Path(path).resolve() for path in lean_src_path.split(os.pathsep) if path]
    packages = manifest_packages(source)
    by_name = {package["name"]: package for package in packages}
    allowlist = package_allowlist(source, packages)
    indexed = indexed_versions(database)
    sources: set[Path] = set()
    untrusted: list[str] = []
    dependencies: dict[tuple[str, str, str | None], None] = {}
    root = source.resolve()
    toolchain_prefix = Path(lean_prefix).resolve()
    pending = direct_imports((root / "Challenge.lean").read_text(encoding="utf-8"))
    visited_modules: set[str] = set()

    while pending:
        module = pending.pop()
        if module in visited_modules:
            continue
        visited_modules.add(module)
        relative = Path(*module.split(".")).with_suffix(".lean")
        resolved = next(
            (
                candidate
                for search_root in search_roots
                if (candidate := (search_root / relative).resolve()).is_file()
            ),
            None,
        )
        if resolved is None:
            raise VerificationError(f"cannot resolve imported source module {module!r}")
        sources.add(resolved)
        try:
            resolved.relative_to(toolchain_prefix)
            continue
        except ValueError:
            pass
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
        if package_name in allowlist:
            repository, _level = allowlist[package_name]
            dependencies[(repository, "allowlisted", None)] = None
            continue
        repository = str(package["repository"])
        revision = package["revision"]
        palomar_id = indexed.get((repository.lower(), revision))
        if palomar_id:
            dependencies[(repository, "palomar-indexed", palomar_id)] = None
            pending.extend(direct_imports(resolved.read_text(encoding="utf-8")))
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
    qualified = any(
        item["provenance"] == "palomar-indexed" or item["repository"].lower() == "taucetiproject/tauceti"
        for item in serialized_dependencies
    )
    return {
        "source_count": len(sources),
        "dependencies": serialized_dependencies,
        "untrusted_sources": untrusted[:100],
        "trust_level": "qualified" if qualified else "high",
    }


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
    try:
        comparator = Path(args.comparator).resolve()
        lean4export = Path(args.lean4export).resolve()
        landrun = Path(args.landrun).resolve()
        for tool in (comparator, lean4export, landrun):
            if not tool.is_file():
                raise VerificationError(f"missing verifier tool: {tool}")
        env = os.environ.copy()
        env.update(
            {
                "COMPARATOR_LANDRUN": str((ROOT / "scripts" / "landrun_passthrough.py").resolve()),
                "COMPARATOR_LEAN4EXPORT": str(lean4export),
                "PALOMAR_LANDRUN_REAL": str(landrun),
                "LEAN_ABORT_ON_PANIC": "1",
            }
        )
        materialize_packages(source, base_env=env)
        lean_prefix = run(["lean", "--print-prefix"], cwd=source).stdout.strip()
        env["PATH"] = f"{Path(lean_prefix) / 'bin'}:{env['PATH']}"
        get_mathlib_cache(source, base_env=env)
        env["LEAN_PATH"] = run(["lake", "env", "printenv", "LEAN_PATH"], cwd=source).stdout.strip()
        command = systemd_command(
            [str(comparator), "comparator.json"],
            cwd=source,
            environment=env,
        )
        proc = run(command, cwd=source, env=env, timeout=330 * 60, check=False)
        log = (proc.stdout + "\n" + proc.stderr).strip()
        report["comparator_log_tail"] = log[-20000:]
        if proc.returncode:
            report["status"] = "fail"
            report["errors"].append(f"Comparator rejected the project (exit {proc.returncode})")
            report["stage"] = "comparator"
            write_json(output, report)
            return 0

        audit = audit_challenge_sources(
            source,
            database=Path(args.database).resolve(),
            base_env=env,
        )
        report["project_dependencies"] = manifest_packages(source)
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
        write_json(output, report)
    except subprocess.TimeoutExpired:
        report["status"] = "error"
        report["errors"].append("mechanical verification timed out")
        write_json(output, report)
    except Exception as error:  # noqa: BLE001 -- all verifier failures become a bounded report
        report["status"] = "error"
        report["errors"].append(str(error))
        write_json(output, report)
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
