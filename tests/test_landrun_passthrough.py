import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from scripts.landrun_passthrough import (
    adapted_arguments,
    adapted_environment,
    command_index,
    passed_environment_names,
)

LEAN4EXPORT = "/tools/lean4export"
POLICY = {
    "COMPARATOR_LEAN4EXPORT": LEAN4EXPORT,
    "PALOMAR_CHALLENGE_MODULE": "Palomar.Erdos730.Challenge",
    "PALOMAR_SOLUTION_MODULE": "Palomar.Erdos730.Solution",
    "PALOMAR_CHALLENGE_LEAN_PATH": "/work/canonical-challenge:/toolchain/lib/lean",
    "LEAN_PATH": "/source/.lake/build/lib/lean:/mathlib/lib/lean",
}


def landrun_arguments(command: list[str], environment_names: tuple[str, ...]) -> list[str]:
    """What Comparator's `buildLandrunArgs` emits, including its own `--`.

    The fixed prefix, the `--env` entries, the path options and the delimiter
    before the sandboxed command all mirror the pinned Comparator commit; the
    adapter has to read that shape, not a simplified one.
    """
    arguments = ["--best-effort", "--ro", "/", "--rw", "/dev", "-ldd", "-add-exec"]
    for name in environment_names:
        arguments += ["--env", name]
    arguments += ["--ro", "/source", "--ro", "/source/.lake", "--rox", "/toolchain"]
    return [*arguments, "--", *command]


def export_arguments(module: str) -> list[str]:
    """The Landrun arguments Comparator builds for one lean4export run."""
    return landrun_arguments(
        [LEAN4EXPORT, module, "--", "Palomar.Erdos730.statement"],
        ("PATH", "HOME", "LEAN_PATH", "LEAN_ABORT_ON_PANIC"),
    )


class LandrunPassthroughTests(unittest.TestCase):
    def test_comparator_arguments(self) -> None:
        arguments = [
            "--best-effort",
            "--ro",
            "/",
            "--rw",
            "/dev",
            "-ldd",
            "-add-exec",
            "--env",
            "PATH",
            "--rox",
            "/toolchain",
            "/tools/lean4export",
            "Challenge",
            "--",
            "Namespace.theorem",
        ]
        self.assertEqual(command_index(arguments), 11)

    def test_rejects_unknown_option(self) -> None:
        with self.assertRaises(ValueError):
            command_index(["--surprise", "lean"])

    def test_adds_exactly_one_landrun_delimiter(self) -> None:
        arguments = ["--ro", "/source", "/usr/bin/lean", "Challenge", "--", "theorem"]
        self.assertEqual(
            adapted_arguments(arguments),
            [
                "--ro",
                "/source",
                "--env",
                "GIT_CONFIG_GLOBAL=/dev/null",
                "--env",
                "GIT_CONFIG_NOSYSTEM=1",
                "--env",
                "GIT_TERMINAL_PROMPT=0",
                "--",
                "/usr/bin/lean",
                "Challenge",
                "--",
                "theorem",
            ],
        )
        already_delimited = ["--ro", "/source", "--", "/usr/bin/lean", "Challenge"]
        adapted = adapted_arguments(already_delimited)
        self.assertEqual(adapted.count("--"), 1)
        self.assertEqual(adapted[-3:], ["--", "/usr/bin/lean", "Challenge"])
        self.assertIn("GIT_CONFIG_GLOBAL=/dev/null", adapted)
        self.assertIn("GIT_CONFIG_NOSYSTEM=1", adapted)
        self.assertIn("GIT_TERMINAL_PROMPT=0", adapted)


class PassedEnvironmentNameTests(unittest.TestCase):
    def test_reads_both_landrun_spellings(self) -> None:
        arguments = [
            "--env",
            "PATH",
            "--env=HOME",
            "--env",
            "GIT_TERMINAL_PROMPT=0",
            "--ro",
            "/source",
            "/usr/bin/lean",
            "Challenge",
        ]
        self.assertEqual(
            passed_environment_names(arguments),
            {"PATH", "HOME", "GIT_TERMINAL_PROMPT"},
        )

    def test_splits_a_comma_packed_value_as_landrun_does(self) -> None:
        # Landrun 0.1.15 splits an --env value at commas, so this names two
        # variables. Reading it as one name would hide LEAN_PATH.
        arguments = ["--env", "PATH,LEAN_PATH", "/usr/bin/lean", "Challenge"]
        self.assertEqual(passed_environment_names(arguments), {"PATH", "LEAN_PATH"})

    def test_trims_entries_so_the_reading_holds_across_landrun_builds(self) -> None:
        # Landrun builds disagree here. 0.1.15-main installs FOO for
        # `--env " FOO"` and for `--env "PATH, FOO"`; the pinned 0.1.18 build
        # (commit 811cfff5) looks the padded name up literally and installs
        # nothing. Normalizing is correct under both: against a trimming build
        # it stops a name this adapter would otherwise never see, and against a
        # literal one the child simply gets no LEAN_PATH and the export fails
        # loudly rather than quietly using the candidate's path.
        for value in (" LEAN_PATH", "LEAN_PATH ", "PATH, LEAN_PATH", "PATH,,LEAN_PATH"):
            arguments = ["--env", value, "/usr/bin/lean", "Challenge"]
            self.assertIn("LEAN_PATH", passed_environment_names(arguments), value)
            self.assertNotIn("", passed_environment_names(arguments), value)

    def test_ignores_names_in_the_sandboxed_command(self) -> None:
        arguments = ["--ro", "/source", "/usr/bin/lean", "--env", "LEAN_PATH"]
        self.assertEqual(passed_environment_names(arguments), set())


class ChallengeExportEnvironmentTests(unittest.TestCase):
    def test_challenge_export_gets_the_protected_search_path(self) -> None:
        environment = adapted_environment(
            export_arguments("Palomar.Erdos730.Challenge"), POLICY
        )
        self.assertEqual(
            environment["LEAN_PATH"], "/work/canonical-challenge:/toolchain/lib/lean"
        )

    def test_solution_export_keeps_the_candidate_search_path(self) -> None:
        # Palomar's protected directory holds only the Challenge, and Lean
        # picks a search path entry by root component alone, so putting it on
        # this export's path makes `Palomar.Erdos730.Solution` unreadable
        # (PalomarSubmission#108).
        environment = adapted_environment(
            export_arguments("Palomar.Erdos730.Solution"), POLICY
        )
        self.assertEqual(
            environment["LEAN_PATH"], "/source/.lake/build/lib/lean:/mathlib/lib/lean"
        )

    def test_commands_without_lean_path_are_untouched(self) -> None:
        # Comparator's `lake build` passes PATH, HOME and LEAN_ABORT_ON_PANIC
        # only, and the external kernels pass nothing.
        build = landrun_arguments(
            ["/usr/bin/lake", "build", "Palomar.Erdos730.Solution"],
            ("PATH", "HOME", "LEAN_ABORT_ON_PANIC"),
        )
        self.assertEqual(adapted_environment(build, POLICY), dict(POLICY))
        kernel = landrun_arguments(["/tools/nanoda_bin", "/tmp/config.json"], ())
        self.assertEqual(adapted_environment(kernel, POLICY), dict(POLICY))

    def test_unclassifiable_module_is_refused(self) -> None:
        # Fail closed: exporting the Challenge with the candidate's search path
        # would compare the candidate's statement against itself.
        with self.assertRaises(ValueError):
            adapted_environment(export_arguments("Palomar.Erdos730.Other"), POLICY)

    def test_unexpected_lean_path_consumer_is_refused(self) -> None:
        arguments = list(export_arguments("Palomar.Erdos730.Challenge"))
        arguments[arguments.index(LEAN4EXPORT)] = "/tools/somewhere-else"
        with self.assertRaises(ValueError):
            adapted_environment(arguments, POLICY)

    def test_missing_policy_is_refused(self) -> None:
        for name in (
            "PALOMAR_CHALLENGE_MODULE",
            "PALOMAR_SOLUTION_MODULE",
            "PALOMAR_CHALLENGE_LEAN_PATH",
        ):
            partial = {key: value for key, value in POLICY.items() if key != name}
            with self.assertRaises(ValueError, msg=name):
                adapted_environment(
                    export_arguments("Palomar.Erdos730.Challenge"), partial
                )

    def test_export_naming_no_module_is_refused(self) -> None:
        arguments = ["--env", "LEAN_PATH", "--ro", "/source", LEAN4EXPORT]
        with self.assertRaises(ValueError):
            adapted_environment(arguments, POLICY)

    def test_inline_lean_path_assignment_is_refused(self) -> None:
        # Landrun would install this value itself, so replacing LEAN_PATH in
        # this process would not reach the child: the Challenge would be
        # exported against the candidate's path and compared with itself.
        arguments = list(export_arguments("Palomar.Erdos730.Challenge"))
        arguments[arguments.index("LEAN_PATH")] = "LEAN_PATH=/candidate"
        with self.assertRaises(ValueError):
            adapted_environment(arguments, POLICY)

    def test_comma_packed_lean_path_is_still_seen(self) -> None:
        # Landrun splits this into PATH and LEAN_PATH, both taking the value
        # this process holds, so the override still reaches the child. Reading
        # the entry as a single name would have missed it and left the
        # candidate's path in place.
        arguments = list(export_arguments("Palomar.Erdos730.Challenge"))
        arguments[arguments.index("LEAN_PATH")] = "PATH,LEAN_PATH"
        environment = adapted_environment(arguments, POLICY)
        self.assertEqual(
            environment["LEAN_PATH"], "/work/canonical-challenge:/toolchain/lib/lean"
        )

    def test_comma_packed_inline_lean_path_is_refused(self) -> None:
        arguments = list(export_arguments("Palomar.Erdos730.Challenge"))
        arguments[arguments.index("LEAN_PATH")] = "PATH,LEAN_PATH=/candidate"
        with self.assertRaises(ValueError):
            adapted_environment(arguments, POLICY)

    def test_whitespace_padded_lean_path_is_still_seen(self) -> None:
        # Landrun trims the entry and installs LEAN_PATH, so the override has
        # to apply here too rather than the command passing through untouched
        # with the candidate's search path in place.
        for value in (" LEAN_PATH", "LEAN_PATH ", "PATH, LEAN_PATH"):
            arguments = list(export_arguments("Palomar.Erdos730.Challenge"))
            arguments[arguments.index("LEAN_PATH")] = value
            environment = adapted_environment(arguments, POLICY)
            self.assertEqual(
                environment["LEAN_PATH"],
                "/work/canonical-challenge:/toolchain/lib/lean",
                value,
            )

    def test_whitespace_padded_inline_lean_path_is_refused(self) -> None:
        for value in (" LEAN_PATH=/candidate", "PATH, LEAN_PATH=/candidate"):
            arguments = list(export_arguments("Palomar.Erdos730.Challenge"))
            arguments[arguments.index("LEAN_PATH")] = value
            with self.assertRaises(ValueError, msg=value):
                adapted_environment(arguments, POLICY)

    def test_inline_lean_path_in_the_joined_option_is_refused(self) -> None:
        # Landrun accepts `--env=NAME=VALUE` as well as `--env NAME=VALUE`.
        arguments = list(export_arguments("Palomar.Erdos730.Challenge"))
        index = arguments.index("LEAN_PATH")
        arguments[index - 1 : index + 1] = ["--env=LEAN_PATH=/candidate"]
        with self.assertRaises(ValueError):
            adapted_environment(arguments, POLICY)

    def test_repeated_lean_path_is_refused(self) -> None:
        # Two entries leave which one Landrun installs, and which one the
        # child's getenv resolves, to the ordering rather than to this policy.
        for extra in (["--env", "LEAN_PATH"], ["--env", "LEAN_PATH=/candidate"]):
            arguments = list(export_arguments("Palomar.Erdos730.Challenge"))
            index = arguments.index("LEAN_PATH") - 1
            arguments[index:index] = extra
            with self.assertRaises(ValueError, msg=str(extra)):
                adapted_environment(arguments, POLICY)

    def test_exporting_more_than_one_module_is_refused(self) -> None:
        # lean4export takes any number of modules before `--`. Classifying on
        # the first would call `lean4export Solution Challenge` a Solution
        # export and hand it the candidate's search path.
        arguments = list(export_arguments("Palomar.Erdos730.Solution"))
        arguments.insert(arguments.index(LEAN4EXPORT) + 2, "Palomar.Erdos730.Challenge")
        with self.assertRaises(ValueError):
            adapted_environment(arguments, POLICY)


class AdapterProcessTests(unittest.TestCase):
    """Exercise the adapter as Comparator runs it: argv rewrite and exec together."""

    def run_adapter(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            stand_in = Path(directory) / "landrun"
            stand_in.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json, os, sys
                    print(json.dumps({"argv": sys.argv[1:], "LEAN_PATH": os.environ.get("LEAN_PATH")}))
                    """
                )
            )
            stand_in.chmod(0o755)
            environment = {
                **os.environ,
                "PALOMAR_LANDRUN_REAL": str(stand_in),
                "COMPARATOR_LEAN4EXPORT": LEAN4EXPORT,
                "PALOMAR_CHALLENGE_MODULE": POLICY["PALOMAR_CHALLENGE_MODULE"],
                "PALOMAR_SOLUTION_MODULE": POLICY["PALOMAR_SOLUTION_MODULE"],
                "PALOMAR_CHALLENGE_LEAN_PATH": POLICY["PALOMAR_CHALLENGE_LEAN_PATH"],
                "LEAN_PATH": POLICY["LEAN_PATH"],
            }
            return subprocess.run(
                [sys.executable, str(root / "scripts" / "landrun_passthrough.py"), *arguments],
                capture_output=True,
                text=True,
                env=environment,
                cwd=root,
            )

    def test_challenge_export_reaches_landrun_with_the_protected_path(self) -> None:
        result = self.run_adapter(export_arguments("Palomar.Erdos730.Challenge"))
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        self.assertEqual(observed["LEAN_PATH"], POLICY["PALOMAR_CHALLENGE_LEAN_PATH"])
        # The Git isolation and the delimiter placement survive the override:
        # the first `--` is Landrun's, and the command follows it directly.
        argv = observed["argv"]
        self.assertEqual(argv[argv.index("--") + 1], LEAN4EXPORT)
        self.assertIn("GIT_CONFIG_GLOBAL=/dev/null", argv)

    def test_solution_export_reaches_landrun_with_the_candidate_path(self) -> None:
        result = self.run_adapter(export_arguments("Palomar.Erdos730.Solution"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["LEAN_PATH"], POLICY["LEAN_PATH"])

    def test_refusal_does_not_exec_and_names_the_adapter(self) -> None:
        arguments = list(export_arguments("Palomar.Erdos730.Challenge"))
        arguments[arguments.index("LEAN_PATH")] = "LEAN_PATH=/candidate"
        result = self.run_adapter(arguments)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        # `comparator_failure` keys the palomar-owned diagnostic on this prefix.
        self.assertIn("landrun adapter:", result.stderr)


if __name__ == "__main__":
    unittest.main()
