#!/usr/bin/env python3
"""Read the primitive target list of every supported Lean release.

`primitive_targets` parses `Lake/CLI/Check.lean` from the submitted
toolchain, and refuses a shape it does not recognise. A Lean release that
changes that shape then fails every submission on it until Palomar adapts.
This check runs the same parser over each release tag at or above the
`toolchains.json` minimum, so a new release that breaks it fails here first.

It reads the release tags with a shallow, blobless Git fetch, so it needs
no toolchain install and no GitHub API quota. The file at
`src/lake/Lake/CLI/Check.lean` in the tag is the one a toolchain ships as
`src/lean/lake/Lake/CLI/Check.lean`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts.verification_errors import VerificationError  # noqa: E402
from scripts.verify_submission import VERSION_RE, parse_lean_version, primitive_targets  # noqa: E402

LEAN4_REMOTE = "https://github.com/leanprover/lean4"


def supported_release_tags(tags: list[str], minimum: str) -> list[str]:
    """The release tags Palomar accepts for a new submission, oldest first."""
    floor = parse_lean_version(minimum, VERSION_RE)
    releases = [tag for tag in tags if VERSION_RE.fullmatch(tag)]
    return sorted(
        (tag for tag in releases if parse_lean_version(tag, VERSION_RE) >= floor),
        key=lambda tag: parse_lean_version(tag, VERSION_RE),
    )


def release_problems(tag: str, check_lean: bytes) -> list[str]:
    """Why `primitive_targets` cannot be trusted on this release, if it cannot."""
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "src" / "lean" / "lake" / "Lake" / "CLI" / "Check.lean"
        source.parent.mkdir(parents=True)
        source.write_bytes(check_lean)
        try:
            names = primitive_targets(Path(directory))
        except VerificationError as error:
            return [f"{tag}: {error}"]
    problems = []
    if names[:3] != ["Nat.add", "Nat.sub", "Nat.mul"]:
        problems.append(f"{tag}: the list does not begin with Nat.add, Nat.sub, Nat.mul: {names}")
    if names[-1] != "outParam":
        problems.append(f"{tag}: the list does not end with outParam: {names}")
    for required in ("eagerReduce", "String.mk", "Char.ofNat"):
        if required not in names:
            problems.append(f"{tag}: the list lacks {required}: {names}")
    if "Nat.zero" in names:
        problems.append(f"{tag}: the list includes the commented-out Nat.zero: {names}")
    return problems


def git(*argv: str, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--remote", default=LEAN4_REMOTE, help="the lean4 repository to read tags from")
    arguments = parser.parse_args()
    minimum = json.loads((ROOT / "toolchains.json").read_text(encoding="utf-8"))["minimum"]
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        git("init", "--quiet", cwd=work)
        listed = git("ls-remote", "--tags", "--refs", arguments.remote, "v*", cwd=work).stdout.decode()
        names = [line.split("\t", 1)[1].removeprefix("refs/tags/") for line in listed.splitlines()]
        tags = supported_release_tags(names, minimum)
        if not tags:
            print(f"::error::no lean4 release tags at or above {minimum}")
            return 1
        git(
            "fetch", "--quiet", "--depth=1", "--filter=blob:none", "--no-tags", arguments.remote,
            *(f"refs/tags/{tag}:refs/tags/{tag}" for tag in tags), cwd=work,
        )
        problems = []
        for tag in tags:
            shown = subprocess.run(
                ["git", "show", f"{tag}:src/lake/Lake/CLI/Check.lean"], cwd=work, capture_output=True
            )
            if shown.returncode != 0:
                found = [f"{tag}: src/lake/Lake/CLI/Check.lean is missing"]
            else:
                found = release_problems(tag, shown.stdout)
            problems.extend(found)
            print(f"{tag}: {'FAILED' if found else 'ok'}")
    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
