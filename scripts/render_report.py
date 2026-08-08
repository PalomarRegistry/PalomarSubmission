"""The path-bound contract between render preparation and execution."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = 2
SHA1_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
PATH_FIELDS = (
    "project_path",
    "challenge_path",
    "solution_path",
    "comparator_config_path",
    "lakefile_path",
    "lean_toolchain_path",
)


class RenderReportError(ValueError):
    """A render report does not satisfy the one supported contract."""


def _text(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        qualifier = "a string" if allow_empty else "a nonempty string"
        raise RenderReportError(f"render report {field} must be {qualifier}")
    return value


@dataclass(frozen=True, slots=True)
class AcceptedRenderPaths:
    """The complete accepted path set supplied by registration."""

    project_path: str
    challenge_path: str
    solution_path: str
    comparator_config_path: str
    lakefile_path: str
    lean_toolchain_path: str

    def __post_init__(self) -> None:
        _text(self.project_path, "source.project_path", allow_empty=True)
        for field in (
            "challenge_path",
            "solution_path",
            "comparator_config_path",
            "lakefile_path",
            "lean_toolchain_path",
        ):
            _text(getattr(self, field), f"source.{field}")

    @classmethod
    def from_mapping(cls, value: object) -> AcceptedRenderPaths:
        if not isinstance(value, Mapping):
            raise RenderReportError("render report source must be an object")
        return cls(
            project_path=_text(
                value.get("project_path"), "source.project_path", allow_empty=True
            ),
            challenge_path=_text(value.get("challenge_path"), "source.challenge_path"),
            solution_path=_text(value.get("solution_path"), "source.solution_path"),
            comparator_config_path=_text(
                value.get("comparator_config_path"), "source.comparator_config_path"
            ),
            lakefile_path=_text(value.get("lakefile_path"), "source.lakefile_path"),
            lean_toolchain_path=_text(
                value.get("lean_toolchain_path"), "source.lean_toolchain_path"
            ),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "project_path": self.project_path,
            "challenge_path": self.challenge_path,
            "solution_path": self.solution_path,
            "comparator_config_path": self.comparator_config_path,
            "lakefile_path": self.lakefile_path,
            "lean_toolchain_path": self.lean_toolchain_path,
        }


@dataclass(frozen=True, slots=True)
class RenderSource:
    """Source identity and paths bound into a render report."""

    repository: str
    repository_url: str
    commit: str
    challenge_sha256: str
    paths: AcceptedRenderPaths

    def __post_init__(self) -> None:
        _text(self.repository, "source.repository")
        _text(self.repository_url, "source.repository_url")
        if not SHA1_RE.fullmatch(self.commit):
            raise RenderReportError("render report source.commit must be a lowercase SHA-1")
        if not SHA256_RE.fullmatch(self.challenge_sha256):
            raise RenderReportError(
                "render report source.challenge_sha256 must be a lowercase SHA-256"
            )

    @classmethod
    def from_mapping(cls, value: object) -> RenderSource:
        if not isinstance(value, Mapping):
            raise RenderReportError("render report source must be an object")
        expected = {
            "repository",
            "repository_url",
            "commit",
            "challenge_sha256",
            *PATH_FIELDS,
        }
        if set(value) != expected:
            missing = sorted(expected - set(value))
            unexpected = sorted(set(value) - expected)
            details = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if unexpected:
                details.append("unexpected " + ", ".join(unexpected))
            raise RenderReportError(
                "render report source must contain exactly the current accepted path set: "
                + "; ".join(details)
            )
        return cls(
            repository=_text(value.get("repository"), "source.repository"),
            repository_url=_text(value.get("repository_url"), "source.repository_url"),
            commit=_text(value.get("commit"), "source.commit"),
            challenge_sha256=_text(
                value.get("challenge_sha256"), "source.challenge_sha256"
            ),
            paths=AcceptedRenderPaths.from_mapping(value),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "repository": self.repository,
            "repository_url": self.repository_url,
            "commit": self.commit,
            "challenge_sha256": self.challenge_sha256,
            **self.paths.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class PreparedRenderReport:
    """The validated data the execution phase is allowed to consume."""

    source: RenderSource
    lean_toolchain: str
    verso_commit: str
    prepared_at: str

    def __post_init__(self) -> None:
        _text(self.lean_toolchain, "lean_toolchain")
        _text(self.prepared_at, "prepared_at")
        if not SHA1_RE.fullmatch(self.verso_commit):
            raise RenderReportError("render report verso_commit must be a lowercase SHA-1")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "pending",
            "stage": "prepared",
            "errors": [],
            "prepared_at": self.prepared_at,
            "source": self.source.as_dict(),
            "lean_toolchain": self.lean_toolchain,
            "verso_commit": self.verso_commit,
        }


def intake_report(prepared_at: str) -> dict[str, Any]:
    """A schema-current failure report, before a request has been accepted."""
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "error",
        "stage": "intake",
        "errors": [],
        "prepared_at": _text(prepared_at, "prepared_at"),
    }


def parse_prepared_report(value: object) -> PreparedRenderReport:
    """Validate untyped JSON before execution uses any report field."""
    if not isinstance(value, Mapping):
        raise RenderReportError("prepared render report must be a JSON object")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise RenderReportError(
            "prepared render report must use schema_version 2 with the complete accepted path set"
        )
    expected = {
        "schema_version",
        "status",
        "stage",
        "errors",
        "prepared_at",
        "source",
        "lean_toolchain",
        "verso_commit",
    }
    if set(value) != expected:
        raise RenderReportError("prepared render report has fields outside the current contract")
    if value.get("status") != "pending" or value.get("stage") != "prepared":
        raise RenderReportError("prepared render report is not pending at the prepared stage")
    if value.get("errors") != []:
        raise RenderReportError("prepared render report carries intake errors")
    return PreparedRenderReport(
        source=RenderSource.from_mapping(value.get("source")),
        lean_toolchain=_text(value.get("lean_toolchain"), "lean_toolchain"),
        verso_commit=_text(value.get("verso_commit"), "verso_commit"),
        prepared_at=_text(value.get("prepared_at"), "prepared_at"),
    )
