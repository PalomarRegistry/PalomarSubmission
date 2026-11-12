#!/usr/bin/env python3
"""Select the small Lean release set a dispatch conformance report would name.

The pull-request cold build stays on one toolchain, the image
``compatibility.yml`` already installs. This selector does not build Lean
and must not be used to add those builds to ``pull_request``.

Selected at implementation
--------------------------
These two constants are frozen here. They are versions already named in this
repository, not a scan of every tag the repository mentions.

* ``COMPATIBILITY_WORKFLOW_TOOLCHAIN`` is ``leanprover/lean4:v4.35.0-rc2``,
  the toolchain ``compatibility.yml`` installs for the cold build.
* ``TOOLCHAIN_FLOOR`` is ``v4.35.0-rc2``, the
  ``trusted_tools.toolchain_floor`` in ``verification-profile.json``.

The supported baseline is read from ``toolchains.json`` (``minimum``) so the
report also names the current floor. A release outside that set is refused.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TOOLCHAINS_PATH = ROOT / "toolchains.json"

# Selected at implementation. See the module docstring.
COMPATIBILITY_WORKFLOW_TOOLCHAIN = "leanprover/lean4:v4.35.0-rc2"
TOOLCHAIN_FLOOR = "v4.35.0-rc2"


class ReleaseMatrixError(ValueError):
    """An unknown release was asked for and will not be tested."""


def _lean_toolchain(release: str) -> str:
    if release.startswith("leanprover/lean4:"):
        return release
    if release.startswith("v"):
        return f"leanprover/lean4:{release}"
    raise ReleaseMatrixError(
        f"refusing unknown release {release!r}; the conformance matrix will not test it"
    )


def supported_baseline(toolchains_path: Path | None = None) -> str:
    """The current supported baseline, read from toolchains.json."""
    path = toolchains_path or TOOLCHAINS_PATH
    document = json.loads(path.read_text(encoding="utf-8"))
    minimum = document.get("minimum") if isinstance(document, dict) else None
    if not isinstance(minimum, str) or not minimum.startswith("v"):
        raise ReleaseMatrixError("toolchains.json minimum is not a release tag")
    return _lean_toolchain(minimum)


def matrix_entries(toolchains_path: Path | None = None) -> list[dict[str, str]]:
    """Baseline from toolchains.json, plus the two selected-at-implementation constants."""
    return [
        {
            "role": "supported_baseline",
            "origin": "toolchains.json",
            "toolchain": supported_baseline(toolchains_path),
        },
        {
            "role": "compatibility_workflow",
            "origin": "selected-at-implementation",
            "toolchain": COMPATIBILITY_WORKFLOW_TOOLCHAIN,
            "documented_as": "compatibility.yml installs leanprover/lean4:v4.35.0-rc2",
        },
        {
            "role": "toolchain_floor",
            "origin": "selected-at-implementation",
            "toolchain": _lean_toolchain(TOOLCHAIN_FLOOR),
            "documented_as": "verification-profile trusted_tools.toolchain_floor",
        },
    ]


def known_toolchains(toolchains_path: Path | None = None) -> list[str]:
    seen: list[str] = []
    for entry in matrix_entries(toolchains_path):
        if entry["toolchain"] not in seen:
            seen.append(entry["toolchain"])
    return seen


def select_release(release: str, toolchains_path: Path | None = None) -> str:
    """Return a matrix toolchain. Anything else is refused, not tested."""
    canonical = _lean_toolchain(release.strip())
    if canonical not in known_toolchains(toolchains_path):
        raise ReleaseMatrixError(
            f"refusing unknown release {release!r}; the conformance matrix will not test it"
        )
    return canonical


def _checker_present(checker: Path | None) -> str | None:
    if checker is None:
        return None
    candidate = Path(checker)
    if candidate.is_symlink() or not candidate.is_file():
        return None
    return str(candidate.resolve())


def conformance_report(
    *,
    checker: Path | None = None,
    release: str | None = None,
    toolchains_path: Path | None = None,
) -> dict[str, Any]:
    """Pinned description of what a dispatch conformance run would test.

    An unknown ``release`` is refused before a report is built, so it cannot
    be recorded as tested. When the checker binary is absent the report is
    skipped and inconclusive: the matrix is named, not executed.
    """
    entries = matrix_entries(toolchains_path)
    if release is not None and release.strip():
        chosen = select_release(release, toolchains_path)
        entries = [entry for entry in entries if entry["toolchain"] == chosen]
        if not entries:
            raise ReleaseMatrixError(
                f"refusing unknown release {release!r}; the conformance matrix will not test it"
            )
    checker_path = _checker_present(checker)
    distinct: list[str] = []
    for entry in entries:
        if entry["toolchain"] not in distinct:
            distinct.append(entry["toolchain"])
    report: dict[str, Any] = {
        "schema_version": 1,
        "pull_request_cold_build": "single-toolchain",
        "selected_at_implementation": [
            {"name": "compatibility_workflow", "toolchain": COMPATIBILITY_WORKFLOW_TOOLCHAIN},
            {"name": "toolchain_floor", "toolchain": _lean_toolchain(TOOLCHAIN_FLOOR)},
        ],
        "would_test": entries,
        "distinct_releases": distinct,
    }
    if checker_path is None:
        report["status"] = "skipped"
        report["conclusion"] = "inconclusive"
        report["reason"] = "checker binary is absent"
        return report
    report["status"] = "selected"
    report["conclusion"] = "would-test"
    report["checker"] = checker_path
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--checker", type=Path, default=None)
    parser.add_argument("--release", default=None)
    parser.add_argument("--toolchains", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        report = conformance_report(
            checker=args.checker,
            release=args.release,
            toolchains_path=args.toolchains,
        )
    except (OSError, json.JSONDecodeError, ReleaseMatrixError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
