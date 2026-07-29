#!/usr/bin/env python3
"""Preserve a sandboxed command's arguments when invoking Landrun.

Landrun's CLI parser consumes the first ``--`` even when it belongs to the
sandboxed program. Comparator's lean4export invocation needs that delimiter,
so this adapter inserts a Landrun delimiter before the command itself.
"""

from __future__ import annotations

import os
import sys
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


def main() -> int:
    try:
        real = Path(os.environ["PALOMAR_LANDRUN_REAL"]).resolve(strict=True)
        if not real.is_file():
            raise ValueError("PALOMAR_LANDRUN_REAL is not a file")
        arguments = sys.argv[1:]
        index = command_index(arguments)
        os.execv(str(real), [str(real), *arguments[:index], "--", *arguments[index:]])
    except (KeyError, OSError, ValueError) as error:
        print(f"landrun adapter: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
