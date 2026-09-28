"""Non-executing checks on the immutable submitted Lean source snapshot."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from scripts.verification_errors import VerificationError

MAX_LEAN_SOURCE_LINES = 10_000
SOURCE_REQUIREMENTS_VERSION = 1


def has_module_header(text: str) -> bool:
    """Recognize the initial marker, without confusing comments with headers.

    Documentation comments are commands, not header whitespace. Do not strip a
    BOM or arbitrary Unicode whitespace: Lean's header parser does not either.
    This cheap check precedes installation; execute confirms with --deps-json.
    """
    index = 0
    while index < len(text):
        if text[index] in " \r\n":
            index += 1
        elif text.startswith("--", index):
            end = text.find("\n", index + 2)
            index = len(text) if end < 0 else end + 1
        elif text.startswith("/-", index) and not text.startswith(("/--", "/-!"), index):
            index += 2
            depth = 1
            while depth and index < len(text):
                if text.startswith("/-", index):
                    depth += 1
                    index += 2
                elif text.startswith("-/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            if depth:
                return False
        else:
            return text.startswith("module", index) and (
                index + 6 == len(text)
                or text[index + 6] in " \r\n"
                or text.startswith(("--", "/-"), index + 6)
            )
    return False


def physical_lines(text: str) -> int:
    """LF/CRLF lines; an unterminated final line counts, a final LF adds none."""
    return text.count("\n") + int(bool(text) and not text.endswith("\n"))


def lean_source_files(root: Path) -> list[Path]:
    """All regular Lean sources, including contained projects/path dependencies.

    Lake configuration and generated/dependency state are separate contracts.
    Never traverse symlinks or Git internals.
    """
    files = []
    for directory, subdirectories, names in os.walk(root, followlinks=False):
        subdirectories[:] = sorted(
            name for name in subdirectories
            if name not in {".git", ".lake"} and not (Path(directory) / name).is_symlink()
        )
        for name in sorted(names):
            path = Path(directory) / name
            if (
                name.endswith(".lean") and name != "lakefile.lean"
                and not path.is_symlink() and path.is_file()
            ):
                files.append(path)
    return files


def source_issues(text: str, path: str) -> list[VerificationError]:
    issues = []
    lines = physical_lines(text)
    if lines > MAX_LEAN_SOURCE_LINES:
        issues.append(VerificationError(
            f"{path} has {lines:,} lines; each submitted Lean source file must have at most 10,000 lines",
            code="source.file_too_long", path=path, line=MAX_LEAN_SOURCE_LINES + 1,
            next_action=(
                "Split the source into smaller modules or reduce the certificate, "
                "commit the changes, and submit the new commit."
            ),
        ))
    if not has_module_header(text):
        issues.append(VerificationError(
            f"{path} must begin with the module header keyword (ordinary comments may precede it)",
            code="source.module_required", path=path, line=1,
            next_action=(
                "Port the Lean source to the module system, including public declarations/imports "
                "and exposed definitions as needed; rebuild, commit, and submit the new commit."
            ),
        ))
    return issues


def inspect_lean_sources(root: Path) -> tuple[dict[str, Any], list[VerificationError]]:
    files = lean_source_files(root)
    issues: list[VerificationError] = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        try:
            # Keep LF-based physical counts: universal newline conversion would
            # turn bare CR into extra lines not present in the committed file.
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            issues.append(VerificationError(
                f"{relative} is not valid UTF-8", code="source.invalid_utf8", path=relative,
            ))
            continue
        issues.extend(source_issues(text, relative))
    return {
        "schema_version": SOURCE_REQUIREMENTS_VERSION,
        "module_required": True,
        "maximum_lines": MAX_LEAN_SOURCE_LINES,
        "files_checked": len(files),
    }, issues
