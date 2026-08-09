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

This lives in a file rather than in a workflow heredoc so that the tests can
run the production code against a planted fixture. Asserting that a glob
appears in YAML would prove only that a string is present, which is the shape
of the failure this check exists to catch.
"""

from __future__ import annotations

import argparse
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


def scanned_paths(root: pathlib.Path) -> list[pathlib.Path]:
    """Every file the check reads, in a fixed order."""
    return [
        root / "SECURITY.md",
        root / "README.md",
        *sorted((root / "docs").rglob("*.md")),
        *sorted((root / "scripts").rglob("*.py")),
    ]


def report(root: pathlib.Path, stream) -> bool:
    """Print one line per match and say whether anything was found."""
    failed = False
    for path in scanned_paths(root):
        relative = path.relative_to(root)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            # A target that cannot be read is a target nobody read. Passing it
            # over quietly is the same silence this check exists to break.
            print(f"{relative}: unreadable: {error}", file=stream)
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
