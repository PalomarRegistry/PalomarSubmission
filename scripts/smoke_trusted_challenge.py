#!/usr/bin/env python3
"""Exercise the production statement boundary on a freshly cloned fixture."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from verify_submission import (
    EXECUTION_BUDGET_SECONDS,
    VerificationError,
    audit_challenge_sources,
    build_allowlisted_roots,
    compile_canonical_challenge,
    enforced_comparator_config,
    get_mathlib_cache,
    install_execution_deadline,
    lake_environment_value,
    manifest_packages,
    materialize_packages,
    package_allowlist,
    package_lake_directories,
    protected_lean_path,
    reject_untrusted_package_artifacts,
    require_protected_paths,
    run,
    sandboxed_run,
    system_readable_paths,
    tool_snapshot,
    trusted_lake_directories,
    verify_sandbox_confinement,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--landrun", type=Path, required=True)
    parser.add_argument("--comparator", type=Path, required=True)
    parser.add_argument("--lean4export", type=Path, required=True)
    parser.add_argument("--nanoda", type=Path, required=True)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    install_execution_deadline(
        os.environ.get("PALOMAR_JOB_STARTED_AT"),
        int(os.environ.get("PALOMAR_EXECUTION_BUDGET_SECONDS", EXECUTION_BUDGET_SECONDS)),
    )

    source = args.source.resolve(strict=True)
    landrun = args.landrun.resolve(strict=True)
    comparator = args.comparator.resolve(strict=True)
    lean4export = args.lean4export.resolve(strict=True)
    nanoda = args.nanoda.resolve(strict=True)
    adapter = args.adapter.resolve(strict=True)
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.pop("LAKE_PKG_URL_MAP", None)

    lean_command = shutil.which("lean", path=environment["PATH"])
    if not lean_command:
        raise VerificationError("Lean is unavailable")
    lean_prefix = Path(
        run([lean_command, "--print-prefix"], cwd=source, env=environment).stdout.strip()
    ).resolve(strict=True)
    environment["PATH"] = f"{lean_prefix / 'bin'}:{environment['PATH']}"
    lean = (lean_prefix / "bin" / "lean").resolve(strict=True)
    lake = (lean_prefix / "bin" / "lake").resolve(strict=True)
    python = Path(sys.executable).resolve(strict=True)
    printenv_command = shutil.which("printenv", path=environment["PATH"])
    touch_command = shutil.which("touch", path=environment["PATH"])
    if not printenv_command or not touch_command:
        raise VerificationError("required system tools are unavailable")
    printenv = Path(printenv_command).absolute()
    touch = Path(touch_command).absolute()

    writable_directories = materialize_packages(source, base_env=environment)
    home = source / ".lake" / "config" / "home"
    temporary = source / ".lake" / "config" / "tmp"
    home.mkdir()
    temporary.mkdir()
    environment.update(
        {
            "COMPARATOR_LANDRUN": str(adapter),
            "COMPARATOR_LEAN4EXPORT": str(lean4export),
            "COMPARATOR_NANODA": str(nanoda),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": str(home.resolve()),
            "PALOMAR_LANDRUN_REAL": str(landrun),
            "TMPDIR": str(temporary.resolve()),
            "LEAN_ABORT_ON_PANIC": "1",
        }
    )

    executable_paths = [
        lean_prefix,
        python.parent.parent,
        lean,
        lake,
        python,
        printenv,
        touch,
        landrun,
        comparator,
        lean4export,
        nanoda,
        adapter,
    ]
    for raw in ("/usr", "/bin", "/lib", "/lib64", "/run/current-system/sw", "/nix/store"):
        path = Path(raw)
        if path.exists():
            executable_paths.append(path.resolve())
    executable_paths = sorted(set(executable_paths))
    comparator_config = enforced_comparator_config(
        source / "comparator.json", work / "enforced-comparator.json"
    )
    readable_paths = sorted({source, comparator_config, *system_readable_paths()})
    tools = tool_snapshot(
        [
            Path(__file__).resolve(),
            lean,
            lake,
            python,
            printenv,
            touch,
            landrun,
            comparator,
            lean4export,
            nanoda,
            comparator_config,
            adapter,
        ]
    )

    verify_sandbox_confinement(
        work / "initial-write-denied",
        work / "initial-read-denied",
        positive_read=source / "Challenge.lean",
        python=python,
        touch=touch,
        cwd=source,
        environment=environment,
        landrun=landrun,
        writable_directories=writable_directories,
        readable_paths=readable_paths,
        executable_paths=executable_paths,
        tools=tools,
    )

    packages = manifest_packages(source)
    allowlist = package_allowlist(source, packages, base_env=environment)
    reject_untrusted_package_artifacts(source, packages, allowlist)
    get_mathlib_cache(
        source,
        base_env=environment,
        allowlist=allowlist,
        lake=lake,
        landrun=landrun,
        readable_paths=readable_paths,
        executable_paths=executable_paths,
        tools=tools,
    )
    build_allowlisted_roots(
        source,
        packages=packages,
        allowlist=allowlist,
        base_env=environment,
        lake=lake,
        landrun=landrun,
        readable_paths=readable_paths,
        executable_paths=executable_paths,
        tools=tools,
    )

    trusted = set(trusted_lake_directories(source, allowlist))
    trusted_builds = {package_lake_directories(source, name)[0] for name in allowlist}
    candidate_writable = [path for path in writable_directories if path not in trusted]
    require_protected_paths(
        [
            Path(__file__).resolve(),
            lean,
            lake,
            python,
            printenv,
            touch,
            landrun,
            comparator,
            lean4export,
            nanoda,
            comparator_config,
            adapter,
        ],
        candidate_writable,
    )
    executable_paths = sorted({*executable_paths, *trusted_builds})
    canonical, dependency_sources, trusted_lean_paths = compile_canonical_challenge(
        work,
        source,
        lean=lean,
        lean_prefix=lean_prefix,
        allowlist=allowlist,
        environment=environment,
        landrun=landrun,
        readable_paths=readable_paths,
        executable_paths=executable_paths,
        tools=tools,
    )
    readable_paths = sorted({*readable_paths, canonical.parent})
    require_protected_paths([canonical], candidate_writable)
    audit = audit_challenge_sources(
        source,
        dependency_sources=dependency_sources,
        lean_prefix=lean_prefix,
        allowlist=allowlist,
        writable_directories=candidate_writable,
    )
    if audit["untrusted_sources"]:
        raise VerificationError(
            "cold-build fixture has untrusted Challenge sources: "
            + ", ".join(audit["untrusted_sources"][:10])
        )

    verify_sandbox_confinement(
        work / "write-denied",
        work / "read-denied",
        positive_read=source / "Challenge.lean",
        python=python,
        touch=touch,
        cwd=source,
        environment=environment,
        landrun=landrun,
        writable_directories=candidate_writable,
        protected_write_directories=sorted(trusted_builds),
        readable_paths=readable_paths,
        executable_paths=executable_paths,
        tools=tools,
    )
    environment["LEAN_PATH"] = lake_environment_value(
        "LEAN_PATH",
        source=source,
        lake=lake,
        printenv=printenv,
        environment=environment,
        landrun=landrun,
        writable_directories=candidate_writable,
        readable_paths=readable_paths,
        executable_paths=executable_paths,
        tools=tools,
        allowed_roots=[source, lean_prefix],
    )
    environment["LEAN_SRC_PATH"] = lake_environment_value(
        "LEAN_SRC_PATH",
        source=source,
        lake=lake,
        printenv=printenv,
        environment=environment,
        landrun=landrun,
        writable_directories=candidate_writable,
        readable_paths=readable_paths,
        executable_paths=executable_paths,
        tools=tools,
        allowed_roots=[source, lean_prefix],
    )
    environment["LEAN_PATH"] = protected_lean_path(
        canonical,
        trusted_lean_paths,
        environment["LEAN_PATH"],
    )
    # These builds exercise arbitrary proof-dependency compatibility. Lake may
    # put workspace directories before the inherited path while building; the
    # protected LEAN_PATH is reapplied above and carried into Comparator below.
    for target in ("Challenge", "Solution"):
        sandboxed_run(
            [str(lake), "build", target],
            cwd=source,
            environment=environment,
            landrun=landrun,
            writable_directories=candidate_writable,
            readable_paths=readable_paths,
            executable_paths=executable_paths,
            tools=tools,
            timeout=7200,
        )

    comparison = sandboxed_run(
        [str(comparator), str(comparator_config)],
        cwd=source,
        environment=environment,
        landrun=landrun,
        writable_directories=candidate_writable,
        readable_paths=readable_paths,
        executable_paths=executable_paths,
        tools=tools,
        timeout=7200,
        check=False,
    )
    if comparison.returncode:
        detail = (comparison.stdout + "\n" + comparison.stderr).strip()[-4000:]
        raise VerificationError(f"cold-build Comparator integration failed: {detail}")

    print(
        json.dumps(
            {
                "status": "pass",
                "canonical_challenge": str(canonical),
                "challenge_sources": audit["source_count"],
                "challenge_dependencies": audit["dependencies"],
                "project_dependencies": len(packages),
                "comparator": "pass",
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
