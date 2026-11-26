"""Git bundles of a confidential submission's source, and their descriptor.

Standard library only: the credentialed snapshot, validation and restore jobs
import this module without installing anything, and the credential-free
verify and render jobs import it to read source from the bundles alone.

The contract is `palomar-snapshot/1` in the confidential design (contract v2,
sections 2.2, 3.3 and 4). A bundle holds the complete history of exactly one
commit under exactly one ref, `refs/palomar/verified`, with no prerequisites,
so it can be restored into an empty repository and pushed as a new archive.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from scripts.verification_errors import VerificationError

SCHEMA = "palomar-snapshot/1"
SOURCE_MANIFEST_SCHEMA = "palomar-source-manifest/1"
VERIFIED_REF = "refs/palomar/verified"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
SUBMISSION_ID_RE = re.compile(r"^[0-9a-z]{12}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
FULL_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}$")
TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
PROJECT_FIELDS = (
    "project_path",
    "challenge_path",
    "solution_path",
    "comparator_config_path",
    "lakefile_path",
    "lean_toolchain_path",
)
MAX_DEPENDENCIES = 50
MAX_SNAPSHOT_BYTES = 500 * 1024 * 1024
MAX_MANIFEST_FILES = 200_000
HISTORY_FORMAT = "%H %P%n%an <%ae>%n%cn <%ce>%n%aI %cI"
# Every Git invocation here ignores ambient configuration, hooks, replace refs,
# grafts, prompts and credential helpers. Callers add the protocols they need.
GIT_BASE = (
    "git",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "gc.auto=0",
    "-c",
    "maintenance.auto=false",
    "-c",
    "credential.helper=",
    "-c",
    "core.askPass=",
    "-c",
    "protocol.allow=never",
    "-c",
    "transfer.fsckObjects=true",
    "-c",
    "fetch.fsckObjects=true",
    "-c",
    "http.followRedirects=false",
    "-c",
    "core.fsmonitor=false",
)


class SnapshotError(VerificationError):
    """The snapshot, its bundles or its descriptor are not acceptable."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="confidential.snapshot_invalid")


def canonical_json(value: Any) -> bytes:
    """Contract v2 canonical JSON: sorted keys, no whitespace, raw UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git_environment(home: Path) -> dict[str, str]:
    """A closed environment: nothing from the runner reaches Git."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "SSH_ASKPASS": "/bin/false",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_GRAFT_FILE": "/dev/null",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "LC_ALL": "C",
    }


def git(
    arguments: list[str],
    *,
    git_dir: Path | None,
    env: dict[str, str],
    extra_config: tuple[str, ...] = (),
    input_bytes: bytes | None = None,
    timeout: int = 1800,
) -> bytes:
    command = [*GIT_BASE, *extra_config]
    if git_dir is not None:
        command += ["--git-dir", str(git_dir)]
    command += arguments
    try:
        completed = subprocess.run(
            command, env=env, input=input_bytes, capture_output=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as error:
        raise SnapshotError(f"git {arguments[0]} timed out") from error
    if completed.returncode != 0:
        # Git's stderr names refs and paths of the source; keep it out of the
        # message, which can reach a report. The exit status is enough to act on.
        raise SnapshotError(f"git {arguments[0]} failed with exit status {completed.returncode}")
    return completed.stdout


def init_bare(path: Path, env: dict[str, str]) -> Path:
    if path.exists():
        raise SnapshotError("repository directory already exists")
    git(["init", "--quiet", "--bare", str(path)], git_dir=None, env=env)
    return path


# --------------------------------------------------------------------------
# Dependency URLs (contract v2, 5.4)


@dataclass(frozen=True)
class GitHubRepository:
    full_name: str

    @property
    def url(self) -> str:
        return f"https://github.com/{self.full_name}"

    @property
    def key(self) -> str:
        return self.full_name.lower()


def canonical_github_repository(url: str) -> GitHubRepository:
    """Accept exactly `https://github.com/<owner>/<repo>` with an optional `.git`.

    Any other host, scheme, port, credential, query, fragment or path shape
    fails: a confidential snapshot fetches nothing else.
    """
    if not isinstance(url, str) or any(ord(character) < 0x21 for character in url):
        raise SnapshotError("dependency URL must be a plain https://github.com/<owner>/<repo> URL")
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise SnapshotError("dependency URL must be a plain https://github.com/<owner>/<repo> URL")
    path = parsed.path.removesuffix(".git")
    parts = path.split("/")
    if len(parts) != 3 or parts[0] != "" or not FULL_NAME_RE.fullmatch(f"{parts[1]}/{parts[2]}"):
        raise SnapshotError("dependency URL must be a plain https://github.com/<owner>/<repo> URL")
    if parts[2] in {".", ".."} or parts[2].endswith("."):
        raise SnapshotError("dependency URL must be a plain https://github.com/<owner>/<repo> URL")
    return GitHubRepository(f"{parts[1]}/{parts[2]}")


def require_public_github_addresses(resolver=socket.getaddrinfo) -> None:
    """Refuse to fetch when github.com resolves to a private or reserved address."""
    try:
        answers = resolver("github.com", 443, type=socket.SOCK_STREAM)
    except OSError as error:
        raise SnapshotError("github.com did not resolve") from error
    addresses = {answer[4][0] for answer in answers}
    if not addresses or not all(ipaddress.ip_address(address).is_global for address in addresses):
        raise SnapshotError("github.com resolves to a non-public address")


# --------------------------------------------------------------------------
# Repository facts


def validate_path(raw: bytes) -> str:
    try:
        path = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SnapshotError("the source contains a path that is not valid UTF-8") from error
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in path):
        raise SnapshotError("the source contains a path with a control character")
    return path


def tree_entries(git_dir: Path, commit: str, env: dict[str, str]) -> list[tuple[str, str, str, int, str]]:
    """Every entry of the commit's tree: (mode, type, object, size, path)."""
    output = git(["ls-tree", "-r", "-z", "-l", "--full-tree", commit], git_dir=git_dir, env=env)
    entries = []
    for record in output.split(b"\0"):
        if not record:
            continue
        header, _, raw_path = record.partition(b"\t")
        mode, kind, obj, size = header.decode("ascii").split(maxsplit=3)
        entries.append((mode, kind, obj, -1 if size.strip() == "-" else int(size), validate_path(raw_path)))
    if len(entries) > MAX_MANIFEST_FILES:
        raise SnapshotError("the source tree has too many entries")
    return entries


def read_blobs(git_dir: Path, objects: list[str], env: dict[str, str]) -> dict[str, bytes]:
    if not objects:
        return {}
    output = git(
        ["cat-file", "--batch"],
        git_dir=git_dir,
        env=env,
        input_bytes="".join(f"{o}\n" for o in objects).encode(),
    )
    blobs: dict[str, bytes] = {}
    offset = 0
    for obj in objects:
        end = output.index(b"\n", offset)
        header = output[offset:end].decode("ascii").split()
        if len(header) != 3 or header[0] != obj or header[1] != "blob":
            raise SnapshotError("a source object could not be read")
        size = int(header[2])
        blobs[obj] = output[end + 1 : end + 1 + size]
        offset = end + 1 + size + 1
    return blobs


LFS_FILTER_RE = re.compile(rb"(^|\s)filter=lfs(\s|$)", re.M)


def restorable_tree(entries: list[tuple[str, str, str, int, str]], blobs: dict[str, bytes]) -> None:
    """Refuse gitlinks and LFS filter attributes: neither restores from a bundle."""
    for mode, _, obj, _, path in entries:
        if mode == "160000":
            raise SnapshotError("the source contains a submodule (gitlink), which a bundle cannot preserve")
        if (path == ".gitattributes" or path.endswith("/.gitattributes")) and LFS_FILTER_RE.search(
            blobs[obj]
        ):
            raise SnapshotError("the source uses Git LFS, which a bundle cannot preserve")


def source_manifest(
    git_dir: Path, commit: str, project_path: str, env: dict[str, str]
) -> tuple[dict[str, Any], list[tuple[str, str, str, int, str]], dict[str, bytes]]:
    """`palomar-source-manifest/1` for every regular file of the tree at commit."""
    entries = tree_entries(git_dir, commit, env)
    wanted = [obj for mode, kind, obj, _, path in entries if kind == "blob"]
    blobs = read_blobs(git_dir, sorted(set(wanted)), env)
    restorable_tree(entries, blobs)
    files = [
        {"path": path, "sha256": sha256_bytes(blobs[obj]), "size": size}
        for mode, kind, obj, size, path in entries
        if kind == "blob" and mode in {"100644", "100755"}
    ]
    files.sort(key=lambda item: item["path"].encode("utf-8"))
    return {"schema": SOURCE_MANIFEST_SCHEMA, "project_path": project_path, "files": files}, entries, blobs


def history(git_dir: Path, commit: str, env: dict[str, str]) -> dict[str, Any]:
    """`commit_count`, `root_commits` and `history_sha256` (contract v2, 2.2)."""
    listing = git(
        ["rev-list", f"--format={HISTORY_FORMAT}", "--no-commit-header", commit], git_dir=git_dir, env=env
    )
    count = int(git(["rev-list", "--count", commit], git_dir=git_dir, env=env).decode().strip())
    roots = git(["rev-list", "--max-parents=0", commit], git_dir=git_dir, env=env).decode().split()
    return {"commit_count": count, "root_commits": sorted(roots), "history_sha256": sha256_bytes(listing)}


def commit_tree(git_dir: Path, commit: str, env: dict[str, str]) -> str:
    return git(["rev-parse", "--verify", f"{commit}^{{tree}}"], git_dir=git_dir, env=env).decode().strip()


def project_kinds(entries: list[tuple[str, str, str, int, str]]) -> tuple[set[str], set[str]]:
    files = {path for _, kind, _, _, path in entries if kind == "blob"}
    directories = {""}
    for path in files:
        parts = path.split("/")[:-1]
        directories.update("/".join(parts[: index + 1]) for index in range(len(parts)))
    return files, directories


def check_project_paths(project: dict[str, str], entries: list[tuple[str, str, str, int, str]]) -> None:
    files, directories = project_kinds(entries)
    if project["project_path"] not in directories:
        raise SnapshotError("the recorded project path is not a directory at the commit")
    for field in PROJECT_FIELDS[1:]:
        if project[field] not in files:
            raise SnapshotError(f"the recorded {field} is not a file at the commit")


def project_sha256(project: dict[str, str]) -> str:
    """The digest of the six project paths that the ticket's `admitted` names."""
    return sha256_bytes(canonical_json([project[field] for field in PROJECT_FIELDS]))


def lake_manifest_path(project_path: str) -> str:
    return f"{project_path}/lake-manifest.json" if project_path else "lake-manifest.json"


def manifest_git_packages(raw: bytes | None) -> list[dict[str, str]]:
    """Git packages of a Lake manifest, in manifest order; path packages are local."""
    if raw is None:
        return []
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SnapshotError("lake-manifest.json is not valid JSON") from error
    packages = manifest.get("packages") if isinstance(manifest, dict) else None
    if not isinstance(packages, list):
        raise SnapshotError("lake-manifest.json has no package list")
    found = []
    for package in packages:
        if not isinstance(package, dict):
            raise SnapshotError("lake-manifest.json has a malformed package")
        if package.get("type") == "path":
            continue
        if package.get("type") != "git":
            raise SnapshotError("lake-manifest.json has a package that is neither git nor path")
        name, url, rev = package.get("name"), package.get("url"), package.get("rev")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise SnapshotError("lake-manifest.json has an unsafe package name")
        if not isinstance(rev, str) or not SHA_RE.fullmatch(rev):
            raise SnapshotError(f"Git package {name!r} is not pinned to a full commit")
        found.append({"name": name, "url": canonical_github_repository(url).url, "rev": rev})
    if len(found) > MAX_DEPENDENCIES:
        raise SnapshotError("the Lake manifest names more than 50 Git packages")
    if len({package["name"] for package in found}) != len(found):
        raise SnapshotError("the Lake manifest names a package twice")
    return found


# --------------------------------------------------------------------------
# Bundles


def bundle_header(path: Path) -> tuple[dict[str, str], list[str], list[tuple[str, str]]]:
    """Parse a v2 or v3 bundle header: (capabilities, prerequisites, refs)."""
    capabilities: dict[str, str] = {}
    prerequisites: list[str] = []
    refs: list[tuple[str, str]] = []
    with path.open("rb") as handle:
        signature = handle.readline()
        if signature not in {b"# v2 git bundle\n", b"# v3 git bundle\n"}:
            raise SnapshotError("not a git bundle")
        while True:
            line = handle.readline()
            if not line or len(line) > 4096:
                raise SnapshotError("bundle header is malformed")
            if line == b"\n":
                break
            text = line.decode("utf-8", "strict").rstrip("\n")
            if text.startswith("@"):
                key, _, value = text[1:].partition("=")
                capabilities[key] = value
            elif text.startswith("-"):
                prerequisites.append(text[1:].split(" ", 1)[0])
            else:
                obj, _, ref = text.partition(" ")
                refs.append((obj, ref))
    return capabilities, prerequisites, refs


def check_bundle_header(path: Path, target: str) -> None:
    """The Worker's header rule (contract v2, 2.2), applied on every side."""
    capabilities, prerequisites, refs = bundle_header(path)
    if prerequisites:
        raise SnapshotError("bundle has prerequisites; it does not hold complete history")
    if "filter" in capabilities or set(capabilities) - {"object-format"}:
        raise SnapshotError("bundle declares a filter or an unknown capability")
    if capabilities.get("object-format", "sha1") != "sha1":
        raise SnapshotError("bundle uses an unsupported object format")
    if refs != [(target, VERIFIED_REF)]:
        raise SnapshotError(f"bundle must hold exactly {VERIFIED_REF} at the recorded commit")


def create_bundle(git_dir: Path, commit: str, destination: Path, env: dict[str, str]) -> None:
    if git(["rev-parse", "--is-shallow-repository"], git_dir=git_dir, env=env).strip() != b"false":
        raise SnapshotError("the fetched source is shallow; a snapshot needs complete history")
    git(["update-ref", VERIFIED_REF, commit], git_dir=git_dir, env=env)
    git(["bundle", "create", "--version=2", str(destination), VERIFIED_REF], git_dir=git_dir, env=env)
    check_bundle_header(destination, commit)


def restore_bundle(bundle: Path, target: str, destination: Path, env: dict[str, str]) -> Path:
    """Import a bundle into an empty bare repository and prove it complete."""
    check_bundle_header(bundle, target)
    init_bare(destination, env)
    allow_file = ("-c", "protocol.file.allow=always")
    git(["bundle", "verify", "--quiet", str(bundle)], git_dir=destination, env=env, extra_config=allow_file)
    git(
        ["fetch", "--quiet", "--no-tags", str(bundle), f"{VERIFIED_REF}:{VERIFIED_REF}"],
        git_dir=destination,
        env=env,
        extra_config=allow_file,
    )
    resolved = git(["rev-parse", "--verify", f"{VERIFIED_REF}^{{commit}}"], git_dir=destination, env=env)
    if resolved.decode().strip() != target:
        raise SnapshotError("restored bundle does not resolve to the recorded commit")
    git(["fsck", "--full", "--strict", "--no-dangling"], git_dir=destination, env=env)
    return destination


def fetch_bundle_into_checkout(bundle: Path, target: str, checkout: Path, env: dict[str, str]) -> None:
    """Check out `target` in a fresh non-bare repository from a verified bundle only."""
    check_bundle_header(bundle, target)
    checkout.mkdir(parents=True)
    git(["init", "--quiet", str(checkout)], git_dir=None, env=env)
    git_dir = checkout / ".git"
    allow_file = ("-c", "protocol.file.allow=always")
    git(
        ["fetch", "--quiet", "--no-tags", str(bundle), f"{VERIFIED_REF}:{VERIFIED_REF}"],
        git_dir=git_dir,
        env=env,
        extra_config=allow_file,
    )
    git(["fsck", "--full", "--strict", "--no-dangling"], git_dir=git_dir, env=env)
    git(["--work-tree", str(checkout), "checkout", "--quiet", "--detach", target], git_dir=git_dir, env=env)
    resolved = git(["rev-parse", "HEAD"], git_dir=git_dir, env=env).decode().strip()
    if resolved != target:
        raise SnapshotError("checked-out bundle does not resolve to the recorded commit")


# --------------------------------------------------------------------------
# Descriptor


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SnapshotError(f"snapshot descriptor: {message}")


def _bundle_reference(value: Any, submission_id: str, repository_id: int, target: str, label: str) -> None:
    _require(
        isinstance(value, dict) and set(value) == {"key", "sha256", "size", "ref"}, f"{label} bundle shape"
    )
    _require(
        value["key"] == f"v2/{submission_id}/snapshot/{repository_id}-{target}.bundle", f"{label} bundle key"
    )
    _require(
        isinstance(value["sha256"], str) and bool(SHA256_RE.fullmatch(value["sha256"])), f"{label} sha256"
    )
    _require(type(value["size"]) is int and 0 < value["size"] <= MAX_SNAPSHOT_BYTES, f"{label} size")
    _require(value["ref"] == VERIFIED_REF, f"{label} ref")


def _positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def check_descriptor(value: Any) -> dict[str, Any]:
    """Closed-shape check of `palomar-snapshot/1`."""
    _require(isinstance(value, dict), "not an object")
    _require(
        set(value)
        == {
            "schema",
            "submission_id",
            "nonce",
            "pipeline_commit",
            "caller",
            "run",
            "captured_at",
            "source",
            "project",
            "lake_manifest_sha256",
            "source_manifest_sha256",
            "dependencies",
        },
        "unexpected or missing fields",
    )
    _require(value["schema"] == SCHEMA, "schema")
    submission_id = value["submission_id"]
    _require(
        isinstance(submission_id, str) and bool(SUBMISSION_ID_RE.fullmatch(submission_id)), "submission_id"
    )
    _require(isinstance(value["nonce"], str) and bool(NONCE_RE.fullmatch(value["nonce"])), "nonce")
    _require(
        isinstance(value["pipeline_commit"], str) and bool(SHA_RE.fullmatch(value["pipeline_commit"])),
        "pipeline_commit",
    )
    caller = value["caller"]
    _require(
        isinstance(caller, dict)
        and set(caller) == {"repository_id", "workflow_sha"}
        and _positive_int(caller["repository_id"])
        and isinstance(caller["workflow_sha"], str)
        and bool(SHA_RE.fullmatch(caller["workflow_sha"])),
        "caller",
    )
    run = value["run"]
    _require(
        isinstance(run, dict)
        and set(run) == {"id", "attempt"}
        and _positive_int(run["id"])
        and _positive_int(run["attempt"]),
        "run",
    )
    _require(
        isinstance(value["captured_at"], str) and bool(TIMESTAMP_RE.fullmatch(value["captured_at"])),
        "captured_at",
    )
    source = value["source"]
    _require(
        isinstance(source, dict)
        and set(source)
        == {
            "route",
            "repository_id",
            "owner_id",
            "full_name",
            "visibility",
            "commit",
            "tree",
            "history",
            "bundle",
        },
        "source fields",
    )
    _require(source["route"] in {"source-app", "public"}, "source route")
    _require(
        source["visibility"] == ("private" if source["route"] == "source-app" else "public"),
        "source visibility must follow its route",
    )
    _require(_positive_int(source["repository_id"]) and _positive_int(source["owner_id"]), "source ids")
    _require(
        isinstance(source["full_name"], str) and bool(FULL_NAME_RE.fullmatch(source["full_name"])),
        "source full_name",
    )
    for field in ("commit", "tree"):
        _require(isinstance(source[field], str) and bool(SHA_RE.fullmatch(source[field])), f"source {field}")
    record = source["history"]
    _require(
        isinstance(record, dict)
        and set(record) == {"commit_count", "root_commits", "history_sha256"}
        and _positive_int(record["commit_count"])
        and isinstance(record["root_commits"], list)
        and record["root_commits"]
        and record["root_commits"] == sorted(set(record["root_commits"]))
        and all(isinstance(item, str) and SHA_RE.fullmatch(item) for item in record["root_commits"])
        and isinstance(record["history_sha256"], str)
        and bool(SHA256_RE.fullmatch(record["history_sha256"])),
        "source history",
    )
    _bundle_reference(source["bundle"], submission_id, source["repository_id"], source["commit"], "source")
    project = value["project"]
    _require(
        isinstance(project, dict)
        and set(project) == set(PROJECT_FIELDS)
        and all(isinstance(project[field], str) for field in PROJECT_FIELDS)
        and all(project[field] for field in PROJECT_FIELDS[1:]),
        "project paths",
    )
    for field in ("lake_manifest_sha256", "source_manifest_sha256"):
        _require(isinstance(value[field], str) and bool(SHA256_RE.fullmatch(value[field])), field)
    dependencies = value["dependencies"]
    _require(isinstance(dependencies, list) and len(dependencies) <= MAX_DEPENDENCIES, "dependencies")
    names = set()
    for dependency in dependencies:
        _require(
            isinstance(dependency, dict)
            and set(dependency) == {"name", "url", "rev", "kind", "repository_id", "bundle"},
            "dependency fields",
        )
        _require(
            isinstance(dependency["name"], str)
            and bool(NAME_RE.fullmatch(dependency["name"]))
            and dependency["name"] not in names,
            "dependency name",
        )
        names.add(dependency["name"])
        _require(canonical_github_repository(dependency["url"]).url == dependency["url"], "dependency url")
        _require(
            isinstance(dependency["rev"], str) and bool(SHA_RE.fullmatch(dependency["rev"])), "dependency rev"
        )
        _require(_positive_int(dependency["repository_id"]), "dependency repository_id")
        if dependency["kind"] == "trusted-root":
            _require(dependency["bundle"] is None, "a trusted-root dependency is not bundled")
        else:
            _require(dependency["kind"] == "public", "dependency kind")
            _bundle_reference(
                dependency["bundle"],
                submission_id,
                dependency["repository_id"],
                dependency["rev"],
                f"dependency {dependency['name']}",
            )
    return value


def descriptor_bundles(descriptor: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    """Every bundle reference with the commit its ref must name."""
    found = [(descriptor["source"]["bundle"], descriptor["source"]["commit"])]
    found += [
        (item["bundle"], item["rev"]) for item in descriptor["dependencies"] if item["bundle"] is not None
    ]
    return found


# --------------------------------------------------------------------------
# The bundle directory the verify and render jobs receive


@dataclass
class SourceBundles:
    """Source for the credential-free jobs, from bundles only.

    The directory is the `source-bundles-<request_id>` artifact written by the
    validation or restore job: `descriptor.json` (canonical bytes),
    `source-manifest.json`, and `bundles/<repository_id>-<commit>.bundle`.
    """

    directory: Path
    descriptor: dict[str, Any]
    snapshot_sha256: str
    # The renderer adds its own trusted packages (Verso and its closure) to
    # the workspace manifest; those are not the submission's and are fetched
    # as today. The verifier allows nothing unlisted.
    allow_unlisted: bool = False

    @classmethod
    def load(cls, directory: Path, snapshot_sha256: str) -> SourceBundles:
        if not SHA256_RE.fullmatch(snapshot_sha256 or ""):
            raise SnapshotError("snapshot_sha256 must be 64 lowercase hexadecimal characters")
        directory = directory.resolve()
        raw = (directory / "descriptor.json").read_bytes()
        if sha256_bytes(raw) != snapshot_sha256:
            raise SnapshotError("the bundled descriptor is not the snapshot this run names")
        descriptor = check_descriptor(json.loads(raw))
        if canonical_json(descriptor) != raw:
            raise SnapshotError("the bundled descriptor is not canonical JSON")
        manifest = (directory / "source-manifest.json").read_bytes()
        if sha256_bytes(manifest) != descriptor["source_manifest_sha256"]:
            raise SnapshotError("the bundled source manifest does not match the descriptor")
        for reference, target in descriptor_bundles(descriptor):
            path = cls._path(directory, reference)
            if path.stat().st_size != reference["size"] or sha256_file(path) != reference["sha256"]:
                raise SnapshotError("a bundle does not match the descriptor")
            check_bundle_header(path, target)
        return cls(directory, descriptor, snapshot_sha256)

    @staticmethod
    def _path(directory: Path, reference: dict[str, Any]) -> Path:
        path = directory / "bundles" / reference["key"].rsplit("/", 1)[1]
        if path.is_symlink() or not path.is_file():
            raise SnapshotError("a bundle named by the descriptor is missing")
        return path

    def require_source(self, repository: str, commit: str) -> None:
        source = self.descriptor["source"]
        if source["full_name"].lower() != repository.lower() or source["commit"] != commit:
            raise SnapshotError("the snapshot is of a different repository or commit than this run names")

    def source_bundle(self) -> Path:
        return self._path(self.directory, self.descriptor["source"]["bundle"])

    def dependency(self, url: str, rev: str) -> dict[str, Any] | None:
        """The descriptor row for a manifest package.

        A package the snapshot does not list is refused, except a renderer
        package when `allow_unlisted` is set; `None` then means "fetch it as
        a trusted package, as today".
        """
        key = canonical_github_repository(url).key
        for item in self.descriptor["dependencies"]:
            if canonical_github_repository(item["url"]).key == key:
                if item["rev"] != rev:
                    raise SnapshotError("a Lake package is pinned to another revision than the snapshot's")
                return item
        if self.allow_unlisted:
            return None
        raise SnapshotError("a Lake package is not in the snapshot; no stage refetches confidential source")

    def dependency_bundle(self, item: dict[str, Any]) -> Path:
        return self._path(self.directory, item["bundle"])

    def checkout(self, bundle: Path, target: str, checkout: Path) -> None:
        with tempfile.TemporaryDirectory(prefix="palomar-bundle-home-") as home:
            fetch_bundle_into_checkout(bundle, target, checkout, git_environment(Path(home)))
