#!/usr/bin/env python3
"""Exercise the production renderer on a module-identity-sensitive fixture."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts.render_challenge import execute, toolchain_verso_commit  # noqa: E402
from scripts.render_report import (  # noqa: E402
    AcceptedRenderPaths,
    PreparedRenderReport,
    RenderSource,
)
from scripts.verification_errors import VerificationError  # noqa: E402
from scripts.verify_submission import (  # noqa: E402
    load_comparator_config,
    module_source_suffix,
    now,
    sha256,
    write_json,
)


def regular_file(root: Path, relative: Path, field: str) -> Path:
    path = root.joinpath(*relative.parts)
    if path.is_symlink() or not path.is_file():
        raise VerificationError(f"renderer smoke {field} is missing")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--landrun", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--renderer-commit", required=True)
    parser.add_argument("--landrun-commit", required=True)
    parser.add_argument("--workflow-url", required=True)
    args = parser.parse_args()

    source = args.source.resolve(strict=True)
    work = args.work_dir.resolve()
    if work.exists() or work.is_symlink():
        raise VerificationError("renderer smoke work directory is not fresh")
    work.mkdir(parents=True)
    checkout = work / "source"
    shutil.copytree(
        source,
        checkout,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git", ".lake"),
    )

    comparator = regular_file(checkout, Path("comparator.json"), "Comparator config")
    config = load_comparator_config(comparator)
    challenge_relative = module_source_suffix(config["challenge_module"])
    solution_relative = module_source_suffix(config["solution_module"])
    challenge = regular_file(checkout, challenge_relative, "Challenge source")
    regular_file(checkout, solution_relative, "Solution source")
    toolchain_file = regular_file(checkout, Path("lean-toolchain"), "Lean toolchain")
    lakefiles = [
        path
        for path in (checkout / "lakefile.toml", checkout / "lakefile.lean")
        if path.is_file() and not path.is_symlink()
    ]
    if len(lakefiles) != 1:
        raise VerificationError("renderer smoke fixture has no unique Lakefile")
    toolchain = toolchain_file.read_text(encoding="utf-8").strip()
    report = PreparedRenderReport(
        source=RenderSource(
            repository="PalomarRegistry/PalomarSubmission",
            repository_url="https://github.com/PalomarRegistry/PalomarSubmission",
            commit=args.renderer_commit,
            challenge_sha256=sha256(challenge),
            paths=AcceptedRenderPaths(
                project_path="",
                challenge_path=challenge_relative.as_posix(),
                solution_path=solution_relative.as_posix(),
                comparator_config_path="comparator.json",
                lakefile_path=lakefiles[0].name,
                lean_toolchain_path="lean-toolchain",
            ),
        ),
        lean_toolchain=toolchain,
        verso_commit=toolchain_verso_commit(toolchain),
        prepared_at=now(),
    ).as_dict()
    output = work / "report.json"
    write_json(output, report)

    status = execute(
        argparse.Namespace(
            work_dir=str(work),
            output=str(output),
            landrun=str(args.landrun),
            renderer_commit=args.renderer_commit,
            landrun_commit=args.landrun_commit,
            workflow_url=args.workflow_url,
        )
    )
    result = json.loads(output.read_text(encoding="utf-8"))
    if status or result.get("status") != "pass":
        raise VerificationError(
            "real renderer smoke failed: " + "; ".join(result.get("errors", []))
        )

    module_output = module_source_suffix(config["challenge_module"]).with_suffix("")
    original_olean = (work / "workspace" / ".lake" / "build" / "lib" / "lean").joinpath(
        *module_source_suffix(config["challenge_module"]).with_suffix(".olean").parts
    )
    synthetic_olean = work / "workspace" / ".lake" / "build" / "lib" / "lean" / "Challenge.olean"
    raw_entrypoint = work / "workspace" / ".lake" / "build" / "palomar-render-raw"
    raw_entrypoint = raw_entrypoint.joinpath(*module_output.parts) / "index.html"
    bundle = work / "result" / "bundle"
    stable_entrypoint = bundle / "Challenge" / "index.html"
    remapped_original = bundle.joinpath(*module_output.parts) / "index.html"
    if not original_olean.is_file() or synthetic_olean.exists():
        raise VerificationError("renderer smoke compiled the synthetic Challenge module")
    if not raw_entrypoint.is_file() or not stable_entrypoint.is_file() or remapped_original.exists():
        raise VerificationError("renderer smoke did not remap the original module page exactly once")
    rendered = stable_entrypoint.read_text(encoding="utf-8")
    if config["challenge_module"] not in rendered or "modulePrivate" not in rendered:
        raise VerificationError("renderer smoke lost module-sensitive private source evidence")
    if result.get("entrypoint") != "Challenge/index.html":
        raise VerificationError("renderer smoke changed the public artifact entrypoint")

    print(
        json.dumps(
            {
                "status": "pass",
                "challenge_module": config["challenge_module"],
                "compiled_olean": str(original_olean),
                "raw_entrypoint": str(raw_entrypoint),
                "public_entrypoint": result["entrypoint"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
