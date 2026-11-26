"""Snapshot, validate and restore a confidential submission's source.

Run only by `confidential-snapshot.yml`, `confidential-validate.yml` and
`confidential-restore.yml`, on GitHub-hosted runners, with nothing installed
beyond the standard library. None of these jobs runs submitted code: Git
fetches into bare repositories and nothing is ever checked out.

Every request to the broker carries a fresh GitHub OIDC token for the
audience `https://submit.palomar-registry.org/confidential`. The broker checks
it, the ticket, the caller template and the pipeline commit before it answers
(contract v2, 3.2). The installation token in a snapshot capability is masked
before use, used only through `GIT_CONFIG_*` extraheader variables in one Git
process, never written to disk, never used for dependencies, and revoked in
a `finally`.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.source_bundles import (  # noqa: E402
    MAX_SNAPSHOT_BYTES,
    PROJECT_FIELDS,
    SHA256_RE,
    SHA_RE,
    VERIFIED_REF,
    SnapshotError,
    canonical_github_repository,
    canonical_json,
    check_descriptor,
    check_project_paths,
    commit_tree,
    create_bundle,
    descriptor_bundles,
    git,
    git_environment,
    history,
    init_bare,
    lake_manifest_path,
    manifest_git_packages,
    read_blobs,
    require_public_github_addresses,
    restore_bundle,
    sha256_bytes,
    sha256_file,
    source_manifest,
)

BROKER = "https://submit.palomar-registry.org"
AUDIENCE = "https://submit.palomar-registry.org/confidential"
GITHUB_API = "https://api.github.com"
GITHUB = "https://github.com"
SINGLE_PUT_BYTES = 95 * 1000 * 1000
PART_BYTES = 64 * 1024 * 1024
OPERATIONS = {"snapshot", "validate", "restore"}
ALLOWLIST = Path(__file__).resolve().parents[1] / "allowed-challenge-repositories.json"


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


OPENER = urllib.request.build_opener(NoRedirects)


def mask(value: str) -> None:
    """Register a value with the Actions log masker before it is used."""
    if value:
        print(f"::add-mask::{value}", flush=True)


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SnapshotError("the broker answered with malformed JSON") from error


Transport = Callable[[str, str, dict[str, str], bytes | None], Response]


def http(method: str, url: str, headers: dict[str, str], body: bytes | None) -> Response:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with OPENER.open(request, timeout=300) as answer:
            return Response(answer.status, {k.lower(): v for k, v in answer.headers.items()}, answer.read())
    except urllib.error.HTTPError as error:
        return Response(error.code, {k.lower(): v for k, v in error.headers.items()}, error.read())
    except urllib.error.URLError as error:
        raise SnapshotError(f"request to {url.split('?')[0]} failed") from error


# --------------------------------------------------------------------------
# OIDC


def oidc_token(audience: str = AUDIENCE, transport: Transport = http) -> str:
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    bearer = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not bearer:
        raise SnapshotError("this job has no OIDC token permission")
    answer = transport(
        "GET", f"{url}&{urlencode({'audience': audience})}", {"Authorization": f"Bearer {bearer}"}, None
    )
    if answer.status != 200:
        raise SnapshotError("the OIDC token request failed")
    token = answer.json().get("value")
    if not isinstance(token, str) or token.count(".") != 2:
        raise SnapshotError("the OIDC token is malformed")
    mask(token)
    return token


def jwt_claims(token: str) -> dict[str, Any]:
    """The claims of a token this job just received from GitHub.

    Not a verification: the broker verifies. The job reads its own identity
    from here so that nothing a caller passes can choose it.
    """
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    if not isinstance(claims, dict):
        raise SnapshotError("the OIDC token has no claims")
    return claims


@dataclass(frozen=True)
class JobIdentity:
    pipeline_commit: str
    caller_repository_id: int
    caller_workflow_sha: str
    run_id: int
    run_attempt: int

    @classmethod
    def from_claims(cls, claims: dict[str, Any], workflow: str) -> JobIdentity:
        prefix = f"PalomarRegistry/PalomarSubmission/.github/workflows/{workflow}@"
        ref = str(claims.get("job_workflow_ref", ""))
        sha = str(claims.get("job_workflow_sha", ""))
        if not ref.startswith(prefix) or not SHA_RE.fullmatch(sha):
            raise SnapshotError(f"this job is not PalomarSubmission's {workflow}")
        try:
            identity = cls(
                pipeline_commit=sha,
                caller_repository_id=int(claims["repository_id"]),
                caller_workflow_sha=str(claims["workflow_sha"]),
                run_id=int(claims["run_id"]),
                run_attempt=int(claims["run_attempt"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise SnapshotError("the OIDC token lacks the run identity") from error
        if not SHA_RE.fullmatch(identity.caller_workflow_sha):
            raise SnapshotError("the OIDC token lacks the caller's workflow SHA")
        return identity


# --------------------------------------------------------------------------
# Broker


@dataclass
class Broker:
    """The Worker's confidential endpoints (contract v2, 3.2 and 3.3)."""

    token_source: Callable[[], str]
    base: str = BROKER
    transport: Transport = http
    capability: str | None = None

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token_source()}",
            "User-Agent": "PalomarSubmission/confidential",
        }
        if self.capability:
            headers["X-Palomar-Capability"] = self.capability
        return {**headers, **(extra or {})}

    def _json(self, method: str, path: str, value: Any, expected: tuple[int, ...] = (200,)) -> Any:
        body = canonical_json(value)
        answer = self.transport(
            method, f"{self.base}{path}", self._headers({"Content-Type": "application/json"}), body
        )
        if answer.status not in expected:
            raise SnapshotError(f"the broker refused {path.split('?')[0]} with status {answer.status}")
        return answer.json()

    def open(self, ticket: str, operation: str) -> dict[str, Any]:
        capability = self._json(
            "POST", "/api/confidential/broker", {"ticket": ticket, "operation": operation}
        )
        if not isinstance(capability, dict) or capability.get("operation") != operation:
            raise SnapshotError("the broker returned a capability for another operation")
        value = capability.get("capability")
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
            raise SnapshotError("the broker returned a malformed capability")
        mask(value)
        self.capability = value
        return capability

    def plan(self, dependencies: list[dict[str, Any]]) -> list[str]:
        answer = self._json("POST", "/api/confidential/snapshot/plan", {"dependencies": dependencies})
        keys = answer.get("object_keys") if isinstance(answer, dict) else None
        if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
            raise SnapshotError("the broker returned a malformed snapshot plan")
        return keys

    def _object_path(self, key: str, query: dict[str, str] | None = None) -> str:
        path = f"/api/confidential/objects/{quote(key, safe='/')}"
        return f"{path}?{urlencode(query)}" if query else path

    def put(self, key: str, path: Path) -> None:
        size = path.stat().st_size
        digest = sha256_file(path)
        headers = {"X-Palomar-Sha256": digest, "Content-Type": "application/octet-stream"}
        if size <= SINGLE_PUT_BYTES:
            answer = self.transport(
                "PUT", f"{self.base}{self._object_path(key)}", self._headers(headers), path.read_bytes()
            )
            if answer.status not in (200, 201):
                raise SnapshotError(f"the broker refused an object upload with status {answer.status}")
            return
        started = self._json(
            "POST", self._object_path(key, {"uploads": ""}), {"sha256": digest, "size": size}
        )
        upload = started.get("upload_id") if isinstance(started, dict) else None
        if not isinstance(upload, str) or not upload:
            raise SnapshotError("the broker returned no multipart upload id")
        receipts = []
        with path.open("rb") as handle:
            for number in range(1, 10_001):
                part = handle.read(PART_BYTES)
                if not part:
                    break
                answer = self.transport(
                    "PUT",
                    f"{self.base}{self._object_path(key, {'part': str(number), 'upload': upload})}",
                    self._headers({"Content-Type": "application/octet-stream"}),
                    part,
                )
                if answer.status != 200:
                    raise SnapshotError(f"the broker refused an upload part with status {answer.status}")
                receipts.append(answer.json())
        self._json(
            "POST",
            self._object_path(key, {"complete": upload}),
            {"parts": receipts, "sha256": digest, "size": size},
        )

    def get(self, key: str, destination: Path) -> str:
        answer = self.transport("GET", f"{self.base}{self._object_path(key)}", self._headers(), None)
        if answer.status != 200:
            raise SnapshotError(f"the broker refused an object download with status {answer.status}")
        digest = sha256_bytes(answer.body)
        if answer.headers.get("x-palomar-sha256") != digest:
            raise SnapshotError("a downloaded object does not match the SHA-256 the broker sent")
        destination.write_bytes(answer.body)
        return digest

    def finalize(self, descriptor: dict[str, Any]) -> str:
        answer = self._json("POST", "/api/confidential/snapshot/finalize", {"descriptor": descriptor})
        value = answer.get("snapshot_sha256") if isinstance(answer, dict) else None
        if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
            raise SnapshotError("the broker returned no snapshot digest")
        return value


# --------------------------------------------------------------------------
# GitHub reads


@dataclass
class GitHub:
    """Repository metadata reads and the source token's revocation."""

    api: str = GITHUB_API
    transport: Transport = http
    read_token: str | None = None

    def repository(self, full_name: str, token: str | None) -> dict[str, Any]:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "PalomarSubmission/confidential"}
        credential = token or self.read_token
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        answer = self.transport("GET", f"{self.api}/repos/{full_name}", headers, None)
        if answer.status != 200:
            raise SnapshotError(f"GitHub repository metadata was unavailable (status {answer.status})")
        value = answer.json()
        if (
            not isinstance(value, dict)
            or str(value.get("full_name", "")).lower() != full_name.lower()
            or type(value.get("id")) is not int
            or not isinstance(value.get("owner"), dict)
            or type(value["owner"].get("id")) is not int
            or type(value.get("private")) is not bool
        ):
            # A renamed or transferred repository answers under another name.
            raise SnapshotError("GitHub repository metadata names a different repository")
        return value

    def revoke(self, token: str) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "PalomarSubmission/confidential",
        }
        answer = self.transport("DELETE", f"{self.api}/installation/token", headers, None)
        if answer.status not in (204, 401):
            raise SnapshotError(f"the source token could not be revoked (status {answer.status})")


# --------------------------------------------------------------------------
# Fetching


@dataclass
class Fetcher:
    """Fetch one commit's complete history into a fresh bare repository."""

    base: str = GITHUB
    protocols: tuple[str, ...] = ("https",)
    check_addresses: bool = True
    home: Path = field(default_factory=lambda: Path(tempfile.mkdtemp(prefix="palomar-git-home-")))

    def fetch(self, full_name: str, commit: str, destination: Path, *, token: str | None = None) -> Path:
        if self.check_addresses:
            require_public_github_addresses()
        env = git_environment(self.home)
        init_bare(destination, env)
        if token is not None:
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            mask(basic)
            # Process environment only: never a URL, never .git/config.
            env = {
                **env,
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"http.{self.base}/.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
            }
        allowed = tuple(
            item for protocol in self.protocols for item in ("-c", f"protocol.{protocol}.allow=always")
        )
        git(
            [
                "fetch",
                "--quiet",
                "--no-tags",
                "--no-write-fetch-head",
                f"{self.base}/{full_name}",
                f"+{commit}:{VERIFIED_REF}",
            ],
            git_dir=destination,
            env=env,
            extra_config=allowed,
            timeout=3600,
        )
        env.pop("GIT_CONFIG_VALUE_0", None)
        resolved = (
            git(
                ["rev-parse", "--verify", f"{VERIFIED_REF}^{{commit}}"],
                git_dir=destination,
                env=git_environment(self.home),
            )
            .decode()
            .strip()
        )
        if resolved != commit:
            raise SnapshotError("the fetched commit is not the admitted one")
        return destination


# --------------------------------------------------------------------------
# Dependency classification


def allowlist_roots() -> list[dict[str, Any]]:
    config = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    return [root for root in config["roots"] if isinstance(root, dict)]


def root_aliases(roots: list[dict[str, Any]]) -> dict[str, str]:
    aliases = {}
    for root in roots:
        for alias in [root["repository"], *root.get("repository_aliases", [])]:
            aliases[alias.lower()] = root["repository"]
    return aliases


def trusted_closure(
    packages: list[dict[str, str]], read_root_manifest: Callable[[str, str], bytes | None]
) -> set[str]:
    """Names of packages that are allowlisted roots or in a root's pinned closure.

    The same rule as the verifier's `package_allowlist`: a package is trusted
    only if it is a root, or the root's own manifest at the submitted revision
    names it with the same repository and revision. Anything else is `public`
    and bundled.
    """
    aliases = root_aliases(allowlist_roots())

    def canonical(url: str) -> str:
        name = canonical_github_repository(url).full_name
        return aliases.get(name.lower(), name).lower()

    trusted: set[str] = set()
    by_name = {package["name"]: package for package in packages}
    for package in packages:
        if canonical(package["url"]) not in {value.lower() for value in aliases.values()}:
            continue
        trusted.add(package["name"])
        for nested in manifest_git_packages(read_root_manifest(package["url"], package["rev"])):
            actual = by_name.get(nested["name"])
            if (
                actual
                and canonical(actual["url"]) == canonical(nested["url"])
                and actual["rev"] == nested["rev"]
            ):
                trusted.add(nested["name"])
    return trusted


def root_manifest_reader(work: Path, fetcher: Fetcher) -> Callable[[str, str], bytes | None]:
    """Read an allowlisted root's lake-manifest.json with a blobless public fetch."""

    def read(url: str, rev: str) -> bytes | None:
        repository = canonical_github_repository(url)
        destination = work / f"root-{repository.full_name.replace('/', '-')}-{rev}"
        if not destination.exists():
            env = git_environment(fetcher.home)
            init_bare(destination, env)
            allowed = tuple(
                item for protocol in fetcher.protocols for item in ("-c", f"protocol.{protocol}.allow=always")
            )
            git(
                ["remote", "add", "origin", f"{fetcher.base}/{repository.full_name}"],
                git_dir=destination,
                env=env,
            )
            git(
                ["fetch", "--quiet", "--no-tags", "--depth=1", "--filter=blob:none", "origin", rev],
                git_dir=destination,
                env=env,
                extra_config=allowed,
                timeout=1800,
            )
        env = git_environment(fetcher.home)
        allowed = tuple(
            item for protocol in fetcher.protocols for item in ("-c", f"protocol.{protocol}.allow=always")
        )
        try:
            return git(
                ["cat-file", "blob", f"{rev}:lake-manifest.json"],
                git_dir=destination,
                env=env,
                extra_config=allowed,
            )
        except SnapshotError:
            return None

    return read


# --------------------------------------------------------------------------
# The snapshot itself


def now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def object_key(submission_id: str, repository_id: int, commit: str) -> str:
    return f"v2/{submission_id}/snapshot/{repository_id}-{commit}.bundle"


def check_snapshot_capability(capability: dict[str, Any]) -> dict[str, Any]:
    source = capability.get("source")
    project = capability.get("project")
    ids = capability.get("repository_ids")
    if (
        not isinstance(source, dict)
        or not isinstance(source.get("full_name"), str)
        or (source.get("token") is not None and not isinstance(source.get("token"), str))
        or not isinstance(project, dict)
        or set(project) != set(PROJECT_FIELDS)
        or not isinstance(ids, list)
        or len(ids) != 1
        or type(ids[0]) is not int
        or not SHA_RE.fullmatch(str(capability.get("commit", "")))
        or not re.fullmatch(r"[0-9a-f]{32}", str(capability.get("nonce", "")))
        or not re.fullmatch(r"[0-9a-z]{12}", str(capability.get("submission_id", "")))
    ):
        raise SnapshotError("the broker returned a malformed snapshot capability")
    return capability


def bundle_name(key: str) -> str:
    return key.rsplit("/", 1)[1]


def snapshot(
    ticket: str,
    out: Path,
    work: Path,
    *,
    broker: Broker,
    identity: JobIdentity,
    github: GitHub,
    fetcher: Fetcher,
) -> dict[str, Any]:
    """Fetch, describe, bundle and upload. Returns the unfinalized descriptor."""
    capability = check_snapshot_capability(broker.open(ticket, "snapshot"))
    token = capability["source"]["token"]
    try:
        if token is not None:
            mask(token)
            mask(base64.b64encode(f"x-access-token:{token}".encode()).decode())
        return _snapshot(
            capability, token, out, work, broker=broker, identity=identity, github=github, fetcher=fetcher
        )
    finally:
        if token is not None:
            github.revoke(token)


def _snapshot(
    capability: dict[str, Any],
    token: str | None,
    out: Path,
    work: Path,
    *,
    broker: Broker,
    identity: JobIdentity,
    github: GitHub,
    fetcher: Fetcher,
) -> dict[str, Any]:
    submission_id = capability["submission_id"]
    commit = capability["commit"]
    project = {field: str(capability["project"][field]) for field in PROJECT_FIELDS}
    full_name = canonical_github_repository(f"{GITHUB}/{capability['source']['full_name']}").full_name
    metadata = github.repository(full_name, token)
    route = "source-app" if token is not None else "public"
    if metadata["id"] != capability["repository_ids"][0] or metadata["private"] != (route == "source-app"):
        raise SnapshotError("the source repository's identity or visibility changed since admission")
    env = git_environment(fetcher.home)
    source_git = fetcher.fetch(full_name, commit, work / "source.git", token=token)
    token = None  # Nothing below may use the source credential.
    manifest, entries, _ = source_manifest(source_git, commit, project["project_path"], env)
    check_project_paths(project, entries)
    raw_manifest = next(
        (
            read_blobs(source_git, [obj], env)[obj]
            for _, kind, obj, _, path in entries
            if kind == "blob" and path == lake_manifest_path(project["project_path"])
        ),
        None,
    )
    if raw_manifest is None:
        raise SnapshotError("the project has no committed lake-manifest.json")
    packages = manifest_git_packages(raw_manifest)
    trusted = trusted_closure(packages, root_manifest_reader(work, fetcher))
    dependencies = []
    for package in packages:
        repository = canonical_github_repository(package["url"])
        public = github.repository(repository.full_name, None)
        if public["private"]:
            raise SnapshotError(
                f"Git package {package['name']!r} is not public; private dependencies are refused"
            )
        dependencies.append(
            {
                **package,
                "url": repository.url,
                "repository_id": public["id"],
                "kind": "trusted-root" if package["name"] in trusted else "public",
            }
        )
    planned = broker.plan(
        [
            {
                "name": item["name"],
                "url": item["url"],
                "rev": item["rev"],
                "repository_id": item["repository_id"],
                "kind": item["kind"],
            }
            for item in dependencies
        ]
    )
    bundles = out / "bundles"
    bundles.mkdir(parents=True)
    source_key = object_key(submission_id, metadata["id"], commit)
    create_bundle(source_git, commit, bundles / bundle_name(source_key), env)
    rows = []
    for item in dependencies:
        bundle = None
        if item["kind"] == "public":
            key = object_key(submission_id, item["repository_id"], item["rev"])
            path = bundles / bundle_name(key)
            if not path.exists():
                dependency_git = fetcher.fetch(
                    canonical_github_repository(item["url"]).full_name,
                    item["rev"],
                    work / f"dependency-{item['repository_id']}-{item['rev']}.git",
                )
                create_bundle(dependency_git, item["rev"], path, env)
            bundle = {
                "key": key,
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
                "ref": VERIFIED_REF,
            }
        rows.append(
            {
                "name": item["name"],
                "url": item["url"],
                "rev": item["rev"],
                "kind": item["kind"],
                "repository_id": item["repository_id"],
                "bundle": bundle,
            }
        )
    manifest_bytes = canonical_json(manifest)
    (out / "source-manifest.json").write_bytes(manifest_bytes)
    total = sum(path.stat().st_size for path in bundles.iterdir()) + len(manifest_bytes)
    if total > MAX_SNAPSHOT_BYTES:
        raise SnapshotError("the snapshot exceeds 500 MiB")
    descriptor = {
        "schema": "palomar-snapshot/1",
        "submission_id": submission_id,
        "nonce": capability["nonce"],
        "pipeline_commit": identity.pipeline_commit,
        "caller": {
            "repository_id": identity.caller_repository_id,
            "workflow_sha": identity.caller_workflow_sha,
        },
        "run": {"id": identity.run_id, "attempt": identity.run_attempt},
        "captured_at": now(),
        "source": {
            "route": route,
            "repository_id": metadata["id"],
            "owner_id": metadata["owner"]["id"],
            "full_name": metadata["full_name"],
            "visibility": "private" if metadata["private"] else "public",
            "commit": commit,
            "tree": commit_tree(source_git, commit, env),
            "history": history(source_git, commit, env),
            "bundle": {
                "key": source_key,
                "sha256": sha256_file(bundles / bundle_name(source_key)),
                "size": (bundles / bundle_name(source_key)).stat().st_size,
                "ref": VERIFIED_REF,
            },
        },
        "project": project,
        "lake_manifest_sha256": sha256_bytes(raw_manifest),
        "source_manifest_sha256": sha256_bytes(manifest_bytes),
        "dependencies": rows,
    }
    check_descriptor(descriptor)
    keys = {reference["key"] for reference, _ in descriptor_bundles(descriptor)}
    manifest_key = f"v2/{submission_id}/snapshot/source-manifest.json"
    if not keys | {manifest_key} <= set(planned) | set(capability.get("object_keys", [])):
        raise SnapshotError("the broker's plan does not cover every snapshot object")
    for reference, _ in descriptor_bundles(descriptor):
        broker.put(reference["key"], bundles / bundle_name(reference["key"]))
    broker.put(manifest_key, out / "source-manifest.json")
    (out / "descriptor-draft.json").write_bytes(canonical_json(descriptor))
    return descriptor


# --------------------------------------------------------------------------
# Validation: restore what was uploaded, prove it, then finalize


def restore_and_check(
    descriptor: dict[str, Any],
    bundles: Path,
    manifest_bytes: bytes,
    work: Path,
    *,
    fetcher: Fetcher,
) -> None:
    """Every check of the validation job (contract v2, 3.3) against local bytes."""
    check_descriptor(descriptor)
    env = git_environment(fetcher.home)
    if sha256_bytes(manifest_bytes) != descriptor["source_manifest_sha256"]:
        raise SnapshotError("the source manifest does not match the descriptor")
    restored: dict[str, Path] = {}
    for reference, target in descriptor_bundles(descriptor):
        path = bundles / bundle_name(reference["key"])
        if path.stat().st_size != reference["size"] or sha256_file(path) != reference["sha256"]:
            raise SnapshotError("a bundle does not match the descriptor")
        restored[reference["key"]] = restore_bundle(
            path, target, work / f"restored-{bundle_name(reference['key'])}", env
        )
    source = descriptor["source"]
    source_git = restored[source["bundle"]["key"]]
    if commit_tree(source_git, source["commit"], env) != source["tree"]:
        raise SnapshotError("the restored tree differs from the descriptor")
    if history(source_git, source["commit"], env) != source["history"]:
        raise SnapshotError("the restored history differs from the descriptor")
    manifest, entries, blobs = source_manifest(
        source_git, source["commit"], descriptor["project"]["project_path"], env
    )
    if canonical_json(manifest) != manifest_bytes:
        raise SnapshotError("the restored source manifest differs from the uploaded one")
    check_project_paths(descriptor["project"], entries)
    lake = next(
        (
            blobs[obj]
            for _, kind, obj, _, path in entries
            if kind == "blob" and path == lake_manifest_path(descriptor["project"]["project_path"])
        ),
        None,
    )
    if lake is None or sha256_bytes(lake) != descriptor["lake_manifest_sha256"]:
        raise SnapshotError("the restored lake-manifest.json differs from the descriptor")
    packages = manifest_git_packages(lake)
    trusted = trusted_closure(packages, root_manifest_reader(work, fetcher))
    expected = [
        (p["name"], p["url"], p["rev"], "trusted-root" if p["name"] in trusted else "public")
        for p in packages
    ]
    recorded = [(d["name"], d["url"], d["rev"], d["kind"]) for d in descriptor["dependencies"]]
    if expected != recorded:
        raise SnapshotError("the descriptor's dependencies are not the restored manifest's closure")


def validate(
    ticket: str,
    draft: Path,
    out: Path,
    work: Path,
    *,
    broker: Broker,
    identity: JobIdentity,
    fetcher: Fetcher,
) -> str:
    capability = broker.open(ticket, "validate")
    descriptor = check_descriptor(json.loads(draft.read_bytes()))
    if (
        descriptor["pipeline_commit"] != identity.pipeline_commit
        or descriptor["run"] != {"id": identity.run_id, "attempt": identity.run_attempt}
        or descriptor["submission_id"] != capability.get("submission_id")
    ):
        raise SnapshotError("the descriptor is not this run's snapshot")
    keys = capability.get("object_keys")
    manifest_key = f"v2/{descriptor['submission_id']}/snapshot/source-manifest.json"
    wanted = {reference["key"] for reference, _ in descriptor_bundles(descriptor)} | {manifest_key}
    if not isinstance(keys, list) or set(keys) != wanted:
        raise SnapshotError("the complete snapshot objects are not the ones the descriptor names")
    bundles = out / "bundles"
    bundles.mkdir(parents=True)
    for key in sorted(wanted - {manifest_key}):
        broker.get(key, bundles / bundle_name(key))
    broker.get(manifest_key, out / "source-manifest.json")
    restore_and_check(descriptor, bundles, (out / "source-manifest.json").read_bytes(), work, fetcher=fetcher)
    raw = canonical_json(descriptor)
    digest = broker.finalize(descriptor)
    if digest != sha256_bytes(raw):
        raise SnapshotError("the broker finalized a different descriptor")
    (out / "descriptor.json").write_bytes(raw)
    return digest


def restore(
    ticket: str, out: Path, work: Path, *, broker: Broker, fetcher: Fetcher, expected: str | None
) -> str:
    capability = broker.open(ticket, "restore")
    digest = capability.get("snapshot_sha256")
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest) or (expected and digest != expected):
        raise SnapshotError("the restore capability names another snapshot")
    submission_id = str(capability.get("submission_id", ""))
    out.mkdir(parents=True, exist_ok=True)
    broker.get(f"v2/{submission_id}/snapshot/descriptor.json", out / "descriptor.json")
    raw = (out / "descriptor.json").read_bytes()
    if sha256_bytes(raw) != digest:
        raise SnapshotError("the restored descriptor is not the recorded snapshot")
    descriptor = check_descriptor(json.loads(raw))
    if canonical_json(descriptor) != raw:
        raise SnapshotError("the restored descriptor is not canonical JSON")
    bundles = out / "bundles"
    bundles.mkdir(parents=True)
    for reference, _ in descriptor_bundles(descriptor):
        broker.get(reference["key"], bundles / bundle_name(reference["key"]))
    broker.get(f"v2/{submission_id}/snapshot/source-manifest.json", out / "source-manifest.json")
    restore_and_check(descriptor, bundles, (out / "source-manifest.json").read_bytes(), work, fetcher=fetcher)
    return digest


# --------------------------------------------------------------------------
# Command line


def running_revision() -> str:
    """The commit of the PalomarSubmission checkout this script runs from."""
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="palomar-git-home-") as home:
        return (
            git(["rev-parse", "HEAD"], git_dir=root / ".git", env=git_environment(Path(home)))
            .decode()
            .strip()
        )


def write_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=sorted(OPERATIONS))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--draft", type=Path)
    parser.add_argument("--snapshot-sha256", default="")
    args = parser.parse_args()
    ticket = os.environ.get("PALOMAR_TICKET", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", ticket):
        print("::error::this run carries no valid broker ticket")
        return 1
    mask(ticket)
    workflow = f"confidential-{args.command}.yml"
    identity = JobIdentity.from_claims(jwt_claims(oidc_token()), workflow)
    if (
        running_revision() != identity.pipeline_commit
        or os.environ.get("PALOMAR_PIPELINE_COMMIT") != identity.pipeline_commit
    ):
        # The scripts running now must be the revision the broker will check.
        print("::error::the checked-out pipeline is not this job's pinned revision")
        return 1
    broker = Broker(token_source=oidc_token)
    fetcher = Fetcher()
    for directory in (args.out, args.work_dir):
        directory.mkdir(parents=True, exist_ok=False)
    try:
        if args.command == "snapshot":
            snapshot(
                ticket,
                args.out,
                args.work_dir,
                broker=broker,
                identity=identity,
                github=GitHub(read_token=os.environ.get("GITHUB_TOKEN")),
                fetcher=fetcher,
            )
        elif args.command == "validate":
            if args.draft is None:
                parser.error("validate needs --draft")
            write_output(
                "snapshot_sha256",
                validate(
                    ticket,
                    args.draft,
                    args.out,
                    args.work_dir,
                    broker=broker,
                    identity=identity,
                    fetcher=fetcher,
                ),
            )
        else:
            write_output(
                "snapshot_sha256",
                restore(
                    ticket,
                    args.out,
                    args.work_dir,
                    broker=broker,
                    fetcher=fetcher,
                    expected=args.snapshot_sha256 or None,
                ),
            )
    except SnapshotError as error:
        # The message names no repository, path or ref; it is safe in a log.
        print(f"::error::{error}")
        return 1
    finally:
        shutil.rmtree(args.work_dir, ignore_errors=True)
        shutil.rmtree(fetcher.home, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
