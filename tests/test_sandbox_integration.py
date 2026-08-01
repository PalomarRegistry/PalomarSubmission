import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.verify_submission import (
    audit_challenge_sources,
    build_indexed_roots,
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
                lean=lean,
                lean_prefix=lean_prefix,
                allowlist={},
                indexed={},
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
        "set PALOMAR_TEST_LANDRUN and PALOMAR_TEST_LEAN for indexed compilation",
    )
    def test_indexed_challenge_dependency_is_rebuilt_protected_and_audited(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            work = Path(directory).resolve()
            source = work / "source"
            package = source / ".lake" / "packages" / "indexed"
            package.mkdir(parents=True)
            indexed_source = package / "Indexed.lean"
            indexed_source.write_text("def Indexed.meaning : Nat := 42\n")
            (package / "lakefile.toml").write_text(
                'name = "indexed"\ndefaultTargets = ["Indexed"]\n\n'
                '[[lean_lib]]\nname = "Indexed"\n'
            )
            (package / "lake-manifest.json").write_text(
                '{"version":"1.2.0","packagesDir":".lake/packages","packages":[]}\n'
            )
            subprocess.run(["git", "init", "-q"], cwd=package, check=True)
            subprocess.run(["git", "add", "."], cwd=package, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Palomar test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "indexed fixture",
                ],
                cwd=package,
                check=True,
            )
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=package,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            build, config = remove_untrusted_lake_state(package)
            (source / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {
                                "name": "indexed",
                                "type": "git",
                                "url": "https://github.com/example/indexed",
                                "rev": revision,
                            }
                        ]
                    }
                )
            )
            source.mkdir(exist_ok=True)
            (source / "Challenge.lean").write_text(
                "import Indexed\ntheorem probe : Indexed.meaning = 42 := rfl\n"
            )
            lean = Path(os.environ["PALOMAR_TEST_LEAN"]).resolve(strict=True)
            lean_prefix = Path(
                subprocess.run(
                    [str(lean), "--print-prefix"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            ).resolve(strict=True)
            lake = (lean_prefix / "bin" / "lake").resolve(strict=True)
            landrun = Path(os.environ["PALOMAR_TEST_LANDRUN"]).resolve(strict=True)
            python = Path(sys.executable).resolve(strict=True)
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{lean_prefix / 'bin'}:{os.environ['PATH']}",
                    "LEAN_ABORT_ON_PANIC": "1",
                }
            )
            executable_paths = [lean_prefix, python.parent.parent, lean, lake, landrun]
            for raw in ("/usr", "/bin", "/lib", "/lib64", "/run/current-system/sw", "/nix/store"):
                path = Path(raw)
                if path.exists():
                    executable_paths.append(path.resolve())
            executable_paths = sorted(set(executable_paths))
            readable_paths = sorted({source.resolve(), *system_readable_paths()})
            tools = tool_snapshot([lean, lake, landrun])
            packages = [
                {
                    "name": "indexed",
                    "repository": "example/indexed",
                    "url": "https://github.com/example/indexed",
                    "revision": revision,
                }
            ]
            record = {
                "repository": "example/indexed",
                "revision": revision,
                "palomar_id": "PALOMAR-2026-08-01-000001",
                "palomar_version": 1,
            }
            build_indexed_roots(
                source,
                packages=packages,
                indexed={"indexed": record},
                allowlist={},
                base_env=environment,
                lake=lake,
                landrun=landrun,
                readable_paths=readable_paths,
                executable_paths=executable_paths,
                tools=tools,
            )
            indexed_olean = build / "lib" / "lean" / "Indexed.olean"
            self.assertTrue(indexed_olean.is_file())
            canonical, dependency_sources, trusted_paths = compile_canonical_challenge(
                work,
                source,
                lean=lean,
                lean_prefix=lean_prefix,
                allowlist={},
                indexed={"indexed": record},
                environment=environment,
                landrun=landrun,
                readable_paths=readable_paths,
                executable_paths=executable_paths,
                tools=tools,
            )
            self.assertTrue(canonical.is_file())
            self.assertIn(indexed_olean.parent.resolve(), trusted_paths)
            audit = audit_challenge_sources(
                source,
                database=work / "database",
                dependency_sources=dependency_sources,
                lean_prefix=lean_prefix,
                allowlist={},
                indexed={"indexed": record},
                writable_directories=[],
            )
            self.assertEqual(audit["untrusted_sources"], [])
            self.assertEqual(audit["trust_level"], "qualified")
            self.assertEqual(audit["dependencies"][0]["palomar_version"], 1)
            self.assertEqual(audit["review_source_files"][0]["path"], "Indexed.lean")
            self.assertFalse(config.is_symlink())


def path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


if __name__ == "__main__":
    unittest.main()
