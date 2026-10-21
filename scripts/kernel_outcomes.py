"""Interpret trusted con-ron outcomes without trusting declaration-name text."""

from __future__ import annotations

import re
from typing import Any

STATUS_LINE = re.compile(r"^con-ron: (?:accepted |rejected: |declined: |error: )", re.MULTILINE)


def con_ron_outcome(returncode: int, stdout: str, stderr: str) -> dict[str, Any]:
    """Exit 1 always blocks; only positively identified support limits decline.

    Names inside diagnostics are untrusted even though the process is trusted.
    An injected second verdict never changes the exit code's meaning, and an
    unknown/internal error is never promoted to a successful verification.
    """
    # The checker writes its verdict on stderr, and its acceptance count on
    # stdout on some releases. Only one verdict across both streams is valid.
    log = stdout + "\n" + stderr
    matches = list(STATUS_LINE.finditer(log))
    status = "error"
    reason = "unrecognized-checker-result"
    if len(matches) == 1:
        line = log[matches[0].start():].splitlines()[0]
        if returncode == 0 and re.fullmatch(
            r"con-ron: accepted [0-9]+ declarations \(--verified\)(?: t=[0-9.]+s)?", line
        ):
            status, reason = "accepted", "complete"
        elif returncode == 1:
            status, reason = "rejected", "invalid-declaration"
        elif returncode == 2 and line.startswith("con-ron: declined: "):
            detail = line.removeprefix("con-ron: declined: ")
            # These are model/representation limits. In particular, a
            # non-standard axiom is never an advisory support limitation.
            if detail.startswith((
                "in-process model of ", "reflexive member ", "mutual inductive ",
                "nested inductive ", "unsupported inductive ",
            )):
                status, reason = "declined", "unsupported-inductive"
        elif returncode == 3 and line.startswith("con-ron: error: fuel exhausted: "):
            status, reason = "declined", "fuel-exhausted"
    if returncode == 1:
        status, reason = "rejected", "invalid-declaration"
    return {
        "name": "con-ron", "status": status, "reason": reason,
        "exit_code": returncode,
        "detail": "".join(c for c in log.strip() if c in "\n\t" or ord(c) >= 32)[-2000:],
    }
