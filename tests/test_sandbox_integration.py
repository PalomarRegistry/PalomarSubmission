import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.render_challenge import executable_paths as renderer_executable_paths
from scripts.verify_submission import (
    compile_canonical_challenge,
    protected_lean_path,
    remove_untrusted_lake_state,
    system_readable_paths,
    tool_snapshot,
    verify_sandbox_confinement,
)


class SandboxIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("PALOMAR_TEST_LANDRUN"),
        "set PALOMAR_TEST_LANDRUN to exercise the real Landrun/systemd boundary",
    )
    def test_real_read_write_process_and_network_boundary(self):
        # `PrivateTmp=yes` deliberately hides the host /tmp. Put the fixture in
        # the checked-out workspace, like the real GitHub runner work tree.
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            work = Path(directory).resolve()
            source = work / "source"
            source.mkdir()
            challenge = source / "Challenge.lean"
            challenge.write_text("theorem probe : True := by trivial\n")
            frozen = source / "trusted-build"
            frozen.mkdir()
            build, config = remove_untrusted_lake_state(source)
            home = config / "home"
            tmp = config / "tmp"
            home.mkdir()
            tmp.mkdir()

            python = Path(sys.executable).resolve(strict=True)
            landrun = Path(os.environ["PALOMAR_TEST_LANDRUN"]).resolve(strict=True)
            touch_command = shutil.which("touch")
            self.assertIsNotNone(touch_command)
            touch = Path(touch_command).absolute()
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home.resolve()),
                    "TMPDIR": str(tmp.resolve()),
                    "LEAN_ABORT_ON_PANIC": "1",
                }
            )
            executable_paths = [
                Path(sys.executable).resolve().parent.parent,
                python,
                landrun,
                touch,
                frozen,
            ]
            for raw in (
                "/usr",
                "/bin",
                "/lib",
                "/lib64",
                "/run/current-system/sw",
                "/nix/store",
            ):
                path = Path(raw)
                if path.exists():
                    executable_paths.append(path.resolve())

            verify_sandbox_confinement(
                work / "write-denied",
                work / "read-denied",
                positive_read=challenge,
                python=python,
                touch=touch,
                cwd=source,
                environment=environment,
                landrun=landrun,
                writable_directories=[build, config],
                protected_write_directories=[frozen],
                readable_paths=sorted({source.resolve(), *system_readable_paths()}),
                executable_paths=sorted(set(executable_paths)),
                tools=tool_snapshot([python, landrun, touch]),
            )

    @unittest.skipUnless(
        os.environ.get("PALOMAR_TEST_LANDRUN"),
        "set PALOMAR_TEST_LANDRUN to exercise the real Landrun/systemd boundary",
    )
    def test_real_renderer_policy_proves_the_same_controls(self):
        # The render build runs untrusted compile-time Lean, so it is held to
        # the verifier's controls. Its policy is not the verifier's: the read
        # allowlist is the render workspace alone, with no certificate or
        # name-service files, and the executable set comes from the renderer's
        # own resolver. Probe that policy rather than a lookalike.
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            work = Path(directory).resolve()
            workspace_checkout = work / "workspace"
            workspace_checkout.mkdir()
            challenge = workspace_checkout / "Challenge.lean"
            challenge.write_text("theorem probe : True := by trivial\n")
            build, config = remove_untrusted_lake_state(workspace_checkout)
            home = config / "home"
            tmp = config / "tmp"
            home.mkdir()
            tmp.mkdir()

            python = Path(sys.executable).resolve(strict=True)
            landrun = Path(os.environ["PALOMAR_TEST_LANDRUN"]).resolve(strict=True)
            touch_command = shutil.which("touch")
            self.assertIsNotNone(touch_command)
            touch = Path(touch_command).absolute()
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home.resolve()),
                    "TMPDIR": str(tmp.resolve()),
                    "LEAN_ABORT_ON_PANIC": "1",
                }
            )
            # Without a Lean toolchain the renderer's resolver still produces
            # the shape that matters here: the interpreter prefix, the pinned
            # programs, their linkage, and the immutable system directories.
            toolchain_prefix = python.parent.parent
            executable_paths = renderer_executable_paths(
                toolchain_prefix, [landrun, python, touch]
            )

            verify_sandbox_confinement(
                work / "render-landrun-write-denial-probe",
                work / "render-landrun-read-denial-probe",
                positive_read=challenge,
                python=python,
                touch=touch,
                cwd=workspace_checkout,
                environment=environment,
                landrun=landrun,
                writable_directories=[build, config],
                readable_paths=[workspace_checkout.resolve()],
                executable_paths=executable_paths,
                tools=tool_snapshot([python, landrun, touch]),
            )

    @unittest.skipUnless(
        os.environ.get("PALOMAR_TEST_LANDRUN") and os.environ.get("PALOMAR_TEST_LEAN"),
        "set PALOMAR_TEST_LANDRUN and PALOMAR_TEST_LEAN for canonical compilation",
    )
    def test_challenge_is_compiled_outside_candidate_lake_state(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            work = Path(directory).resolve()
            source = work / "source"
            source.mkdir()
            (source / "Challenge.lean").write_text("theorem probe : True := by trivial\n")
            lean = Path(os.environ["PALOMAR_TEST_LEAN"]).resolve(strict=True)
            lean_prefix = Path(
                subprocess.run(
                    [str(lean), "--print-prefix"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            ).resolve(strict=True)
            landrun = Path(os.environ["PALOMAR_TEST_LANDRUN"]).resolve(strict=True)
            python = Path(sys.executable).resolve(strict=True)
            executable_paths = [lean_prefix, python.parent.parent, lean, landrun]
            for raw in ("/usr", "/bin", "/lib", "/lib64", "/nix/store"):
                path = Path(raw)
                if path.exists():
                    executable_paths.append(path.resolve())
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{lean_prefix / 'bin'}:{os.environ['PATH']}",
                    "LEAN_ABORT_ON_PANIC": "1",
                }
            )
            canonical, dependencies, trusted_paths = compile_canonical_challenge(
                work,
                source,
                checkout=source,
                lean=lean,
                lean_prefix=lean_prefix,
                allowlist={},
                environment=environment,
                landrun=landrun,
                readable_paths=sorted({source.resolve(), *system_readable_paths()}),
                executable_paths=sorted(set(executable_paths)),
                tools=tool_snapshot([lean, landrun]),
            )
            self.assertEqual(canonical.parent, work / "canonical-challenge")
            self.assertTrue(canonical.is_file())
            self.assertEqual([path.name for path in canonical.parent.iterdir()], ["Challenge.olean"])
            self.assertEqual(trusted_paths[0], lean_prefix / "lib" / "lean")
            self.assertTrue(all(path_is_relative_to(path, lean_prefix) for path in dependencies))

            candidate = work / "candidate-build" / "lib" / "lean"
            candidate.mkdir(parents=True)
            fake_source = work / "FakeChallenge.lean"
            fake_source.write_text("theorem probe : False := by sorry\n")
            subprocess.run(
                [str(lean), "-o", str(candidate / "Challenge.olean"), str(fake_source)],
                check=True,
                env=environment,
                capture_output=True,
                text=True,
            )
            check_source = work / "CheckProtected.lean"
            check_source.write_text("import Challenge\nexample : True := probe\n")
            protected_environment = environment.copy()
            protected_environment["LEAN_PATH"] = protected_lean_path(
                canonical,
                trusted_paths,
                str(candidate),
            )
            subprocess.run(
                [str(lean), str(check_source)],
                check=True,
                env=protected_environment,
                capture_output=True,
                text=True,
            )

    @unittest.skipUnless(
        os.environ.get("PALOMAR_TEST_LANDRUN") and os.environ.get("PALOMAR_TEST_LEAN"),
        "set PALOMAR_TEST_LANDRUN and PALOMAR_TEST_LEAN for canonical compilation",
    )
    def test_protected_alias_does_not_capture_a_sibling_solution_module(self):
        """A search root owns a top-level prefix, not one object-file path."""
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            work = Path(directory).resolve()
            source = work / "source"
            (source / "Shared").mkdir(parents=True)
            challenge_source = source / "Shared" / "Challenge.lean"
            challenge_source.write_text("theorem challengeProbe : True := by trivial\n")
            solution_source = source / "Shared" / "Solution.lean"
            solution_source.write_text("theorem solutionProbe : True := by trivial\n")
            lean, lean_prefix, landrun, environment, executable_paths = canonical_toolchain()
            canonical, _dependencies, trusted_paths = compile_canonical_challenge(
                work,
                source,
                checkout=source,
                challenge_source=challenge_source,
                challenge_module="Shared.Challenge",
                published_module="PalomarCanonicalProbe.Challenge",
                lean=lean,
                lean_prefix=lean_prefix,
                allowlist={},
                environment=environment,
                landrun=landrun,
                readable_paths=sorted({source.resolve(), *system_readable_paths()}),
                executable_paths=executable_paths,
                tools=tool_snapshot([lean, landrun]),
            )
            candidate = work / "candidate-build" / "lib" / "lean"
            (candidate / "Shared").mkdir(parents=True)
            subprocess.run(
                [
                    str(lean),
                    "-R",
                    str(source),
                    "-o",
                    str(candidate / "Shared" / "Solution.olean"),
                    str(solution_source),
                ],
                check=True,
                env=environment,
                capture_output=True,
                text=True,
            )
            check_source = work / "CheckBoth.lean"
            check_source.write_text(
                "import PalomarCanonicalProbe.Challenge\n"
                "import Shared.Solution\n"
                "#check challengeProbe\n"
                "#check solutionProbe\n"
            )
            protected_environment = environment.copy()
            protected_environment["LEAN_PATH"] = protected_lean_path(
                canonical,
                trusted_paths,
                str(candidate),
                protected_root=work / "canonical-challenge",
            )
            subprocess.run(
                [str(lean), "-R", str(work), str(check_source)],
                check=True,
                env=protected_environment,
                capture_output=True,
                text=True,
            )

    @unittest.skipUnless(
        os.environ.get("PALOMAR_TEST_LANDRUN") and os.environ.get("PALOMAR_TEST_LEAN"),
        "set PALOMAR_TEST_LANDRUN and PALOMAR_TEST_LEAN for canonical compilation",
    )
    def test_module_system_challenge_publishes_every_artifact(self):
        # A module-system source compiles to a public module plus private,
        # server and IR sidecars. Importing it fails unless all four reach the
        # protected search path, so Comparator would reject the project.
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            work = Path(directory).resolve()
            source = work / "source"
            source.mkdir()
            (source / "Challenge.lean").write_text(
                "module\n\npublic theorem probe : True := by trivial\n"
            )
            lean, lean_prefix, landrun, environment, executable_paths = canonical_toolchain()
            canonical, _dependencies, trusted_paths = compile_canonical_challenge(
                work,
                source,
                checkout=source,
                lean=lean,
                lean_prefix=lean_prefix,
                allowlist={},
                environment=environment,
                landrun=landrun,
                readable_paths=sorted({source.resolve(), *system_readable_paths()}),
                executable_paths=executable_paths,
                tools=tool_snapshot([lean, landrun]),
            )
            self.assertEqual(
                sorted(path.name for path in canonical.parent.iterdir()),
                [
                    "Challenge.ir",
                    "Challenge.olean",
                    "Challenge.olean.private",
                    "Challenge.olean.server",
                ],
            )

            check_source = work / "CheckProtected.lean"
            check_source.write_text("import Challenge\nexample : True := probe\n")
            protected_environment = environment.copy()
            protected_environment["LEAN_PATH"] = protected_lean_path(
                canonical,
                trusted_paths,
                "",
            )
            subprocess.run(
                [str(lean), str(check_source)],
                check=True,
                env=protected_environment,
                capture_output=True,
                text=True,
            )


def canonical_toolchain() -> tuple[Path, Path, Path, dict[str, str], list[Path]]:
    """Resolve the real Lean and Landrun binaries the canonical compile needs."""
    lean = Path(os.environ["PALOMAR_TEST_LEAN"]).resolve(strict=True)
    lean_prefix = Path(
        subprocess.run(
            [str(lean), "--print-prefix"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    ).resolve(strict=True)
    landrun = Path(os.environ["PALOMAR_TEST_LANDRUN"]).resolve(strict=True)
    python = Path(sys.executable).resolve(strict=True)
    executable_paths = [lean_prefix, python.parent.parent, lean, landrun]
    for raw in ("/usr", "/bin", "/lib", "/lib64", "/nix/store"):
        path = Path(raw)
        if path.exists():
            executable_paths.append(path.resolve())
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{lean_prefix / 'bin'}:{os.environ['PATH']}",
            "LEAN_ABORT_ON_PANIC": "1",
        }
    )
    return lean, lean_prefix, landrun, environment, sorted(set(executable_paths))


def path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


if __name__ == "__main__":
    unittest.main()
