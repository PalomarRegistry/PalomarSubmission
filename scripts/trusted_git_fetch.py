"""Retry only network failures while fetching immutable Palomar tool revisions."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

TRUSTED_REPOSITORIES = {"leanprover/comparator", "robsimmons/nanoda_lib", "leanprover/lean4export"}
TRANSIENT = re.compile(
    r"Could not resolve host|Failed to connect|Connection reset|Connection timed out|"
    r"remote end hung up|early EOF|HTTP [25][0-9][0-9]|returned error: (408|429|5[0-9][0-9])"
)
PERMANENT = re.compile(
    r"Authentication failed|Repository not found|not our ref|certificate|SSL certificate", re.I
)


def fetch(repository: str, revision: str, destination: Path, *, budget: float = 300) -> None:
    if repository not in TRUSTED_REPOSITORIES or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("trusted tool fetch requires an approved repository and immutable commit")
    if destination.exists():
        raise ValueError("trusted tool destination already exists")
    started = os.environ.get("PALOMAR_JOB_STARTED_AT")
    if started:
        budget = min(budget, float(started) + 19800 - time.time())
    deadline = time.monotonic() + budget
    environment = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    with tempfile.TemporaryDirectory(prefix="palomar-tool-", dir=destination.parent) as raw:
        checkout = Path(raw) / "source"
        subprocess.run(["git", "init", "--quiet", str(checkout)], env=environment, check=True)
        command = ["git", "-C", str(checkout), "-c", "core.hooksPath=/dev/null"]
        subprocess.run(
            [*command, "remote", "add", "origin", f"https://github.com/{repository}.git"],
            env=environment,
            check=True,
        )
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            result = subprocess.run(
                [*command, "fetch", "--quiet", "--depth=1", "origin", revision],
                env=environment,
                capture_output=True,
                text=True,
                timeout=remaining,
            )
            if result.returncode == 0:
                subprocess.run(
                    [*command, "checkout", "--quiet", "--detach", revision],
                    env=environment,
                    check=True,
                    timeout=max(1, deadline - time.monotonic()),
                )
                checkout.rename(destination)
                return
            if PERMANENT.search(result.stderr) or not TRANSIENT.search(result.stderr):
                raise RuntimeError("trusted immutable tool fetch failed permanently")
            if attempt < 2 and time.monotonic() + (5, 20)[attempt] < deadline:
                time.sleep((5, 20)[attempt])
            else:
                break
    raise RuntimeError("trusted tool fetch exhausted its bounded retry budget")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("repository")
    parser.add_argument("revision")
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    fetch(args.repository, args.revision, args.destination)
