"""Non-executing checks on the immutable submitted Lean source snapshot."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from scripts.verification_errors import VerificationError

MAX_LEAN_SOURCE_LINES = 10_000
SOURCE_REQUIREMENTS_VERSION = 1
COMMENT_MARKER = re.compile(r"/-|-/")
# Lean 4 Init/Meta/Defs.lean identifier characters; Python Unicode classes
# are broader. A qualified identifier also continues across a dot.
ID_LETTER_LIKE = (
    r"\u03b1-\u03ba\u03bc-\u03c9\u0391-\u039f\u03a1-\u03a2\u03a4-\u03a9"
    r"\u03ca-\u03fb\u1f00-\u1ffe\u2100-\u214f\U0001d49c-\U0001d59f"
    r"\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u017f"
)
ID_FIRST = rf"A-Za-z_{ID_LETTER_LIKE}"
ID_REST = rf"{ID_FIRST}0-9'!?\u2080-\u2089\u2090-\u209c\u1d62-\u1d6a\u2c7c"
IDENTIFIER_CONTINUATION = re.compile(rf"[{ID_REST}]|\.[{ID_FIRST}«]")


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
            # Lean consumes the character after the plain opener before
            # scanning its body (unlike nested openers). Match its parser.
            index += 3
            depth = 1
            while depth:
                marker = COMMENT_MARKER.search(text, index)
                if marker is None:
                    break
                depth += 1 if marker.group() == "/-" else -1
                index = marker.end()
            if depth:
                return False
        else:
            return text.startswith("module", index) and (
                IDENTIFIER_CONTINUATION.match(text, index + 6) is None
            )
    return False


def physical_lines(text: str) -> int:
    """LF/CRLF lines; an unterminated final line counts, a final LF adds none."""
    return text.count("\n") + int(bool(text) and not text.endswith("\n"))


def lean_source_files(root: Path) -> list[Path]:
    """Lean source paths, including contained projects and symlinks to reject.

    Lake configuration shares the line cap, but is exempt from module headers.
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
            if name.endswith(".lean") and (path.is_symlink() or path.is_file()):
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
    if Path(path).name != "lakefile.lean" and not has_module_header(text):
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
        if path.is_symlink():
            issues.append(VerificationError(
                f"{relative} must be a regular Lean file, not a symbolic link",
                code="source.symlink_not_allowed", path=relative,
                next_action="Commit the Lean source as a regular .lean file and submit the new commit.",
            ))
            continue
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
