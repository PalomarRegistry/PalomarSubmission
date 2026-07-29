#!/usr/bin/env python3
"""Post or update the bounded mechanical report on its submission issue."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

MARKER = "<!-- palomar-mechanical-report -->"
STATUS_LABELS = (
    "status:verifying",
    "status:awaiting-review",
    "status:changes-requested",
    "status:verification-error",
    "status:review-in-progress",
    "status:accepted",
    "status:rejected",
    "status:escalated",
)


def gh(args: list[str], *, input_text: str | None = None, check: bool = True) -> str:
    proc = subprocess.run(
        ["gh", *args],
        input=input_text,
        text=True,
        capture_output=True,
        env=os.environ,
        check=False,
    )
    if check and proc.returncode:
        raise SystemExit(proc.stderr.strip() or proc.stdout.strip())
    return proc.stdout


def body_for(report: dict) -> str:
    status = report.get("status", "error")
    icon = {"pass": "✅", "fail": "❌", "error": "⚠️"}.get(status, "⚠️")
    source = report.get("source", {})
    challenge = report.get("challenge", {})
    errors = report.get("errors", [])
    warnings = report.get("warnings", [])
    lines = [
        MARKER,
        f"## {icon} Palomar mechanical verification: `{status}`",
        "",
        f"- Source: `{source.get('repository', 'unknown')}@{source.get('commit', 'unknown')}`",
        f"- Stage: `{report.get('stage', 'unknown')}`",
        f"- Lean: `{report.get('lean_toolchain', 'unknown')}`",
        f"- Challenge: {challenge.get('lines', '?')} lines / {challenge.get('bytes', '?')} bytes",
    ]
    if challenge.get("trust_level"):
        lines.append(f"- Challenge trust surface: `{challenge['trust_level']}`")
    if report.get("workflow_url"):
        lines.append(f"- [Workflow run]({report['workflow_url']})")
    if errors:
        lines.extend(["", "### Errors", *[f"- {item}" for item in errors]])
    if warnings:
        lines.extend(["", "### Warnings", *[f"- {item}" for item in warnings]])
    bounded = dict(report)
    bounded.pop("comparator_log_tail", None)
    lines.extend(
        [
            "",
            "<details><summary>Machine-readable report</summary>",
            "",
            "```json",
            json.dumps(bounded, indent=2, sort_keys=True),
            "```",
            "</details>",
            "",
            (
                "This checks the pinned Lean snapshot and trusted challenge surface. "
                "It is not an editorial acceptance."
            ),
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()
    report = json.loads(Path(args.report).read_text())
    if report.get("status") not in {"pass", "fail", "error"}:
        report["status"] = "error"
        report["stage"] = "infrastructure"
        report.setdefault("errors", []).append(
            "verification infrastructure stopped before producing a final result"
        )
    issue = int(report["issue"]["number"])
    target = {
        "pass": "status:awaiting-review",
        "fail": "status:changes-requested",
        "error": "status:verification-error",
    }.get(report.get("status"), "status:verification-error")
    for label in STATUS_LABELS:
        gh(
            ["issue", "edit", str(issue), "--repo", args.repo, "--remove-label", label],
            check=False,
        )
    gh(["issue", "edit", str(issue), "--repo", args.repo, "--add-label", target])

    comments = json.loads(
        gh(
            [
                "api",
                f"repos/{args.repo}/issues/{issue}/comments",
                "--paginate",
            ]
        )
    )
    body = body_for(report)
    previous = next((comment for comment in comments if MARKER in comment.get("body", "")), None)
    if previous:
        gh(
            [
                "api",
                "--method",
                "PATCH",
                f"repos/{args.repo}/issues/comments/{previous['id']}",
                "--input",
                "-",
            ],
            input_text=json.dumps({"body": body}),
        )
    else:
        gh(["issue", "comment", str(issue), "--repo", args.repo, "--body-file", "-"], input_text=body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
