import json
import tempfile
import unittest
from pathlib import Path

from scripts import submission_contract, verify_submission
from scripts.verification_errors import FormalizationValidationError, VerificationError

ROOT = Path(__file__).resolve().parents[1]


class BrowserPreflightContractTests(unittest.TestCase):
    def test_policy_matches_authoritative_constants(self) -> None:
        policy = json.loads(
            (ROOT / "browser-preflight-policy.json").read_text(encoding="utf-8")
        )

        self.assertEqual(policy["schema_version"], 1)
        self.assertEqual(
            policy["formalization_profile_version"],
            submission_contract.FORMALIZATION_PROFILE_VERSION,
        )
        self.assertEqual(
            policy["limits"],
            {
                "source_bytes": verify_submission.MAX_SOURCE_BYTES,
                "configuration_bytes": verify_submission.MAX_CONFIGURATION_BYTES,
                "formalization_bytes": submission_contract.MAX_FORMALIZATION_BYTES,
            },
        )
        self.assertEqual(
            policy["toolchain"]["minimum"],
            json.loads((ROOT / "toolchains.json").read_text(encoding="utf-8"))["minimum"],
        )
        self.assertEqual(policy["toolchain"]["normalization"], "strip")
        self.assertEqual(policy["toolchain"]["prerelease_ordering"], "rc-before-release")
        self.assertEqual(
            set(policy["comparator"]["required_keys"]),
            verify_submission.COMPARATOR_REQUIRED_KEYS,
        )
        self.assertEqual(
            set(policy["comparator"]["allowed_keys"]),
            verify_submission.COMPARATOR_ALLOWED_KEYS,
        )
        self.assertEqual(
            set(policy["formalization"]["repository_roles"]),
            submission_contract.REPOSITORY_ROLES,
        )
        self.assertEqual(
            set(policy["formalization"]["source_relationship_categories"]),
            submission_contract.SOURCE_RELATIONSHIP_CATEGORIES,
        )
        self.assertEqual(
            policy["formalization"]["classification_cardinality"],
            {"arxiv": [1, 8], "msc2020": [0, 8]},
        )
        self.assertEqual(
            set(policy["comparator"]["standard_axioms"]), verify_submission.STANDARD_AXIOMS
        )

    def test_fixture_contract_is_bounded_and_unique(self) -> None:
        fixtures = json.loads(
            (ROOT / "tests/fixtures/browser-preflight.json").read_text(encoding="utf-8")
        )

        self.assertEqual(fixtures["schema_version"], 1)
        identifiers = [case["id"] for case in fixtures["cases"]]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertGreaterEqual(len(identifiers), 1)
        self.assertLessEqual(len(identifiers), 100)
        for case in fixtures["cases"]:
            self.assertLessEqual(
                set(case),
                {
                    "id",
                    "files",
                    "formalization",
                    "comparator",
                    "toolchain",
                    "expected_codes",
                },
            )
            self.assertEqual(
                case["expected_codes"], sorted(set(case["expected_codes"]))
            )

    def test_fixture_diagnostic_codes_match_authoritative_helpers(self) -> None:
        fixtures = json.loads(
            (ROOT / "tests/fixtures/browser-preflight.json").read_text(encoding="utf-8")
        )
        for case in fixtures["cases"]:
            codes: list[str] = []
            with self.subTest(case=case["id"]), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                values = {
                    name: (ROOT / "tests" / "fixtures" / path).read_text(encoding="utf-8")
                    for name, path in case.get("files", {}).items()
                }
                values.update(
                    (name, case[name])
                    for name in ("formalization", "comparator", "toolchain")
                    if name in case
                )
                if "formalization" in values:
                    path = root / "formalization.yaml"
                    path.write_text(values["formalization"], encoding="utf-8")
                    try:
                        submission_contract.load_formalization_metadata(path)
                    except FormalizationValidationError as error:
                        codes.extend(issue.code for issue in error.issues)
                    except VerificationError as error:
                        codes.append(error.code)
                if "comparator" in values:
                    path = root / "comparator.json"
                    path.write_text(values["comparator"], encoding="utf-8")
                    try:
                        verify_submission.load_comparator_config(path)
                    except VerificationError as error:
                        codes.append(error.code)
                if "toolchain" in values:
                    try:
                        verify_submission.supported_toolchain(values["toolchain"])
                    except VerificationError as error:
                        codes.append(error.code)
                self.assertEqual(sorted(set(codes)), case["expected_codes"])
