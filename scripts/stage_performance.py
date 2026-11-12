"""Summarize supervisor phase timings. This is not a proof certificate."""
from __future__ import annotations

import json
import sys
from typing import Any


def stage_performance(records: list[Any]) -> dict[str, Any]:
    """Pick the slowest phase the observer actually measured.

    A record without a phase name or a finite elapsed time stays unknown.
    Missing telemetry yields an explicit unknown summary rather than a zero.
    """
    phases: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("phase"), str):
            phases.append({"phase": "unknown", "status": "unknown"})
            continue
        elapsed = record.get("elapsed_seconds")
        rss = record.get("max_rss_kib")
        if type(elapsed) not in {int, float} or isinstance(elapsed, bool) or elapsed < 0:
            phases.append({"phase": record["phase"], "status": "unknown"})
            continue
        phases.append({
            "phase": record["phase"],
            "status": "measured",
            "elapsed_seconds": elapsed,
            "max_rss_kib": rss if type(rss) is int and rss >= 0 else None,
        })
    measured = [item for item in phases if item["status"] == "measured"]
    critical = max(measured, key=lambda item: (item["elapsed_seconds"], item["phase"])) if measured else None
    return {
        "schema_version": 1,
        "phases": phases,
        "critical_phase": None if critical is None else critical["phase"],
        "critical_elapsed_seconds": None if critical is None else critical["elapsed_seconds"],
        "source": "supervisor-metrics",
        "certificate": False,
        "note": (
            "These timings attribute this run's supervisor observations. "
            "They do not accept an incomplete verification or request a larger runner."
        ),
    }


def main() -> int:
    document = json.load(sys.stdin)
    records = document.get("resource_usage", document if isinstance(document, list) else [])
    if not isinstance(records, list):
        print("error: resource_usage must be a list", file=sys.stderr)
        return 1
    print(json.dumps(stage_performance(records), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
