#!/usr/bin/env python3
"""Preserve a sandboxed command's arguments when invoking Landrun.

Landrun's CLI parser consumes the first ``--`` even when it belongs to the
sandboxed program. Comparator's lean4export invocation needs that delimiter,
so this adapter inserts a Landrun delimiter before the command itself.

The adapter also decides which search path each of Comparator's two exports
runs with; see `adapted_environment`.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

VALUE_OPTIONS = {
    "--log-level",
    "--ro",
    "--rox",
    "--rw",
    "--rwx",
    "--unix",
    "--bind-tcp",
    "--connect-tcp",
    "--env",
}
FLAG_OPTIONS = {
    "--best-effort",
    "--unrestricted-filesystem",
    "--unrestricted-network",
    "--unrestricted-scoped",
    "--ignore-missing",
    "--log-disable-originating",
    "--log-enable-subprocesses",
    "--log-disable-subdomains",
    "--ldd",
    "-ldd",
    "--add-exec",
    "-add-exec",
}
FIXED_ENVIRONMENT = (
    "GIT_CONFIG_GLOBAL=/dev/null",
    "GIT_CONFIG_NOSYSTEM=1",
    "GIT_TERMINAL_PROMPT=0",
)


def command_index(arguments: list[str]) -> int:
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            return index + 1
        option = argument.split("=", 1)[0]
        if option in VALUE_OPTIONS:
            index += 1 if "=" in argument else 2
            continue
        if option in FLAG_OPTIONS:
            index += 1
            continue
        if argument.startswith("-"):
            raise ValueError(f"unsupported Landrun option: {argument}")
        return index
    raise ValueError("missing sandboxed command")


def adapted_arguments(arguments: list[str]) -> list[str]:
    """Inject fixed Git isolation and exactly one Landrun delimiter."""
    index = command_index(arguments)
    option_end = index - 1 if index and arguments[index - 1] == "--" else index
    fixed = [item for value in FIXED_ENVIRONMENT for item in ("--env", value)]
    return [*arguments[:option_end], *fixed, "--", *arguments[index:]]


def environment_specifications(arguments: list[str]) -> list[str]:
    """Every variable Landrun is asked to install, one entry per variable.

    Landrun splits an ``--env`` value at commas, so ``--env PATH,LEAN_PATH``
    names two variables. Each entry is either ``NAME``, taking the value
    Landrun itself was given, or ``NAME=VALUE``, taking the value inline.

    Entries are stripped and empty ones dropped because Landrun builds disagree
    about padding: 0.1.15-main installs ``FOO`` for ``--env " FOO"`` while the
    pinned 0.1.18 build looks that name up literally and installs nothing.
    Reading the padded form as a distinct name would hide it here against the
    first kind of build, and normalizing costs nothing against the second.
    """
    index = command_index(arguments)
    end = index - 1 if index and arguments[index - 1] == "--" else index
    specifications: list[str] = []
    index = 0
    while index < end:
        option, separator, inline = arguments[index].partition("=")
        if option not in VALUE_OPTIONS:
            index += 1
            continue
        value = inline if separator else arguments[index + 1]
        if option == "--env":
            specifications.extend(entry for raw in value.split(",") if (entry := raw.strip()))
        index += 1 if separator else 2
    return specifications


def specification_name(specification: str) -> str:
    """The variable an ``--env`` entry installs."""
    return specification.split("=", 1)[0].strip()


def passed_environment_names(arguments: list[str]) -> set[str]:
    """The variable names Landrun is asked to pass to the sandboxed command."""
    return {specification_name(entry) for entry in environment_specifications(arguments)}


def adapted_environment(arguments: list[str], environment: Mapping[str, str]) -> dict[str, str]:
    """Give the Challenge export the protected search path and no other command.

    Comparator exports the Challenge and the Solution in two separate
    lean4export processes and passes ``LEAN_PATH`` to both. Only the Challenge
    export may resolve modules in Palomar's protected directory. Putting that
    directory on the Solution export's path as well makes every module sharing
    the Challenge's root component unreadable, because Lean picks a search path
    entry by root component alone (PalomarSubmission#108), and it buys nothing:
    the Solution environment is meant to be the candidate's, and comparing the
    two exported statements is what catches a candidate Lake plan that builds
    ``Challenge.lean`` into a different statement.

    The override works by replacing this process's own ``LEAN_PATH`` before
    handing over to Landrun, which is why every command carrying ``LEAN_PATH``
    must match the pinned invocation shape exactly: one bare ``--env
    LEAN_PATH`` entry, the configured lean4export, and exactly one module
    before its ``--``. An inline ``--env LEAN_PATH=...`` would be installed by
    Landrun itself and defeat the override, a second entry would leave which
    value the child resolves to the ordering rather than to this policy, and a
    second module would make the exported environment more than the one
    classified here. Each is refused rather than run, so a change in how
    Comparator spawns lean4export stops the run instead of silently exporting
    the candidate's Challenge as if it were Palomar's.

    This checks Comparator's invocation, not Comparator itself. A revision
    that set ``LEAN_PATH`` inside a wrapper, or that stopped going through
    ``COMPARATOR_LANDRUN``, would be invisible here and remains the pin
    review's responsibility.
    """
    installed = [
        entry
        for entry in environment_specifications(arguments)
        if specification_name(entry) == "LEAN_PATH"
    ]
    if not installed:
        return dict(environment)
    if len(installed) != 1 or "=" in installed[0]:
        raise ValueError("LEAN_PATH is not passed exactly once, by name alone")
    command = arguments[command_index(arguments) :]
    challenge_module = environment.get("PALOMAR_CHALLENGE_MODULE")
    solution_module = environment.get("PALOMAR_SOLUTION_MODULE")
    challenge_lean_path = environment.get("PALOMAR_CHALLENGE_LEAN_PATH")
    if not (challenge_module and solution_module and challenge_lean_path):
        raise ValueError("Challenge export search path is not configured")
    lean4export = environment.get("COMPARATOR_LEAN4EXPORT")
    if not lean4export or command[:1] != [lean4export]:
        raise ValueError(f"unexpected LEAN_PATH consumer: {' '.join(command[:1]) or '(none)'}")
    # lean4export takes any number of modules before `--`; Comparator passes
    # one, and one is what this classification can speak for.
    if len(command) < 3 or command[2] != "--":
        raise ValueError("lean4export does not export exactly one module")
    module = command[1]
    if module == challenge_module:
        return {**environment, "LEAN_PATH": challenge_lean_path}
    if module == solution_module:
        return dict(environment)
    raise ValueError(f"lean4export exports an unconfigured module: {module}")


def main() -> int:
    try:
        real = Path(os.environ["PALOMAR_LANDRUN_REAL"]).resolve(strict=True)
        if not real.is_file():
            raise ValueError("PALOMAR_LANDRUN_REAL is not a file")
        arguments = sys.argv[1:]
        environment = adapted_environment(arguments, os.environ)
        os.execve(str(real), [str(real), *adapted_arguments(arguments)], environment)
    except (KeyError, OSError, ValueError) as error:
        print(f"landrun adapter: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
