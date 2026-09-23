"""Standard-library-only finalization of interrupted verification workflows.

Inputs and step outcomes are supplied by the trusted workflow, never by a
candidate file. This module can run even when dependency installation failed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Any

MAX_REPORT_BYTES = 4 * 1024 * 1024
SETUP_STEPS = (
    "checkout",
    "python",
    "ruby",
    "dependencies",
    "prepare",
    "disk",
    "capacity",
    "elan",
    "toolchain",
    "bwrap",
    "execute",
)


def finalize(report: Any, inputs: dict, steps: dict, *, workflow_url: str) -> dict:
    repository = inputs.get("repository", "")
    commit = inputs.get("commit", "")
    identifier = inputs.get("request_id", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("invalid workflow repository")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("invalid workflow commit")
    if not re.fullmatch(r"[a-z0-9_-]{1,100}", identifier):
        raise ValueError("invalid workflow request identifier")
    bound = (
        isinstance(report, dict)
        # 1 until the verifier starts, 2 once it has (a correction report is
        # 2 throughout); the finalizer binds either.
        and report.get("schema_version") in {1, 2}
        and isinstance(report.get("source"), dict)
        and isinstance(report.get("submission"), dict)
        and report.get("source", {}).get("repository") == repository
        and report.get("source", {}).get("commit") == commit
        and report.get("submission", {}).get("submission_id") == identifier
    )
    failed = next(
        (name for name in SETUP_STEPS if steps.get(name, {}).get("outcome") in {"failure", "cancelled"}), None
    )
    terminal = (
        bound
        and report.get("status") in {"pass", "fail", "error"}
        and isinstance(report.get("errors"), list)
        and isinstance(report.get("diagnostics", []), list)
        and (report["status"] == "pass" or bool(report["errors"]))
    )
    preflight = (
        bound
        and inputs.get("mode") == "preflight"
        and report.get("status") == "pending"
        and steps.get("prepare", {}).get("outputs", {}).get("ready") == "true"
        and failed is None
    )
    if terminal or preflight:
        # Preserve valid verifier diagnostics. A workflow step failing after a
        # pass is still caught by the final job gate, not rewritten as a proof failure.
        result = dict(report)
    else:
        malformed = report is not None and not bound
        stage = failed or "reporting"
        explanation = (
            "The mechanical report was malformed or did not match the workflow inputs."
            if malformed
            else f"Verification did not finish; workflow step: {stage}."
        )
        result = {
            **(report if bound else {}),
            "schema_version": 1,
            "source": {"repository": repository, "commit": commit},
            "submission": {"submission_id": identifier},
            "status": "error",
            "phase": "verification",
            "stage": stage,
            "checked_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "errors": [explanation],
            "warnings": [],
            "diagnostics_schema_version": 1,
            "diagnostics": [
                {
                    "code": "palomar.reporting_failed" if malformed else "palomar.workflow_incomplete",
                    "stage": stage,
                    "owner": "palomar",
                    "summary": explanation,
                    "explanation": explanation,
                    "retryable": True,
                    "repairable": False,
                    "next_action": "No repository change is indicated. Report the workflow URL to Palomar.",
                }
            ],
        }
        if bound:
            result["last_active_stage"] = report.get("stage")
        if inputs.get("mode") in {"preflight", "correction"} or stage in {
            "checkout",
            "python",
            "ruby",
            "dependencies",
            "prepare",
        }:
            result["phase"] = "preparation"
    result["workflow_url"] = workflow_url
    attempt = inputs.get("execution_attempt", "")
    if attempt:
        if not re.fullmatch(r"[0-9a-f]{32}", attempt):
            raise ValueError("invalid execution attempt")
        result["execution_attempt"] = attempt
    result["execution_profile"] = inputs.get("execution_profile") or resolved_profile(report)
    return result


def resolved_profile(report: Any) -> str:
    """The profile the verifier ran under when the dispatch named none.

    The verifier records it in the report's resource evidence; before that
    evidence exists (a failure during setup) the answer is the catalogue's
    default, which is what the workflow's profile job resolved.
    """
    if isinstance(report, dict):
        evidence = report.get("verification_profile")
        if isinstance(evidence, dict) and isinstance(evidence.get("id"), str):
            return evidence["id"]
    catalogue = json.loads(
        (Path(__file__).resolve().parents[1] / "execution-profiles.json").read_text(encoding="utf-8")
    )
    return str(catalogue["default"])


def gate(report: dict, mode: str, ready: str, steps: dict) -> bool:
    uploaded = any(steps.get(f"upload{n}", {}).get("outcome") == "success" for n in (1, 2, 3))
    failed = any(steps.get(name, {}).get("outcome") in {"failure", "cancelled"} for name in SETUP_STEPS)
    return (
        uploaded
        and not failed
        and (
            report.get("status") == "pass"
            or (mode == "preflight" and ready == "true" and report.get("status") == "pending")
        )
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["finalize", "gate"])
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    inputs = json.loads(os.environ["PALOMAR_INPUTS"])
    steps = json.loads(os.environ["PALOMAR_STEPS"])
    report = None
    try:
        if args.report.is_symlink() or args.report.stat().st_size > MAX_REPORT_BYTES:
            raise ValueError("unsafe report")
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = {} if args.report.exists() else None
    if args.command == "gate":
        ok = gate(
            report or {},
            inputs.get("mode", "full"),
            steps.get("prepare", {}).get("outputs", {}).get("ready", ""),
            steps,
        )
        if not ok:
            print("::error::Verification or report delivery did not complete successfully")
            for error in (report or {}).get("errors", []):
                # Plain output, never interpolate candidate text into workflow commands.
                print(json.dumps(str(error)[:2000]))
        return 0 if ok else 1
    result = finalize(report, inputs, steps, workflow_url=os.environ["PALOMAR_WORKFLOW_URL"])
    temporary = args.report.with_suffix(".finalizing.json")
    with temporary.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    temporary.replace(args.report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
