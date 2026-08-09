#!/usr/bin/env python3
"""Reject wording that only the retired issue-based intake produced.

Not the bare word "issue": reporting one privately is still the right thing to
do, and the tracker still has issues. These are collocations only the retired
intake produced, so a match means a file has drifted back to describing a
pipeline that is gone.

The scan reads `SECURITY.md`, `README.md`, every Markdown file below `docs/`,
and every Python file below `scripts/`, including the ones sitting directly in
it. The Python half is here because the workflow's own entrypoint went on
describing the retired mechanism in its first line while a check named after
exactly that wording stayed green: a check that does not read the files it was
written to protect reports the absence of a problem it never looked for.

`docs/launch-security-review.md` is a historical record and is scanned anyway,
because a reader who arrives at a stale sentence cannot tell which kind of
document they are in. Describing the July design in words that are not these
ones costs a sentence and reads better.

Anything the scan cannot read is a failure rather than a file it passes over,
because a shrinking scan surface is invisible in a passing run. That covers a
root that is missing or is not a directory, a subdirectory that cannot be
listed, a file that is not UTF-8, and a subdirectory reached through a symbolic
link, whose tree this repository does not hold: git stores the link, so the
files behind it are either scanned under their real path or are not repository
content at all.

This lives in a file rather than in a workflow heredoc so that the tests can
run the production code against a planted fixture. Asserting that a glob
appears in YAML would prove only that a string is present, which is the shape
of the failure this check exists to catch.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys

RETIRED = re.compile(
    r"issue-form|issue field|issue number|issue-triggered|issue-write"
    r"|issue-reporting|issue-based|triggering issue|reporting job|report_issue"
    r"|issues/new",
    re.IGNORECASE,
)

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _below(directory: pathlib.Path, suffix: str):
    """Files with this suffix below this directory, and what stopped the walk.

    `os.walk` rather than `Path.rglob`, because `rglob` swallows the errors it
    meets while walking. A subtree nobody could list would then leave no trace
    at all: the scan would cover fewer files and still pass.
    """
    files: list[pathlib.Path] = []
    blocked: list[tuple[pathlib.Path, str]] = []
    if not directory.is_dir():
        return files, [(directory, "not a directory the scan can enumerate")]

    def stopped(error: OSError) -> None:
        blocked.append((pathlib.Path(error.filename), str(error)))

    for parent, directories, names in os.walk(directory, onerror=stopped):
        directories.sort()
        for name in directories:
            child = pathlib.Path(parent) / name
            if child.is_symlink():
                blocked.append((child, "symbolic link to a directory"))
        files.extend(
            pathlib.Path(parent) / name for name in sorted(names) if name.endswith(suffix)
        )
    return sorted(files), blocked


def scan_surface(root: pathlib.Path):
    """Every file the check reads, in a fixed order, and what it could not read."""
    files = [root / "SECURITY.md", root / "README.md"]
    blocked: list[tuple[pathlib.Path, str]] = []
    for directory, suffix in ((root / "docs", ".md"), (root / "scripts", ".py")):
        found, stopped = _below(directory, suffix)
        files += found
        blocked += stopped
    return files, blocked


def report(root: pathlib.Path, stream) -> bool:
    """Print one line per match and per unread file, and say whether there were any."""
    files, blocked = scan_surface(root)
    failed = False
    for path, reason in blocked:
        print(f"{path.relative_to(root)}: not scanned: {reason}", file=stream)
        failed = True
    for path in files:
        relative = path.relative_to(root)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            print(f"{relative}: not scanned: {error}", file=stream)
            failed = True
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for match in RETIRED.finditer(line):
                print(
                    f"{relative}:{number}: retired intake wording: {match.group(0)}",
                    file=stream,
                )
                failed = True
    return failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reject retired intake wording.")
    parser.add_argument(
        "root",
        nargs="?",
        type=pathlib.Path,
        default=REPOSITORY_ROOT,
        help="repository root to scan, defaulting to the checkout this file is in",
    )
    arguments = parser.parse_args(argv)
    return int(report(arguments.root, sys.stdout))


if __name__ == "__main__":
    raise SystemExit(main())
