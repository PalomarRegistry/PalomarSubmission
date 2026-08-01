#!/usr/bin/env python3
"""Recheck whether a submission event may claim its issue for verification."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

RETRY_LABELS = {
    "status:changes-requested",
    "status:verification-error",
}
BLOCKING_LABELS = {
    "status:verifying",
    "status:awaiting-review",
    "status:review-in-progress",
    "status:accepted",
    "status:rejected",
    "status:escalated",
}


def gh(args: list[str]) -> str:
    proc = subprocess.run(
        ["gh", *args],
        text=True,
        capture_output=True,
        env=os.environ,
        check=False,
    )
    if proc.returncode:
        raise SystemExit(proc.stderr.strip() or proc.stdout.strip())
    return proc.stdout


def label_names(issue: dict) -> set[str]:
    labels = issue.get("labels", [])
    if not isinstance(labels, list):
        return set()
    return {
        label["name"] for label in labels if isinstance(label, dict) and isinstance(label.get("name"), str)
    }


def eligible(event_name: str, event: dict, issue: dict) -> bool:
    """Decide from the triggering event and current API state, never the snapshot alone."""
    event_issue = event.get("issue")
    if not isinstance(event_issue, dict):
        return False
    if issue.get("state") != "open" or "pull_request" in issue:
        return False
    labels = label_names(issue)
    if "submission" not in labels or labels & BLOCKING_LABELS:
        return False

    if event_name == "issues":
        label = event.get("label")
        return (
            event.get("action") == "labeled" and isinstance(label, dict) and label.get("name") == "submission"
        )

    if event_name != "issue_comment" or event.get("action") != "created":
        return False
    if "pull_request" in event_issue:
        return False
    comment = event.get("comment")
    if not isinstance(comment, dict) or comment.get("body") != "/reverify":
        return False
    commenter = comment.get("user", {}).get("login")
    event_author = event_issue.get("user", {}).get("login")
    current_author = issue.get("user", {}).get("login")
    return bool(commenter and commenter == event_author == current_author and labels & RETRY_LABELS)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=True)
    parser.add_argument("--event-name", required=True, choices=("issues", "issue_comment"))
    parser.add_argument("--issue-number", required=True, type=int)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()
    if args.issue_number < 1:
        raise SystemExit("event issue number must be positive")
    try:
        event = json.loads(Path(args.event).read_text())
        event_number = int(event["issue"]["number"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise SystemExit(f"could not read a valid issue event: {error}") from error
    if event_number != args.issue_number:
        raise SystemExit(f"event issue {event_number} does not match requested issue {args.issue_number}")
    try:
        issue = json.loads(gh(["api", f"repos/{args.repo}/issues/{args.issue_number}"]))
    except json.JSONDecodeError as error:
        raise SystemExit("GitHub returned invalid issue JSON") from error
    if not isinstance(issue, dict):
        raise SystemExit("GitHub returned a non-object issue")
    print("true" if eligible(args.event_name, event, issue) else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
