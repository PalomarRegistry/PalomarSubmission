#!/usr/bin/env python3
"""Time a TauCeti-dependent fixture's trusted-root stage with and without the Lake cache.

Measurement only, not part of verification. The trusted channel has no maps yet, so the
cache runs read TauCeti's shared channel to time the mechanism; production never does.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts import verify_submission as verifier  # noqa: E402

SCENARIOS = {
    "narrow": frozenset(
        {
            "TauCeti.GroupTheory.FreeGroup.Hopfian",
            "TauCeti.GroupTheory.FreeGroup.Schreier",
            "TauCeti.LinearAlgebra.Matrix.GeneralLinearGroup.Elementary",
            "TauCeti.LinearAlgebra.Matrix.GeneralLinearGroup.Transvection",
            "TauCeti.LinearAlgebra.Matrix.SpecialLinearGroup.Transvection",
        }
    ),
    "wide": frozenset(
        {
            "TauCeti.AlgebraicGeometry.EllipticCurve.GaloisCohomology.WeilPairing.Reciprocity",
            "TauCeti.NumberTheory.QuadraticForm.Global.WorkedExamples.Sqrt5",
            "TauCeti.NumberTheory.ClassFieldTheory.ClassFields.CyclotomicLevel",
            "TauCeti.RepresentationTheory.ClassicalGroups.GelfandTsetlin.Basis",
        }
    ),
}
SHARED_CHANNEL = {
    "revision_endpoint": "https://cache.taucetiproject.org/revisions",
    "artifact_endpoint": "https://cache.taucetiproject.org/artifacts",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--bwrap", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    verifier.install_execution_deadline(os.environ.get("PALOMAR_JOB_STARTED_AT"), 19800)

    source = args.source.resolve(strict=True)
    bwrap = verifier.configure_bwrap(args.bwrap)
    environment = verifier.strip_trusted_lake_cache_environment(os.environ.copy())
    environment.pop("LAKE_PKG_URL_MAP", None)
    lean_command = shutil.which("lean", path=environment["PATH"])
    lean_prefix = Path(
        verifier.run([lean_command, "--print-prefix"], cwd=source, env=environment).stdout.strip()
    ).resolve(strict=True)
    environment["PATH"] = f"{lean_prefix / 'bin'}:{environment['PATH']}"
    lake = (lean_prefix / "bin" / "lake").resolve(strict=True)
    python = Path(sys.executable).resolve(strict=True)
    results: dict[str, object] = {}

    started = time.monotonic()
    verifier.materialize_packages(source, checkout=source, base_env=environment)
    home = source / ".lake" / "config" / "home"
    temporary = source / ".lake" / "config" / "tmp"
    home.mkdir()
    temporary.mkdir()
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": str(home.resolve()),
            "TMPDIR": str(temporary.resolve()),
            "LEAN_ABORT_ON_PANIC": "1",
        }
    )
    executable_paths = [lean_prefix, python.parent.parent, lake, python]
    for raw in ("/usr", "/bin", "/lib", "/lib64"):
        if Path(raw).exists():
            executable_paths.append(Path(raw).resolve())
    executable_paths = sorted(set(executable_paths))
    readable_paths = sorted({source, *verifier.system_readable_paths()})
    lean = (lean_prefix / "bin" / "lean").resolve(strict=True)
    tools = verifier.tool_snapshot([lake, lean, python, bwrap])
    packages = verifier.manifest_packages(source)
    allowlist = verifier.package_allowlist(source, packages, checkout=source, base_env=environment)
    results["materialize_and_provenance_seconds"] = round(time.monotonic() - started, 1)

    started = time.monotonic()
    verifier.get_mathlib_cache(
        source,
        checkout=source,
        base_env=environment,
        allowlist=allowlist,
        lake=lake,
        readable_paths=readable_paths,
        executable_paths=executable_paths,
        tools=tools,
    )
    results["mathlib_cache_seconds"] = round(time.monotonic() - started, 1)

    real_roots = verifier.allowed_roots

    def shared_channel_roots():
        roots, aliases = real_roots()
        roots = [dict(root) for root in roots]
        for root in roots:
            if "trusted_lake_cache" in root:
                root["trusted_lake_cache"] = {**root["trusted_lake_cache"], **SHARED_CHANNEL}
        return roots, {key: next(r for r in roots if r["repository"] == value["repository"])
                       for key, value in aliases.items()}

    for name, imports in SCENARIOS.items():
        for mode in ("cache", "source"):
            started = time.monotonic()
            with mock.patch.object(verifier, "allowed_roots", side_effect=shared_channel_roots):
                evidence = verifier.build_allowlisted_roots(
                    source,
                    checkout=source,
                    packages=packages,
                    allowlist=allowlist,
                    base_env=environment,
                    lake=lake,
                    readable_paths=readable_paths,
                    executable_paths=executable_paths,
                    tools=tools,
                    required_imports=imports,
                    use_trusted_lake_cache=mode == "cache",
                )
            results[f"{name}_{mode}"] = {
                "trusted_roots_seconds": round(time.monotonic() - started, 1),
                "evidence": evidence,
            }
            print(json.dumps({f"{name}_{mode}": results[f"{name}_{mode}"]}), flush=True)
            args.output.write_text(json.dumps(results, indent=2) + "\n")
    args.output.write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
