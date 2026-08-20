#!/usr/bin/env python3
"""Project the browser preflight policy from the verifier's own constants.

The intake page fetches browser-preflight-policy.json and refuses to certify
its own bundled copy of these rules unless the two documents are identical, so
the published policy has to restate this repository's contract exactly.
Deriving it here means the restatement cannot quietly fall behind the contract:
the committed file is this projection, and the contract tests fail when it is
not. Rerun with --write after changing any constant the projection reads.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from scripts import submission_contract, verify_submission

ROOT = Path(__file__).resolve().parent.parent
POLICY_PATH = ROOT / "browser-preflight-policy.json"

# JavaScript spells a named group (?<name>...), so the browser gets the
# verifier's toolchain pattern with the names removed and nothing else changed.
NAMED_GROUP_RE = re.compile(r"\?P<[a-z]+>")

# Authoritative checks the page cannot run at all: each one needs the checked
# out repository, a network fetch, or a tool the browser does not have.
DEFERRED_CHECKS = (
    "classification-taxonomies",
    "git-attributes-lfs",
    "lakefile-toml",
    "licensee",
    "release-tag",
    "substantive-repository",
    "trusted-hashes",
)


def browser_preflight_policy() -> dict[str, Any]:
    """Return the policy document the intake page checks itself against."""
    toolchains = json.loads((ROOT / "toolchains.json").read_text(encoding="utf-8"))
    return {
        "schema_version": 1,
        "formalization_profile_version": submission_contract.FORMALIZATION_PROFILE_VERSION,
        "limits": {
            "source_bytes": verify_submission.MAX_SOURCE_BYTES,
            "configuration_bytes": verify_submission.MAX_CONFIGURATION_BYTES,
            "formalization_bytes": submission_contract.MAX_FORMALIZATION_BYTES,
        },
        "toolchain": {
            "pattern": NAMED_GROUP_RE.sub("", verify_submission.TOOLCHAIN_RE.pattern),
            "minimum": toolchains["minimum"],
            # supported_toolchain strips the file before matching it, and
            # TOOLCHAIN_RE sorts a candidate before the release it leads to.
            "normalization": "strip",
            "prerelease_ordering": "rc-before-release",
        },
        "formalization": {
            "repository_roles": sorted(submission_contract.REPOSITORY_ROLES),
            "source_relationship_categories": sorted(
                submission_contract.SOURCE_RELATIONSHIP_CATEGORIES
            ),
            "classification_cardinality": {
                name: list(bounds)
                for name, bounds in submission_contract.CLASSIFICATION_CARDINALITY.items()
            },
        },
        "comparator": {
            "required_keys": sorted(verify_submission.COMPARATOR_REQUIRED_KEYS),
            "allowed_keys": sorted(verify_submission.COMPARATOR_ALLOWED_KEYS),
            "standard_axioms": sorted(verify_submission.STANDARD_AXIOMS),
        },
        "deferred_checks": list(DEFERRED_CHECKS),
    }


def policy_document() -> str:
    """Return the exact text browser-preflight-policy.json must contain."""
    return json.dumps(browser_preflight_policy(), indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="rewrite the committed policy instead of checking it",
    )
    arguments = parser.parse_args(argv)
    document = policy_document()
    if arguments.write:
        POLICY_PATH.write_text(document, encoding="utf-8")
        return 0
    if POLICY_PATH.read_text(encoding="utf-8") != document:
        print(f"{POLICY_PATH.name} is stale; rerun this script with --write")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
