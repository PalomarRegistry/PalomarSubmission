"""Bounded retries for immutable, checksum-pinned trusted tool downloads."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import tempfile
import time
from email.utils import parsedate_to_datetime
from pathlib import Path

TRANSIENT_CURL = {5, 6, 7, 18, 28, 35, 52, 55, 56}
TRANSIENT_HTTP = {408, 429, *range(500, 600)}


def retry_delay(attempt: int, headers: str) -> float:
    values = re.findall(r"(?im)^retry-after:\s*([^\r\n]+)", headers)
    if values:
        value = values[-1].strip()
        if value.isdigit():
            return min(60, int(value))
        try:
            return max(0, min(60, parsedate_to_datetime(value).timestamp() - time.time()))
        except (ValueError, TypeError, OverflowError):
            pass
    return (5, 20)[attempt]


def download(url: str, destination: Path, digest: str, *, budget: float = 300) -> None:
    if not url.startswith("https://") or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("trusted downloads require HTTPS and a pinned SHA-256")
    started = os.environ.get("PALOMAR_JOB_STARTED_AT")
    if started:
        budget = min(budget, float(started) + 19800 - time.time())
    deadline = time.monotonic() + budget
    with tempfile.TemporaryDirectory(prefix="palomar-download-", dir=destination.parent) as raw:
        temporary = Path(raw) / "download"
        headers = Path(raw) / "headers"
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            completed = subprocess.run(
                [
                    "curl",
                    "--silent",
                    "--show-error",
                    "--location",
                    "--proto",
                    "=https",
                    "--proto-redir",
                    "=https",
                    "--tlsv1.2",
                    "--connect-timeout",
                    "30",
                    "--max-time",
                    str(remaining),
                    "--dump-header",
                    str(headers),
                    "--output",
                    str(temporary),
                    "--write-out",
                    "%{http_code}",
                    url,
                ],
                capture_output=True,
                text=True,
                timeout=remaining + 5,
            )
            status = int(completed.stdout) if completed.stdout.isdigit() else 0
            if completed.returncode == 0 and 200 <= status < 300:
                if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
                    raise ValueError("trusted download checksum mismatch")
                temporary.replace(destination)
                return
            if not (
                completed.returncode in TRANSIENT_CURL
                or (completed.returncode == 0 and status in TRANSIENT_HTTP)
            ):
                raise RuntimeError(f"trusted download refused (curl={completed.returncode}, HTTP={status})")
            if attempt < 2:
                delay = retry_delay(attempt, headers.read_text(errors="replace") if headers.exists() else "")
                if time.monotonic() + delay >= deadline:
                    break
                time.sleep(delay)
    raise RuntimeError("trusted download exhausted its bounded retry budget")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    download(args.url, args.output, args.sha256)


if __name__ == "__main__":
    main()
