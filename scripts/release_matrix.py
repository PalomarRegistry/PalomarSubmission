"""Select a bounded Lean release matrix. This does not run a cold build.

The per-PR compatibility job stays on the single baseline. A dispatch report
names the baseline, one stable release and one release candidate that already
have checker revisions in this repository. An unknown release is refused.
A release whose checker binary is not on disk is skipped, not treated as a pass.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


class ReleaseMatrixError(ValueError):
    pass


def toolchain_map(document: dict) -> dict:
    if isinstance(document.get("con_ron"), dict):
        return document["con_ron"]
    return document


def selected_releases(revisions: dict, minimum: str) -> list[dict]:
    """baseline, stable, and one RC, all present in checker-revisions.json."""
    revisions = toolchain_map(revisions)
    if not isinstance(revisions, dict):
        raise ReleaseMatrixError("checker revisions must be an object")
    baseline = f"leanprover/lean4:{minimum}"
    stable = "leanprover/lean4:v4.35.0"
    candidate = "leanprover/lean4:v4.35.0-rc4"
    chosen = [
        ("baseline", baseline),
        ("stable", stable),
        ("release-candidate", candidate),
    ]
    report = []
    for role, toolchain in chosen:
        if toolchain not in revisions:
            raise ReleaseMatrixError(f"selected {role} {toolchain} has no checker revision")
        report.append({"role": role, "toolchain": toolchain, "per_pr": role == "baseline"})
    return report


def refuse_unknown(toolchain: str, revisions: dict) -> None:
    revisions = toolchain_map(revisions)
    if toolchain not in revisions:
        raise ReleaseMatrixError(f"unknown release {toolchain} is not in the conformance matrix")


def conformance_report(revisions: dict, minimum: str, checker_present: dict[str, bool]) -> dict:
    rows = []
    for item in selected_releases(revisions, minimum):
        present = checker_present.get(item["toolchain"])
        if present is True:
            status = "selected"
        elif present is False or present is None:
            status = "skipped"
        else:
            raise ReleaseMatrixError("checker presence must be a boolean or omitted")
        rows.append({**item, "status": status, "reason": None if status == "selected" else "checker binary absent"})
    return {
        "schema_version": 1,
        "pull_request_cold_build": "baseline-only",
        "releases": rows,
        "negative_control": "unknown releases are refused",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-unknown", help="refuse this toolchain if it is not selected")
    args = parser.parse_args(argv)
    try:
        packages = json.loads((ROOT / "checker-revisions.json").read_text(encoding="utf-8"))
        if not isinstance(packages, dict):
            raise ReleaseMatrixError("checker-revisions.json is malformed")
        minimum = json.loads((ROOT / "toolchains.json").read_text(encoding="utf-8"))["minimum"]
        if args.check_unknown:
            refuse_unknown(args.check_unknown, packages)
        report = conformance_report(packages, minimum, {})
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except (OSError, json.JSONDecodeError, ReleaseMatrixError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
