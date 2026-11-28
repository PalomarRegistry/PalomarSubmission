"""Source tarballs and withdrawal tombstones for confidential runs.

A confidential run never checks out GitHub. The submission server stores the
submitted commit's tarball in R2 at intake and dispatches the run with three
presigned URLs' worth of inputs: `source_url` and `source_sha256` for that
tarball, and `tombstone_url` for the submission's withdrawal tombstone. The
job refuses a tarball whose SHA-256 differs, unpacks it into a fresh Git
repository with one commit of the whole tree (so the verifier's tracked-file
and attribute checks still run), and stops before starting and before each
upload while a tombstone exists.

Only HTTP 404 from the tombstone URL means "not withdrawn". Any other answer,
including an expired URL or a network failure, stops the run. The URLs are
credentials for one object each, so no message here ever repeats them.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.verification_errors import VerificationError  # noqa: E402

# The verifier's own source cap applies to the compressed download and to the
# unpacked tree alike.
MAX_SOURCE_BYTES = 500 * 1024 * 1024
MAX_URL_LENGTH = 4096
SHA256_RE = re.compile(r"[0-9a-f]{64}")
RETRY_DELAYS = (5, 20)
INPUTS = ("source_url", "source_sha256", "tombstone_url")
# A fixed identity and date make the synthetic commit reproducible; nothing in
# it names the source repository.
COMMIT_ENVIRONMENT = {
    "GIT_AUTHOR_NAME": "Palomar",
    "GIT_AUTHOR_EMAIL": "confidential@palomar-registry.org",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
    "GIT_COMMITTER_NAME": "Palomar",
    "GIT_COMMITTER_EMAIL": "confidential@palomar-registry.org",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _open(url: str, timeout: float) -> Any:
    return build_opener(_NoRedirect).open(Request(url, method="GET"), timeout=timeout)


def checked_url(value: str, field: str) -> str:
    parsed = urlparse(value)
    if (
        len(value) > MAX_URL_LENGTH
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(character.isspace() for character in value)
    ):
        raise VerificationError(f"{field} must be an HTTPS URL without credentials")
    return value


def confidential_inputs(inputs: Mapping[str, Any]) -> dict[str, str] | None:
    """The confidential source inputs, or None for an ordinary public run.

    A public run supplies none of the three. A confidential run supplies all
    three, and a partial set is refused rather than read as public.
    """
    values = {name: str(inputs.get(name) or "").strip() for name in INPUTS}
    if not any(values.values()):
        return None
    if not all(values.values()):
        raise VerificationError("Confidential runs need source_url, source_sha256 and tombstone_url together")
    checked_url(values["source_url"], "source_url")
    checked_url(values["tombstone_url"], "tombstone_url")
    if not SHA256_RE.fullmatch(values["source_sha256"]):
        raise VerificationError("source_sha256 must be 64 lowercase hexadecimal characters")
    return values


def _with_retries(action: Callable[[], Any], sleep: Callable[[float], None]) -> Any:
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            return action()
        except HTTPError as error:
            error.close()
            transient = error.code in {408, 429} or error.code >= 500
            if not transient or attempt == len(RETRY_DELAYS):
                raise
        except (URLError, TimeoutError, ConnectionError):
            if attempt == len(RETRY_DELAYS):
                raise
        sleep(RETRY_DELAYS[attempt])
    raise AssertionError("unreachable")


def tombstone_absent(url: str, *, sleep: Callable[[float], None] = time.sleep) -> bool:
    """True only when the tombstone URL answers 404. Everything else stops."""

    def probe() -> bool:
        try:
            with _open(url, 30) as response:
                response.read(4096)
            return False
        except HTTPError as error:
            error.close()
            if error.code == 404:
                return True
            raise

    try:
        return _with_retries(probe, sleep)
    except (HTTPError, URLError, TimeoutError, ConnectionError, OSError):
        return False


def download(
    url: str, expected_sha256: str, destination: Path, *, sleep: Callable[[float], None] = time.sleep
) -> None:
    """Download the tarball and refuse it unless its SHA-256 is the expected one."""

    def fetch() -> None:
        digest = hashlib.sha256()
        total = 0
        with _open(url, 120) as response, destination.open("wb") as handle:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_SOURCE_BYTES:
                    raise VerificationError("Confidential source tarball exceeds the 500 MiB cap")
                digest.update(chunk)
                handle.write(chunk)
        if digest.hexdigest() != expected_sha256:
            raise VerificationError("Confidential source tarball does not match source_sha256")

    try:
        _with_retries(fetch, sleep)
    except VerificationError:
        destination.unlink(missing_ok=True)
        raise
    except (HTTPError, URLError, TimeoutError, ConnectionError, OSError) as error:
        destination.unlink(missing_ok=True)
        # Never repeat the URL: it is a credential for the object.
        raise VerificationError("Confidential source tarball could not be downloaded") from error


def unpack(archive: Path, destination: Path) -> None:
    """Unpack a GitHub commit tarball, dropping its single top-level directory.

    GitHub names that directory after the repository, so it never reaches
    the destination. Only directories, regular files and symlinks inside the
    tree are accepted; the standard-library `data` filter refuses absolute
    paths, `..` and links that leave the tree.
    """
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        roots = {PurePosixPath(member.name).parts[0] for member in members if member.name}
        if len(roots) != 1:
            raise VerificationError("Confidential source tarball must hold exactly one top-level directory")
        total = 0
        selected = []
        for member in members:
            parts = PurePosixPath(member.name).parts
            if len(parts) == 1:
                if not member.isdir():
                    raise VerificationError("Confidential source tarball has a top-level file")
                continue
            if ".git" in parts[1:]:
                raise VerificationError("Confidential source tarball contains Git metadata")
            if not (member.isdir() or member.isfile() or member.issym()):
                raise VerificationError("Confidential source tarball contains an unsupported entry type")
            total += member.size
            if total > MAX_SOURCE_BYTES:
                raise VerificationError("Confidential source exceeds the 500 MiB cap")
            member.name = PurePosixPath(*parts[1:]).as_posix()
            selected.append(member)
        destination.mkdir(parents=True)
        try:
            bundle.extractall(destination, members=selected, filter="data")
        except tarfile.FilterError as error:
            raise VerificationError("Confidential source tarball contains an unsafe entry") from error


def commit_tree(checkout: Path) -> None:
    """Make the unpacked tree one commit, so Git-based checks see every file.

    `--force` keeps files that the tree's own `.gitignore` would skip: the
    tarball holds exactly the commit's tracked files.
    """
    environment = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        **COMMIT_ENVIRONMENT,
    }
    git = [
        "git", "-c", "core.hooksPath=/dev/null", "-c", "gc.auto=0", "-c", "maintenance.auto=false",
        "-c", "commit.gpgSign=false", "-C", str(checkout),
    ]
    for command in (
        ["git", "-c", "core.hooksPath=/dev/null", "init", "-q", str(checkout)],
        [*git, "add", "--all", "--force"],
        [*git, "commit", "-q", "--no-verify", "--allow-empty", "-m", "Confidential source"],
    ):
        result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise VerificationError("Confidential source could not be committed")


def materialize(url: str, expected_sha256: str, destination: Path) -> None:
    """Replace `git clone` for a confidential run: download, check, unpack, commit."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="palomar-confidential-", dir=destination.parent) as raw:
        archive = Path(raw) / "source.tar.gz"
        download(url, expected_sha256, archive)
        staged = Path(raw) / "source"
        unpack(archive, staged)
        archive.unlink()
        commit_tree(staged)
        shutil.move(str(staged), str(destination))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stop a confidential run while its tombstone exists.")
    parser.add_argument("command", choices=["check-tombstone"])
    parser.parse_args(argv)
    try:
        url = checked_url(os.environ.get("PALOMAR_TOMBSTONE_URL", ""), "tombstone_url")
    except VerificationError as error:
        print(f"::error::{error}")
        return 1
    if not tombstone_absent(url):
        print("::error::The submission is withdrawn, or its tombstone could not be checked; stopping")
        return 1
    print("No withdrawal tombstone")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
