#!/usr/bin/env python3
"""Post or update the bounded mechanical report on its submission issue."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

MARKER = "<!-- palomar-mechanical-report -->"
MAX_COMMENT_BYTES = 60 * 1024
MAX_DIAGNOSTIC_ITEMS = 20
MAX_DIAGNOSTIC_CHARS = 4_000
MAX_EMBEDDED_ITEMS = 100
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
    errors = bounded_diagnostics(report.get("errors", []))
    warnings = bounded_diagnostics(report.get("warnings", []))
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
    if report.get("error_kind"):
        lines.append(f"- Infrastructure outcome: `{report['error_kind']}`")
    if report.get("retryable") is True:
        lines.append("- Retryable on a more capable worker: `yes`")
    if report.get("workflow_url"):
        lines.append(f"- [Workflow run]({report['workflow_url']})")
    if errors:
        lines.extend(["", "### Errors", ""])
        lines.extend(inert_diagnostics(errors))
    if warnings:
        lines.extend(["", "### Warnings", ""])
        lines.extend(inert_diagnostics(warnings))
    bounded = report_for_comment(report)
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
    body = "\n".join(lines) + "\n"
    if len(body.encode("utf-8")) > MAX_COMMENT_BYTES:
        # The compact fallback retains the authoritative binding/outcome while
        # directing operators to the workflow artifact for the complete JSON.
        workflow = report.get("workflow_url")
        compact = lines[:7]
        if workflow:
            compact.extend([f"- [Complete report artifact]({workflow})"])
        if errors:
            compact.extend(["", "### First error", "", *inert_diagnostics(errors[:1])])
        compact.extend(
            [
                "",
                "The detailed diagnostics exceed GitHub's comment limit and are available in the workflow artifact.",
                "",
                "This checks the pinned Lean snapshot and trusted challenge surface. It is not an editorial acceptance.",
            ]
        )
        body = "\n".join(compact) + "\n"
    return body


def bounded_diagnostics(items: object) -> list[str]:
    if not isinstance(items, list):
        return ["malformed diagnostic list omitted"]
    result: list[str] = []
    for item in items[:MAX_DIAGNOSTIC_ITEMS]:
        value = str(item)
        if len(value) > MAX_DIAGNOSTIC_CHARS:
            value = value[:MAX_DIAGNOSTIC_CHARS] + "\n<diagnostic truncated>"
        result.append(value)
    if len(items) > MAX_DIAGNOSTIC_ITEMS:
        result.append(f"<{len(items) - MAX_DIAGNOSTIC_ITEMS} additional diagnostics omitted>")
    return result


def report_for_comment(report: dict) -> dict:
    """Keep the issue comment bounded; the workflow artifact stays complete."""
    bounded = dict(report)
    bounded.pop("comparator_log_tail", None)
    bounded["errors"] = bounded_diagnostics(bounded.get("errors", []))
    bounded["warnings"] = bounded_diagnostics(bounded.get("warnings", []))
    challenge = bounded.get("challenge")
    if isinstance(challenge, dict):
        challenge = dict(challenge)
        bounded["challenge"] = challenge
        records = challenge.pop("review_source_files", None)
        if isinstance(records, list):
            challenge["review_source_file_count"] = len(records)
            challenge["review_source_files"] = records[:MAX_EMBEDDED_ITEMS]
            if len(records) > MAX_EMBEDDED_ITEMS:
                challenge["review_source_files_omitted"] = len(records) - MAX_EMBEDDED_ITEMS
    for key in ("project_dependencies", "resource_usage"):
        records = bounded.get(key)
        if isinstance(records, list) and len(records) > MAX_EMBEDDED_ITEMS:
            bounded[key] = records[:MAX_EMBEDDED_ITEMS]
            bounded[f"{key}_omitted"] = len(records) - MAX_EMBEDDED_ITEMS
    return bounded


def inert_diagnostics(items: list[object]) -> list[str]:
    """Render hostile diagnostics as indented Markdown code, never structure."""
    lines: list[str] = []
    for index, item in enumerate(items):
        if index:
            lines.append("    ")
        value = str(item).replace("\r\n", "\n").replace("\r", "\n")
        lines.extend(f"    {line}" for line in value.split("\n"))
    return lines


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--issue-number", required=True, type=int)
    parser.add_argument("--allow-missing-report", action="store_true")
    args = parser.parse_args()
    if args.issue_number < 1:
        raise SystemExit("event issue number must be positive")
    try:
        report = json.loads(Path(args.report).read_text())
        if not isinstance(report, dict):
            raise ValueError("mechanical report root is not an object")
    except (OSError, json.JSONDecodeError, ValueError) as error:
        if not args.allow_missing_report:
            raise SystemExit(f"could not read mechanical report: {error}") from error
        report = {
            "status": "error",
            "stage": "infrastructure",
            "issue": {"number": args.issue_number},
            "source": {},
            "errors": ["verification ended without a readable mechanical report"],
            "warnings": [],
        }
    try:
        reported_issue = int(report["issue"]["number"])
    except (KeyError, TypeError, ValueError) as error:
        raise SystemExit("mechanical report has no valid issue number") from error
    if reported_issue != args.issue_number:
        raise SystemExit(
            f"mechanical report issue {reported_issue} does not match event issue {args.issue_number}"
        )
    if report.get("status") not in {"pass", "fail", "error"}:
        report["status"] = "error"
        report["stage"] = "infrastructure"
        report.setdefault("errors", []).append(
            "verification infrastructure stopped before producing a final result"
        )
    issue = args.issue_number
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
    previous = next(
        (
            comment
            for comment in comments
            if MARKER in comment.get("body", "")
            and comment.get("user", {}).get("login") == "github-actions[bot]"
        ),
        None,
    )
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
