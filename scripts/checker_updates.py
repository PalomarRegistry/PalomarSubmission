"""Materialize maintainer-pinned con-ron updates independently of the compiler.

Only the manifest shipped with this verifier can select an update. Submitted
Lake files, environment variables and configuration cannot choose a checker.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts.trusted_download import (
    DownloadAuthenticationError,
    DownloadPermanentError,
    DownloadTransientError,
    download,
)
from scripts.verification_errors import VerificationError

MANIFEST = Path(__file__).resolve().parent.parent / "checker-revisions.json"
TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-rc[0-9]+)?")


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate checker manifest key: {key}")
        result[key] = value
    return result


def revision_manifest() -> dict[str, dict[str, str]]:
    document = json.loads(MANIFEST.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    if set(document) != {"schema_version", "con_ron"} or document["schema_version"] != 1:
        raise ValueError("unsupported checker revision manifest")
    revisions = document["con_ron"]
    if not isinstance(revisions, dict):
        raise ValueError("checker revisions must be keyed by exact source toolchain")
    for source, revision in revisions.items():
        if not source.startswith("leanprover/lean4:") or not TAG.fullmatch(source.split(":")[-1]):
            raise ValueError("invalid checker source toolchain")
        if not isinstance(revision, dict) or set(revision) != {
            "release_tag", "release_commit", "archive_sha256", "binary_sha256",
        }:
            raise ValueError("invalid checker revision fields")
        if not TAG.fullmatch(revision["release_tag"]):
            raise ValueError("invalid checker release tag")
        for field, length in (("release_commit", 40), ("archive_sha256", 64), ("binary_sha256", 64)):
            if not re.fullmatch(r"[0-9a-f]{" + str(length) + "}", revision[field]):
                raise ValueError(f"invalid checker {field}")
    return revisions


def selected_revision(toolchain: str) -> dict[str, str] | None:
    return revision_manifest().get(toolchain)


def verify_release_commits(resolve: Callable[[str, str], str]) -> None:
    """CI authenticates every mapped release, including non-fixture toolchains."""
    releases = {(value["release_tag"], value["release_commit"]) for value in revision_manifest().values()}
    for tag, commit in sorted(releases):
        if resolve("leanprover/lean4", tag) != commit:
            raise ValueError(f"pinned checker release commit disagrees with the official tag {tag}")


def materialize_checker(toolchain: str, bundled: Path, destination: Path) -> tuple[Path, dict[str, Any]]:
    """Return the actual executable and public provenance; never fall back on failure."""
    archive = None
    try:
        revision = selected_revision(toolchain)
        if revision is None:
            return bundled, {"kind": "bundled", "source_toolchain": toolchain}
        if platform.system() != "Linux" or platform.machine() != "x86_64":
            raise ValueError("pinned checker updates currently require Linux x86_64")
        destination.mkdir(parents=True, exist_ok=False)
        release = revision["release_tag"]
        stem = f"lean-{release.removeprefix('v')}-linux"
        url = f"https://github.com/leanprover/lean4/releases/download/{release}/{stem}.tar.zst"
        archive = destination / "release.tar.zst"
        download(url, archive, revision["archive_sha256"], max_bytes=700 * 1024 * 1024)
        # The archive is authenticated before parsing, and only this exact
        # member is written to stdout. tar never extracts paths onto the host.
        completed = subprocess.run(
            ["tar", "--zstd", "-xOf", str(archive), "--", f"{stem}/bin/con-ron"],
            capture_output=True, check=True, timeout=300,
        )
        binary = completed.stdout
        if hashlib.sha256(binary).hexdigest() != revision["binary_sha256"]:
            raise DownloadAuthenticationError("checker binary checksum mismatch")
        executable = destination / "con-ron"
        executable.write_bytes(binary)
        executable.chmod(0o555)
        return executable.resolve(), {
            "kind": "maintainer-pinned-release", "source_toolchain": toolchain,
            **revision, "archive_url": url,
        }
    except (OSError, ValueError, TypeError, subprocess.SubprocessError, RuntimeError) as error:
        code, retryable = "palomar.checker_policy_invalid", False
        if isinstance(error, DownloadAuthenticationError):
            code = "palomar.checker_authentication_failed"
        elif isinstance(error, (DownloadTransientError, subprocess.TimeoutExpired)):
            code, retryable = "provider.checker_download_failed", True
        elif isinstance(error, DownloadPermanentError):
            code = "provider.checker_download_refused"
        elif isinstance(error, subprocess.CalledProcessError):
            code = "palomar.checker_install_failed"
        raise VerificationError(
            "Palomar could not authenticate the selected checker update",
            code=code, owner="provider" if code.startswith("provider.") else "palomar",
            detail=str(error)[:1500],
            next_action=(
                "Do not change the repository. Retry the same commit later."
                if retryable
                else "Palomar must repair its pinned checker release; do not change the submission."
            ),
            retryable=retryable,
        ) from error
    finally:
        if archive is not None:
            archive.unlink(missing_ok=True)
