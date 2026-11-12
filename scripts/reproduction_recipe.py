"""Write a pinned local reproduction recipe from a mechanical verification report.

The recipe is not an official hosted verification. It omits authorization text,
ORCID records and any token-shaped strings.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any

SHA = re.compile(r"^[0-9a-f]{40}$")
HEX = re.compile(r"^[0-9a-f]{64}$")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TOOLCHAIN = re.compile(r"^leanprover/lean4:[A-Za-z0-9._+-]+$")


class RecipeError(ValueError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RecipeError(message)


def reproduction_recipe(report: dict[str, Any]) -> str:
    _require(report.get("schema_version") == 2, "report schema_version must be 2")
    source = report.get("source")
    _require(isinstance(source, dict), "report has no source binding")
    repository = source.get("repository")
    commit = source.get("commit")
    url = source.get("repository_url")
    _require(isinstance(repository, str) and REPO.fullmatch(repository), "source repository is not an owner/name")
    _require(isinstance(commit, str) and SHA.fullmatch(commit), "source commit is not a full SHA")
    _require(
        isinstance(url, str) and url == f"https://github.com/{repository}",
        "source URL does not match the recorded repository",
    )
    toolchain = report.get("lean_toolchain")
    _require(isinstance(toolchain, str) and TOOLCHAIN.fullmatch(toolchain), "lean toolchain pin is missing")
    digests = report.get("tool_digests")
    _require(isinstance(digests, dict) and digests, "tool digests are missing")
    for name, digest in digests.items():
        _require(isinstance(name, str) and isinstance(digest, str) and HEX.fullmatch(digest),
                 f"tool digest for {name!r} is not sha256")
    profile = report.get("verification_profile")
    profile_id = profile.get("id") if isinstance(profile, dict) else None
    _require(profile_id is None or isinstance(profile_id, str), "profile id is malformed")
    project = source.get("project_path") or "."
    _require(isinstance(project, str) and ".." not in project.split("/"), "project path escapes the repository")
    theorems = []
    comparator = report.get("comparator")
    if isinstance(comparator, dict) and isinstance(comparator.get("theorem_names"), list):
        theorems = [name for name in comparator["theorem_names"] if isinstance(name, str)]
    lines = [
        "# Local reproduction recipe",
        "",
        "This is a developer recipe for the pins in one mechanical report.",
        "A local result is not an official Palomar verification.",
        "",
        f"- Repository: `https://github.com/{repository}`",
        f"- Commit: `{commit}`",
        f"- Project path: `{project}`",
        f"- Lean toolchain: `{toolchain}`",
        f"- Profile: `{profile_id or 'not recorded'}`",
        f"- Bubblewrap tag: `{report.get('bwrap_source_tag') or 'not recorded'}`",
        "",
        "## Tool digests",
        "",
    ]
    for name in sorted(digests):
        lines.append(f"- `{name}`: `{digests[name]}`")
    if theorems:
        lines.extend(["", "## Comparator targets", ""])
        lines.extend(f"- `{name}`" for name in theorems)
    lines.extend([
        "",
        "Check the checked-out commit and each binary digest before running.",
        "Use the repository's existing sandbox. Do not treat this file as a receipt.",
        "",
    ])
    text = "\n".join(lines)
    _require("authorization" not in text and "orcid" not in text.lower(), "recipe leaked submission state")
    return text


def main() -> int:
    try:
        report = json.load(sys.stdin)
        if not isinstance(report, dict):
            raise RecipeError("report must be a JSON object")
        sys.stdout.write(reproduction_recipe(report))
        return 0
    except (RecipeError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
