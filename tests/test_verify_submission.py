import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

import yaml

import scripts.submission_contract as submission_contract
import scripts.verify_submission as verifier
from scripts.submission_contract import (
    OPTIONAL_FIELDS,
    load_formalization_metadata,
    normalize_repository,
    submission_request,
)
from scripts.verification_errors import FormalizationValidationError, VerificationError
from scripts.verification_profile import effective_memory_bytes
from scripts.verify_submission import (
    EXECUTION_BUDGET_SECONDS,
    LicenseDetectorError,
    LicenseValidationError,
    ResourceExhausted,
    _ConfinementProbeResult,
    _deadline_timeout,
    _run_filesystem_confinement_probe,
    allowed_roots,
    audit_challenge_sources,
    canonical_repository,
    comparator_verdict,
    compile_canonical_challenge,
    detect_spdx_identifier,
    ensure_lake_manifest,
    execute,
    github_repository,
    lake_environment_value,
    lean_header,
    load_comparator_config,
    materialize_packages,
    normalized_repository_path,
    package_allowlist,
    parse_lean_header,
    project_tree_url,
    protected_comparator_config,
    protected_lean_path,
    reject_committed_build_artifacts,
    reject_untrusted_package_artifacts,
    remove_untrusted_lake_state,
    repository_license_file,
    require_protected_paths,
    resolve_module_source,
    run,
    sandboxed_run,
    trusted_package_url_map,
    validate_preservable_git_checkout,
    validate_writable_directories,
    verify_official_revision,
    verify_sandbox_confinement,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
# The shape of `primitiveTargets` in Lake/CLI/Check.lean at v4.35.0-rc2, with
# a commented-out entry and a trailing comment, which the parser must skip.
FAKE_CHECK_LEAN = """\
def runKernels (exportPath : System.FilePath) : M Unit := do
  pure ()

def primitiveTargets : M (Array Lean.Name) := do
  -- The challenge needs to have all the built-in constants of the kernel.
  -- List from `git grep new_persistent_expr_const src/kernel/`
  return #[
    -- ``Nat.zero,
    ``Nat.add,
    ``Nat.sub, -- ``Nat.mul
    ``String.mk,
    ``outParam
  ]

def builtinTargets : M (Array Lean.Name) := do
  let mut additional := #[]
  if (← getLegalAxioms).contains ``Quot.sound then
    additional := additional ++ #[``Quot, ``Quot.mk, ``Quot.lift, ``Quot.ind]
  return additional
"""


# Captured from `lean --deps-json` on a module-system and a plain source.
MODULE_HEADER_JSON = (
    '{"imports":[{"errors":[],"result":{"imports":[{"importAll":false,'
    '"isExported":true,"isMeta":false,"module":"Init"}],"isModule":true}}]}'
)
PLAIN_HEADER_JSON = (
    '{"imports":[{"errors":[],"result":{"imports":[{"importAll":false,'
    '"isExported":true,"isMeta":false,"module":"Init"}],"isModule":false}}]}'
)


class RegistryCorrectionContractTests(unittest.TestCase):
    def correction(self):
        metadata = {
            "title": "Correct title",
            "abstract": "A result.",
            "authors": [{"name": "Ada"}],
            "classification": {"arxiv": ["math.LO"], "msc2020": ["03B35"]},
            "provenance": {
                "responsible_maintainers": [{"name": "Ada"}],
                "mathematical_sources": [],
                "related_formalizations": [],
            },
        }
        identifier = "PALOMAR-2026-08-31-000001"
        return {
            "schema_version": 1,
            "kind": "palomar-maintainer",
            "based_on": {"id": identifier, "version": 2},
            "baseline": {
                "id": identifier,
                "version": 2,
                "path": f"entries/{identifier}-v2.json",
                "sha256": "a" * 64,
            },
            "explanation": "Correct a transcription error in the title.",
            "metadata": metadata,
            "changed_fields": ["title"],
            "edits": [{"field": "title", "value": metadata["title"]}],
        }

    def test_registry_correction_is_closed_and_bound_to_its_effective_edit(self):
        value = self.correction()
        self.assertEqual(
            submission_contract.registry_correction(json.dumps(value)), value
        )
        value["edits"][0]["value"] = "Something else"
        with self.assertRaisesRegex(VerificationError, "edits disagree"):
            submission_contract.registry_correction(json.dumps(value))

    def test_registry_correction_rejects_a_noncanonical_baseline_path(self):
        value = self.correction()
        value["baseline"]["path"] = "entries/another.json"
        with self.assertRaisesRegex(VerificationError, "baseline is malformed"):
            submission_contract.registry_correction(json.dumps(value))

    def test_registry_correction_requires_orcids_in_structured_person_fields(self):
        value = self.correction()
        value["metadata"]["authors"] = [{
            "name": "Ada Lovelace",
            "orcid": "0000-0002-1825-0097",
        }]
        self.assertEqual(
            submission_contract.registry_correction(json.dumps(value)), value
        )

        value["metadata"]["authors"] = [
            "Ada Lovelace (ORCID 0000-0002-1825-0097)"
        ]
        with self.assertRaisesRegex(VerificationError, "separate orcid field"):
            submission_contract.registry_correction(json.dumps(value))

    def test_correction_source_evidence_is_inherited_from_exact_baseline_bytes(self):
        correction = self.correction()
        repository = "example/project"
        commit = "b" * 40
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            (checkout / "lean").mkdir()
            challenge = checkout / "lean" / "Challenge.lean"
            solution = checkout / "lean" / "Solution.lean"
            challenge.write_text("theorem challenge : True := trivial\n")
            solution.write_text("theorem solution : True := trivial\n")
            baseline = {
                "id": correction["based_on"]["id"],
                "version": correction["based_on"]["version"],
                "source": {"repository": repository, "commit": commit},
                "formalization": {
                    "challenge_path": "lean/Challenge.lean",
                    "solution_path": "lean/Solution.lean",
                },
                "verification": {
                    "challenge_sha256": verifier.sha256(challenge),
                    "solution_sha256": verifier.sha256(solution),
                },
            }
            raw = json.dumps(baseline, separators=(",", ":")).encode()
            correction["baseline"]["sha256"] = hashlib.sha256(raw).hexdigest()
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = raw
            open_url = mock.Mock(return_value=response)
            evidence = verifier.correction_source_evidence(
                correction,
                checkout=checkout,
                repository=repository,
                commit=commit,
                open_url=open_url,
            )

        request = open_url.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://data.palomar-registry.org/"
            f"entries/{correction['based_on']['id']}-v2.json",
        )
        self.assertEqual(
            evidence,
            {
                "challenge": {
                    "path": "lean/Challenge.lean",
                    "sha256": baseline["verification"]["challenge_sha256"],
                },
                "solution": {
                    "path": "lean/Solution.lean",
                    "sha256": baseline["verification"]["solution_sha256"],
                },
            },
        )

    def test_correction_source_evidence_rejects_changed_registered_source(self):
        correction = self.correction()
        repository = "example/project"
        commit = "b" * 40
        baseline = {
            "id": correction["based_on"]["id"],
            "version": correction["based_on"]["version"],
            "source": {"repository": repository, "commit": commit},
            "formalization": {
                "challenge_path": "Challenge.lean",
                "solution_path": "Solution.lean",
            },
            "verification": {
                "challenge_sha256": "0" * 64,
                "solution_sha256": "0" * 64,
            },
        }
        raw = json.dumps(baseline, separators=(",", ":")).encode()
        correction["baseline"]["sha256"] = hashlib.sha256(raw).hexdigest()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = raw
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            (checkout / "Challenge.lean").write_text("changed\n")
            (checkout / "Solution.lean").write_text("changed\n")
            with self.assertRaisesRegex(VerificationError, "registered challenge digest"):
                verifier.correction_source_evidence(
                    correction,
                    checkout=checkout,
                    repository=repository,
                    commit=commit,
                    open_url=mock.Mock(return_value=response),
                )


class VerifySubmissionTests(unittest.TestCase):
    def test_tree_size_never_walks_git_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "source").mkdir()
            (root / "source" / "Main.lean").write_bytes(b"theorem")
            (root / ".git" / "objects" / "99").mkdir(parents=True)
            (root / ".git" / "objects" / "99" / "object").write_bytes(b"ignored")
            (root / "source" / ".git").mkdir()
            (root / "source" / ".git" / "object").write_bytes(b"ignored")
            (root / "linked").symlink_to(root / ".git", target_is_directory=True)

            scandir = os.scandir
            visited_git_metadata = []

            def reject_git_metadata(path):
                if ".git" in Path(path).parts:
                    visited_git_metadata.append(path)
                    raise FileNotFoundError(path)
                return scandir(path)

            with mock.patch("scripts.verify_submission.os.scandir", side_effect=reject_git_metadata):
                self.assertEqual(verifier.tree_size(root), len(b"theorem"))
            self.assertEqual(visited_git_metadata, [])

    def test_mathlib_cache_summary_distinguishes_complete_missing_and_unknown(self):
        self.assertTrue(verifier.mathlib_cache_availability("\rDownloaded: 42 file(s)"))
        self.assertFalse(verifier.mathlib_cache_availability(
            "Downloaded: 0 file(s)\nWarning: some files were not found in the cache."
        ))
        self.assertFalse(verifier.mathlib_cache_availability(
            "Downloaded: 17 file(s)\nWarning: some files were not found in the cache."
        ))
        self.assertTrue(verifier.mathlib_cache_availability("No files to download"))
        self.assertIsNone(verifier.mathlib_cache_availability("older client output"))

    def test_staged_lake_metadata_accepts_generic_nested_archive_pairs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staged = root / "staged" / ".lake"
            canonical = root / "canonical" / ".lake"
            for lake in (staged, canonical):
                (lake / "build").mkdir(parents=True)
                (lake / "config").mkdir()
            nested = staged / "release" / "web"
            nested.mkdir(parents=True)
            archive = nested / "arbitrary.bundle"
            trace = nested / "arbitrary.bundle.trace"
            archive.write_bytes(b"archive")
            trace.write_bytes(b"trace")

            metadata = verifier._staged_lake_metadata(staged, canonical, "generic")

            self.assertEqual(
                {source.relative_to(staged).as_posix() for source, _ in metadata},
                {
                    "release/web/arbitrary.bundle",
                    "release/web/arbitrary.bundle.trace",
                },
            )

    def test_staged_lake_metadata_rejects_control_state_and_resource_excess(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staged = root / "staged" / ".lake"
            canonical = root / "canonical" / ".lake"
            for lake in (staged, canonical):
                (lake / "build").mkdir(parents=True)
                (lake / "config").mkdir()
            (staged / "lakefile.olean").write_bytes(b"compiled config")
            (staged / "lakefile.olean.trace").write_bytes(b"trace")
            with self.assertRaisesRegex(VerificationError, "control-plane state"):
                verifier._staged_lake_metadata(staged, canonical, "generic")

            (staged / "lakefile.olean").unlink()
            (staged / "lakefile.olean.trace").unlink()
            (staged / "release.tar.gz").write_bytes(b"archive")
            (staged / "release.tar.gz.trace").write_bytes(b"trace")
            with (
                mock.patch.object(verifier, "MAX_STAGED_LAKE_METADATA_FILES", 1),
                self.assertRaisesRegex(VerificationError, "exceeds its limit"),
            ):
                verifier._staged_lake_metadata(staged, canonical, "generic")

    def test_staged_lake_metadata_rejects_unpaired_or_linked_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staged = root / "staged" / ".lake"
            canonical = root / "canonical" / ".lake"
            for lake in (staged, canonical):
                (lake / "build").mkdir(parents=True)
                (lake / "config").mkdir()
            archive = staged / "release.tar.gz"
            archive.write_bytes(b"archive")
            with self.assertRaisesRegex(VerificationError, "paired archive state"):
                verifier._staged_lake_metadata(staged, canonical, "generic")

            trace = staged / "release.tar.gz.trace"
            trace.write_bytes(b"trace")
            outside = root / "outside"
            outside.write_bytes(b"outside")
            archive.unlink()
            os.link(outside, archive)
            with self.assertRaisesRegex(VerificationError, "not a regular file"):
                verifier._staged_lake_metadata(staged, canonical, "generic")

    def test_staged_build_validation_rejects_escape_and_external_hardlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build = root / "build"
            build.mkdir()
            outside = root / "outside"
            outside.write_bytes(b"outside")
            (build / "escape").symlink_to(Path("../outside"))
            with self.assertRaisesRegex(VerificationError, "escaping symlink"):
                verifier._validate_staged_build_tree(build, "generic")

            (build / "escape").unlink()
            os.link(outside, build / "linked")
            with self.assertRaisesRegex(VerificationError, "external hard link"):
                verifier._validate_staged_build_tree(build, "generic")

            (build / "linked").unlink()
            os.mkfifo(build / "fifo")
            with self.assertRaisesRegex(VerificationError, "special file"):
                verifier._validate_staged_build_tree(build, "generic")

    def test_staged_build_validation_allows_contained_relative_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory) / "build"
            target = build / "lib" / "artifact"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"artifact")
            links = build / "bin" / "nested"
            links.mkdir(parents=True)
            (links / "artifact").symlink_to(Path("../../lib/artifact"))

            verifier._validate_staged_build_tree(build, "generic")

    def test_manifest_packages_directory_does_not_widen_to_an_ancestor_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            ancestor = Path(directory)
            (ancestor / ".git").mkdir()
            checkout = ancestor / "workspace"
            source = checkout / "project"
            source.mkdir(parents=True)
            outside = ancestor / "outside" / ".lake" / "packages"
            outside.mkdir(parents=True)
            (source / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "version": "1.2.0",
                        "packagesDir": "../../outside/.lake/packages",
                        "packages": [],
                    }
                )
            )

            with self.assertRaisesRegex(VerificationError, "escapes the repository checkout"):
                verifier.manifest_packages_directory(source, checkout=checkout)

    def test_manifest_packages_directory_accepts_an_explicit_worktree_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / "worktree"
            source = checkout / "project"
            source.mkdir(parents=True)
            (checkout / ".git").write_text("gitdir: /tmp/example\n")
            (source / "lake-manifest.json").write_text(
                json.dumps({"version": "1.2.0", "packages": []})
            )

            self.assertEqual(
                verifier.manifest_packages_directory(source, checkout=checkout),
                (source / ".lake" / "packages").resolve(),
            )

    def test_explicit_writable_boundary_does_not_widen_to_an_ancestor_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            ancestor = Path(directory)
            (ancestor / ".git").mkdir()
            checkout = ancestor / "workspace"
            source = checkout / "project"
            source.mkdir(parents=True)
            (source / "lake-manifest.json").write_text(
                json.dumps({"version": "1.2.0", "packages": []})
            )
            outside = ancestor / "outside"
            outside.mkdir()

            with self.assertRaisesRegex(VerificationError, "escapes the source tree"):
                validate_writable_directories(checkout, [outside])

            with mock.patch(
                "scripts.verify_submission.validate_writable_directories",
                wraps=validate_writable_directories,
            ) as validate:
                writable = materialize_packages(
                    source, checkout=checkout, base_env={"PATH": "/usr/bin"}
                )

            self.assertEqual(validate.call_args.args[0], checkout.resolve())
            self.assertTrue(writable)
            self.assertTrue(all(path.is_relative_to(checkout) for path in writable))

    def test_recorded_path_dependencies_use_checkout_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory) / "checkout"
            source = checkout / "project"
            package = checkout / "vendor" / "helper"
            source.mkdir(parents=True)
            package.mkdir(parents=True)
            (source / "linked-vendor").symlink_to(
                checkout / "vendor", target_is_directory=True
            )
            packages = [
                {
                    "name": "helper",
                    "repository": "path:helper",
                    "url": "path:linked-vendor/helper",
                    "revision": "source-tree",
                }
            ]

            with self.assertRaisesRegex(VerificationError, "symlinked component"):
                verifier.recorded_project_dependencies(source, checkout, packages)

    def test_trusted_lake_directories_reject_symlink_redirection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            package = source / ".lake" / "packages" / "trusted"
            package.mkdir(parents=True)
            (source / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {
                                "name": "trusted",
                                "type": "git",
                                "url": "https://github.com/example/trusted",
                                "rev": "1" * 40,
                            }
                        ]
                    }
                )
            )
            redirected = root / "redirected-lake"
            (redirected / "build").mkdir(parents=True)
            (redirected / "config").mkdir()
            (package / ".lake").symlink_to(redirected, target_is_directory=True)

            with self.assertRaisesRegex(
                VerificationError,
                "trusted package 'trusted' Lake build directory contains a symlinked path component",
            ):
                verifier.package_lake_directories(
                    source, "trusted", checkout=source
                )

    def test_boundary_sensitive_production_calls_supply_an_explicit_checkout(self):
        import ast
        import inspect

        from scripts import render_challenge, verify_submission

        helper_names = {
            "audit_challenge_sources",
            "build_allowlisted_roots",
            "compile_canonical_challenge",
            "get_mathlib_cache",
            "manifest_packages_directory",
            "materialize_packages",
            "package_allowlist",
            "package_checkout",
            "package_lake_directories",
            "reset_trusted_lake_state",
            "source_package",
            "trusted_lake_directories",
            "validate_writable_directories",
        }
        checked = 0
        modules = (
            (verify_submission.__name__, Path(inspect.getfile(verify_submission))),
            (render_challenge.__name__, Path(inspect.getfile(render_challenge))),
            (
                "scripts.smoke_trusted_challenge",
                REPOSITORY_ROOT / "scripts" / "smoke_trusted_challenge.py",
            ),
        )
        for module_name, module_path in modules:
            source_text = module_path.read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(source_text)):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Name):
                    helper_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    helper_name = node.func.attr
                else:
                    continue
                if helper_name not in helper_names:
                    continue
                if any(isinstance(argument, ast.Starred) for argument in node.args) or any(
                    keyword.arg is None for keyword in node.keywords
                ):
                    continue
                target = getattr(verify_submission, helper_name)
                names = [keyword.arg for keyword in node.keywords]
                try:
                    # Signature binding proves every required explicit argument is
                    # present; it cannot prove which runtime Path value was supplied.
                    inspect.signature(target).bind(
                        *[mock.ANY] * len(node.args),
                        **{name: mock.ANY for name in names},
                    )
                except TypeError as error:
                    self.fail(
                        f"{module_name} line {node.lineno} calls "
                        f"{target.__name__}() without required explicit arguments: {error}"
                    )
                checked += 1
        self.assertGreater(checked, 30, "no boundary-sensitive calls were checked")

    def test_non_github_git_dependency_is_not_preservable(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {
                                "name": "elsewhere",
                                "type": "git",
                                "url": "https://gitlab.com/example/elsewhere",
                                "rev": "1" * 40,
                            }
                        ]
                    }
                )
            )
            with self.assertRaisesRegex(VerificationError, "must be hosted on GitHub"):
                verifier.manifest_packages(source)

    def test_git_dependency_without_a_repository_is_not_preservable(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {
                                "name": "missing",
                                "type": "git",
                                "rev": "1" * 40,
                            }
                        ]
                    }
                )
            )
            with self.assertRaisesRegex(VerificationError, "must be hosted on GitHub"):
                verifier.manifest_packages(source)

    def test_git_submodules_are_not_preservable(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
            subprocess.run(
                ["git", "update-index", "--add", "--cacheinfo", "160000," + "1" * 40 + ",vendor"],
                cwd=checkout,
                check=True,
            )
            with self.assertRaisesRegex(VerificationError, "Git submodule 'vendor'"):
                validate_preservable_git_checkout(checkout, "submitted source")

            # Dependency submodules are never initialized or read by Palomar.
            # Their exact gitlinks remain part of the archived repository tree.
            validate_preservable_git_checkout(
                checkout,
                "Git package 'historical'",
                allow_inert_submodules=True,
            )

    def test_git_lfs_paths_are_not_preservable(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
            (checkout / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
            (checkout / "large.bin").write_text("version https://git-lfs.github.com/spec/v1\n")
            subprocess.run(["git", "add", "."], cwd=checkout, check=True)
            with self.assertRaisesRegex(VerificationError, "Git LFS"):
                validate_preservable_git_checkout(
                    checkout,
                    "Git package 'dependency'",
                    allow_inert_submodules=True,
                )

    def test_a_project_with_no_manifest_and_no_toml_lakefile_says_so(self):
        """The message named the wrong lakefile: a copy-paste that would have
        sent a submitter looking at a file they do not have."""
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            (project / "lakefile.lean").write_text("-- arbitrary Lean\n")
            with self.assertRaisesRegex(
                VerificationError, "must configure Lake with"
            ):
                verifier.ensure_lake_manifest(project, Path(directory))

    def test_repository_license_file_is_one_nonempty_regular_root_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            license_path = root / "licence.MD"
            license_path.write_text("standard terms\n")
            self.assertEqual(repository_license_file(root), license_path)

            (root / "COPYING").write_text("other terms\n")
            with self.assertRaisesRegex(LicenseValidationError, "exactly one"):
                repository_license_file(root)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "LICENSE").write_text("  \n")
            with self.assertRaisesRegex(LicenseValidationError, "must not be empty"):
                repository_license_file(root)

    def test_repository_license_file_rejects_missing_and_symlinked_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(LicenseValidationError, "no conventional"):
                repository_license_file(root)
            target = root / "terms"
            target.write_text("terms\n")
            (root / "LICENSE").symlink_to(target)
            with self.assertRaisesRegex(LicenseValidationError, "not a regular"):
                repository_license_file(root)

    def test_detect_spdx_identifier_requires_one_consistent_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "bundle"
            bundle.touch()
            license_path = root / "LICENSE"
            license_path.write_text("terms\n")
            result = {
                "licenses": [{"spdx_id": "Apache-2.0"}],
                "matched_files": [{"matched_license": "Apache-2.0"}],
            }
            completed = subprocess.CompletedProcess(
                [str(bundle)], 0, json.dumps(result), ""
            )
            with mock.patch("scripts.verify_submission.run", return_value=completed):
                self.assertEqual(
                    detect_spdx_identifier(license_path, bundle), "Apache-2.0"
                )

            result["licenses"] = []
            rejected = subprocess.CompletedProcess([str(bundle)], 0, json.dumps(result), "")
            with (
                mock.patch("scripts.verify_submission.run", return_value=rejected),
                self.assertRaisesRegex(LicenseValidationError, "unambiguous"),
            ):
                detect_spdx_identifier(license_path, bundle)

            for malformed in (
                {
                    "licenses": [{"spdx_id": "NOASSERTION"}],
                    "matched_files": [{"matched_license": "NOASSERTION"}],
                },
                {
                    "licenses": [{"spdx_id": "Apache-2.0"}],
                    "matched_files": [{"matched_license": "MIT"}],
                },
                {
                    "licenses": [{"spdx_id": "Apache-2.0"}],
                    "matched_files": [
                        {"matched_license": "Apache-2.0"},
                        {"matched_license": "Apache-2.0"},
                    ],
                },
            ):
                rejected = subprocess.CompletedProcess(
                    [str(bundle)], 0, json.dumps(malformed), ""
                )
                with (
                    mock.patch("scripts.verify_submission.run", return_value=rejected),
                    self.assertRaisesRegex(LicenseValidationError, "unambiguous"),
                ):
                    detect_spdx_identifier(license_path, bundle)

            failed = subprocess.CompletedProcess(
                [str(bundle)], 7, "", "bundler could not load licensee"
            )
            with (
                mock.patch("scripts.verify_submission.run", return_value=failed),
                self.assertRaisesRegex(
                    LicenseDetectorError, "exit 7: bundler could not load licensee"
                ),
            ):
                detect_spdx_identifier(license_path, bundle)

    @unittest.skipUnless(
        os.environ.get("PALOMAR_TEST_LICENSEE"),
        "set PALOMAR_TEST_LICENSEE to a Bundler executable for the detector integration test",
    )
    def test_real_licensee_detects_repository_mit_license(self):
        bundle = Path(os.environ["PALOMAR_TEST_LICENSEE"])
        self.assertEqual(detect_spdx_identifier(REPOSITORY_ROOT / "LICENSE", bundle), "MIT")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy2(REPOSITORY_ROOT / "LICENSE", root / "LICENSE")
            (root / "LICENSES").mkdir()
            shutil.copy2(REPOSITORY_ROOT / "LICENSE", root / "LICENSES" / "LICENSE")
            self.assertEqual(detect_spdx_identifier(root / "LICENSE", bundle), "MIT")

    def test_phase_timeout_is_capped_by_global_deadline(self):
        with (
            mock.patch("scripts.verify_submission._EXECUTION_DEADLINE", 100.0),
            mock.patch("scripts.verify_submission._MONOTONIC", return_value=90.0),
        ):
            self.assertEqual(_deadline_timeout(600, ["probe"]), 10)
        with (
            mock.patch("scripts.verify_submission._EXECUTION_DEADLINE", 100.0),
            mock.patch("scripts.verify_submission._MONOTONIC", return_value=101.0),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            _deadline_timeout(600, ["probe"])

    def test_execute_requires_a_real_verifier_owned_checkout(self):
        cases = ("checkout-symlink", "git-file")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                work = root / "work"
                work.mkdir()
                if case == "checkout-symlink":
                    real_source = root / "real-source"
                    (real_source / ".git").mkdir(parents=True)
                    (work / "source").symlink_to(real_source, target_is_directory=True)
                    expected = "source checkout is not a real directory"
                else:
                    source = work / "source"
                    source.mkdir()
                    (source / ".git").write_text("gitdir: /tmp/not-owned\n")
                    expected = "no real Git metadata directory"
                report_path = root / "report.json"
                report_path.write_text(
                    json.dumps({"status": "pending", "errors": [], "source": {}})
                )
                args = Namespace(
                    output=report_path,
                    work_dir=work,
                    bwrap=sys.executable,
                    bwrap_source_tag="v0.12.0",
                    workflow_url="https://github.com/example/project/actions/runs/1",
                )

                self.assertEqual(execute(args), 0)

                report = json.loads(report_path.read_text())
                self.assertEqual(report["status"], "error")
                self.assertEqual(report["phase"], "verification")
                self.assertIn(expected, report["errors"][0])

    def test_expired_job_deadline_is_reported_and_restored(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "work" / "source").mkdir(parents=True)
            (root / "work" / "source" / ".git").mkdir()
            report_path = root / "report.json"
            report_path.write_text(json.dumps({"status": "pending", "errors": []}))
            args = Namespace(
                output=report_path,
                work_dir=root / "work",
                bwrap=sys.executable,
                bwrap_source_tag="v0.12.0",
                workflow_url="https://github.com/example/project/actions/runs/1",
            )
            with (
                mock.patch.dict(os.environ, {"PALOMAR_JOB_STARTED_AT": "1"}),
                mock.patch.object(verifier, "_EXECUTION_DEADLINE", 123.0),
                mock.patch("scripts.verify_submission.shutil.which", return_value=sys.executable),
            ):
                self.assertEqual(execute(args), 0)
                self.assertEqual(verifier._EXECUTION_DEADLINE, 123.0)

            report = json.loads(report_path.read_text())
            self.assertEqual(report["status"], "error")
            self.assertEqual(report["stage"], "resource-exhausted")
            self.assertEqual(report["phase"], "verification")
            self.assertEqual(report["error_kind"], "infrastructure/resource-exhausted")
            self.assertTrue(report["retryable"])
            self.assertIn("retry on a longer-running worker", report["errors"][0])

    def test_default_capacity_supports_ten_hour_verification(self):
        self.assertGreaterEqual(EXECUTION_BUDGET_SECONDS, 10 * 60 * 60)
        profile = json.loads((REPOSITORY_ROOT / "verification-profile.json").read_text())
        properties = verifier.permissive_resource_properties()
        self.assertIn(
            f"MemoryMax={effective_memory_bytes() * profile['limits']['memory_max_percent'] // 100}",
            properties,
        )
        self.assertFalse(
            any(
                property_value.startswith("CPUQuota=")
                for property_value in properties
            )
        )

    def test_payload_exit_137_does_not_establish_resource_exhaustion(self):
        completed = subprocess.CompletedProcess(["supervise"], 137, "", "killed")
        with (
            mock.patch("scripts.verify_submission.verify_tool_snapshot"),
            mock.patch("scripts.verify_submission._BWRAP", Path("/opt/bwrap")),
            mock.patch("scripts.verify_submission.supervisor_command", return_value=["supervise"]),
            mock.patch("scripts.verify_submission.run", return_value=completed),
            mock.patch(
                "scripts.verify_submission.supervisor_outcome",
                return_value={"Result": "exit-code", "memory_events": {}},
            ),
            mock.patch("scripts.verify_submission._RESOURCE_METRICS_PATH", None),
            self.assertRaisesRegex(VerificationError, r"failed \(137\)") as raised,
        ):
            sandboxed_run(
                ["lean", "Challenge.lean"],
                cwd=REPOSITORY_ROOT,
                environment={},
                writable_directories=[],
                executable_paths=[],
                tools={},
            )

        self.assertNotIsInstance(raised.exception, ResourceExhausted)

    def test_candidate_output_cannot_forge_resource_exhaustion(self):
        completed = subprocess.CompletedProcess(
            ["supervise"], 0, "out of memory; timed out; no space left on device", ""
        )
        with (
            mock.patch("scripts.verify_submission.verify_tool_snapshot"),
            mock.patch("scripts.verify_submission._BWRAP", Path("/opt/bwrap")),
            mock.patch("scripts.verify_submission.supervisor_command", return_value=["supervise"]),
            mock.patch("scripts.verify_submission.run", return_value=completed),
            mock.patch(
                "scripts.verify_submission.supervisor_outcome",
                return_value={"Result": "success", "memory_events": {}},
            ),
            mock.patch("scripts.verify_submission._RESOURCE_METRICS_PATH", None),
        ):
            result = sandboxed_run(
                ["lean", "Challenge.lean"],
                cwd=REPOSITORY_ROOT,
                environment={},
                writable_directories=[],
                executable_paths=[],
                tools={},
            )
        self.assertEqual(result.returncode, 0)

    def test_cgroup_oom_is_retryable_even_when_comparator_returns_one(self):
        completed = subprocess.CompletedProcess(["supervise"], 1, "", "")
        with (
            mock.patch("scripts.verify_submission.verify_tool_snapshot"),
            mock.patch("scripts.verify_submission._BWRAP", Path("/opt/bwrap")),
            mock.patch("scripts.verify_submission.supervisor_command", return_value=["supervise"]),
            mock.patch("scripts.verify_submission.run", return_value=completed),
            mock.patch(
                "scripts.verify_submission.supervisor_outcome",
                return_value={
                    "Result": "exit-code",
                    "memory_events": {"oom": 1, "oom_kill": 1},
                },
            ),
            mock.patch("scripts.verify_submission._RESOURCE_METRICS_PATH", None),
            self.assertRaisesRegex(ResourceExhausted, "resource ceiling"),
        ):
            sandboxed_run(
                ["comparator", "comparator.json"],
                cwd=REPOSITORY_ROOT,
                environment={},
                writable_directories=[],
                executable_paths=[],
                tools={},
            )

    def test_missing_cgroup_outcome_is_a_retryable_provider_error(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaises(VerificationError) as raised,
        ):
            verifier.supervisor_outcome(Path(directory) / "missing-status.json")
        self.assertEqual(raised.exception.owner, "provider")
        self.assertEqual(raised.exception.code, "provider.resource_telemetry_missing")
        self.assertTrue(raised.exception.retryable)

    def test_resource_wrapper_records_bounded_usage(self):
        with tempfile.TemporaryDirectory() as temporary:
            metrics = Path(temporary) / "metrics.jsonl"
            subprocess.run(
                [
                    sys.executable,
                    str(REPOSITORY_ROOT / "scripts" / "measure_resources.py"),
                    "--output",
                    str(metrics),
                    "--phase",
                    "fixture",
                    "--disk-path",
                    temporary,
                    "--",
                    sys.executable,
                    "-c",
                    "value = bytearray(1024 * 1024); print(len(value))",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            record = json.loads(metrics.read_text())
            self.assertEqual(record["phase"], "fixture")
            self.assertEqual(record["returncode"], 0)
            self.assertGreater(record["max_rss_kib"], 0)
            self.assertGreaterEqual(record["peak_tasks_observed"], 1)

    def test_command_output_capture_is_bounded(self):
        with mock.patch("scripts.verify_submission.MAX_CAPTURE_BYTES", 1024):
            proc = run(
                [
                    sys.executable,
                    "-c",
                    "import sys; print('a' * 4096); print('b' * 4096, file=sys.stderr)",
                ]
            )
        self.assertIn("<output truncated", proc.stdout)
        self.assertIn("<output truncated", proc.stderr)
        self.assertTrue(proc.stdout.endswith("\n"))
        self.assertLess(len(proc.stdout.encode()), 1200)
        self.assertLess(len(proc.stderr.encode()), 1200)





    def test_no_workflow_builds_a_retired_verifier_tool(self):
        workflows = REPOSITORY_ROOT / ".github" / "workflows"
        retired = ("landrun", "leanprover/comparator", "lean4export", "nanoda_lib", "setup-go")
        for path in [*workflows.glob("*.yml"), *workflows.glob("*.yaml")]:
            text = path.read_text()
            for name in retired:
                self.assertNotIn(name, text, f"{path.name} still names {name}")

    def test_every_verifier_workflow_passes_the_installed_bubblewrap_release(self):
        expected = verifier.bwrap_source_tag_from_installer()
        self.assertEqual(expected, "v0.12.0")
        workflows = REPOSITORY_ROOT / ".github" / "workflows"
        passing = []
        for path in [*workflows.glob("*.yml"), *workflows.glob("*.yaml")]:
            text = path.read_text()
            tags = re.findall(r"--bwrap-source-tag (\S+)", text)
            if tags:
                passing.append(path.name)
                self.assertEqual(set(tags), {expected}, path.name)
        self.assertEqual(
            sorted(passing), ["compatibility.yml", "render-challenge.yml", "submission.yml"]
        )

    def test_every_verifier_workflow_installs_hash_pinned_dependencies(self):
        workflows = REPOSITORY_ROOT / ".github" / "workflows"
        verifier_entrypoints = (
            "scripts.render_challenge",
            "scripts/smoke_trusted_challenge.py",
            "scripts/verify_submission",
        )
        installers = []
        for path in [*workflows.glob("*.yml"), *workflows.glob("*.yaml")]:
            text = path.read_text()
            if any(entrypoint in text for entrypoint in verifier_entrypoints):
                installers.append(path.name)
                self.assertIn(
                    "pip install --disable-pip-version-check --require-hashes --no-deps -r",
                    text,
                    path.name,
                )
        self.assertEqual(
            sorted(installers),
            ["compatibility.yml", "render-challenge.yml", "submission.yml"],
        )


    def test_repository_paths_and_nested_tree_urls_are_canonical(self):
        self.assertEqual(
            normalized_repository_path("examples/Sharp Smoothing", "project").as_posix(),
            "examples/Sharp Smoothing",
        )
        self.assertEqual(
            project_tree_url(
                "https://github.com/example/project",
                "1" * 40,
                "examples/Sharp Smoothing",
            ),
            f"https://github.com/example/project/tree/{'1' * 40}/examples/Sharp%20Smoothing",
        )
        for unsafe in (
            "../project",
            "./project",
            "project//nested",
            "/project",
            "a\\b",
            ".git/config",
            "nested/.git/config",
            ".lake/build",
            "nested/.LAKE/build",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(VerificationError):
                normalized_repository_path(unsafe, "project")


    def test_repository_normalization(self):
        self.assertEqual(
            normalize_repository("https://github.com/example/result.git"),
            ("example/result", "https://github.com/example/result"),
        )
        with self.assertRaises(VerificationError):
            normalize_repository("https://evil.example/example/result")

    def test_github_repository_variants(self):
        self.assertEqual(github_repository("https://github.com/a/b.git"), "a/b")
        self.assertEqual(github_repository("git@github.com:a/b.git"), "a/b")
        self.assertIsNone(github_repository("https://gitlab.com/a/b"))

    def test_allowlisted_repository_aliases(self):
        roots, aliases = allowed_roots()
        self.assertEqual(
            {root["repository"] for root in roots},
            {
                "leanprover-community/mathlib4",
                "TauCetiProject/TauCeti",
                "leanprover/cslib",
            },
        )
        self.assertEqual(
            {root["official_ref"] for root in roots},
            {"refs/heads/master", "refs/heads/main"},
        )
        self.assertEqual(
            canonical_repository("formalfrontier/tauceti", aliases),
            "TauCetiProject/TauCeti",
        )
        tauceti = next(root for root in roots if root["repository"] == "TauCetiProject/TauCeti")
        mathlib = next(
            root for root in roots if root["repository"] == "leanprover-community/mathlib4"
        )
        self.assertIs(mathlib["include_semver_release_tags"], True)
        self.assertEqual(
            tauceti["accepted_revisions"],
            ["221bb56a017bb794421eac4fa543d7a5e85add75"],
        )
        self.assertNotIn("include_semver_release_tags", tauceti)
        cslib = next(
            root for root in roots if root["repository"] == "leanprover/cslib"
        )
        self.assertEqual(cslib["repository_aliases"], [])
        self.assertEqual(cslib["official_ref"], "refs/heads/main")
        self.assertEqual(cslib["trust_level"], "qualified")
        self.assertNotIn("accepted_revisions", cslib)
        self.assertNotIn("include_semver_release_tags", cslib)
        self.assertEqual(
            canonical_repository("LeanProver/CSLib", aliases),
            "leanprover/cslib",
        )

    def test_header_reports_what_the_author_wrote(self):
        # Lean injects `Init` into every header without `prelude`; the report is
        # about the modules a Challenge names for itself.
        payload = json.dumps(
            {
                "imports": [
                    {
                        "errors": [],
                        "result": {
                            "isModule": True,
                            "imports": [
                                {"module": "Init"},
                                {"module": "Init"},
                                {"module": "TauCeti.Topology"},
                                {"module": "Mathlib"},
                                {"module": "Mathlib"},
                            ],
                        },
                    }
                ]
            }
        )
        header = parse_lean_header(payload)
        self.assertTrue(header.is_module)
        self.assertEqual(header.imports, ("Mathlib", "TauCeti.Topology"))

    def test_header_rejects_an_unreadable_report(self):
        for payload, message in (
            ("not json", "parsable header"),
            ('{"imports":[]}', "no single header"),
            ('{"imports":[{"errors":["bad header"]}]}', "rejected the header"),
            ('{"imports":[{"errors":[],"result":{}}]}', "whether the source is a module"),
            ('{"imports":[{"errors":[],"result":{"isModule":false}}]}', "the header imports"),
            (
                '{"imports":[{"errors":[],"result":{"isModule":false,"imports":[{}]}}]}',
                "unreadable header import",
            ),
        ):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(VerificationError, message):
                    parse_lean_header(payload)

    def test_comparator_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "comparator.json"
            config = {
                "challenge_module": "Challenge",
                "solution_module": "Solution",
                "theorem_names": ["headline"],
                "definition_names": [],
                "permitted_axioms": ["propext", "Quot.sound", "Classical.choice"],
                "enable_nanoda": False,
            }
            path.write_text(json.dumps(config, indent=2) + "\n")
            self.assertEqual(load_comparator_config(path)["theorem_names"], ["headline"])
            kernels = {"nanoda": ["/toolchain/bin/nanoda_bin"], "con-ron": ["/toolchain/bin/con-ron"]}

            protected = Path(directory) / "protected.json"
            with mock.patch(
                "scripts.verify_submission.secrets.token_hex",
                return_value="ab" * 12,
            ) as nonce:
                protected_comparator_config(path, protected, kernels=kernels)
            nonce.assert_called_once_with(12)
            protected_values = json.loads(protected.read_text())
            self.assertNotIn("enable_nanoda", protected_values)
            self.assertEqual(protected_values["external_kernels"], kernels)
            self.assertRegex(
                protected_values["challenge_module"],
                r"^PalomarCanonical[0-9a-f]{24}\.Challenge$",
            )
            self.assertNotEqual(
                protected_values["challenge_module"], config["challenge_module"]
            )
            self.assertNotIn("verification_profile", protected_values)
            self.assertFalse(json.loads(path.read_text())["enable_nanoda"])

            protected.unlink()
            valid_json = json.dumps(config)
            for key, value in (
                ("enable_nanoda", "false"),
                ("permitted_axioms", '["sorryAx"]'),
            ):
                with self.subTest(duplicate=key):
                    path.write_text(valid_json[:-1] + f', "{key}": {value}' + "}")
                    with self.assertRaisesRegex(
                        VerificationError, f"duplicate keys: {key}"
                    ):
                        protected_comparator_config(path, protected, kernels=kernels)
                    self.assertFalse(protected.exists())

            path.write_text("{")
            with self.assertRaisesRegex(VerificationError, "valid UTF-8 JSON"):
                load_comparator_config(path)
            path.write_bytes(b"\xff")
            with self.assertRaisesRegex(VerificationError, "valid UTF-8 JSON"):
                load_comparator_config(path)

            for value in (False, None, 0, 1, "true", [], {}):
                with self.subTest(enable_nanoda=value):
                    config["enable_nanoda"] = value
                    path.write_text(json.dumps(config))
                    self.assertEqual(load_comparator_config(path)["enable_nanoda"], value)
                    protected_comparator_config(path, protected, kernels=kernels)
                    self.assertNotIn("enable_nanoda", json.loads(protected.read_text()))

            config["enable_nanoda"] = True

            config["future_relaxation"] = True
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(VerificationError, "unknown keys"):
                load_comparator_config(path)

            config.pop("future_relaxation")
            config.pop("enable_nanoda")
            path.write_text(json.dumps(config))
            self.assertNotIn("enable_nanoda", load_comparator_config(path))
            protected_comparator_config(path, protected, kernels=kernels)
            self.assertNotIn("enable_nanoda", json.loads(protected.read_text()))

            config["challenge_module"] = "Audit.PeriodicGeneral.Challenge"
            config["solution_module"] = "Audit.PeriodicGeneral.Solution"
            path.write_text(json.dumps(config))
            loaded = load_comparator_config(path)
            self.assertEqual(loaded["challenge_module"], config["challenge_module"])
            self.assertEqual(loaded["solution_module"], config["solution_module"])

            config["solution_module"] = config["challenge_module"]
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(VerificationError, "must be distinct"):
                load_comparator_config(path)

    def test_toolchain_policy_has_only_the_consumed_minimum(self):
        settings = json.loads((REPOSITORY_ROOT / "toolchains.json").read_text())
        self.assertEqual(settings, {"schema_version": 2, "minimum": "v4.35.0-rc2"})

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid_shapes = (
                {"schema_version": 2, "minimum": "v4.28.0", "unknown": True},
                {"minimum": "v4.28.0"},
                {"schema_version": 2},
            )
            for settings in invalid_shapes:
                with self.subTest(settings=settings):
                    (root / "toolchains.json").write_text(json.dumps(settings))
                    with mock.patch.object(verifier, "ROOT", root):
                        with self.assertRaisesRegex(
                            VerificationError, "exactly schema_version and minimum"
                        ):
                            verifier.supported_toolchain("leanprover/lean4:v4.32.0")

            for schema_version in (True, 2.0, 3):
                with self.subTest(schema_version=schema_version):
                    (root / "toolchains.json").write_text(
                        json.dumps(
                            {"schema_version": schema_version, "minimum": "v4.28.0"}
                        )
                    )
                    with mock.patch.object(verifier, "ROOT", root):
                        with self.assertRaisesRegex(VerificationError, "schema version 2"):
                            verifier.supported_toolchain("leanprover/lean4:v4.32.0")

    def test_toolchain_commit_is_the_lean4_release_the_toolchain_names(self):
        with mock.patch(
            "scripts.verify_submission.resolve_release_commit",
            side_effect=lambda repo, tag: f"{repo}@{tag}",
        ) as resolve:
            self.assertEqual(
                verifier.toolchain_commit("leanprover/lean4:v4.35.0-rc2"),
                "leanprover/lean4@v4.35.0-rc2",
            )
        resolve.assert_called_once_with("leanprover/lean4", "v4.35.0-rc2")
        with self.assertRaises(VerificationError) as raised:
            verifier.toolchain_commit("leanprover/lean4:v4.34.0")
        self.assertEqual(raised.exception.code, "toolchain.unsupported")

    def test_module_resolution_uses_lake_source_roots_but_stays_in_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            source_root = project / "src"
            challenge = source_root / "Audit" / "Challenge.lean"
            challenge.parent.mkdir(parents=True)
            challenge.write_text("theorem example : True := by trivial\n")
            self.assertEqual(
                resolve_module_source(
                    "Audit.Challenge",
                    project=project,
                    lean_source_path=str(source_root),
                ),
                challenge.resolve(),
            )

            outside = root / "outside"
            (outside / "Audit").mkdir(parents=True)
            (outside / "Audit" / "Challenge.lean").write_text("theorem bad : True := by trivial\n")
            with self.assertRaisesRegex(VerificationError, "outside the selected project"):
                resolve_module_source(
                    "Audit.Challenge", project=project, lean_source_path=str(outside)
                )

    def test_trusted_manifest_fallback_reuses_contained_path_dependency_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            (checkout / "lakefile.lean").write_text("import Lake\n")
            (checkout / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "version": "1.2.0",
                        "packagesDir": ".lake/packages",
                        "packages": [
                            {
                                "name": "mathlib",
                                "type": "git",
                                "url": "https://github.com/leanprover-community/mathlib4",
                                "rev": "1" * 40,
                                "inherited": False,
                                "configFile": "lakefile.lean",
                                "manifestFile": "lake-manifest.json",
                            }
                        ],
                    }
                )
            )
            project = checkout / "scripts" / "comparator"
            project.mkdir(parents=True)
            (project / "lakefile.toml").write_text(
                'name = "Comparator"\n[[require]]\nname = "main"\npath = "../.."\n'
            )
            self.assertTrue(ensure_lake_manifest(project, checkout))
            manifest = json.loads((project / "lake-manifest.json").read_text())
            self.assertEqual(manifest["packagesDir"], "../../.lake/packages")
            self.assertEqual(manifest["packages"][0]["dir"], "../..")
            self.assertFalse(manifest["packages"][0]["inherited"])
            self.assertEqual(manifest["packages"][1]["name"], "mathlib")
            self.assertTrue(manifest["packages"][1]["inherited"])
            self.assertFalse(ensure_lake_manifest(project, checkout))

    def test_manifest_fallback_rejects_unlocked_git_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "lakefile.toml").write_text(
                'name = "Example"\n[[require]]\nname = "mathlib"\n'
                'git = "https://github.com/leanprover-community/mathlib4"\nrev = "main"\n'
            )
            with self.assertRaisesRegex(VerificationError, "committed manifests"):
                ensure_lake_manifest(project, project)

    def test_manifest_fallback_rejects_symlinked_path_components(self):
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            target = checkout / "target"
            target.mkdir()
            (target / "lakefile.toml").write_text('name = "Target"\n')
            (target / "lake-manifest.json").write_text(
                json.dumps({"version": "1.2.0", "packages": []})
            )
            project = checkout / "project"
            project.mkdir()
            (project / "linked").symlink_to(target, target_is_directory=True)
            (project / "lakefile.toml").write_text(
                'name = "Example"\n[[require]]\nname = "target"\npath = "linked"\n'
            )
            with self.assertRaisesRegex(VerificationError, "symlinked component"):
                ensure_lake_manifest(project, checkout)

    def test_formalization_metadata_mechanical_minimum(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "formalization.yaml"
            path.write_text(
                """\
version: v0.4
project:
  name: Example result
  description: A formalization of the example result.
  authors: [Ada Lovelace]
  license: Apache-2.0
  responsible_maintainers: [Ada Lovelace]
classification:
  arxiv: [math.LO, cs.LO]
  msc2020: [03B35, 68V15]
sources:
  - title: A source theorem
    authors:
      - name: Emmy Noether
    contributors:
      - name: Wilhelm Magnus
        role: problem-proposer
      - name: Evgenii Khukhro
        role: editor
    id: doi:10.1000/example
    relationship: formalizes
automation:
  methods:
    - method: manual
review:
  status: self-assessed
"""
            )
            metadata = load_formalization_metadata(path)
            self.assertEqual(metadata["project"]["name"], "Example result")
            self.assertEqual(metadata["classification"]["arxiv"], ["math.LO", "cs.LO"])
            provenance = submission_contract.normalized_provenance(metadata)
            self.assertEqual(
                provenance["mathematical_sources"][0]["contributors"],
                [
                    {"name": "Wilhelm Magnus", "role": "problem-proposer"},
                    {"name": "Evgenii Khukhro", "role": "editor"},
                ],
            )

    def test_project_name_fits_the_public_registry_title(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "formalization.yaml"
            path.write_text(
                """\
project:
  name: """ + "x" * 301 + """
  description: A formalization of the example result.
  authors: [Ada Lovelace]
  license: Apache-2.0
  responsible_maintainers: [Ada Lovelace]
classification:
  arxiv: [math.LO]
  msc2020: [03B35]
sources:
  - title: Original result
    type: original-proof
    relationship: other
automation:
  methods:
    - method: manual
review:
  status: self-assessed
""",
                encoding="utf-8",
            )
            with self.assertRaises(FormalizationValidationError) as caught:
                load_formalization_metadata(path)

        issue = next(
            issue
            for issue in caught.exception.issues
            if issue.field == "project.name"
        )
        self.assertIn("exceeds 300 characters", str(issue))
        self.assertTrue(issue.repairable)

    def test_project_author_orcid_forms_are_accepted_during_preflight(self):
        document = """\
version: v0.4
project:
  name: Example result
  description: A formalization of the example result.
  authors:
    - name: Ada Lovelace
      orcid: ORCID_VALUE
  license: Apache-2.0
  responsible_maintainers: [Ada Lovelace]
classification:
  arxiv: [math.LO]
  msc2020: []
sources:
  - title: A source theorem
    relationship: formalizes
automation:
  methods:
    - method: manual
review:
  status: self-assessed
"""
        for orcid in (
            "0000-0002-0201-310X",
            "https://orcid.org/0000-0002-0201-310X",
            "https://orcid.org/0000-0002-0201-310X/",
        ):
            with self.subTest(orcid=orcid), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "formalization.yaml"
                path.write_text(document.replace("ORCID_VALUE", orcid), encoding="utf-8")
                metadata = load_formalization_metadata(path)
                self.assertEqual(metadata["project"]["authors"][0]["orcid"], orcid)

    def test_project_author_invalid_orcid_is_rejected_during_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "formalization.yaml"
            path.write_text(
                """\
version: v0.4
project:
  name: Example result
  description: A formalization of the example result.
  authors:
    - name: Ada Lovelace
      orcid: https://example.com/0000-0002-0201-310X
  license: Apache-2.0
  responsible_maintainers: [Ada Lovelace]
classification:
  arxiv: [math.LO]
  msc2020: []
sources:
  - title: A source theorem
    relationship: formalizes
automation:
  methods:
    - method: manual
review:
  status: self-assessed
""",
                encoding="utf-8",
            )
            with self.assertRaises(FormalizationValidationError) as caught:
                load_formalization_metadata(path)

        issue = next(issue for issue in caught.exception.issues if issue.field)
        self.assertEqual(issue.field, "project.authors[0].orcid")
        self.assertIn("must be a valid bare ORCID iD", str(issue))
        self.assertFalse(issue.repairable)

    def test_project_author_orcid_checksum_is_checked_during_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "formalization.yaml"
            path.write_text(
                """\
project:
  name: Example result
  description: A formalization of the example result.
  authors:
    - name: Ada Lovelace
      orcid: 0000-0002-1825-0098
  license: Apache-2.0
  responsible_maintainers: [Ada Lovelace]
classification:
  arxiv: [math.LO]
  msc2020: []
sources:
  - title: Original result
    type: original-proof
    relationship: other
automation:
  methods:
    - method: manual
review:
  status: self-assessed
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                FormalizationValidationError, "must be a valid bare ORCID"
            ):
                load_formalization_metadata(path)

    def test_people_reject_orcid_ids_embedded_in_names(self):
        identifier = "0009-0009-9699-9712"
        cases = (
            (
                {
                    "project": {
                        "authors": [f"Idris Ali Shaik (ORCID {identifier})"],
                        "responsible_maintainers": ["Ada Lovelace"],
                    },
                    "sources": [{
                        "title": "Original result",
                        "type": "original-proof",
                        "relationship": "other",
                    }],
                },
                "project.authors[0]",
            ),
            (
                {
                    "project": {
                        "authors": ["Ada Lovelace"],
                        "responsible_maintainers": [{
                            "name": f"Idris Ali Shaik ({identifier})"
                        }],
                    },
                    "sources": [{
                        "title": "Original result",
                        "type": "original-proof",
                        "relationship": "other",
                    }],
                },
                "project.responsible_maintainers[0].name",
            ),
            (
                {
                    "project": {
                        "authors": ["Ada Lovelace"],
                        "responsible_maintainers": ["Ada Lovelace"],
                    },
                    "sources": [{
                        "title": "A source theorem",
                        "authors": [
                            f"Idris Ali Shaik https://orcid.org/{identifier}"
                        ],
                        "relationship": "formalizes",
                    }],
                },
                "sources[0].authors[0]",
            ),
        )
        for data, field in cases:
            with self.subTest(field=field):
                with self.assertRaises(VerificationError) as caught:
                    provenance = submission_contract.normalized_provenance(data)
                    submission_contract.declared_orcids(data, provenance)
                self.assertEqual(caught.exception.code, "formalization.orcid_in_name")
                self.assertEqual(caught.exception.field, field)
                self.assertIn("separate orcid field", str(caught.exception))

    def test_correction_mode_can_read_legacy_names_before_validating_the_overlay(self):
        identifier = "0009-0009-9699-9712"
        data = {
            "project": {
                "authors": [f"Idris Ali Shaik (ORCID {identifier})"],
                "responsible_maintainers": [f"Idris Ali Shaik ({identifier})"],
            },
            "sources": [{
                "title": "Original result",
                "type": "original-proof",
                "relationship": "other",
            }],
        }

        provenance = submission_contract.normalized_provenance(
            data, allow_legacy_orcid_names=True
        )
        self.assertEqual(
            submission_contract.declared_orcids(
                data, provenance, allow_legacy_orcid_names=True
            ),
            [],
        )
        with self.assertRaisesRegex(VerificationError, "separate orcid field"):
            submission_contract.normalized_provenance(data)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "formalization.yaml"
            path.write_text(
                f"""\
project:
  name: Example result
  description: A formalization of the example result.
  authors: [Idris Ali Shaik (ORCID {identifier})]
  license: Apache-2.0
  responsible_maintainers: [Idris Ali Shaik ({identifier})]
classification:
  arxiv: [math.LO]
  msc2020: [03B35]
sources:
  - title: Original result
    type: original-proof
    relationship: other
automation:
  methods:
    - method: manual
review:
  status: self-assessed
""",
                encoding="utf-8",
            )
            loaded = load_formalization_metadata(
                path, allow_legacy_orcid_names=True
            )
            self.assertEqual(loaded["project"]["authors"][0], data["project"]["authors"][0])
            with self.assertRaisesRegex(FormalizationValidationError, "separate orcid field"):
                load_formalization_metadata(path)

    def test_project_author_invalid_github_login_is_rejected_during_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "formalization.yaml"
            path.write_text(
                """\
version: v0.4
project:
  name: Example result
  description: A formalization of the example result.
  authors:
    - name: Ada Lovelace
      github: https://github.com/ada
  license: Apache-2.0
  responsible_maintainers: [Ada Lovelace]
classification:
  arxiv: [math.LO]
  msc2020: []
sources:
  - title: A source theorem
    relationship: formalizes
automation:
  methods:
    - method: manual
review:
  status: self-assessed
""",
                encoding="utf-8",
            )
            with self.assertRaises(FormalizationValidationError) as caught:
                load_formalization_metadata(path)

        issue = next(issue for issue in caught.exception.issues if issue.field)
        self.assertEqual(issue.field, "project.authors[0].github")
        self.assertFalse(issue.repairable)

    def test_legacy_person_aliases_pass_full_metadata_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "formalization.yaml"
            path.write_text(
                """\
project:
  name: Example result
  description: A formalization of the example result.
  authors: [Ada Lovelace]
  license: Apache-2.0
  responsible_maintainer: Ada Lovelace
classification:
  arxiv: [math.LO]
  msc2020: [03B35]
sources:
  - title: A source theorem
    author:
      name: Emmy Noether
      orcid: 0000-0002-1694-233X
    relationship: formalizes
automation:
  methods:
    - method: manual
review:
  status: self-assessed
""",
                encoding="utf-8",
            )
            metadata = load_formalization_metadata(path)

        provenance = submission_contract.normalized_provenance(metadata)
        self.assertEqual(
            provenance["responsible_maintainers"], [{"name": "Ada Lovelace"}]
        )
        self.assertEqual(
            provenance["mathematical_sources"][0]["authors"],
            [{"name": "Emmy Noether", "orcid": "0000-0002-1694-233X"}],
        )

    def test_current_palomar_template_shape_passes_real_metadata_and_provenance_parsing(self):
        # Exact snapshot of PalomarTemplate@128a6c5ce5f48622e69927ccd639cbff401022e8.
        # Pinning the bytes makes a cross-repository contract change deliberate rather
        # than silently turning this into a hand-written approximation of the template.
        fixture = REPOSITORY_ROOT / "tests/fixtures/palomar-template-formalization.yaml"
        raw = fixture.read_bytes()
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(),
            "04f3c577a1315833b7cf04ac8d7c8762689067a527a9cc3ec7f64f2ffbf12e24",
        )
        template = yaml.load(raw, Loader=submission_contract.UniqueKeySafeLoader)
        self.assertEqual(template["version"], "v0.4")
        self.assertNotIn("repository", template)
        self.assertEqual(
            template["sources"][0]["type"],
            "TEMPLATE: concise source kind, for example article, book, or formalization",
        )

        template["project"]["name"] = "Example result"
        template["project"]["authors"] = ["Ada Lovelace"]
        template["project"]["responsible_maintainers"] = ["Ada Lovelace"]
        template["classification"] = {"arxiv": ["math.LO"], "msc2020": ["03B35"]}
        template["sources"][0].update(
            {
                "title": "A source theorem",
                "type": "paper",
                "relationship": "formalizes",
                "author_endorsement": "n/a",
            }
        )
        template["related_formalizations"] = []

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "formalization.yaml"
            path.write_text(json.dumps(template), encoding="utf-8")
            metadata = load_formalization_metadata(path)
        provenance = submission_contract.normalized_provenance(metadata)
        self.assertEqual(provenance["result_origin"], "source-based")
        self.assertEqual(provenance["repository_role"], "substantive-development")
        self.assertEqual(provenance["mathematical_sources"][0]["type"], "paper")
        self.assertNotIn("declared", provenance)

    def test_prepared_report_uses_only_nested_artifact_digests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "fixture"
            fixture.mkdir()
            (fixture / "lakefile.toml").write_text('name = "Example"\n')
            (fixture / "lean-toolchain").write_text("leanprover/lean4:v4.35.0-rc2\n")
            (fixture / "LICENSE").write_text("Apache License Version 2.0\n")
            (fixture / "formalization.yaml").write_text(
                """\
project:
  name: Example result
  description: A formalization of the example result.
  authors: [Ada Lovelace]
  license: Apache-2.0
  responsible_maintainers: [Ada Lovelace]
repository:
  role: substantive-development
classification:
  arxiv: [math.LO]
  msc2020: [03B35]
sources:
  - title: A source theorem
    authors: [Emmy Noether]
    relationship: formalizes
automation:
  methods:
    - method: manual
review:
  status: self-assessed
"""
            )
            (fixture / "comparator.json").write_text(
                json.dumps(
                    {
                        "challenge_module": "Challenge",
                        "solution_module": "Solution",
                        "theorem_names": ["example"],
                        "permitted_axioms": [],
                        "enable_nanoda": True,
                    }
                )
            )
            event = root / "event.json"
            event.write_text(
                json.dumps(
                    {
                        "inputs": {
                            "repository": "owner/repo",
                            "commit": "b" * 40,
                            "request_id": "abc123def456",
                            "options": json.dumps(
                                {
                                    "authorization_relationship": (
                                        "I am a Palomar Technical Maintainer testing the workflow"
                                    ),
                                    "comparator_config_path": "comparator.json",
                                }
                            ),
                        }
                    }
                )
            )
            work = root / "work"
            work.mkdir()
            output = root / "report.json"
            args = Namespace(
                event=str(event),
                output=str(output),
                work_dir=str(work),
                licensee=str(root / "licensee"),
            )

            def clone_fixture(_url, _commit, destination):
                shutil.copytree(fixture, destination)

            with (
                mock.patch("scripts.verify_submission.clone_commit", side_effect=clone_fixture),
                mock.patch("scripts.verify_submission.validate_preservable_git_checkout"),
                mock.patch(
                    "scripts.verify_submission.detect_spdx_identifier",
                    return_value="Apache-2.0",
                ),
                mock.patch(
                    "scripts.verify_submission.resolve_release_commit",
                    return_value="c" * 40,
                ),
                mock.patch("scripts.verify_submission.workflow_output"),
            ):
                self.assertEqual(verifier.prepare(args), 0)

            report = json.loads(output.read_text())
            self.assertEqual(report["status"], "pending", report["errors"])
            self.assertEqual(report["phase"], "preparation")
            self.assertEqual(report["schema_version"], 1)
            self.assertEqual(report["warnings"], [])
            self.assertEqual(
                report["submission"]["authorization"],
                {"relationship": "technical-test"},
            )
            self.assertEqual(
                report["formalization"]["sha256"],
                verifier.sha256(fixture / "formalization.yaml"),
            )
            self.assertEqual(
                report["comparator"]["sha256"],
                verifier.sha256(fixture / "comparator.json"),
            )
            self.assertNotIn("formalization_sha256", report)
            self.assertNotIn("comparator_config_sha256", report)
            self.assertNotIn("declared", report["provenance"])
            self.assertIn(report["provenance"]["result_origin"], {"original", "source-based"})

    def test_prepared_report_records_no_msc2020_codes_when_the_key_is_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "fixture"
            fixture.mkdir()
            (fixture / "lakefile.toml").write_text('name = "Example"\n')
            (fixture / "lean-toolchain").write_text("leanprover/lean4:v4.35.0-rc2\n")
            (fixture / "LICENSE").write_text("Apache License Version 2.0\n")
            (fixture / "formalization.yaml").write_text(
                """\
project:
  name: Example result
  description: A formalization of the example result.
  authors: [Ada Lovelace]
  license: Apache-2.0
  responsible_maintainers: [Ada Lovelace]
repository:
  role: substantive-development
classification:
  arxiv: [math.LO]
sources:
  - title: A source theorem
    authors: [Emmy Noether]
    relationship: formalizes
automation:
  methods:
    - method: manual
review:
  status: self-assessed
"""
            )
            (fixture / "comparator.json").write_text(
                json.dumps(
                    {
                        "challenge_module": "Challenge",
                        "solution_module": "Solution",
                        "theorem_names": ["example"],
                        "permitted_axioms": [],
                        "enable_nanoda": True,
                    }
                )
            )
            event = root / "event.json"
            event.write_text(
                json.dumps(
                    {
                        "inputs": {
                            "repository": "owner/repo",
                            "commit": "b" * 40,
                            "request_id": "abc123def456",
                            "options": json.dumps(
                                {
                                    "authorization_relationship": (
                                        "I am a Palomar Technical Maintainer testing the workflow"
                                    ),
                                    "comparator_config_path": "comparator.json",
                                }
                            ),
                        }
                    }
                )
            )
            work = root / "work"
            work.mkdir()
            output = root / "report.json"
            args = Namespace(
                event=str(event),
                output=str(output),
                work_dir=str(work),
                licensee=str(root / "licensee"),
            )

            def clone_fixture(_url, _commit, destination):
                shutil.copytree(fixture, destination)

            with (
                mock.patch("scripts.verify_submission.clone_commit", side_effect=clone_fixture),
                mock.patch("scripts.verify_submission.validate_preservable_git_checkout"),
                mock.patch(
                    "scripts.verify_submission.detect_spdx_identifier",
                    return_value="Apache-2.0",
                ),
                mock.patch(
                    "scripts.verify_submission.resolve_release_commit",
                    return_value="c" * 40,
                ),
                mock.patch("scripts.verify_submission.workflow_output"),
            ):
                self.assertEqual(verifier.prepare(args), 0)

            report = json.loads(output.read_text())
            self.assertEqual(report["status"], "pending", report["errors"])
            self.assertEqual(report["classification"]["msc2020"], [])
            self.assertEqual(
                [entry["code"] for entry in report["classification"]["arxiv"]], ["math.LO"]
            )

    def test_formalization_metadata_accepts_many_arxiv_but_rejects_unknown_classifications(self):
        valid = """\
project:
  name: Example result
  description: A formalization of the example result.
  authors: [Ada Lovelace]
  license: Apache-2.0
  responsible_maintainers: [Ada Lovelace]
repository:
  role: substantive-development
classification:
  arxiv: [math.LO]
  msc2020: [03B35]
sources:
  - title: A source theorem
    authors: [Emmy Noether]
    id: doi:10.1000/example
    relationship: formalizes
automation:
  methods:
    - method: manual
review:
  status: self-assessed
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "formalization.yaml"
            path.write_text(valid.replace("math.LO", "math.NOTREAL"))
            with self.assertRaisesRegex(VerificationError, "not a recognized classification"):
                load_formalization_metadata(path)
            path.write_text(valid.replace("[math.LO]", "[{code: math.LO}]"))
            with self.assertRaisesRegex(VerificationError, "not a recognized classification"):
                load_formalization_metadata(path)
            path.write_text(
                valid.replace("[math.LO]", "[math.LO, cs.LO, math.CO]")
                .replace("[03B35]", "[]")
            )
            metadata = load_formalization_metadata(path)
            self.assertEqual(
                metadata["classification"]["arxiv"],
                ["math.LO", "cs.LO", "math.CO"],
            )
            self.assertEqual(
                submission_contract.formalization_repair_draft(metadata)["values"][
                    "classification.arxiv"
                ],
                ["math.LO", "cs.LO", "math.CO"],
            )
            path.write_text(
                valid.replace(
                    "[math.LO]",
                    "[math.LO, cs.LO, math.CO, math.PR, stat.CO, cs.DS, math.NT, "
                    "math.AG, math.AT]",
                )
            )
            with self.assertRaisesRegex(VerificationError, "1\u20138 classification codes"):
                load_formalization_metadata(path)
            path.write_text(valid.replace("03B35", "99Z99"))
            with self.assertRaisesRegex(VerificationError, "not a recognized classification"):
                load_formalization_metadata(path)

    def test_formalization_metadata_accepts_case_insensitive_classification_keys(self):
        valid = """\
project:
  name: Example result
  description: A formalization of the example result.
  authors: [Ada Lovelace]
  license: Apache-2.0
  responsible_maintainers: [Ada Lovelace]
classification:
  arXiv: [math.LO]
  MSC2020: [03B35]
sources:
  - title: A source theorem
    relationship: formalizes
automation:
  methods:
    - method: manual
review:
  status: self-assessed
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "formalization.yaml"
            path.write_text(valid)
            metadata = load_formalization_metadata(path)
            self.assertEqual(
                metadata["classification"],
                {"arxiv": ["math.LO"], "msc2020": ["03B35"]},
            )

            path.write_text(valid.replace("  arXiv:", "  arxiv: [math.CO]\n  arXiv:"))
            with self.assertRaisesRegex(VerificationError, "differing only by case"):
                load_formalization_metadata(path)

    def test_provenance_derives_an_original_result_from_its_source_entry(self):
        provenance = submission_contract.normalized_provenance(
            {
                "project": {"responsible_maintainers": ["Ada Lovelace"]},
                "repository": {"role": "substantive-development"},
                "sources": [
                    {
                        "title": "Original proof of the example theorem",
                        "type": "original-proof",
                        "relationship": "other",
                    }
                ],
            }
        )
        self.assertEqual(provenance["result_origin"], "original")
        self.assertNotIn("declared", provenance)

    def test_obsolete_top_level_provenance_is_ignored(self):
        current = {
            "project": {"responsible_maintainers": ["Ada Lovelace"]},
            "repository": {"role": "substantive-development"},
            "sources": [
                {
                    "title": "Background textbook",
                    "type": "original-proof",
                    "relationship": "other",
                }
            ],
        }
        for obsolete in (
            {"result_origin": "source-based"},
            {"notes": "free-form provenance does not belong here"},
            {},
            "original",
            None,
        ):
            with self.subTest(obsolete=obsolete):
                provenance = submission_contract.normalized_provenance(
                    {**current, "provenance": obsolete}
                )
                self.assertEqual(provenance["result_origin"], "original")

    def test_source_based_provenance_requires_a_substantive_relationship(self):
        data = {
            "project": {"responsible_maintainers": ["Ada Lovelace"]},
            "repository": {"role": "substantive-development"},
            "sources": [
                {
                    "title": "Background textbook",
                    "relationship": "background",
                }
            ],
        }
        with self.assertRaisesRegex(
            VerificationError, "must include a formalizes, adapts, or independently-proves"
        ):
            submission_contract.normalized_provenance(data)

    def test_singular_person_field_names_are_accepted(self):
        current = {
            "project": {"responsible_maintainers": ["Ada Lovelace"]},
            "repository": {"role": "substantive-development"},
            "sources": [
                {
                    "title": "A source theorem",
                    "authors": ["Emmy Noether"],
                    "relationship": "formalizes",
                }
            ],
        }
        obsolete_maintainer = json.loads(json.dumps(current))
        obsolete_maintainer["project"] = {
            "responsible_maintainer": "Ada Lovelace"
        }
        provenance = submission_contract.normalized_provenance(obsolete_maintainer)
        self.assertEqual(
            provenance["responsible_maintainers"], [{"name": "Ada Lovelace"}]
        )

        obsolete_author = json.loads(json.dumps(current))
        obsolete_author["sources"][0].pop("authors")
        obsolete_author["sources"][0]["author"] = "Emmy Noether"
        provenance = submission_contract.normalized_provenance(obsolete_author)
        self.assertEqual(
            provenance["mathematical_sources"][0]["authors"],
            [{"name": "Emmy Noether"}],
        )

    def test_singular_person_fields_accept_lists(self):
        provenance = submission_contract.normalized_provenance(
            {
                "project": {"responsible_maintainer": ["Ada", "Grace"]},
                "sources": [
                    {
                        "title": "A source theorem",
                        "author": ["Emmy", "Sofia"],
                        "relationship": "formalizes",
                    }
                ],
            }
        )
        self.assertEqual(
            provenance["responsible_maintainers"],
            [{"name": "Ada"}, {"name": "Grace"}],
        )
        self.assertEqual(
            provenance["mathematical_sources"][0]["authors"],
            [{"name": "Emmy"}, {"name": "Sofia"}],
        )

    def test_empty_optional_singular_author_is_ignored(self):
        provenance = submission_contract.normalized_provenance(
            {
                "project": {"responsible_maintainer": "Ada Lovelace"},
                "sources": [
                    {
                        "title": "A source theorem",
                        "author": None,
                        "relationship": "formalizes",
                    }
                ],
            }
        )
        self.assertEqual(provenance["mathematical_sources"][0]["authors"], [])

    def test_canonical_person_fields_take_precedence_over_singular_aliases(self):
        provenance = submission_contract.normalized_provenance(
            {
                "project": {
                    "responsible_maintainers": ["Ada Lovelace"],
                    "responsible_maintainer": 7,
                },
                "provenance": {"result_origin": "original"},
                "sources": [
                    {
                        "title": "A source theorem",
                        "authors": ["Emmy Noether"],
                        "author": 7,
                        "relationship": "formalizes",
                    }
                ],
            }
        )
        self.assertEqual(
            provenance["responsible_maintainers"], [{"name": "Ada Lovelace"}]
        )
        self.assertEqual(
            provenance["mathematical_sources"][0]["authors"],
            [{"name": "Emmy Noether"}],
        )
        self.assertEqual(provenance["result_origin"], "source-based")

        invalid_canonical = {
            "project": {
                "responsible_maintainers": [],
                "responsible_maintainer": "Ada Lovelace",
            },
            "sources": [
                {"title": "A source theorem", "relationship": "formalizes"}
            ],
        }
        with self.assertRaisesRegex(
            VerificationError, r"project\.responsible_maintainers must be a nonempty list"
        ):
            submission_contract.normalized_provenance(invalid_canonical)

    def test_source_contributors_require_named_roles(self):
        current = {
            "project": {"responsible_maintainers": ["Ada Lovelace"]},
            "sources": [
                {
                    "title": "A source theorem",
                    "relationship": "formalizes",
                    "contributors": [{"name": "Wilhelm Magnus", "role": "problem-proposer"}],
                }
            ],
        }
        provenance = submission_contract.normalized_provenance(current)
        self.assertEqual(
            provenance["mathematical_sources"][0]["contributors"],
            [{"name": "Wilhelm Magnus", "role": "problem-proposer"}],
        )

        for contributors in (
            ["Wilhelm Magnus"],
            [{"name": "Wilhelm Magnus"}],
            [{"name": "", "role": "editor"}],
            [{"name": "Wilhelm Magnus", "role": ""}],
        ):
            invalid = json.loads(json.dumps(current))
            invalid["sources"][0]["contributors"] = contributors
            with self.subTest(contributors=contributors):
                with self.assertRaisesRegex(
                    VerificationError, r"sources\[0\]\.contributors"
                ):
                    submission_contract.normalized_provenance(invalid)

    def test_current_provenance_declarations_are_required(self):
        current = {
            "project": {"responsible_maintainers": ["Ada Lovelace"]},
            "repository": {"role": "substantive-development"},
            "sources": [
                {"title": "A source theorem", "relationship": "formalizes"}
            ],
        }
        cases = (
            ("responsible_maintainers", {**current, "project": {}}, "nonempty list"),
            ("sources", {**current, "sources": []}, "nonempty list"),
        )
        for label, data, message in cases:
            with self.subTest(label):
                with self.assertRaisesRegex(VerificationError, message):
                    submission_contract.normalized_provenance(data)

    def test_omitted_repository_defaults_to_substantive_development(self):
        provenance = submission_contract.normalized_provenance(
            {
                "project": {"responsible_maintainers": ["Ada Lovelace"]},
                "sources": [
                    {"title": "A source theorem", "relationship": "formalizes"}
                ],
            }
        )
        self.assertEqual(provenance["repository_role"], "substantive-development")
        self.assertNotIn("substantive_formalization", provenance)

    def test_repository_role_remains_enumerated_while_descriptions_are_free_text(self):
        current = {
            "project": {"responsible_maintainers": ["Ada Lovelace"]},
            "repository": {"role": "substantive-development"},
            "sources": [
                {"title": "A source theorem", "relationship": "formalizes"}
            ],
        }
        invalid_role = json.loads(json.dumps(current))
        invalid_role["repository"]["role"] = "thin_wrapper"
        with self.assertRaisesRegex(
            VerificationError, "repository.role must be one of:"
        ):
            submission_contract.normalized_provenance(invalid_role)

        custom_relationship = json.loads(json.dumps(current))
        custom_relationship["sources"].append({
            "title": "An informal account",
            "relationship": "informal proof sketch",
            "note": "Suggested the key lemma without stating the final theorem.",
            "author_endorsement": "discussed in correspondence",
        })
        custom_relationship["related_formalizations"] = [{
            "id": "https://example.com/formalization",
            "relationship": "shares its computational infrastructure",
        }]
        provenance = submission_contract.normalized_provenance(custom_relationship)
        self.assertEqual(provenance["mathematical_sources"][1]["relationship"], "other")
        self.assertEqual(
            provenance["mathematical_sources"][1]["author_endorsement"],
            "discussed in correspondence",
        )
        self.assertEqual(
            provenance["mathematical_sources"][1]["note"],
            "Suggested the key lemma without stating the final theorem.",
        )
        self.assertEqual(
            provenance["related_formalizations"][0]["relationship"],
            "shares its computational infrastructure",
        )

        invalid_type = json.loads(json.dumps(current))
        invalid_type["sources"][0]["type"] = "x" * 201
        with self.assertRaisesRegex(
            VerificationError,
            r"sources\[0\]\.type exceeds 200 characters",
        ):
            submission_contract.normalized_provenance(invalid_type)

    def test_source_types_are_bounded_free_text_with_original_proof_reserved(self):
        for source_type in (
            "article", "paper", "book", "formalization", "web-discussion",
            "conversation", "original-proof",
        ):
            with self.subTest(source_type=source_type):
                source = {
                    "title": "A mathematical source",
                    "type": source_type,
                    "relationship": (
                        "other" if source_type == "original-proof" else "formalizes"
                    ),
                }
                provenance = submission_contract.normalized_provenance(
                    {
                        "project": {"responsible_maintainers": ["Ada Lovelace"]},
                        "repository": {"role": "substantive-development"},
                        "sources": [source],
                    }
                )
                self.assertEqual(provenance["mathematical_sources"][0]["type"], source_type)

    def test_original_proof_cannot_also_claim_a_source_based_origin(self):
        with self.assertRaisesRegex(
            VerificationError,
            "every source must use relationship background or other.*Remove type: original-proof",
        ):
            submission_contract.normalized_provenance(
                {
                    "project": {"responsible_maintainers": ["Ada Lovelace"]},
                    "repository": {"role": "substantive-development"},
                    "sources": [
                        {
                            "title": "Original proof",
                            "type": "original-proof",
                            "relationship": "other",
                        },
                        {
                            "title": "A source theorem",
                            "relationship": "formalizes",
                        },
                    ],
                }
            )

    def test_original_proof_source_itself_needs_relationship_other(self):
        with self.assertRaisesRegex(
            VerificationError,
            r"type: original-proof declares.*must use relationship: other.*prior publication",
        ):
            submission_contract.normalized_provenance(
                {
                    "project": {"responsible_maintainers": ["Ada Lovelace"]},
                    "repository": {"role": "substantive-development"},
                    "sources": [
                        {
                            "title": "Original proof",
                            "type": "original-proof",
                            "relationship": "formalizes",
                        }
                    ],
                }
            )

    def test_original_proof_entry_uses_other_while_accompanying_sources_may_be_background(self):
        provenance = submission_contract.normalized_provenance(
            {
                "project": {"responsible_maintainers": ["Ada Lovelace"]},
                "repository": {"role": "substantive-development"},
                "sources": [
                    {
                        "title": "Original proof",
                        "type": "original-proof",
                        "relationship": "other",
                    },
                    {
                        "title": "Background textbook",
                        "type": "book",
                        "relationship": "background",
                    },
                ],
            }
        )
        self.assertEqual(provenance["result_origin"], "original")

        invalid = {
            "project": {"responsible_maintainers": ["Ada Lovelace"]},
            "repository": {"role": "substantive-development"},
            "sources": [
                {
                    "title": "Original proof",
                    "type": "original-proof",
                    "relationship": "background",
                }
            ],
        }
        with self.assertRaisesRegex(
            VerificationError,
            r"type: original-proof declares.*must use relationship: other.*prior publication",
        ):
            submission_contract.normalized_provenance(invalid)

    def test_original_proof_source_still_requires_relationship(self):
        with self.assertRaisesRegex(
            VerificationError,
            r"sources\[0\]\.relationship.*every source needs a relationship.*original-proof",
        ):
            submission_contract.normalized_provenance(
                {
                    "project": {"responsible_maintainers": ["Ada Lovelace"]},
                    "repository": {"role": "substantive-development"},
                    "sources": [
                        {
                            "title": "Original proof",
                            "type": "original-proof",
                        }
                    ],
                }
            )

    def test_source_author_contact_state_is_carried_by_endorsement(self):
        provenance = submission_contract.normalized_provenance(
            {
                "project": {"responsible_maintainers": ["Ada Lovelace"]},
                "repository": {"role": "substantive-development"},
                "sources": [
                    {
                        "title": "Source",
                        "relationship": "formalizes",
                        "author_endorsement": "not-contacted",
                    }
                ],
            }
        )
        self.assertEqual(
            provenance["mathematical_sources"][0]["author_endorsement"],
            "not-contacted",
        )

    def test_thin_wrapper_records_the_substantive_repository_at_a_full_commit(self):
        revision = "a" * 40
        provenance = submission_contract.normalized_provenance(
            {
                "project": {"responsible_maintainers": ["Ada Lovelace"]},
                "repository": {
                    "role": "thin-wrapper",
                    "substantive_formalization": {
                        "id": "example/substantive",
                        "revision": revision,
                    },
                },
                "sources": [
                    {
                        "title": "Original proof",
                        "type": "original-proof",
                        "relationship": "other",
                    }
                ],
            }
        )
        self.assertEqual(
            provenance["substantive_formalization"]["tree_url"],
            f"https://github.com/example/substantive/tree/{revision}",
        )

    def test_substantive_target_without_role_implies_thin_wrapper(self):
        revision = "a" * 40
        provenance = submission_contract.normalized_provenance(
            {
                "project": {"responsible_maintainers": ["Ada Lovelace"]},
                "repository": {
                    "substantive_formalization": {
                        "id": "example/substantive",
                        "revision": revision,
                    }
                },
                "sources": [
                    {"title": "A source theorem", "relationship": "formalizes"}
                ],
            }
        )
        self.assertEqual(provenance["repository_role"], "thin-wrapper")
        self.assertEqual(
            provenance["substantive_formalization"]["commit"], revision
        )

    def test_thin_wrapper_requires_a_substantive_repository_target(self):
        with self.assertRaisesRegex(
            VerificationError,
            r"repository\.substantive_formalization is a required mapping.*thin-wrapper",
        ):
            submission_contract.normalized_provenance(
                {
                    "project": {"responsible_maintainers": ["Ada Lovelace"]},
                    "repository": {"role": "thin-wrapper"},
                    "sources": [
                        {"title": "A source theorem", "relationship": "formalizes"}
                    ],
                }
            )

    def test_substantive_repository_rejects_a_thin_wrapper_target(self):
        with self.assertRaisesRegex(
            VerificationError,
            r"repository\.substantive_formalization is valid only.*thin wrapper.*remove it",
        ):
            submission_contract.normalized_provenance(
                {
                    "project": {"responsible_maintainers": ["Ada Lovelace"]},
                    "repository": {
                        "role": "substantive-development",
                        "substantive_formalization": {
                            "id": "example/ignored",
                            "revision": "a" * 40,
                        },
                    },
                    "sources": [
                        {"title": "A source theorem", "relationship": "formalizes"}
                    ],
                }
            )

    def test_formalization_metadata_must_be_valid_yaml(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "formalization.yaml"
            path.write_text("project: [unterminated\n")
            with self.assertRaisesRegex(VerificationError, "not valid YAML"):
                load_formalization_metadata(path)

    def test_formalization_metadata_must_contain_required_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "formalization.yaml"
            path.write_text("{}\n")
            with self.assertRaises(FormalizationValidationError) as caught:
                load_formalization_metadata(path)
            self.assertEqual(
                {issue.field for issue in caught.exception.issues},
                {
                    "project.name", "project.description", "project.authors", "project.license",
                    "project.responsible_maintainers", "classification.arxiv",
                    "sources", "automation.methods", "review.status",
                },
            )

    def test_formalization_metadata_rejects_empty_required_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "formalization.yaml"
            path.write_text(
                """\
project:
  name: ""
  authors: []
  license: ""
  responsible_maintainers: [Ada Lovelace]
classification:
  arxiv: []
  msc2020: []
sources:
  - title: A source theorem
    relationship: formalizes
automation:
  methods: []
review:
  status: ""
"""
            )
            with self.assertRaisesRegex(VerificationError, "project.name"):
                load_formalization_metadata(path)

    def test_formalization_metadata_rejects_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "formalization.yaml"
            path.write_text("project:\n  name: first\n  name: second\n")
            with self.assertRaisesRegex(VerificationError, "duplicate key"):
                load_formalization_metadata(path)

    def test_formalization_metadata_rejects_yaml_merge_keys_before_expansion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "formalization.yaml"
            path.write_text(
                "base: &base {key: value}\n"
                "nested: &nested {<<: [*base, *base]}\n"
                "<<: [*nested, *nested]\n"
            )
            with self.assertRaisesRegex(VerificationError, "must not use YAML merge keys"):
                load_formalization_metadata(path)

    def test_protected_lean_path_precedes_candidate_shadow_modules(self):
        canonical = Path("/protected/Challenge.olean")
        value = protected_lean_path(
            canonical,
            [Path("/toolchain/lib/lean"), Path("/mathlib/lib/lean")],
            "/evil/lib/lean:/source/.lake/build/lib/lean",
        )
        self.assertEqual(
            value.split(":"),
            [
                "/protected",
                "/toolchain/lib/lean",
                "/mathlib/lib/lean",
                "/evil/lib/lean",
                "/source/.lake/build/lib/lean",
            ],
        )

    def test_hostile_canonical_build_cannot_publish_sibling_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            source = work / "source"
            source.mkdir()
            (source / "Challenge.lean").write_text("theorem result : True := by trivial\n")
            lean_prefix = work / "toolchain"
            (lean_prefix / "lib" / "lean").mkdir(parents=True)

            def fake_sandbox(command, **_kwargs):
                if "--deps-json" in command:
                    return mock.Mock(stdout=PLAIN_HEADER_JSON, stderr="", returncode=0)
                if "-o" in command:
                    output = Path(command[command.index("-o") + 1])
                    output.write_bytes(b"canonical")
                    (output.parent / "Mathlib.Forged.olean").write_bytes(b"hostile sibling")
                return mock.Mock(stdout="", stderr="", returncode=0)

            with mock.patch("scripts.verify_submission.sandboxed_run", side_effect=fake_sandbox):
                canonical, dependencies, trusted_paths = compile_canonical_challenge(
                    work,
                    source,
                    checkout=source,
                    lean=Path("/tools/lean"),
                    lean_prefix=lean_prefix,
                    allowlist={},
                    environment={},
                    readable_paths=[source],
                    executable_paths=[],
                    tools={},
                )
            self.assertEqual(canonical.read_bytes(), b"canonical")
            self.assertEqual([path.name for path in canonical.parent.iterdir()], ["Challenge.olean"])
            self.assertEqual(dependencies, [])
            self.assertEqual(trusted_paths, [(lean_prefix / "lib" / "lean").resolve()])

    def test_module_system_challenge_publishes_every_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            source = work / "source"
            source.mkdir()
            (source / "Challenge.lean").write_text(
                "module\n\npublic theorem result : True := by trivial\n"
            )
            lean_prefix = work / "toolchain"
            (lean_prefix / "lib" / "lean").mkdir(parents=True)

            def fake_sandbox(command, **_kwargs):
                if "--deps-json" in command:
                    return mock.Mock(stdout=MODULE_HEADER_JSON, stderr="", returncode=0)
                if "-o" in command:
                    output = Path(command[command.index("-o") + 1])
                    output.write_bytes(b"canonical")
                    output.with_name(f"{output.name}.private").write_bytes(b"private")
                    output.with_name(f"{output.name}.server").write_bytes(b"server")
                    output.with_name(f"{output.stem}.ir").write_bytes(b"ir")
                return mock.Mock(stdout="", stderr="", returncode=0)

            with mock.patch("scripts.verify_submission.sandboxed_run", side_effect=fake_sandbox):
                canonical, _dependencies, _trusted = compile_canonical_challenge(
                    work,
                    source,
                    checkout=source,
                    lean=Path("/tools/lean"),
                    lean_prefix=lean_prefix,
                    allowlist={},
                    environment={},
                    readable_paths=[source],
                    executable_paths=[],
                    tools={},
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

    def test_module_system_challenge_missing_a_sidecar_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            source = work / "source"
            source.mkdir()
            (source / "Challenge.lean").write_text(
                "module\n\npublic theorem result : True := by trivial\n"
            )
            lean_prefix = work / "toolchain"
            (lean_prefix / "lib" / "lean").mkdir(parents=True)

            def fake_sandbox(command, **_kwargs):
                if "--deps-json" in command:
                    return mock.Mock(stdout=MODULE_HEADER_JSON, stderr="", returncode=0)
                if "-o" in command:
                    output = Path(command[command.index("-o") + 1])
                    output.write_bytes(b"canonical")
                    output.with_name(f"{output.name}.private").write_bytes(b"private")
                    output.with_name(f"{output.stem}.ir").write_bytes(b"ir")
                return mock.Mock(stdout="", stderr="", returncode=0)

            with mock.patch("scripts.verify_submission.sandboxed_run", side_effect=fake_sandbox):
                with self.assertRaises(VerificationError) as caught:
                    compile_canonical_challenge(
                        work,
                        source,
                        checkout=source,
                        lean=Path("/tools/lean"),
                        lean_prefix=lean_prefix,
                        allowlist={},
                        environment={},
                        readable_paths=[source],
                        executable_paths=[],
                        tools={},
                    )
            self.assertIn("Challenge.olean.server", str(caught.exception))

    def test_sidecars_beside_a_plain_challenge_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            source = work / "source"
            source.mkdir()
            (source / "Challenge.lean").write_text("theorem result : True := by trivial\n")
            lean_prefix = work / "toolchain"
            (lean_prefix / "lib" / "lean").mkdir(parents=True)

            def fake_sandbox(command, **_kwargs):
                if "--deps-json" in command:
                    return mock.Mock(stdout=PLAIN_HEADER_JSON, stderr="", returncode=0)
                if "-o" in command:
                    output = Path(command[command.index("-o") + 1])
                    output.write_bytes(b"canonical")
                    # Hostile elaboration plants a sidecar the compiler did not write.
                    output.with_name(f"{output.name}.private").write_bytes(b"planted")
                return mock.Mock(stdout="", stderr="", returncode=0)

            with mock.patch("scripts.verify_submission.sandboxed_run", side_effect=fake_sandbox):
                with self.assertRaises(VerificationError) as caught:
                    compile_canonical_challenge(
                        work,
                        source,
                        checkout=source,
                        lean=Path("/tools/lean"),
                        lean_prefix=lean_prefix,
                        allowlist={},
                        environment={},
                        readable_paths=[source],
                        executable_paths=[],
                        tools={},
                    )
            self.assertIn("unexpected module artifacts", str(caught.exception))

    def test_module_header_is_read_from_lean(self):
        for payload, expected in ((MODULE_HEADER_JSON, True), (PLAIN_HEADER_JSON, False)):
            with self.subTest(expected=expected):
                with mock.patch(
                    "scripts.verify_submission.sandboxed_run",
                    return_value=mock.Mock(stdout=payload, stderr="", returncode=0),
                ) as sandbox:
                    self.assertIs(
                        lean_header(
                            Path("/source"),
                            lean_source=Path("/source/Challenge.lean"),
                            lean=Path("/tools/lean"),
                            environment={},
                            writable_directories=[],
                            readable_paths=[],
                            executable_paths=[],
                            tools={},
                        ).is_module,
                        expected,
                    )
                self.assertIn("--deps-json", sandbox.call_args.args[0])

    def test_comparator_rejection_quotes_the_lines_that_say_why(self):
        log = "\n".join(
            ["Running nanoda kernel on solution"]
            + [f"\u2714 [{n}/2012] Built Something (1.4s)" for n in range(2011)]
            + ["nanoda kernel accepts the solution",
               "error: Challenge and solution theorem statement do not match: 'main'"]
        )
        error = comparator_verdict(1, log)
        diagnostic = error.diagnostic("comparator")
        self.assertEqual(diagnostic["code"], "comparator.rejected")
        self.assertEqual(diagnostic["owner"], "submitter")
        self.assertIn("statement do not match", diagnostic["explanation"])
        self.assertNotIn("Built Something", diagnostic["explanation"])
        # The submitter is shown the explanation only where it says more.
        self.assertNotEqual(diagnostic["explanation"], diagnostic["summary"])

    def test_every_verdict_the_comparator_can_reach_is_a_rejection(self):
        for line in (
            "error: Const not found in challenge: 'Foo.bar'",
            "error: Const not found in solution: 'Foo.bar'",
            "error: Constant not found in solution 'Foo.bar'",
            "error: Const does not match between challenge and target 'Foo.bar'",
            "error: Challenge and solution constant kind don't match: 'Foo.bar'",
            "error: Challenge constant is not a definition: 'Foo.bar'",
            "error: Solution constant is not a definition: 'Foo.bar'",
            "error: Solution constant is not a theorem: 'Foo.bar'",
            "error: Illegal axiom detected: 'sorryAx'",
            "con-ron kernel accepts the solution\nLean default kernel rejected the solution\n"
            "error: Lean default exited with 1",
            "nanoda kernel rejected the solution\nLean default kernel rejected the solution\n"
            "error: nanoda exited with 101",
        ):
            with self.subTest(line=line):
                error = comparator_verdict(1, "Running con-ron kernel on solution\n" + line)
                self.assertEqual(error.code, "comparator.rejected")
                self.assertEqual(error.owner, "submitter")
                self.assertFalse(error.retryable)

    def test_an_independent_kernel_alone_does_not_reject(self):
        # `runExternalKernel` says "rejected" for every nonzero exit, a crash
        # included; Lean's own kernel is the arbiter.
        for log in (
            "con-ron kernel rejected the solution\nLean default kernel accepts the solution\n"
            "error: con-ron exited with 139",
            "nanoda kernel rejected the solution\ncon-ron kernel accepts the solution\n"
            "Lean default kernel accepts the solution\nerror: nanoda exited with 101",
        ):
            with self.subTest(log=log[:24]):
                error = comparator_verdict(1, log)
                self.assertEqual(error.code, "palomar.kernel_disagreement")
                self.assertEqual(error.owner, "palomar")
                self.assertTrue(error.retryable)

    def test_a_marker_quoted_inside_a_declaration_name_is_not_a_marker(self):
        # The transcript quotes names the submitter chose; only a line of the
        # comparator's own counts, and names cannot start a line.
        error = comparator_verdict(
            1, "error: Challenge and solution theorem statement do not match: '«bwrap: x»'"
        )
        self.assertEqual(error.code, "comparator.rejected")
        error = comparator_verdict(
            1, "error: Const not found in solution: '«Lean default kernel rejected the solution»'"
        )
        self.assertEqual(error.code, "comparator.rejected")
        error = comparator_verdict(1, "something: error: Illegal axiom detected: 'x'")
        self.assertEqual(error.code, "palomar.comparator_unclassified")

    def test_a_comparator_that_could_not_run_is_palomars(self):
        error = comparator_verdict(2, "error: `con-ron` kernel `/toolchain/bin/con-ron` was not found")
        self.assertEqual(error.code, "palomar.comparator_cannot_run")
        self.assertEqual(error.owner, "palomar")
        self.assertTrue(error.retryable)
        self.assertIn("was not found", error.detail)

    def test_a_kernel_whose_nested_sandbox_failed_is_not_a_rejection(self):
        for log in (
            "bwrap: setting up uid map: Permission denied\n"
            "nanoda kernel rejected the solution\nerror: nanoda exited with 1",
            "Error while interacting with con-ron kernel\n"
            "error: Error while interacting with con-ron kernel: no such file",
        ):
            with self.subTest(log=log[:30]):
                error = comparator_verdict(1, log)
                self.assertEqual(error.code, "palomar.comparator_sandbox_failed")
                self.assertEqual(error.owner, "palomar")
                self.assertTrue(error.retryable)

    def test_an_unexplained_comparator_exit_is_not_a_rejection(self):
        for returncode, log in ((1, "error: Expected JSON object"), (134, ""), (1, "")):
            with self.subTest(returncode=returncode, log=log):
                error = comparator_verdict(returncode, log)
                self.assertEqual(error.code, "palomar.comparator_unclassified")
                self.assertEqual(error.owner, "palomar")
                self.assertTrue(error.retryable)
        self.assertIsNone(comparator_verdict(0, "Your solution is okay!"))

    def test_a_diagnostic_without_detail_is_unchanged(self):
        diagnostic = VerificationError("plain failure").diagnostic("comparator")
        self.assertEqual(diagnostic["explanation"], diagnostic["summary"])

    def test_a_palomar_owned_stage_makes_the_same_commit_retryable(self):
        report = {}
        verifier.report_diagnostic(
            report,
            VerificationError("trusted cache failed", repairable=True),
            stage="trusted-cache",
        )

        [diagnostic] = report["diagnostics"]
        self.assertEqual(diagnostic["owner"], "palomar")
        self.assertTrue(diagnostic["retryable"])
        self.assertFalse(diagnostic["repairable"])
        self.assertIn("Retry the same commit", diagnostic["next_action"])

    def test_a_submitter_owned_stage_keeps_repository_changes_nonretryable(self):
        report = {}
        verifier.report_diagnostic(
            report,
            VerificationError("candidate source failed"),
            stage="candidate-setup",
        )

        [diagnostic] = report["diagnostics"]
        self.assertEqual(diagnostic["owner"], "submitter")
        self.assertFalse(diagnostic["retryable"])
        self.assertFalse(diagnostic["repairable"])
        self.assertIn("Update the repository", diagnostic["next_action"])

    def test_an_explicit_palomar_owner_rewrites_submitter_actionability(self):
        report = {}
        verifier.report_diagnostic(
            report,
            VerificationError("trusted tool changed", repairable=True),
            stage="setup",
            owner="palomar",
        )

        [diagnostic] = report["diagnostics"]
        self.assertEqual(diagnostic["owner"], "palomar")
        self.assertTrue(diagnostic["retryable"])
        self.assertFalse(diagnostic["repairable"])
        self.assertIn("Retry the same commit", diagnostic["next_action"])

    def test_candidate_setup_failure_requires_repository_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / "work"
            source = work / "source"
            (source / ".git").mkdir(parents=True)
            report_path = root / "report.json"
            report_path.write_text(
                json.dumps(
                    {
                        "status": "pending",
                        "errors": [],
                        "warnings": [],
                        "source": {},
                    }
                )
            )
            lean_prefix = root / "lean"
            (lean_prefix / "bin").mkdir(parents=True)
            for name in verifier.TOOLCHAIN_TOOLS:
                (lean_prefix / "bin" / name).touch()
                (lean_prefix / "bin" / name).chmod(0o755)
            check_source = lean_prefix / "src" / "lean" / "lake" / "Lake" / "CLI" / "Check.lean"
            check_source.parent.mkdir(parents=True)
            check_source.write_text(FAKE_CHECK_LEAN)
            printenv = root / "printenv"
            touch = root / "touch"
            printenv.touch()
            touch.touch()
            commands = {
                "lean": lean_prefix / "bin" / "lean",
                "lake": lean_prefix / "bin" / "lake",
                "printenv": printenv,
                "touch": touch,
            }
            args = Namespace(
                output=report_path,
                work_dir=work,
                bwrap=sys.executable,
                bwrap_source_tag="v0.12.0",
                workflow_url="https://github.com/example/project/actions/runs/1",
            )

            with (
                mock.patch(
                    "scripts.verify_submission.shutil.which",
                    side_effect=lambda name, **_kwargs: str(commands[name]),
                ),
                mock.patch(
                    "scripts.verify_submission.run",
                    return_value=subprocess.CompletedProcess(
                        ["lean", "--print-prefix"], 0, str(lean_prefix), ""
                    ),
                ),
                mock.patch("scripts.verify_submission.ensure_lake_manifest", return_value=False),
                mock.patch(
                    "scripts.verify_submission.materialize_packages",
                    side_effect=VerificationError("unsafe package name in Lake manifest"),
                ),
            ):
                self.assertEqual(execute(args), 0)

            report = json.loads(report_path.read_text())
            self.assertEqual(report["status"], "fail")
            self.assertEqual(report["stage"], "candidate-setup")
            [diagnostic] = report["diagnostics"]
            self.assertEqual(diagnostic["owner"], "submitter")
            self.assertFalse(diagnostic["retryable"])
            self.assertIn("Update the repository", diagnostic["next_action"])

    def test_submitted_lake_state_is_discarded(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            poison = package / ".lake" / "packages" / "poison" / "payload"
            poison.parent.mkdir(parents=True)
            poison.write_text("untrusted")
            build, config = remove_untrusted_lake_state(package)
            self.assertEqual({path.name for path in (package / ".lake").iterdir()}, {"build", "config"})
            self.assertEqual(build, (package / ".lake" / "build").resolve())
            self.assertEqual(config, (package / ".lake" / "config").resolve())

    def test_trusted_state_reset_validates_every_target_before_deleting_any(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            packages_dir = source / ".lake" / "packages"
            first = packages_dir / "first"
            second = packages_dir / "second"
            for package in (first, second):
                (package / ".lake" / "build").mkdir(parents=True)
                (package / ".lake" / "config").mkdir()
            protected = first / ".lake" / "build" / "must-survive-refusal"
            protected.write_text("protected")
            redirected = source.parent / "redirected"
            (redirected / "build").mkdir(parents=True)
            (redirected / "config").mkdir()
            shutil.rmtree(second / ".lake")
            (second / ".lake").symlink_to(redirected, target_is_directory=True)
            (source / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {
                                "name": name,
                                "type": "git",
                                "url": f"https://github.com/example/{name}",
                                "rev": "1" * 40,
                            }
                            for name in ("first", "second")
                        ]
                    }
                )
            )
            packages = verifier.manifest_packages(source)

            with self.assertRaisesRegex(VerificationError, "symlinked path component"):
                verifier.reset_trusted_lake_state(
                    source,
                    {"first", "second"},
                    packages=packages,
                    checkout=source,
                )

            self.assertEqual(protected.read_text(), "protected")

    def test_manifest_packages_reads_lake_escaped_package_names(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {
                                "name": "\u00abmy-package\u00bb",
                                "type": "git",
                                "url": "https://github.com/example/my-package",
                                "rev": "1" * 40,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            packages = verifier.manifest_packages(source)

        self.assertEqual([package["name"] for package in packages], ["my-package"])
        self.assertEqual([package["manifest_name"] for package in packages], ["\u00abmy-package\u00bb"])

    def test_escaped_unsafe_package_names_fail_before_materialization(self):
        for name in (
            "\u00ab..\u00bb", "\u00ab.git\u00bb", "\u00ab.lake\u00bb", "\u00aba/b\u00bb", "\u00abx\u00bby"
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                source = Path(directory)
                (source / "lake-manifest.json").write_text(
                    json.dumps(
                        {
                            "packages": [
                                {
                                    "name": name,
                                    "type": "git",
                                    "url": "https://github.com/example/package",
                                    "rev": "1" * 40,
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(VerificationError, "unsafe package name"):
                    materialize_packages(
                        source,
                        checkout=source,
                        base_env={"PATH": "/usr/bin"},
                    )

    def test_synthesized_manifest_spells_hyphenated_path_package_as_lake_does(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            (project / "lakefile.toml").write_text(
                'name = "root"\n\n[[require]]\nname = "my-package"\npath = "dep"\n'
            )
            dependency = project / "dep"
            dependency.mkdir()
            (dependency / "lakefile.toml").write_text('name = "my-package"\n')
            (dependency / "lake-manifest.json").write_text(
                json.dumps({"version": "1.1.0", "packagesDir": ".lake/packages", "packages": []})
            )

            self.assertTrue(ensure_lake_manifest(project, project))

            generated = json.loads((project / "lake-manifest.json").read_text(encoding="utf-8"))
            # Lake refuses a bare hyphenated name here: "expected a `Name`".
            self.assertEqual(
                [package["name"] for package in generated["packages"]],
                ["\u00abmy-package\u00bb"],
            )
            self.assertEqual(
                [package["name"] for package in verifier.manifest_packages(project)],
                ["my-package"],
            )

    def test_escaped_and_bare_spellings_of_one_name_fail_before_materialization(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {
                                "name": name,
                                "type": "git",
                                "url": f"https://github.com/example/{repository}",
                                "rev": "1" * 40,
                            }
                            for name, repository in (
                                ("mathlib", "official"),
                                ("\u00abmathlib\u00bb", "substitute"),
                            )
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch("scripts.verify_submission.run") as run,
                self.assertRaisesRegex(VerificationError, "duplicate package name"),
            ):
                materialize_packages(source, checkout=source, base_env={"PATH": "/usr/bin"})
            run.assert_not_called()

    def test_dot_package_names_fail_before_materialization(self):
        for name in (".", ".."):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                source = Path(directory)
                (source / "lake-manifest.json").write_text(
                    json.dumps(
                        {
                            "packages": [
                                {
                                    "name": name,
                                    "type": "git",
                                    "url": "https://github.com/example/package",
                                    "rev": "1" * 40,
                                }
                            ]
                        }
                    )
                )
                with self.assertRaisesRegex(VerificationError, "unsafe package name"):
                    materialize_packages(
                        source,
                        checkout=source,
                        base_env={"PATH": "/usr/bin"},
                    )
                with self.assertRaisesRegex(
                    VerificationError, "unsafe trusted package name"
                ):
                    verifier.reset_trusted_lake_state(
                        source,
                        {name},
                        packages=[{"name": name, "url": "https://github.com/example/package"}],
                        checkout=source,
                    )

    def test_mathlib_cache_starts_after_discarding_ignored_executable_state(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            mathlib = source / ".lake" / "packages" / "mathlib"
            mathlib.mkdir(parents=True)
            (mathlib / ".gitignore").write_text(".lake/\n")
            (mathlib / "lakefile.lean").write_text("package mathlib\n")
            dependency_revision = "1" * 40
            (mathlib / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "version": "1.2.0",
                        "packages": [
                            {
                                "name": "batteries",
                                "type": "git",
                                "url": "https://github.com/leanprover-community/batteries",
                                "rev": dependency_revision,
                            }
                        ],
                    }
                )
            )
            subprocess.run(["git", "init", "--quiet", mathlib], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    mathlib,
                    "add",
                    ".gitignore",
                    "lakefile.lean",
                    "lake-manifest.json",
                ],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    mathlib,
                    "-c",
                    "user.name=Palomar test",
                    "-c",
                    "user.email=test@palomar.invalid",
                    "commit",
                    "--quiet",
                    "-m",
                    "fixture",
                ],
                check=True,
            )
            revision = subprocess.run(
                ["git", "-C", mathlib, "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(
                [
                    "git",
                    "-C",
                    mathlib,
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/leanprover-community/mathlib4",
                ],
                check=True,
            )
            (source / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "version": "1.2.0",
                        "packages": [
                            {
                                "name": "mathlib",
                                "type": "git",
                                "url": "https://github.com/leanprover-community/mathlib4",
                                "rev": revision,
                            },
                            {
                                "name": "batteries",
                                "type": "git",
                                "url": "https://github.com/leanprover-community/batteries",
                                "rev": dependency_revision,
                            }
                        ],
                    }
                )
            )
            batteries = source / ".lake" / "packages" / "batteries"
            (batteries / ".git").mkdir(parents=True)
            poisons = []
            for package in (mathlib, batteries):
                poison = package / ".lake" / "build" / "bin" / "cache"
                poison.parent.mkdir(parents=True)
                poison.write_text("#!/bin/sh\nexit 97\n")
                poison.chmod(0o755)
                (package / ".lake" / "config").mkdir()
                poisons.append(poison)
            expected_replay_writable = {
                (package / ".lake" / leaf).resolve()
                for package in (mathlib, batteries)
                for leaf in ("build", "config")
            }

            def cache_phase(command, **kwargs):
                for package, poison in zip((mathlib, batteries), poisons, strict=True):
                    self.assertFalse(poison.exists())
                    self.assertTrue((package / ".lake" / "build").is_dir())
                    self.assertTrue((package / ".lake" / "config").is_dir())
                self.assertEqual(
                    kwargs.get("unrestricted_network", False),
                    command[-3:] == ["exe", "cache", "get"],
                )
                if kwargs.get("unrestricted_network", False):
                    staged_mathlib = kwargs["cwd"]
                    self.assertNotEqual(staged_mathlib, mathlib)
                    self.assertNotIn(source.resolve(), kwargs["readable_paths"])
                    staged_packages = staged_mathlib.parent
                    self.assertEqual(
                        set(kwargs["writable_directories"]),
                        {
                            (staged_packages / name / ".lake").resolve()
                            for name in ("mathlib", "batteries")
                        },
                    )
                    self.assertFalse(
                        (staged_packages / "mathlib" / ".lake" / "build" / "bin" / "cache").exists()
                    )
                    release = staged_packages / "batteries" / ".lake" / "release"
                    release.mkdir()
                    (release / "generic.bundle").write_bytes(b"archive")
                    (release / "generic.bundle.trace").write_bytes(b"trace")
                else:
                    writable = set(kwargs["writable_directories"])
                    self.assertTrue(expected_replay_writable <= writable)
                    if kwargs["cwd"] == mathlib:
                        self.assertEqual(writable, expected_replay_writable)
                    else:
                        self.assertEqual(len(writable - expected_replay_writable), 1)
                        self.assertEqual(
                            Path(kwargs["cwd"], "lakefile.toml").read_text().splitlines()[0],
                            'name = "palomarTrustedCacheReplay"',
                        )
                    self.assertEqual(
                        (batteries / ".lake" / "release" / "generic.bundle").read_bytes(),
                        b"archive",
                    )
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch(
                "scripts.verify_submission.sandboxed_run", side_effect=cache_phase
            ) as sandbox:
                verifier.get_mathlib_cache(
                    source,
                    checkout=source,
                    base_env={"PATH": os.environ["PATH"]},
                    allowlist={
                        "mathlib": ("leanprover-community/mathlib4", "high"),
                        "batteries": ("leanprover-community/mathlib4", "high"),
                    },
                    lake=Path("/tools/lake"),
                    readable_paths=[source],
                    executable_paths=[],
                    tools={},
                )

            self.assertEqual(sandbox.call_count, 3)

            submitted_manifest = json.loads(
                (source / "lake-manifest.json").read_text()
            )
            canonical_mathlib_url = submitted_manifest["packages"][0]["url"]
            submitted_manifest["packages"][0]["url"] = (
                "https://github.com/example/mathlib4-legacy"
            )
            (source / "lake-manifest.json").write_text(json.dumps(submitted_manifest))
            for poison in poisons:
                poison.parent.mkdir(parents=True, exist_ok=True)
                poison.write_text("#!/bin/sh\nexit 97\n")
            mathlib_root = {"repository": "leanprover-community/mathlib4"}
            aliases = {
                "leanprover-community/mathlib4": mathlib_root,
                "example/mathlib4-legacy": mathlib_root,
            }
            with (
                mock.patch(
                    "scripts.verify_submission.allowed_roots",
                    return_value=([mathlib_root], aliases),
                ),
                mock.patch(
                    "scripts.verify_submission.sandboxed_run",
                    side_effect=cache_phase,
                ) as alias_sandbox,
            ):
                verifier.get_mathlib_cache(
                    source,
                    checkout=source,
                    base_env={"PATH": os.environ["PATH"]},
                    allowlist={
                        "mathlib": ("leanprover-community/mathlib4", "high"),
                        "batteries": ("leanprover-community/mathlib4", "high"),
                    },
                    lake=Path("/tools/lake"),
                    readable_paths=[source],
                    executable_paths=[],
                    tools={},
                )
            self.assertEqual(alias_sandbox.call_count, 3)
            submitted_manifest["packages"][0]["url"] = canonical_mathlib_url
            (source / "lake-manifest.json").write_text(json.dumps(submitted_manifest))

            def mutate_staged_mapping(command, **kwargs):
                if kwargs.get("unrestricted_network", False):
                    staged_mathlib = kwargs["cwd"]
                    batteries_link = staged_mathlib / ".lake" / "packages" / "batteries"
                    batteries_link.unlink()
                    batteries_link.symlink_to(staged_mathlib, target_is_directory=True)
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                mock.patch(
                    "scripts.verify_submission.sandboxed_run",
                    side_effect=mutate_staged_mapping,
                ) as mutated_sandbox,
                self.assertRaisesRegex(VerificationError, "dependency link changed"),
            ):
                verifier.get_mathlib_cache(
                    source,
                    checkout=source,
                    base_env={"PATH": os.environ["PATH"]},
                    allowlist={
                        "mathlib": ("leanprover-community/mathlib4", "high"),
                        "batteries": ("leanprover-community/mathlib4", "high"),
                    },
                    lake=Path("/tools/lake"),
                    readable_paths=[source],
                    executable_paths=[],
                    tools={},
                )
            self.assertEqual(mutated_sandbox.call_count, 1)
            self.assertFalse((batteries / ".lake" / "release").exists())

            def mutate_hardlinked_source(command, **kwargs):
                if kwargs.get("unrestricted_network", False):
                    (mathlib / "lakefile.lean").write_text("package changed\n")
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                mock.patch(
                    "scripts.verify_submission.sandboxed_run",
                    side_effect=mutate_hardlinked_source,
                ) as source_mutation_sandbox,
                self.assertRaisesRegex(
                    VerificationError,
                    "source changed during the network-enabled cache phase",
                ),
            ):
                verifier.get_mathlib_cache(
                    source,
                    checkout=source,
                    base_env={"PATH": os.environ["PATH"]},
                    allowlist={
                        "mathlib": ("leanprover-community/mathlib4", "high"),
                        "batteries": ("leanprover-community/mathlib4", "high"),
                    },
                    lake=Path("/tools/lake"),
                    readable_paths=[source],
                    executable_paths=[],
                    tools={},
                )
            self.assertEqual(source_mutation_sandbox.call_count, 1)
            (mathlib / "lakefile.lean").write_text("package mathlib\n")

            protected = mathlib / ".lake" / "config" / "must-survive-refusal"
            protected.write_text("protected")
            git_dependency = submitted_manifest["packages"][1]
            submitted_manifest["packages"][1] = {
                "name": "batteries",
                "type": "path",
                "dir": "vendor/batteries",
            }
            (source / "lake-manifest.json").write_text(json.dumps(submitted_manifest))
            with self.assertRaisesRegex(
                VerificationError, "may not use a path dependency"
            ):
                verifier.get_mathlib_cache(
                    source,
                    checkout=source,
                    base_env={"PATH": os.environ["PATH"]},
                    allowlist={
                        "mathlib": ("leanprover-community/mathlib4", "high"),
                        "batteries": ("leanprover-community/mathlib4", "high"),
                    },
                    lake=Path("/tools/lake"),
                    readable_paths=[source],
                    executable_paths=[],
                    tools={},
                )
            self.assertEqual(protected.read_text(), "protected")

            submitted_manifest["packages"][1] = git_dependency
            (source / "lake-manifest.json").write_text(json.dumps(submitted_manifest))
            outside_mathlib = source.parent / "redirected-mathlib"
            mathlib.rename(outside_mathlib)
            mathlib.symlink_to(outside_mathlib, target_is_directory=True)
            protected = outside_mathlib / ".lake" / "config" / "must-survive-refusal"
            with self.assertRaisesRegex(
                VerificationError, "escapes the repository checkout"
            ):
                verifier.get_mathlib_cache(
                    source,
                    checkout=source,
                    base_env={"PATH": os.environ["PATH"]},
                    allowlist={
                        "mathlib": ("leanprover-community/mathlib4", "high"),
                        "batteries": ("leanprover-community/mathlib4", "high"),
                    },
                    lake=Path("/tools/lake"),
                    readable_paths=[source],
                    executable_paths=[],
                    tools={},
                )
            self.assertEqual(protected.read_text(), "protected")

    def test_qualified_root_build_discards_only_root_owned_lake_state(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            packages_dir = source / ".lake" / "packages"
            trusted = packages_dir / "trusted"
            shared = packages_dir / "shared"
            for package in (trusted, shared):
                (package / ".lake" / "build").mkdir(parents=True)
                (package / ".lake" / "config").mkdir()

            revision = "1" * 40
            shared_revision = "2" * 40
            (source / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {
                                "name": "trusted",
                                "type": "git",
                                "url": "https://github.com/example/trusted",
                                "rev": revision,
                            },
                            {
                                "name": "shared",
                                "type": "git",
                                "url": "https://github.com/example/shared",
                                "rev": shared_revision,
                            },
                        ]
                    }
                )
            )
            (trusted / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {
                                "name": "shared",
                                "type": "git",
                                "url": "https://github.com/example/shared",
                                "rev": shared_revision,
                            }
                        ]
                    }
                )
            )
            executable = trusted / ".lake" / "build" / "bin" / "hostile"
            executable.parent.mkdir(parents=True)
            executable.write_text("#!/bin/sh\nexit 97\n")
            executable.chmod(0o755)
            compiled = (
                trusted
                / ".lake"
                / "build"
                / "lib"
                / "lean"
                / "Trusted"
                / "Forged.olean"
            )
            compiled.parent.mkdir(parents=True)
            compiled.write_bytes(b"candidate compiled output")
            candidate_home = trusted / ".lake" / "config" / "home" / ".curlrc"
            candidate_home.parent.mkdir()
            candidate_home.write_text("url = https://attacker.invalid")
            shared_state = shared / ".lake" / "build" / "must-remain"
            shared_state.write_text("Mathlib-owned")

            packages = verifier.manifest_packages(source)
            root = {
                "repository": "example/trusted",
                "trust_level": "qualified",
            }
            aliases = {"example/trusted": root}
            expected_writable = {
                (trusted / ".lake" / leaf).resolve()
                for leaf in ("build", "config")
            }

            def trusted_build(command, **kwargs):
                self.assertEqual(command, ["/tools/lake", "build"])
                self.assertFalse(executable.exists())
                self.assertFalse(compiled.exists())
                self.assertFalse(candidate_home.exists())
                self.assertEqual(shared_state.read_text(), "Mathlib-owned")
                self.assertEqual(set(kwargs["writable_directories"]), expected_writable)
                self.assertFalse(kwargs.get("unrestricted_network", False))
                nested = trusted / ".lake" / "packages"
                self.assertTrue((nested / "shared").is_symlink())
                self.assertEqual((nested / "shared").resolve(), shared.resolve())
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                mock.patch(
                    "scripts.verify_submission.allowed_roots",
                    return_value=([root], aliases),
                ),
                mock.patch(
                    "scripts.verify_submission.sandboxed_run",
                    side_effect=trusted_build,
                ) as sandbox,
            ):
                verifier.build_allowlisted_roots(
                    source,
                    checkout=source,
                    packages=packages,
                    allowlist={
                        "trusted": ("example/trusted", "qualified"),
                        "shared": ("leanprover-community/mathlib4", "high"),
                    },
                    base_env={"PATH": "/usr/bin"},
                    lake=Path("/tools/lake"),
                    readable_paths=[source],
                    executable_paths=[],
                    tools={},
                )

            sandbox.assert_called_once()
            self.assertFalse((trusted / ".lake" / "packages").exists())
            self.assertEqual(shared_state.read_text(), "Mathlib-owned")

    def test_committed_artifacts_outside_lake_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            artifact = package / "custom-build" / "lib" / "lean" / "Poison.olean"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"not a trusted build")
            with self.assertRaisesRegex(VerificationError, "committed build artifact"):
                materialize_packages(
                    package, checkout=package, base_env={"PATH": "/usr/bin"}
                )

    def test_module_system_sidecars_count_as_committed_build_artifacts(self):
        for name in ("Stale.olean.private", "Stale.olean.server", "Stale.ir"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                package = Path(directory)
                artifact = package / "lib" / name
                artifact.parent.mkdir(parents=True)
                artifact.write_bytes(b"prebuilt")
                with self.assertRaisesRegex(VerificationError, "committed build artifact"):
                    reject_committed_build_artifacts(package)

    def test_fresh_lake_artifacts_are_removed_not_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            artifact = package / ".lake" / "build" / "lib" / "lean" / "Stale.olean"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"stale")
            remove_untrusted_lake_state(package)
            reject_committed_build_artifacts(package)
            self.assertFalse(artifact.exists())

    def test_official_closure_may_contain_trusted_trace_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            package = source / ".lake" / "packages" / "trusted"
            trace = package / "widget" / "package-lock.json.trace"
            trace.parent.mkdir(parents=True)
            trace.write_text('{"schemaVersion":"trusted"}')
            packages = [
                {
                    "name": "trusted",
                    "repository": "official/trusted",
                    "url": "https://github.com/official/trusted",
                    "revision": "1" * 40,
                }
            ]
            reject_untrusted_package_artifacts(
                source,
                packages,
                {"trusted": ("official/trusted", "high")},
                checkout=source,
            )
            with self.assertRaisesRegex(VerificationError, "committed build artifact"):
                reject_untrusted_package_artifacts(
                    source, packages, {}, checkout=source
                )

    def test_path_dependency_with_custom_prebuilt_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            dependency = source / "vendor" / "helper"
            artifact = dependency / "prebuilt" / "Helper.trace"
            artifact.parent.mkdir(parents=True)
            artifact.write_text("untrusted trace")
            (source / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {
                                "name": "helper",
                                "type": "path",
                                "dir": "vendor/helper",
                            }
                        ]
                    }
                )
            )
            with self.assertRaisesRegex(VerificationError, "committed build artifact"):
                materialize_packages(
                    source, checkout=source, base_env={"PATH": "/usr/bin"}
                )

    def test_path_dependency_lake_state_is_removed_before_artifact_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            dependency = source / "vendor" / "helper"
            stale = dependency / ".lake" / "build" / "lib" / "lean" / "Stale.olean"
            stale.parent.mkdir(parents=True)
            stale.write_text("stale object")
            (source / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {
                                "name": "helper",
                                "type": "path",
                                "dir": "vendor/helper",
                            }
                        ]
                    }
                )
            )
            materialize_packages(
                source, checkout=source, base_env={"PATH": "/usr/bin"}
            )
            self.assertFalse(stale.exists())

    def test_report_and_tools_must_not_be_writable(self):
        with self.assertRaisesRegex(VerificationError, "sandbox-writable"):
            require_protected_paths(
                [Path("/source/.lake/build/comparator")],
                [Path("/source/.lake/build")],
            )
        require_protected_paths(
            [Path("/runner/report.json"), Path("/tools/comparator")],
            [Path("/source/.lake/build")],
        )

    def test_shared_filesystem_probe_returns_evidence_and_cleans_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writable = root / "writable"
            writable.mkdir()
            denied = root / "denied"
            allowed = writable / ".palomar-write-probe"
            events = []

            def run_probe(command, **_kwargs):
                path = Path(command[-1])
                if path == allowed:
                    events.append("allowed")
                    path.touch()
                    return subprocess.CompletedProcess(command, 0, "", "")
                events.append("denied")
                return subprocess.CompletedProcess(command, 1, "", "denied")

            with mock.patch(
                "scripts.verify_submission.sandboxed_run", side_effect=run_probe
            ):
                result = _run_filesystem_confinement_probe(
                    denied,
                    existing_probe_error="probe already exists",
                    touch=Path("/usr/bin/touch"),
                    cwd=root,
                    environment={},
                    writable_directories=[writable],
                    readable_paths=[],
                    executable_paths=[],
                    tools={},
                    after_allowed=lambda: events.append("between"),
                )

            self.assertIsInstance(result, _ConfinementProbeResult)
            self.assertTrue(result.allowed_created)
            self.assertIsNotNone(result.denied)
            self.assertFalse(result.denied_created)
            self.assertEqual(events, ["allowed", "between", "denied"])
            self.assertFalse(allowed.exists())
            self.assertFalse(denied.exists())

    def test_shared_filesystem_probe_stops_after_failed_positive_control(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writable = root / "writable"
            writable.mkdir()
            denied = root / "denied"
            after_allowed = mock.Mock()

            failed = subprocess.CompletedProcess(
                ["touch", str(writable / ".palomar-write-probe")],
                1,
                "",
                "denied",
            )
            with mock.patch(
                "scripts.verify_submission.sandboxed_run", return_value=failed
            ) as run:
                result = _run_filesystem_confinement_probe(
                    denied,
                    existing_probe_error="probe already exists",
                    touch=Path("/usr/bin/touch"),
                    cwd=root,
                    environment={},
                    writable_directories=[writable],
                    readable_paths=[],
                    executable_paths=[],
                    tools={},
                    after_allowed=after_allowed,
                )

            self.assertEqual(run.call_count, 1)
            after_allowed.assert_not_called()
            self.assertFalse(result.allowed_created)
            self.assertIsNone(result.denied)
            self.assertFalse(denied.exists())

    def test_full_confinement_cleans_probe_artifacts_on_runner_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writable = root / "writable"
            writable.mkdir()
            denied = root / "write-denied"
            read_denied = root / "read-denied"
            positive_read = root / "positive-read"
            positive_read.write_text("readable")
            allowed = writable / ".palomar-write-probe"

            def fail_after_creation(command, **_kwargs):
                path = Path(command[-1])
                path.touch()
                if path == allowed:
                    return subprocess.CompletedProcess(command, 0, "", "")
                raise VerificationError("probe runner failed")

            with (
                mock.patch(
                    "scripts.verify_submission.sandboxed_run",
                    side_effect=fail_after_creation,
                ),
                self.assertRaisesRegex(VerificationError, "probe runner failed"),
            ):
                verify_sandbox_confinement(
                    denied,
                    read_denied,
                    positive_read=positive_read,
                    python=Path(sys.executable),
                    touch=Path("/usr/bin/touch"),
                    cwd=root,
                    environment={},
                    writable_directories=[writable],
                    readable_paths=[positive_read],
                    executable_paths=[],
                    tools={},
                )

            self.assertFalse(allowed.exists())
            self.assertFalse(denied.exists())
            self.assertFalse(read_denied.exists())

    def test_full_confinement_fails_closed_when_a_namespace_control_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writable = root / "writable"
            writable.mkdir()
            positive_read = root / "positive-read"
            positive_read.write_text("readable")
            allowed = writable / ".palomar-write-probe"

            def leaky_namespace(command, **_kwargs):
                path = Path(command[-1])
                if "palomar-host-canary" in path.name:
                    return subprocess.CompletedProcess(command, 1, "host canary visible: " + str(path), "")
                path.touch()
                return subprocess.CompletedProcess(command, 0 if path == allowed else 1, "", "")

            with (
                mock.patch("scripts.verify_submission.sandboxed_run", side_effect=leaky_namespace),
                self.assertRaisesRegex(VerificationError, "namespace controls failed.*host canary visible"),
            ):
                verify_sandbox_confinement(
                    root / "write-denied",
                    root / "read-denied",
                    positive_read=positive_read,
                    python=Path(sys.executable),
                    touch=Path("/usr/bin/touch"),
                    cwd=root,
                    environment={},
                    writable_directories=[writable],
                    readable_paths=[positive_read],
                    executable_paths=[],
                    tools={},
                )
            self.assertFalse(allowed.exists())
            self.assertFalse(list(Path.home().glob(".palomar-host-canary-*")))

    def test_full_confinement_rejects_a_created_denied_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            writable = root / "writable"
            writable.mkdir()
            denied = root / "write-denied"
            read_denied = root / "read-denied"
            positive_read = root / "positive-read"
            positive_read.write_text("readable")
            allowed = writable / ".palomar-write-probe"

            def escape_write(command, **_kwargs):
                path = Path(command[-1])
                path.touch()
                return subprocess.CompletedProcess(
                    command,
                    0 if path == allowed or "palomar-host-canary" in path.name else 1,
                    "",
                    "",
                )

            with (
                mock.patch(
                    "scripts.verify_submission.sandboxed_run",
                    side_effect=escape_write,
                ),
                self.assertRaisesRegex(
                    VerificationError, "outer sandbox write policy was not enforced"
                ),
            ):
                verify_sandbox_confinement(
                    denied,
                    read_denied,
                    positive_read=positive_read,
                    python=Path(sys.executable),
                    touch=Path("/usr/bin/touch"),
                    cwd=root,
                    environment={},
                    writable_directories=[writable],
                    readable_paths=[positive_read],
                    executable_paths=[],
                    tools={},
                )

            self.assertFalse(allowed.exists())
            self.assertFalse(denied.exists())
            self.assertFalse(read_denied.exists())

    def test_nonofficial_revision_is_rejected(self):
        remote = mock.Mock(returncode=0)
        fetch = mock.Mock(returncode=0)
        ancestry = mock.Mock(returncode=1)
        with mock.patch("scripts.verify_submission.run", side_effect=[remote, fetch, ancestry]) as run_mock:
            with self.assertRaisesRegex(VerificationError, "not an ancestor"):
                verify_official_revision(
                    Path("/source/.lake/packages/mathlib"),
                    repository="leanprover-community/mathlib4",
                    revision="1" * 40,
                    official_ref="refs/heads/master",
                    git_env={"PATH": "/usr/bin"},
                )
        remote_command = run_mock.call_args_list[0].args[0]
        fetch_command = run_mock.call_args_list[1].args[0]
        self.assertIn("https://github.com/leanprover-community/mathlib4", remote_command)
        self.assertIn("--filter=tree:0", fetch_command)
        self.assertIn(
            "+refs/heads/master:refs/remotes/palomar-official/head",
            fetch_command,
        )

    def test_explicit_legacy_revision_is_accepted_without_broadening_history(self):
        with mock.patch("scripts.verify_submission.run") as run_mock:
            verify_official_revision(
                Path("/source/.lake/packages/TauCeti"),
                repository="TauCetiProject/TauCeti",
                revision="2" * 40,
                official_ref="refs/heads/main",
                accepted_revisions=["2" * 40],
                git_env={"PATH": "/usr/bin"},
            )
        run_mock.assert_not_called()

    def test_canonical_semver_release_tag_accepts_a_diverged_revision(self):
        revision = "3" * 40
        remote = mock.Mock(returncode=0)
        fetch = mock.Mock(returncode=0)
        ancestry = mock.Mock(returncode=1)
        tags = mock.Mock(
            returncode=0,
            stdout=f"{revision}\trefs/tags/v4.33.1\n",
        )
        with mock.patch(
            "scripts.verify_submission.run",
            side_effect=[remote, fetch, ancestry, tags],
        ) as run_mock:
            verify_official_revision(
                Path("/source/.lake/packages/mathlib"),
                repository="leanprover-community/mathlib4",
                revision=revision,
                official_ref="refs/heads/master",
                git_env={"PATH": "/usr/bin"},
                include_semver_release_tags=True,
            )
        tag_command = run_mock.call_args_list[3].args[0]
        self.assertIn("ls-remote", tag_command)
        self.assertIn("palomar-official", tag_command)

    def test_nonrelease_tag_does_not_broaden_canonical_history(self):
        revision = "4" * 40
        remote = mock.Mock(returncode=0)
        fetch = mock.Mock(returncode=0)
        ancestry = mock.Mock(returncode=1)
        tags = mock.Mock(
            returncode=0,
            stdout=f"{revision}\trefs/tags/nightly-testing-2026-08-21\n",
        )
        with mock.patch(
            "scripts.verify_submission.run",
            side_effect=[remote, fetch, ancestry, tags],
        ):
            with self.assertRaisesRegex(VerificationError, "not an ancestor"):
                verify_official_revision(
                    Path("/source/.lake/packages/mathlib"),
                    repository="leanprover-community/mathlib4",
                    revision=revision,
                    official_ref="refs/heads/master",
                    git_env={"PATH": "/usr/bin"},
                    include_semver_release_tags=True,
                )

    def test_writable_dependency_source_is_untrusted(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            writable = source / ".lake" / "packages" / "mathlib" / ".lake" / "build"
            writable.mkdir(parents=True)
            injected = writable / "Evil.lean"
            injected.write_text("def injected := True")
            (source / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {
                                "name": "mathlib",
                                "type": "git",
                                "url": "https://github.com/leanprover-community/mathlib4",
                                "rev": "1" * 40,
                            }
                        ]
                    }
                )
            )
            audit = audit_challenge_sources(
                source,
                checkout=source,
                dependency_sources=[injected],
                lean_prefix=Path(directory) / "toolchain",
                allowlist={"mathlib": ("leanprover-community/mathlib4", "high")},
                writable_directories=[writable],
            )
            self.assertEqual(audit["untrusted_sources"], [str(injected)])

    def test_previously_accepted_palomar_source_is_untrusted(self):
        """A package Palomar already indexed confers no Challenge import privilege."""
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            package = source / ".lake" / "packages" / "accepted"
            package.mkdir(parents=True)
            dependency = package / "Accepted" / "Definitions.lean"
            dependency.parent.mkdir()
            dependency.write_text("def Accepted.answer : Nat := 42\n")
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
                    "fixture",
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
            (source / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {
                                "name": "accepted",
                                "type": "git",
                                "url": "https://github.com/example/accepted",
                                "rev": revision,
                            }
                        ]
                    }
                )
            )
            audit = audit_challenge_sources(
                source,
                checkout=source,
                dependency_sources=[dependency],
                lean_prefix=Path(directory) / "toolchain",
                allowlist={},
                writable_directories=[],
            )
            self.assertEqual(audit["untrusted_sources"], [str(dependency.resolve())])
            self.assertEqual(audit["dependencies"], [])
            self.assertEqual(audit["trust_level"], "high")
            self.assertNotIn("review_source_files", audit)

    def test_solution_only_package_is_outside_challenge_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            (source / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {
                                "name": "proofOnly",
                                "type": "git",
                                "url": "https://github.com/example/proof-only",
                                "rev": "3" * 40,
                            }
                        ]
                    }
                )
            )
            audit = audit_challenge_sources(
                source,
                checkout=source,
                dependency_sources=[],
                lean_prefix=Path(directory) / "toolchain",
                allowlist={},
                writable_directories=[],
            )
            self.assertEqual(audit["untrusted_sources"], [])
            self.assertEqual(audit["dependencies"], [])

    def test_path_package_may_not_point_under_lake(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {
                                "name": "helper",
                                "type": "path",
                                "dir": ".lake/config",
                            }
                        ]
                    }
                )
            )
            with self.assertRaisesRegex(VerificationError, "may not live under .lake"):
                materialize_packages(
                    source, checkout=source, base_env={"PATH": "/usr/bin"}
                )

    def test_trusted_package_url_map_uses_verified_manifest_urls(self):
        packages = [
            {"name": "mathlib", "url": "https://github.com/leanprover-community/mathlib4"},
            {"name": "plausible", "url": "https://github.com/leanprover-community/plausible"},
            {"name": "candidate", "url": "https://github.com/example/candidate"},
        ]
        authoritative = [
            {"name": "mathlib", "url": "https://github.com/leanprover-community/mathlib4"},
            {"name": "plausible", "url": "https://github.com/leanprover-community/plausible"},
        ]
        for package in [*packages, *authoritative]:
            package["manifest_name"] = package["name"]
        self.assertEqual(
            json.loads(trusted_package_url_map(packages, authoritative)),
            {
                "mathlib": "https://github.com/leanprover-community/mathlib4",
                "plausible": "https://github.com/leanprover-community/plausible",
            },
        )
        packages[0]["url"] = "path:../mathlib"
        with self.assertRaisesRegex(VerificationError, "may not use a path dependency"):
            trusted_package_url_map(packages, authoritative[:1])
        packages[0]["url"] = "https://github.com/leanprover-community/mathlib4.git"
        self.assertEqual(
            json.loads(trusted_package_url_map(packages, authoritative[:1])),
            {"mathlib": "https://github.com/leanprover-community/mathlib4"},
        )
        packages[0]["url"] = "https://github.com/attacker/mathlib4"
        with self.assertRaisesRegex(VerificationError, "does not match"):
            trusted_package_url_map(packages, authoritative[:1])
        authoritative[0]["url"] = "git://github.com/leanprover-community/mathlib4"
        with self.assertRaisesRegex(VerificationError, "credential-free HTTPS"):
            trusted_package_url_map(packages, authoritative[:1])
        with self.assertRaisesRegex(VerificationError, "absent from the manifest"):
            trusted_package_url_map(packages, [{"name": "missing", "url": "https://example.com"}])

    def test_trusted_package_url_map_preserves_lake_name_identity(self):
        escaped = "\u00abmy-package\u00bb"
        package = {
            "name": "my-package",
            "manifest_name": escaped,
            "url": "https://github.com/example/my-package",
            "revision": "1" * 40,
        }
        self.assertEqual(
            json.loads(trusted_package_url_map([package], [package])),
            {escaped: "https://github.com/example/my-package"},
        )
        different_name = {**package, "manifest_name": "my-package"}
        with self.assertRaisesRegex(VerificationError, "different manifest spelling"):
            trusted_package_url_map([different_name], [package])

    def test_lake_environment_uses_final_absolute_path_line(self):
        proc = mock.Mock(stdout="untrusted Lake diagnostic\n/first:/second\n")
        with mock.patch("scripts.verify_submission.sandboxed_run", return_value=proc):
            value = lake_environment_value(
                "LEAN_PATH",
                source=Path("/source"),
                lake=Path("/tools/lake"),
                printenv=Path("/usr/bin/printenv"),
                environment={},
                writable_directories=[],
                readable_paths=[Path("/source")],
                executable_paths=[],
                tools={},
                allowed_roots=[Path("/")],
            )
        self.assertEqual(value, "/first:/second")

    def test_official_manifest_closure_rejects_substitution(self):
        good_revision = "1" * 40
        bad_revision = "2" * 40
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            mathlib = source / ".lake" / "packages" / "mathlib"
            mathlib.mkdir(parents=True)
            (mathlib / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {
                                "name": "batteries",
                                "type": "git",
                                "url": "https://github.com/leanprover-community/batteries",
                                "rev": good_revision,
                            }
                        ]
                    }
                )
            )
            packages = [
                {
                    "name": "mathlib",
                    "repository": "leanprover-community/mathlib4",
                    "url": "https://github.com/leanprover-community/mathlib4",
                    "revision": good_revision,
                },
                {
                    "name": "batteries",
                    "repository": "attacker/batteries",
                    "url": "https://github.com/attacker/batteries",
                    "revision": bad_revision,
                },
            ]
            with mock.patch("scripts.verify_submission.verify_official_revision"):
                with self.assertRaisesRegex(VerificationError, "substitutes"):
                    package_allowlist(
                        source,
                        packages,
                        checkout=source,
                        base_env={"PATH": "/usr/bin"},
                    )

    def test_allowlisted_repository_has_one_canonical_package_role(self):
        root = {
            "repository": "example/trusted",
            "repository_aliases": ["example/legacy"],
            "trust_level": "qualified",
        }
        packages = [
            {
                "name": "trusted",
                "repository": "example/trusted",
                "url": "https://github.com/example/trusted",
                "revision": "1" * 40,
            },
            {
                "name": "aaatrusted",
                "repository": "example/legacy",
                "url": "https://github.com/example/legacy",
                "revision": "1" * 40,
            },
        ]
        aliases = {"example/trusted": root, "example/legacy": root}
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                "scripts.verify_submission.allowed_roots",
                return_value=([root], aliases),
            ),
            self.assertRaisesRegex(
                VerificationError,
                "multiple package names to allowlisted repository example/trusted",
            ),
        ):
            package_allowlist(
                Path(directory),
                packages,
                checkout=Path(directory),
                base_env={"PATH": "/usr/bin"},
            )


class RevisionReleaseTagTests(unittest.TestCase):
    """Lake needs the tag naming a pinned revision to fetch a GitHub release."""

    REV = "b" * 40
    OTHER = "c" * 40

    def _tags(self, *refs):
        listing = "".join(f"{sha}\trefs/tags/{name}\n" for name, sha in refs)
        with mock.patch(
            "scripts.verify_submission.run",
            return_value=subprocess.CompletedProcess(["git"], 0, listing, ""),
        ):
            return verifier.revision_release_tags(["git"], self.REV, env={})

    def test_a_lightweight_tag_naming_the_revision_is_fetched(self):
        self.assertEqual(self._tags(("v0.0.87", self.REV)), ["v0.0.87"])

    def test_an_annotated_tag_is_matched_through_its_peeled_ref(self):
        """The direct ref is the tag object, not the commit it names."""
        self.assertEqual(
            self._tags(("v0.0.87", "a" * 40), ("v0.0.87^{}", self.REV)),
            ["v0.0.87"],
        )

    def test_a_tag_naming_another_revision_is_left_alone(self):
        self.assertEqual(self._tags(("v0.0.88", self.OTHER)), [])
        self.assertEqual(
            self._tags(("v0.0.88", "a" * 40), ("v0.0.88^{}", self.OTHER)), []
        )

    def test_every_tag_naming_the_revision_is_returned_in_order(self):
        self.assertEqual(
            self._tags(
                ("v0.0.87", self.REV),
                ("v0.0.86", self.REV),
                ("v0.0.88", self.OTHER),
            ),
            ["v0.0.86", "v0.0.87"],
        )

    def test_a_tag_carrying_a_lean_toolchain_suffix_is_accepted(self):
        self.assertEqual(
            self._tags(("v0.0.95+lean-v4.29.1", self.REV)), ["v0.0.95+lean-v4.29.1"]
        )

    def test_unsafe_tag_names_are_refused(self):
        for name in ("../escape", "a//b", "broken.lock", "trailing/", "-dash", "with space"):
            with self.subTest(name):
                self.assertEqual(self._tags((name, self.REV)), [])

    def test_non_tag_refs_are_ignored(self):
        listing = f"{self.REV}\trefs/heads/main\n{self.REV}\trefs/tags/v1\n"
        with mock.patch(
            "scripts.verify_submission.run",
            return_value=subprocess.CompletedProcess(["git"], 0, listing, ""),
        ):
            self.assertEqual(verifier.revision_release_tags(["git"], self.REV, env={}), ["v1"])


class PackageTagMaterializationTests(unittest.TestCase):
    """The tag naming a pinned revision reaches the dependency checkout."""

    REV = "d" * 40

    def test_materializing_a_git_package_fetches_its_release_tag(self):
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            out = ""
            if "ls-remote" in command:
                out = f"{self.REV}\trefs/tags/v0.0.87\n"
            return subprocess.CompletedProcess(command, 0, out, "")

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {
                                "name": "proofwidgets",
                                "type": "git",
                                "url": "https://github.com/leanprover-community/proofwidgets4",
                                "rev": self.REV,
                            }
                        ]
                    }
                )
            )
            with mock.patch("scripts.verify_submission.run", side_effect=fake_run):
                materialize_packages(
                    source, checkout=source, base_env={"PATH": "/usr/bin"}
                )

        fetches = [c for c in commands if "fetch" in c]
        self.assertIn(
            "+refs/tags/v0.0.87:refs/tags/v0.0.87",
            [argument for command in fetches for argument in command],
        )
        # The tag is fetched only after the pinned revision itself.
        revision_fetch = next(i for i, c in enumerate(commands) if "fetch" in c and self.REV in c)
        tag_fetch = next(
            i
            for i, c in enumerate(commands)
            if "fetch" in c and any(a.startswith("+refs/tags/") for a in c)
        )
        checkout = next(i for i, c in enumerate(commands) if "checkout" in c)
        self.assertLess(revision_fetch, tag_fetch)
        self.assertLess(tag_fetch, checkout)

    def test_a_package_with_no_tag_at_its_revision_skips_the_extra_fetch(self):
        commands = []

        def fake_run(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "lake-manifest.json").write_text(
                json.dumps(
                    {
                        "packages": [
                            {
                                "name": "batteries",
                                "type": "git",
                                "url": "https://github.com/leanprover-community/batteries",
                                "rev": self.REV,
                            }
                        ]
                    }
                )
            )
            with mock.patch("scripts.verify_submission.run", side_effect=fake_run):
                materialize_packages(
                    source, checkout=source, base_env={"PATH": "/usr/bin"}
                )

        self.assertFalse([
            c for c in commands if any(a.startswith("+refs/tags/") for a in c)
        ])


class SubmissionRequestTests(unittest.TestCase):
    """Submissions arrive as a dispatch, and carry no submitter."""

    def dispatch(self, **inputs):
        base = {"repository": "owner/repo", "commit": "b" * 40, "request_id": "abc123def456"}
        return {"inputs": {**base, **inputs}}

    def test_a_dispatch_supplies_the_submission(self):
        values, submission_id = submission_request(
            self.dispatch(options=json.dumps({
                "project_path": "sub",
                "comparator_config_path": "sub/comparator.json",
                "existing_id": "PALOMAR-2026-01-01-000001",
            }))
        )
        self.assertEqual(values["repository_url"], "https://github.com/owner/repo")
        self.assertEqual(values["commit_sha"], "b" * 40)
        self.assertEqual(values["project_path"], "sub")
        self.assertEqual(values["comparator_config_path"], "sub/comparator.json")
        self.assertEqual(values["existing_id"], "PALOMAR-2026-01-01-000001")
        self.assertEqual(submission_id, "abc123def456")

    def test_retired_context_option_is_rejected(self):
        """`context` is retired, and a caller still sending it fails intake.

        The submission form's notes stay private because the submission server
        stopped putting them in the dispatch, not because of anything here: by
        the time this code runs, the inputs are already on the public run page.
        What the allowlist gives is detection, so a stale or regressed caller
        fails loudly rather than having the field silently accepted again.
        """
        self.assertNotIn("context", OPTIONAL_FIELDS)
        with self.assertRaisesRegex(
            VerificationError, "Unrecognized submission options: context"
        ):
            submission_request(
                self.dispatch(options=json.dumps({
                    "comparator_config_path": "sub/comparator.json",
                    "context": "private notes the submitter wrote for the reviewer",
                }))
            )

    def test_every_optional_field_can_be_supplied(self):
        """Every allowlisted field survives intake and reaches the verifier.

        A field the allowlist knows about but intake quietly discarded is the
        worse failure: the submitter believes they told us something and nobody
        ever sees it.
        """
        supplied = {name: "x" for name in OPTIONAL_FIELDS}
        values, _ = submission_request(
            self.dispatch(options=json.dumps(supplied))
        )
        for name in OPTIONAL_FIELDS:
            self.assertIn(name, values, f"{name} cannot be submitted")

    def test_technical_team_test_relationship_is_preserved_explicitly(self):
        self.assertEqual(
            submission_contract.AUTHORIZATION_RELATIONSHIPS,
            {
                "I am a responsible author or maintainer": "maintainer",
                "I have approval from a responsible author or maintainer": "approved",
                "I am a Palomar Technical Maintainer testing the workflow": (
                    "technical-test"
                ),
                "Palomar is making an exceptional registry metadata correction": (
                    "palomar-maintainer"
                ),
            },
        )

    def test_comparator_configuration_must_be_selected_explicitly(self):
        with self.assertRaisesRegex(VerificationError, "must be supplied explicitly"):
            submission_request(self.dispatch(options="{}"))

    def test_dispatch_inputs_are_validated_strictly(self):
        for label, event in [
            ("uppercase id", self.dispatch(request_id="SHOUTING1234")),
            ("short id", self.dispatch(request_id="short")),
            ("malformed repository", self.dispatch(repository="not-a-repo")),
            ("options that are not JSON", self.dispatch(options="{")),
            ("options that are not an object", self.dispatch(options="[]")),
            ("an unrecognized option", self.dispatch(options='{"evil": "x"}')),
            ("a non-string option", self.dispatch(options='{"project_path": 1}')),
            ("no inputs at all", {}),
        ]:
            with self.subTest(label):
                with self.assertRaises(VerificationError):
                    submission_request(event)


class DispatchWorkflowTests(unittest.TestCase):
    def workflow(self):
        path = REPOSITORY_ROOT / ".github" / "workflows" / "submission.yml"
        return yaml.load(path.read_text(), Loader=yaml.BaseLoader)

    def render_workflow(self):
        path = REPOSITORY_ROOT / ".github" / "workflows" / "render-challenge.yml"
        return yaml.load(path.read_text(), Loader=yaml.BaseLoader)

    def test_verification_is_reachable_only_by_dispatch(self):
        self.assertEqual(
            set(self.workflow()["on"]), {"workflow_call", "workflow_dispatch"}
        )
        self.assertEqual(list(self.workflow()["jobs"]), ["profile", "verify"])

    def test_reusable_preflight_uses_the_same_fixed_job(self):
        workflow = self.workflow()
        self.assertEqual(
            workflow["jobs"]["verify"]["runs-on"], "${{ fromJSON(needs.profile.outputs.labels) }}"
        )
        self.assertEqual(workflow["jobs"]["profile"]["runs-on"], "ubuntu-24.04")
        self.assertEqual(
            set(workflow["on"]["workflow_call"]["inputs"]),
            set(workflow["on"]["workflow_dispatch"]["inputs"]) | {"pipeline_commit"},
        )
        checkout = next(
            step
            for step in workflow["jobs"]["verify"]["steps"]
            if step.get("name") == "Checkout submission pipeline"
        )
        self.assertEqual(
            checkout["with"]["ref"],
            "${{ inputs.pipeline_commit || github.workflow_sha }}",
        )

    def test_a_slow_run_delays_only_its_own_submission(self):
        """A literal group here serialises every submission ever made.

        The cap on how much verification may be running belongs to the
        submission server, which decides what is admitted; a shared group adds
        nothing to it, and costs the submissions behind a slow run their place
        in the queue, since GitHub keeps only one run of a group pending.
        """
        concurrency = self.workflow()["jobs"]["verify"]["concurrency"]
        self.assertEqual(
            concurrency["group"],
            "palomar-verify-${{ inputs.pipeline_commit && "
            "format('call-{0}-{1}-', github.run_id, github.run_attempt) || '' }}"
            "${{ inputs.mode }}-${{ inputs.request_id }}",
        )
        self.assertEqual(concurrency["cancel-in-progress"], "false")

    def test_both_dispatched_workflows_queue_per_request(self):
        """Rendering has always queued this way; verification was the exception.

        The two workflows are dispatched by the same server for the same
        submission, so a rule that holds for one and not the other is a
        difference nobody decided on.
        """
        for name, group in (
            ("submission.yml", self.workflow()["jobs"]["verify"]["concurrency"]["group"]),
            ("render-challenge.yml", self.render_workflow()["concurrency"]["group"]),
        ):
            with self.subTest(name):
                self.assertTrue(
                    group.endswith("-${{ inputs.request_id }}"),
                    f"{name} shares one concurrency group across submissions: {group}",
                )

    def test_render_job_takes_its_timeout_from_the_profile(self):
        self.assertEqual(
            self.render_workflow()["jobs"]["render"]["timeout-minutes"],
            "${{ fromJSON(needs.profile.outputs.render_timeout) }}",
        )

    def test_root_project_render_dispatch_uses_the_empty_default(self):
        """GitHub refuses an explicitly empty value for a required input.

        Reviewer supplies the authenticated project path when it is non-empty;
        a repository-root project must therefore be allowed to take the empty
        workflow default instead of making the dispatch API reject it.
        """
        inputs = self.render_workflow()["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["project_path"]["required"], "false")
        self.assertEqual(inputs["project_path"]["default"], "")
        for name, contract in inputs.items():
            if name not in {"project_path", "execution_profile"}:
                self.assertEqual(contract["required"], "true", name)

    def test_the_run_and_artifact_carry_the_submission_id(self):
        path = REPOSITORY_ROOT / ".github" / "workflows" / "submission.yml"
        text = path.read_text()
        self.assertIn("inputs.request_id", text.split("on:")[0])
        upload = next(
            step for step in self.workflow()["jobs"]["verify"]["steps"]
            if "upload-artifact" in str(step.get("uses", ""))
        )
        self.assertIn("inputs.request_id", str(upload["with"]["name"]))

    def test_preflight_and_full_share_one_prepare_step(self):
        workflow = self.workflow()
        inputs = workflow["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["mode"]["default"], "full")
        self.assertEqual(inputs["mode"]["options"], ["preflight", "full", "correction"])
        steps = workflow["jobs"]["verify"]["steps"]
        prepare = next(step for step in steps if step.get("id") == "prepare")
        self.assertNotIn("if", prepare)
        self.assertIn("verify_submission.py prepare", prepare["run"])
        expensive = [
            step for step in steps
            if step.get("name") in {
                "Install pinned elan",
                "Install the submitted Lean toolchain",
                "Build pinned bubblewrap",
                "Run lake comparator and challenge provenance audit",
            }
        ]
        self.assertTrue(expensive)
        for step in expensive:
            self.assertIn("inputs.mode == 'full'", step["if"])

    def test_the_submitted_toolchain_is_installed_and_nothing_else_is_built(self):
        steps = self.workflow()["jobs"]["verify"]["steps"]
        install = next(
            step for step in steps if step.get("name") == "Install the submitted Lean toolchain"
        )
        self.assertEqual(install["id"], "toolchain")
        self.assertEqual(
            install["env"]["SUBMISSION_TOOLCHAIN"], "${{ steps.prepare.outputs.lean_toolchain }}"
        )
        self.assertIn('elan toolchain install "$SUBMISSION_TOOLCHAIN"', install["run"])
        execute = next(step for step in steps if step.get("id") == "execute")
        self.assertIn("--bwrap-source-tag v0.12.0", execute["run"])
        self.assertNotIn("--comparator", execute["run"])
        self.assertEqual(
            [step.get("id") for step in steps if step.get("id") in {"toolchain", "bwrap", "execute"}],
            ["toolchain", "bwrap", "execute"],
        )


class LakeComparatorTests(unittest.TestCase):
    """What Palomar hands `lake comparator`, and what it reads back."""

    def toolchain(self, root: Path) -> Path:
        prefix = root / "toolchain"
        (prefix / "bin").mkdir(parents=True)
        for name in verifier.TOOLCHAIN_TOOLS:
            (prefix / "bin" / name).write_text("#!/bin/sh\n")
            (prefix / "bin" / name).chmod(0o755)
        source = prefix / "src" / "lean" / "lake" / "Lake" / "CLI" / "Check.lean"
        source.parent.mkdir(parents=True)
        source.write_text(FAKE_CHECK_LEAN)
        return prefix

    def test_the_toolchain_must_bundle_every_tool_that_judges(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = self.toolchain(Path(directory))
            tools = verifier.toolchain_tools(prefix)
            self.assertEqual(set(tools), set(verifier.TOOLCHAIN_TOOLS))
            self.assertEqual(
                verifier.protected_kernels(tools),
                {
                    "nanoda": [str(prefix / "bin" / "nanoda_bin")],
                    "con-ron": [str(prefix / "bin" / "con-ron")],
                },
            )
            (prefix / "bin" / "con-ron").unlink()
            with self.assertRaises(VerificationError) as raised:
                verifier.toolchain_tools(prefix)
            self.assertEqual(raised.exception.code, "palomar.toolchain_incomplete")
            self.assertEqual(raised.exception.owner, "palomar")
            self.assertIn("con-ron", str(raised.exception))

    def test_primitive_targets_are_read_from_the_toolchains_own_source(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = self.toolchain(Path(directory))
            self.assertEqual(
                verifier.primitive_targets(prefix),
                ["Nat.add", "Nat.sub", "String.mk", "outParam"],
            )
            source = prefix / "src" / "lean" / "lake" / "Lake" / "CLI" / "Check.lean"
            source.write_text("def somethingElse : M Unit := pure ()\n")
            with self.assertRaises(VerificationError) as raised:
                verifier.primitive_targets(prefix)
            self.assertEqual(raised.exception.code, "palomar.toolchain_unrecognised")
            source.unlink()
            with self.assertRaises(VerificationError) as raised:
                verifier.primitive_targets(prefix)
            self.assertEqual(raised.exception.code, "palomar.toolchain_unrecognised")

    def test_a_primitive_list_assembled_any_other_way_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = self.toolchain(Path(directory))
            source = prefix / "src" / "lean" / "lake" / "Lake" / "CLI" / "Check.lean"
            head = "def primitiveTargets : M (Array Lean.Name) := do\n"
            for body in (
                "  return #[``Nat.add] ++ extraTargets\n",
                "  let extra := #[``Nat.sub]\n  return #[``Nat.add]\n",
                "  return #[``Nat.add, someList]\n",
                "  return #[``Nat.add, ``Nat.add]\n",
                "  return #[]\n",
            ):
                with self.subTest(body=body):
                    source.write_text(head + body + "def builtinTargets := 0\n")
                    with self.assertRaises(VerificationError) as raised:
                        verifier.primitive_targets(prefix)
                    self.assertEqual(raised.exception.code, "palomar.toolchain_unrecognised")
            source.write_text(
                head + "  /- ``Nat.mul is not here -/\n  return #[``Nat.add, -- ``Nat.zero\n    ``outParam]\n"
                + "def builtinTargets := 0\n"
            )
            self.assertEqual(verifier.primitive_targets(prefix), ["Nat.add", "outParam"])

    def test_a_declaration_the_module_does_not_define_is_the_submitters(self):
        panic = subprocess.CompletedProcess(
            ["leanexport"], 134, "",
            "PANIC at LeanExport.dumpConstant LeanExport.Basic:254:48: "
            "Constant Foo.mian not found in environment.\n",
        )
        for module, palomar_owned in (("the Challenge", True), ("the Solution", False)):
            with self.subTest(module=module):
                error = verifier.export_failure(
                    panic, module=module, palomar_owned=palomar_owned,
                    configured_targets={"Foo.mian"},
                )
                self.assertEqual(error.code, "comparator.declaration_missing")
                self.assertEqual(error.owner, "submitter")
                self.assertIn("Foo.mian", error.detail)
        crash = subprocess.CompletedProcess(["leanexport"], 139, "", "Segmentation fault\n")
        error = verifier.export_failure(
            crash, module="the Challenge", palomar_owned=True, configured_targets={"Foo.mian"}
        )
        self.assertEqual(error.code, "palomar.challenge_export_failed")
        self.assertTrue(error.retryable)
        error = verifier.export_failure(
            crash, module="the Solution", palomar_owned=False, configured_targets={"Foo.mian"}
        )
        self.assertEqual(error.code, "solution.export_failed")
        self.assertEqual(error.owner, "submitter")

        unrelated = verifier.export_failure(
            panic, module="the Challenge", palomar_owned=True,
            configured_targets={"Foo.actual"},
        )
        self.assertEqual(unrelated.code, "palomar.challenge_export_failed")
        self.assertEqual(unrelated.owner, "palomar")
        self.assertTrue(unrelated.retryable)

        unicode_panic = subprocess.CompletedProcess(
            ["leanexport"], 134, "",
            "PANIC at LeanExport.dumpConstant\n"
            "Constant Foo.iUnion₂ not found in environment.\n"
            + "\n".join(f"backtrace frame {index}" for index in range(15)),
        )
        unicode_error = verifier.export_failure(
            unicode_panic, module="the Challenge", palomar_owned=True,
            configured_targets={"Foo.iUnion₂"},
        )
        self.assertEqual(unicode_error.code, "comparator.declaration_missing")
        self.assertIn("Foo.iUnion₂", unicode_error.detail)
        self.assertIn("PANIC at", unicode_error.detail)

        report = {}
        verifier.report_diagnostic(
            report, unicode_error, stage="challenge-export", owner=unicode_error.owner,
        )
        self.assertEqual(report["diagnostics"][0]["owner"], "submitter")
        self.assertFalse(report["diagnostics"][0]["retryable"])

    @unittest.skipUnless(
        os.environ.get("PALOMAR_TEST_LEAN"), "set PALOMAR_TEST_LEAN to read a real toolchain"
    )
    def test_the_installed_toolchain_has_the_expected_primitive_targets(self):
        lean = Path(os.environ["PALOMAR_TEST_LEAN"]).resolve(strict=True)
        primitives = verifier.primitive_targets(lean.parent.parent)
        self.assertEqual(primitives[:3], ["Nat.add", "Nat.sub", "Nat.mul"])
        self.assertIn("eagerReduce", primitives)
        self.assertEqual(primitives[-1], "outParam")
        self.assertNotIn("Nat.zero", primitives)

    def test_export_targets_follow_the_comparators_order(self):
        config = {
            "theorem_names": ["Foo.main"],
            "definition_names": ["Foo.hole"],
            "permitted_axioms": ["propext", "Quot.sound"],
        }
        self.assertEqual(
            verifier.comparator_export_targets(config, ["Nat.add", "outParam"]),
            ["Quot", "Quot.mk", "Quot.lift", "Quot.ind", "Foo.main", "propext", "Quot.sound",
             "Nat.add", "outParam", "Foo.hole"],
        )
        config["permitted_axioms"] = ["propext"]
        del config["definition_names"]
        self.assertEqual(
            verifier.comparator_export_targets(config, ["Nat.add"]),
            ["Foo.main", "propext", "Nat.add"],
        )

    def test_the_protected_configuration_registers_the_kernels_and_nothing_submitted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "comparator.json"
            source.write_text(json.dumps({
                "challenge_module": "Challenge",
                "solution_module": "Solution",
                "theorem_names": ["main"],
                "permitted_axioms": ["propext"],
                "enable_nanoda": True,
            }))
            kernels = {"nanoda": ["/toolchain/bin/nanoda_bin"], "con-ron": ["/toolchain/bin/con-ron"]}
            written = verifier.protected_comparator_config(source, root / "protected.json", kernels=kernels)
            config = json.loads(written.read_text())
            self.assertEqual(
                set(config),
                {"challenge_module", "solution_module", "theorem_names", "definition_names",
                 "permitted_axioms", "external_kernels"},
            )
            self.assertRegex(config["challenge_module"], r"^PalomarCanonical[0-9a-f]{24}\.Challenge$")
            self.assertEqual(config["external_kernels"], kernels)
            self.assertEqual(config["definition_names"], [])
            self.assertNotIn("enable_nanoda", config)
            self.assertEqual(
                verifier.validate_protected_comparator_config(written, kernels=kernels), config
            )
            # The submitter's loader must keep refusing what only Palomar may write.
            with self.assertRaises(VerificationError) as raised:
                verifier.load_comparator_config(written)
            self.assertEqual(raised.exception.code, "comparator.unknown_key")
            for broken in (
                {
                    **config,
                    "external_kernels": {
                        "nanoda": ["nanoda_bin"], "con-ron": ["/toolchain/bin/con-ron"],
                    },
                },
                {**config, "external_kernels": {"nanoda": kernels["nanoda"]}},
                {**config, "enable_nanoda": True},
                {**config, "challenge_module": "Challenge"},
                {**config, "permitted_axioms": ["sorryAx"]},
            ):
                with self.subTest(broken=sorted(set(broken) ^ set(config)) or "value"):
                    written.write_text(json.dumps(broken))
                    with self.assertRaises(VerificationError):
                        verifier.validate_protected_comparator_config(written, kernels=kernels)

    def test_the_installed_bubblewrap_release_is_the_one_the_workflows_name(self):
        self.assertEqual(verifier.bwrap_source_tag_from_installer(), "v0.12.0")
        self.assertTrue(verifier.BWRAP_SOURCE_TAG_RE.fullmatch("v0.12.0"))
        self.assertFalse(verifier.BWRAP_SOURCE_TAG_RE.fullmatch("0.12.0"))

    def test_an_ill_typed_export_replaces_only_the_named_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            export = Path(directory) / "solution.export"
            export.write_text(
                '{"meta":{"exporter":{"name":"lean4export"}}}\n'
                '{"in":1,"str":{"pre":0,"str":"other"}}\n'
                '{"in":2,"str":{"pre":0,"str":"palomar_preflight"}}\n'
                '{"thm":{"all":[1],"levelParams":[],"name":1,"type":3,"value":4}}\n'
                '{"thm":{"all":[2],"levelParams":[],"name":2,"type":5,"value":6}}\n'
            )
            tampered = verifier.ill_typed_export(export, "palomar_preflight").decode()
            records = [json.loads(line) for line in tampered.splitlines()]
            self.assertEqual(records[3]["thm"]["value"], 4)
            self.assertEqual(
                records[4]["thm"], {"all": [2], "levelParams": [], "name": 2, "type": 5, "value": 5}
            )
            self.assertEqual(len(records), 5)
            with self.assertRaisesRegex(VerificationError, "does not name"):
                verifier.ill_typed_export(export, "absent")

    def test_declaration_names_cannot_carry_control_characters(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "comparator.json"
            path.write_text(json.dumps({
                "challenge_module": "Challenge",
                "solution_module": "Solution",
                "theorem_names": ["main\nLean default kernel rejected the solution"],
                "permitted_axioms": ["propext"],
            }))
            with self.assertRaisesRegex(VerificationError, "control characters"):
                verifier.load_comparator_config(path)

    def test_export_target_names_reject_undecodable_names_and_options(self):
        for name in (
            "Foo.my thm", "Foo.bar-baz", "Foo.", ".Foo", "--ignore-missing",
            "--export-unsafe", "Foo.«bar»", "Foo.01", "Foo.λ", "Foo.Ж",
            "Foo.漢字", "Foo.ƒ", "Foo.x²", "Foo.Ⅻ", "Foo.٣",
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "comparator.json"
                path.write_text(json.dumps({
                    "challenge_module": "Challenge",
                    "solution_module": "Solution",
                    "theorem_names": [name],
                    "permitted_axioms": ["propext"],
                }))
                with self.assertRaises(VerificationError) as raised:
                    verifier.load_comparator_config(path)
                self.assertEqual(raised.exception.code, "comparator.invalid_declaration_name")
        for name in ("Foo.iUnion₂", "Real.sqrt_α", "List.get!", "Foo.12"):
            with self.subTest(name=name):
                self.assertTrue(verifier.valid_export_target_name(name))

    def test_an_export_must_start_with_the_exporters_header(self):
        with tempfile.TemporaryDirectory() as directory:
            export = Path(directory) / "solution.export"
            with self.assertRaisesRegex(VerificationError, "produced no file"):
                verifier.verify_export(export)
            export.write_text("error: something\n")
            with self.assertRaisesRegex(VerificationError, "exporter's header"):
                verifier.verify_export(export)
            export.write_text('{"meta":{"exporter":{"name":"lean4export"}}}\n{"in":1}\n')
            verifier.verify_export(export)

    def test_exports_are_written_through_the_babysitter_not_the_log_capture(self):
        with (
            mock.patch("scripts.verify_submission.verify_tool_snapshot"),
            mock.patch("scripts.verify_submission._BWRAP", Path("/opt/bwrap")),
            mock.patch("scripts.verify_submission.supervisor_command", return_value=["supervise"]) as command,
            mock.patch(
                "scripts.verify_submission.run",
                return_value=subprocess.CompletedProcess(["supervise"], 0, "", ""),
            ),
            mock.patch(
                "scripts.verify_submission.supervisor_outcome",
                return_value={"Result": "success", "memory_events": {"oom": 0, "oom_kill": 0}},
            ),
            mock.patch("scripts.verify_submission._RESOURCE_METRICS_PATH", None),
            tempfile.TemporaryDirectory() as directory,
        ):
            output = Path(directory) / "solution.export"
            verifier.export_module(
                "Solution", ["main", "Nat.add"], output=output, leanexport=Path("/toolchain/bin/leanexport"),
                cwd=REPOSITORY_ROOT, environment={}, readable_paths=[], executable_paths=[], tools={},
                timeout=60,
            )
            self.assertEqual(command.call_args.kwargs["stdout_path"], output)
            inner = command.call_args.args[0]
            # bubblewrap's own `--` precedes the command; the exporter's is the last.
            separator = len(inner) - 1 - inner[::-1].index("--")
            self.assertEqual(inner[separator + 1:], ["main", "Nat.add"])
            self.assertEqual(
                inner[separator - 2:separator], ["/toolchain/bin/leanexport", "Solution"]
            )
            output.write_text("x")
            with self.assertRaisesRegex(VerificationError, "not fresh"):
                verifier.export_module(
                    "Solution", ["main"], output=output, leanexport=Path("/toolchain/bin/leanexport"),
                    cwd=REPOSITORY_ROOT, environment={}, readable_paths=[], executable_paths=[],
                    tools={}, timeout=60,
                )

    def test_the_judge_phase_binds_no_candidate_tree_and_nests_its_own_sandbox(self):
        with (
            mock.patch("scripts.verify_submission.verify_tool_snapshot"),
            mock.patch("scripts.verify_submission._BWRAP", Path("/opt/bwrap")),
            mock.patch("scripts.verify_submission.supervisor_command", return_value=["supervise"]) as command,
            mock.patch(
                "scripts.verify_submission.run",
                return_value=subprocess.CompletedProcess(["supervise"], 0, "Your solution is okay!", ""),
            ),
            mock.patch(
                "scripts.verify_submission.supervisor_outcome",
                return_value={"Result": "success", "memory_events": {"oom": 0, "oom_kill": 0}},
            ),
            mock.patch("scripts.verify_submission._RESOURCE_METRICS_PATH", None),
            tempfile.TemporaryDirectory() as directory,
        ):
            root = Path(directory)
            exports = root / "exports"
            exports.mkdir()
            for name in ("challenge", "solution"):
                (exports / f"{name}.export").write_text('{"meta":{}}\n')
            config = root / "protected.json"
            config.write_text("{}")
            candidate = root / "candidate"
            candidate.mkdir()
            proc = verifier.judge_exports(
                lake=Path("/toolchain/bin/lake"),
                config=config,
                challenge_export=exports / "challenge.export",
                solution_export=exports / "solution.export",
                scratch=root / "judge",
                bwrap=Path("/opt/bwrap"),
                lean_prefix=Path("/toolchain"),
                environment={"PATH": "/toolchain/bin:/usr/bin", "LEAN_PATH": str(candidate)},
                tools={},
                timeout=60,
            )
            self.assertEqual(proc.returncode, 0)
            confined = command.call_args.args[0]
            self.assertIn("--challenge-from-export", confined)
            self.assertNotIn("--inadvisably-no-sandbox", confined)
            self.assertNotIn("--disable-userns", confined)
            self.assertIn("--unshare-user", confined)
            joined = " ".join(confined)
            self.assertNotIn(str(candidate), joined)
            for name in ("challenge", "solution"):
                path = exports / f"{name}.export"
                self.assertIn(f"--ro-bind {path} {path}", joined)
            self.assertNotIn(f"--ro-bind {exports} {exports}", joined)
            self.assertIn(f"--bind {root / 'judge'} {root / 'judge'}", joined)
            self.assertIn("--setenv COMPARATOR_BWRAP /opt/bwrap", joined)
            self.assertNotIn("LEAN_PATH", joined)
            self.assertTrue((root / "judge" / "project").is_dir())


class MetadataShapeTests(unittest.TestCase):
    """A file in another shape should learn everything it is missing at once."""

    def load(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "formalization.yaml"
            path.write_text(text, encoding="utf-8")
            return load_formalization_metadata(path)

    def test_related_formalization_error_is_not_a_sources_repair(self):
        with self.assertRaises(FormalizationValidationError) as caught:
            self.load(
                "project:\n"
                "  name: Example\n"
                "  description: An example.\n"
                "  authors: [Ada Lovelace]\n"
                "  license: MIT\n"
                "  responsible_maintainers: [Ada Lovelace]\n"
                "classification:\n"
                "  arxiv: [math.LO]\n"
                "  msc2020: [03B35]\n"
                "sources:\n"
                "  - title: Source theorem\n"
                "    relationship: formalizes\n"
                "related_formalizations:\n"
                "  - id: https://example.com/proof\n"
                "    relationship: ''\n"
                "automation:\n"
                "  methods: [{method: manual}]\n"
                "review:\n"
                "  status: self-assessed\n"
            )
        matching = [issue for issue in caught.exception.issues
                    if "related_formalizations[0].relationship" in str(issue)]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].field, "related_formalizations[0].relationship")
        self.assertFalse(matching[0].repairable)
        self.assertNotIn("guided metadata form", matching[0].next_action)

    def test_every_missing_section_is_named_together(self):
        # An old shape must produce the whole guided form in one preflight,
        # rather than an aggregate prose error or one fix/resubmit cycle per field.
        with self.assertRaises(FormalizationValidationError) as caught:
            self.load("result:\n  name: x\nartifacts:\n  challenge: Challenge.lean\n")
        fields = {issue.field for issue in caught.exception.issues}
        self.assertEqual(
            fields,
            {
                "project.name", "project.description", "project.authors", "project.license",
                "project.responsible_maintainers", "classification.arxiv",
                "sources", "automation.methods", "review.status",
            },
        )
        self.assertTrue(all(issue.repairable for issue in caught.exception.issues))

    def test_automation_method_is_bounded_free_text(self):
        metadata = self.load(
            "project:\n"
            "  name: Example\n"
            "  description: A formalization of the example result.\n"
            "  authors: [Ada Lovelace]\n"
            "  license: MIT\n"
            "  responsible_maintainers: [Ada Lovelace]\n"
            "classification:\n"
            "  arxiv: [math.LO]\n"
            "  msc2020: []\n"
            "sources:\n"
            "  - title: Source theorem\n"
            "    relationship: formalizes\n"
            "automation:\n"
            "  methods:\n"
            "    - method: AI-assisted\n"
            "review:\n"
            "  status: self-assessed\n"
        )
        self.assertEqual(metadata["automation"]["methods"][0]["method"], "AI-assisted")

    def test_legacy_values_are_safely_prefilled_without_inference(self):
        with self.assertRaises(FormalizationValidationError) as caught:
            self.load(
                "artifact:\n"
                "  name: Legacy project\n"
                "  authors: [Ada Lovelace]\n"
                "  license: MIT\n"
                "source:\n"
                "  title: A source theorem\n"
                "  authors: [Emmy Noether]\n"
                "  id: arXiv:1234.5678\n"
                "  type: article\n"
                "automation:\n"
                "  method: agent\n"
                "  framework: Example agent\n"
            )
        draft = caught.exception.repair_draft
        self.assertEqual(draft["values"]["project.name"], "Legacy project")
        self.assertEqual(draft["values"]["project.authors"], ["Ada Lovelace"])
        self.assertEqual(draft["values"]["project.license"], "MIT")
        self.assertEqual(draft["values"]["sources"], [{
            "title": "A source theorem",
            "authors": ["Emmy Noether"],
            "id": "arXiv:1234.5678",
            "type": "article",
        }])
        self.assertEqual(draft["values"]["automation.methods"], [{
            "method": "agent", "framework": "Example agent",
        }])
        for inferred in (
            "project.responsible_maintainers", "classification.arxiv",
            "classification.msc2020", "review.status",
        ):
            self.assertNotIn(inferred, draft["values"])
        self.assertNotIn("relationship", draft["values"]["sources"][0])

    def test_a_file_with_the_sections_gets_the_specific_complaint(self):
        # And once the shape is right, the detailed checks speak again.
        with self.assertRaisesRegex(VerificationError, "project.name"):
            self.load(
                "project: {}\nclassification: {}\n"
                "sources: [{title: source, relationship: formalizes}]\n"
                "automation: {}\nreview: {}\n"
            )

    def test_independent_field_problems_are_reported_together(self):
        with self.assertRaises(FormalizationValidationError) as caught:
            self.load(
                "project:\n  name: ''\n  authors: []\n  license: ''\n"
                "classification:\n  arxiv: []\n  msc2020: []\n"
                "sources:\n  - title: source\n    relationship: formalizes\n"
                "automation:\n  methods: []\nreview:\n  status: ''\n"
            )
        messages = [str(issue) for issue in caught.exception.issues]
        for field in (
            "project.name",
            "project.authors",
            "project.license",
            "classification.arxiv",
            "automation.methods",
            "review.status",
        ):
            self.assertTrue(any(field in message for message in messages), field)

    def test_invalid_yaml_has_an_actionable_location_and_no_repair(self):
        with self.assertRaises(VerificationError) as caught:
            self.load("project: [\n")
        error = caught.exception
        self.assertEqual(error.code, "formalization.invalid_yaml")
        self.assertGreaterEqual(error.line or 0, 1)
        self.assertGreaterEqual(error.column or 0, 1)
        self.assertFalse(error.repairable)


class TaxonomyTextTests(unittest.TestCase):
    """The MSC descriptions are shown to readers, so they have to be readable."""

    def test_no_description_has_lost_a_character(self):
        import json
        import pathlib

        codes = json.loads(
            (pathlib.Path(__file__).resolve().parents[1] / "taxonomies" / "msc2020-codes.json")
            .read_text(encoding="utf-8")
        )
        # A '?' inside a description is a character that did not survive an
        # encoding step, not punctuation: no MSC description is a question.
        broken = sorted(code for code, text in codes.items() if "?" in text)
        self.assertEqual(broken, [], "MSC descriptions with a lost character")
        self.assertEqual(
            codes["52C10"], "Erdős problems and related topics of discrete geometry"
        )


if __name__ == "__main__":
    unittest.main()
