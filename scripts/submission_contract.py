"""Validate dispatch intake and the submitted formalization metadata contract."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from scripts.verification_errors import FormalizationValidationError, VerificationError

__all__ = (
    "ARXIV_CATEGORIES",
    "ARXIV_CATEGORY_NAMES",
    "AUTHORIZATION_RELATIONSHIPS",
    "GITHUB_LOGIN_RE",
    "GITHUB_RE",
    "FORMALIZATION_PROFILE_VERSION",
    "MAX_FORMALIZATION_BYTES",
    "MSC2020_CODES",
    "MSC2020_NAMES",
    "OPTIONAL_FIELDS",
    "ORCID_RE",
    "ORIGINAL_PROOF_TYPE",
    "PALOMAR_ID_RE",
    "RELATED_FORMALIZATION_RELATIONSHIPS",
    "REPOSITORY_RE",
    "REPOSITORY_ROLES",
    "REPAIRABLE_FORMALIZATION_FIELDS",
    "SHA_RE",
    "SOURCE_ENDORSEMENTS",
    "SOURCE_RELATIONSHIPS",
    "SOURCE_TYPES",
    "SUBMISSION_ID_RE",
    "UniqueKeySafeLoader",
    "load_formalization_metadata",
    "normalize_repository",
    "normalized_provenance",
    "reject_obsolete_provenance_fields",
    "submission_request",
)

ROOT = Path(__file__).resolve().parent.parent
MAX_FORMALIZATION_BYTES = 256 * 1024
FORMALIZATION_PROFILE_VERSION = 1
REPAIRABLE_FORMALIZATION_FIELDS = frozenset(
    {
        "project.name",
        "project.license",
        "classification.arxiv",
        "classification.msc2020",
        "review.status",
    }
)

ARXIV_CATEGORY_NAMES = json.loads(
    (ROOT / "taxonomies" / "arxiv-categories.json").read_text(encoding="utf-8")
)
MSC2020_NAMES = json.loads(
    (ROOT / "taxonomies" / "msc2020-codes.json").read_text(encoding="utf-8")
)
ARXIV_CATEGORIES = frozenset(ARXIV_CATEGORY_NAMES)
MSC2020_CODES = frozenset(MSC2020_NAMES)
OPTIONAL_FIELDS = frozenset(
    {
        "existing_id",
        "authorization_relationship",
        "authorization_evidence",
        "context",
        "project_path",
        "comparator_config_path",
        "formalization_metadata_path",
    }
)
SUBMISSION_ID_RE = re.compile(r"^[0-9a-z]{12}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GITHUB_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
PALOMAR_ID_RE = re.compile(r"^PALOMAR-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}$")
GITHUB_LOGIN_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$"
)
ORCID_RE = re.compile(r"^[0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9X]{4}$")
AUTHORIZATION_RELATIONSHIPS = {
    "I am a responsible author or maintainer": "maintainer",
    "I have approval from a responsible author or maintainer": "approved",
    "I am a Palomar Technical Maintainer testing the workflow": "technical-test",
}
SOURCE_TYPES = {
    "paper",
    "book",
    "web discussion",
    "folklore",
    "original-proof",
    "other",
}
ORIGINAL_PROOF_TYPE = "original-proof"
REPOSITORY_ROLES = {"substantive-development", "thin-wrapper"}
SOURCE_RELATIONSHIPS = {
    "formalizes",
    "adapts",
    "independently-proves",
    "background",
    "other",
}
SOURCE_ENDORSEMENTS = {
    "participated",
    "endorsed",
    "no-response",
    "not-contacted",
    "declined",
    "n/a",
}
RELATED_FORMALIZATION_RELATIONSHIPS = {
    "builds-on",
    "adapts",
    "independent",
    "supersedes",
    "other",
}


class UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects ambiguous duplicate mapping keys."""


def _construct_unique_mapping(
    loader: UniqueKeySafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            raise VerificationError("formalization.yaml must not use YAML merge keys")
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise VerificationError(
                "formalization.yaml contains an invalid mapping key"
            ) from error
        if duplicate:
            raise VerificationError(
                f"formalization.yaml contains a duplicate key: {key!r}"
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def submission_request(event: dict[str, Any]) -> tuple[dict[str, str], str]:
    """Read the submission out of the dispatch that started this run.

    Submissions arrive through the submission server, which keeps the
    submitter's identity private, so a submission is identified by an opaque
    id and nothing else. Inputs are validated as strictly as a form would be:
    an intake that trusts whoever can dispatch is the wrong place to relax.
    """
    inputs = event.get("inputs")
    if not isinstance(inputs, dict):
        raise VerificationError("workflow dispatch carried no submission inputs")

    submission_id = str(inputs.get("request_id", "")).strip()
    if not SUBMISSION_ID_RE.fullmatch(submission_id):
        raise VerificationError(
            "Submission id must be twelve lowercase alphanumeric characters"
        )

    repository = str(inputs.get("repository", "")).strip()
    if not REPOSITORY_RE.fullmatch(repository):
        raise VerificationError("Repository must be given as owner/name")

    values = {
        "repository_url": f"https://github.com/{repository}",
        "commit_sha": str(inputs.get("commit", "")).strip(),
    }
    # The optional fields arrive as one JSON object: workflow_dispatch allows
    # only ten inputs, and there are more optional fields than that leaves room
    # for. A field missing from the allowlist would be dropped rather than
    # refused, so unknown keys are an error.
    raw_options = str(inputs.get("options", "")).strip()
    if raw_options:
        try:
            options = json.loads(raw_options)
        except json.JSONDecodeError as error:
            raise VerificationError(
                f"Submission options are not valid JSON: {error}"
            ) from error
        if not isinstance(options, dict):
            raise VerificationError("Submission options must be a JSON object")
        unknown = sorted(set(options) - OPTIONAL_FIELDS)
        if unknown:
            raise VerificationError(
                f"Unrecognized submission options: {', '.join(unknown)}"
            )
        for key, value in options.items():
            if not isinstance(value, str):
                raise VerificationError(f"Submission option {key} must be a string")
            values[key] = value

    if not values.get("comparator_config_path", "").strip():
        raise VerificationError(
            "Comparator configuration path must be supplied explicitly for this submission",
            code="comparator.path_missing",
            next_action=(
                "Choose the repository-relative Comparator configuration for this submission, "
                "then submit the same commit again."
            ),
        )

    return values, submission_id


def normalize_repository(url: str) -> tuple[str, str]:
    """Return the canonical owner/name and credential-free GitHub URL."""
    match = GITHUB_RE.fullmatch(url.strip())
    if not match:
        raise VerificationError("Repository URL must be https://github.com/owner/repo")
    owner = match.group("owner")
    repo = match.group("repo")
    if owner in {".", ".."} or repo in {".", ".."}:
        raise VerificationError("invalid GitHub repository path")
    return f"{owner}/{repo}", f"https://github.com/{owner}/{repo}"


def _required_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VerificationError(f"formalization.yaml field {path} must be a mapping")
    return value


def _required_text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VerificationError(
            f"formalization.yaml field {path} must be a nonempty string"
        )
    return value.strip()


def _required_people(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise VerificationError(
            f"formalization.yaml field {path} must be a nonempty list"
        )
    for index, person in enumerate(value):
        item_path = f"{path}[{index}]"
        if isinstance(person, str):
            _required_text(person, item_path)
        elif isinstance(person, dict):
            _required_text(person.get("name"), f"{item_path}.name")
        else:
            raise VerificationError(
                f"formalization.yaml field {item_path} must be a name or a mapping with a name"
            )
    return value


def _person_records(value: Any, path: str, *, required: bool) -> list[dict[str, str]]:
    if value in (None, []) and not required:
        return []
    people = _required_people(value, path)
    records: list[dict[str, str]] = []
    for index, person in enumerate(people):
        item_path = f"{path}[{index}]"
        if isinstance(person, str):
            records.append({"name": person.strip()})
            continue
        record = {
            "name": _required_text(person.get("name"), f"{item_path}.name").strip()
        }
        github = person.get("github")
        if github is not None:
            login = _required_text(github, f"{item_path}.github").strip().removeprefix("@")
            if not GITHUB_LOGIN_RE.fullmatch(login):
                raise VerificationError(
                    f"formalization.yaml field {item_path}.github is invalid"
                )
            record["github"] = login
        orcid = person.get("orcid")
        if orcid is not None:
            identifier = _required_text(orcid, f"{item_path}.orcid").strip()
            if not ORCID_RE.fullmatch(identifier):
                raise VerificationError(
                    f"formalization.yaml field {item_path}.orcid is invalid"
                )
            record["orcid"] = identifier
        records.append(record)
    return records


def _optional_text(value: Any, path: str, *, maximum: int = 10_000) -> str | None:
    if value is None or value == "":
        return None
    text = _required_text(value, path).strip()
    if len(text) > maximum:
        raise VerificationError(
            f"formalization.yaml field {path} exceeds {maximum} characters"
        )
    return text


def reject_obsolete_provenance_fields(data: dict[str, Any]) -> None:
    """Name every known pre-launch provenance spelling in one migration error."""
    obsolete: list[str] = []
    project = data.get("project")
    if isinstance(project, dict) and "responsible_maintainer" in project:
        obsolete.append(
            "project.responsible_maintainer (use project.responsible_maintainers as a "
            "nonempty list)"
        )
    if "provenance" in data:
        obsolete.append(
            "top-level provenance (remove it; use project.responsible_maintainers, "
            "repository, and sources with required relationships)"
        )
    sources = data.get("sources")
    if isinstance(sources, list):
        for index, source in enumerate(sources):
            if isinstance(source, dict) and "author" in source:
                obsolete.append(
                    f"sources[{index}].author (use sources[{index}].authors as a list)"
                )
    if obsolete:
        raise VerificationError(
            "formalization.yaml uses obsolete provenance fields: " + "; ".join(obsolete)
        )


def normalized_provenance(data: dict[str, Any]) -> dict[str, Any]:
    """Validate and canonicalize the current Palomar provenance contract."""
    reject_obsolete_provenance_fields(data)
    project = _required_mapping(data.get("project"), "project")
    maintainers = _person_records(
        project.get("responsible_maintainers"),
        "project.responsible_maintainers",
        required=True,
    )

    raw_repository = data.get("repository")
    if raw_repository is None:
        repository: dict[str, Any] = {}
    else:
        repository = _required_mapping(raw_repository, "repository")
    raw_repository_role = repository.get("role")
    if raw_repository_role is None or raw_repository_role == "":
        repository_role = (
            "thin-wrapper"
            if "substantive_formalization" in repository
            else "substantive-development"
        )
    else:
        repository_role = _required_text(
            raw_repository_role, "repository.role"
        ).strip()
        if repository_role not in REPOSITORY_ROLES:
            allowed = ", ".join(sorted(REPOSITORY_ROLES))
            raise VerificationError(
                f"formalization.yaml field repository.role must be one of: {allowed}"
            )
    substantive: dict[str, str] | None = None
    if (
        repository_role == "substantive-development"
        and "substantive_formalization" in repository
    ):
        raise VerificationError(
            "formalization.yaml field repository.substantive_formalization is valid only "
            "for a thin wrapper; remove it when the submitted repository contains the "
            "substantive development"
        )
    if repository_role == "thin-wrapper":
        if not isinstance(repository.get("substantive_formalization"), dict):
            raise VerificationError(
                "formalization.yaml field repository.substantive_formalization is a required "
                "mapping when repository.role is thin-wrapper"
            )
        item = repository["substantive_formalization"]
        repository_id = _required_text(
            item.get("id"), "repository.substantive_formalization.id"
        )
        if not repository_id.startswith("https://"):
            repository_id = f"https://github.com/{repository_id}"
        repo, url = normalize_repository(repository_id)
        revision = _required_text(
            item.get("revision"), "repository.substantive_formalization.revision"
        ).strip().lower()
        if not SHA_RE.fullmatch(revision):
            raise VerificationError(
                "formalization.yaml field repository.substantive_formalization.revision "
                "must be a full lowercase commit"
            )
        substantive = {
            "repository": repo,
            "repository_url": url,
            "commit": revision,
            "tree_url": f"{url}/tree/{revision}",
        }

    raw_sources = data.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise VerificationError(
            "formalization.yaml field sources must be a nonempty list; use an entry with "
            "type: original-proof when the formalization first presents the result"
        )
    sources: list[dict[str, Any]] = []
    for index, source in enumerate(raw_sources):
        path = f"sources[{index}]"
        item = _required_mapping(source, path)
        raw_relationship = item.get("relationship")
        if not isinstance(raw_relationship, str) or not raw_relationship.strip():
            raise VerificationError(
                f"formalization.yaml field {path}.relationship must be a nonempty string; "
                "every source needs a relationship, including original-proof entries, which "
                "must use other"
            )
        relationship = raw_relationship.strip()
        if relationship not in SOURCE_RELATIONSHIPS:
            allowed = ", ".join(sorted(SOURCE_RELATIONSHIPS))
            raise VerificationError(
                f"formalization.yaml field {path}.relationship must be one of: {allowed}"
            )
        source_type = _optional_text(item.get("type"), f"{path}.type", maximum=200)
        if source_type is not None and source_type not in SOURCE_TYPES:
            allowed = ", ".join(sorted(SOURCE_TYPES))
            raise VerificationError(
                f"formalization.yaml field {path}.type must be one of: {allowed}"
            )
        record: dict[str, Any] = {
            "title": _required_text(item.get("title"), f"{path}.title").strip(),
            "authors": _person_records(
                item.get("authors"), f"{path}.authors", required=False
            ),
            "relationship": relationship,
        }
        for source_key, record_key, maximum in (
            ("id", "identifier", 2_048),
            ("location", "location", 1_000),
            ("license", "license", 500),
            ("author_endorsement", "author_endorsement", 100),
        ):
            raw_value = item.get(source_key)
            value = _optional_text(
                raw_value, f"{path}.{source_key}", maximum=maximum
            )
            if value is not None:
                record[record_key] = value
        if source_type is not None:
            record["type"] = source_type
        endorsement = record.get("author_endorsement")
        if endorsement is not None and endorsement not in SOURCE_ENDORSEMENTS:
            allowed = ", ".join(sorted(SOURCE_ENDORSEMENTS))
            raise VerificationError(
                f"formalization.yaml field {path}.author_endorsement must be one of: {allowed}"
            )
        sources.append(record)

    has_original_proof = any(
        source.get("type") == ORIGINAL_PROOF_TYPE for source in sources
    )
    if has_original_proof:
        result_origin = "original"
    else:
        result_origin = "source-based"
    substantive_relationships = {"formalizes", "adapts", "independently-proves"}
    if result_origin == "source-based" and not any(
        source["relationship"] in substantive_relationships for source in sources
    ):
        raise VerificationError(
            "formalization.yaml sources for a source-based result must include a "
            "formalizes, adapts, or independently-proves relationship"
        )
    if result_origin == "original" and any(
        source.get("type") == ORIGINAL_PROOF_TYPE and source["relationship"] != "other"
        for source in sources
    ):
        raise VerificationError(
            "formalization.yaml type: original-proof declares that this formalization first "
            "presents the result and must use relationship: other. If the source is a prior "
            "publication of the result, use its actual type (such as paper or book) and keep "
            "the substantive relationship instead"
        )
    if result_origin == "original" and any(
        source["relationship"] in substantive_relationships for source in sources
    ):
        raise VerificationError(
            "formalization.yaml declares an original-proof, so every source must use "
            "relationship background or other; formalizes, adapts, and independently-proves "
            "declare a source-based result. Remove type: original-proof when the named source "
            "is a prior presentation of the result"
        )

    raw_related = data.get("related_formalizations", [])
    if not isinstance(raw_related, list):
        raise VerificationError(
            "formalization.yaml field related_formalizations must be a list when present"
        )
    related: list[dict[str, str]] = []
    for index, related_item in enumerate(raw_related):
        path = f"related_formalizations[{index}]"
        item = _required_mapping(related_item, path)
        relationship = _required_text(
            item.get("relationship"), f"{path}.relationship"
        ).strip()
        if relationship not in RELATED_FORMALIZATION_RELATIONSHIPS:
            allowed = ", ".join(sorted(RELATED_FORMALIZATION_RELATIONSHIPS))
            raise VerificationError(
                f"formalization.yaml field {path}.relationship must be one of: {allowed}"
            )
        record = {
            "identifier": _required_text(item.get("id"), f"{path}.id").strip(),
            "relationship": relationship,
        }
        note = _optional_text(item.get("note"), f"{path}.note")
        if note is not None:
            record["note"] = note
        related.append(record)

    result: dict[str, Any] = {
        "result_origin": result_origin,
        "repository_role": repository_role,
        "responsible_maintainers": maintainers,
        "mathematical_sources": sources,
        "related_formalizations": related,
    }
    if substantive is not None:
        result["substantive_formalization"] = substantive
    return result


def _required_classifications(
    value: Any,
    path: str,
    *,
    allowed: frozenset[str],
    minimum: int,
    maximum: int,
) -> list[str]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        count = (
            f"{minimum} or {maximum}"
            if minimum + 1 == maximum
            else f"{minimum}–{maximum}"
        )
        raise VerificationError(
            f"formalization.yaml field {path} must contain {count} classification codes"
        )
    for index, code in enumerate(value):
        if not isinstance(code, str) or code not in allowed:
            raise VerificationError(
                f"formalization.yaml field {path}[{index}] is not a recognized classification code"
            )
    if len(value) != len(set(value)):
        raise VerificationError(
            f"formalization.yaml field {path} must not contain duplicates"
        )
    return value


def load_formalization_metadata(path: Path) -> dict[str, Any]:
    """Parse and enforce Palomar's mechanical minimum for formalization.yaml."""
    if path.stat().st_size > MAX_FORMALIZATION_BYTES:
        raise VerificationError(
            "formalization.yaml exceeds the 256 KiB hard cap",
            code="formalization.too_large",
            path=path.name,
            next_action="Reduce formalization.yaml below 256 KiB, commit it, and submit again.",
        )
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeySafeLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        detail = str(error).splitlines()[0] if str(error) else type(error).__name__
        mark = getattr(error, "problem_mark", None)
        raise VerificationError(
            f"formalization.yaml is not valid YAML: {detail}",
            code="formalization.invalid_yaml",
            path=path.name,
            line=getattr(mark, "line", -1) + 1 if mark is not None else None,
            column=getattr(mark, "column", -1) + 1 if mark is not None else None,
            next_action=(
                "Correct the YAML syntax at the reported location in your repository, "
                "commit the change, and make a new submission."
            ),
        ) from error
    if not isinstance(data, dict):
        raise VerificationError(
            "formalization.yaml must contain one top-level mapping",
            code="formalization.wrong_root_type",
            path=path.name,
        )

    # Named together, and before the field-by-field checks, because a file in an
    # older or a project's own shape otherwise fails one field at a time: fix,
    # resubmit, wait, learn the next one.
    missing = [
        name
        for name in ("project", "classification", "automation", "review")
        if not isinstance(data.get(name), dict)
    ]
    if not isinstance(data.get("sources"), list) or not data["sources"]:
        missing.append("sources (nonempty list)")
    if missing:
        raise VerificationError(
            "formalization.yaml is missing the sections Palomar requires: "
            + ", ".join(missing)
            + ". Palomar uses the mathlib-initiative formalization.yaml v0.3 format as a "
            "base (https://github.com/mathlib-initiative/formalization.yaml) plus Palomar's "
            "current classification and provenance additions; a plain v0.3 file, an older "
            "file, or a project-specific shape needs those sections adding. The repository "
            "section is optional unless this is a thin wrapper around a separately pinned "
            "substantive formalization.",
            code="formalization.missing_sections",
            path=path.name,
        )

    issues: list[VerificationError] = []

    def check(action: Any) -> None:
        try:
            action()
        except VerificationError as error:
            message = str(error)
            match = re.search(r"formalization\.yaml field ([^ ]+)", message)
            field = match.group(1).rstrip(";:,.") if match else error.field
            issues.append(
                VerificationError(
                    message,
                    code=error.code if error.code != "submission.invalid" else "formalization.invalid_field",
                    owner=error.owner,
                    next_action=error.next_action,
                    retryable=error.retryable,
                    path=path.name,
                    line=error.line,
                    column=error.column,
                    field=field,
                    repairable=bool(field in REPAIRABLE_FORMALIZATION_FIELDS),
                )
            )

    project = _required_mapping(data.get("project"), "project")
    check(lambda: _required_text(project.get("name"), "project.name"))
    check(lambda: _required_people(project.get("authors"), "project.authors"))
    check(lambda: _required_text(project.get("license"), "project.license"))

    classification = _required_mapping(data.get("classification"), "classification")
    check(
        lambda: _required_classifications(
            classification.get("arxiv"),
            "classification.arxiv",
            allowed=ARXIV_CATEGORIES,
            minimum=1,
            maximum=2,
        )
    )
    check(
        lambda: _required_classifications(
            classification.get("msc2020"),
            "classification.msc2020",
            allowed=MSC2020_CODES,
            minimum=1,
            maximum=8,
        )
    )

    check(lambda: normalized_provenance(data))

    automation = _required_mapping(data.get("automation"), "automation")

    def check_automation() -> None:
        methods = automation.get("methods")
        if not isinstance(methods, list) or not methods:
            raise VerificationError(
                "formalization.yaml field automation.methods must be a nonempty list"
            )
        for index, method in enumerate(methods):
            item = _required_mapping(method, f"automation.methods[{index}]")
            _required_text(item.get("method"), f"automation.methods[{index}].method")

    check(check_automation)

    review = _required_mapping(data.get("review"), "review")
    check(lambda: _required_text(review.get("status"), "review.status"))
    if issues:
        raise FormalizationValidationError(issues)
    return data
