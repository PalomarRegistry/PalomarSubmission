"""Standard-library-only identity and diagnostics for workflow callers."""

from __future__ import annotations

import re
from typing import Any

from scripts.verification_errors import VerificationError

SUBMISSION_ID_RE = re.compile(r"^[0-9a-z]{12}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class WorkflowInputError(VerificationError):
    """Correct the invocation; changing the submitted source is unnecessary."""

    def __init__(self, message: str, *, field: str, code: str = "workflow.input_invalid") -> None:
        super().__init__(
            message,
            code=code,
            field=field,
            next_action=(
                f"Correct {field} in the workflow inputs and rerun against the same source commit. "
                "No source change is needed."
            ),
        )


def request_identity(inputs: dict[str, Any]) -> tuple[str, str, str]:
    """Validate one identity contract in both preparation and finalization."""
    for field, pattern, message in (
        ("request_id", SUBMISSION_ID_RE, "Submission id must be twelve lowercase alphanumeric characters"),
        ("repository", REPOSITORY_RE, "Repository must be given as owner/name"),
        ("commit", SHA_RE, "Commit SHA must be 40 lowercase hexadecimal characters"),
    ):
        value = inputs.get(field, "")
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise WorkflowInputError(message, field=field)
    return inputs["repository"], inputs["commit"], inputs["request_id"]


def report_identity(inputs: dict[str, Any]) -> dict[str, Any]:
    """Bind failures to workflow inputs even when those inputs are invalid.

    This is an envelope, not evidence of admission or verification. The
    finalizer independently rejects invalid identities before preserving a pass.
    """
    return {
        "source": {
            "repository": str(inputs.get("repository", ""))[:200],
            "commit": str(inputs.get("commit", ""))[:100],
        },
        "submission": {"submission_id": str(inputs.get("request_id", ""))[:100]},
    }
