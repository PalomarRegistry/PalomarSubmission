import json
import unittest
from io import BytesIO
from urllib.error import HTTPError, URLError

from scripts.orcid_validation import (
    canonical_identifier,
    contains_identifier,
    valid_checksum,
    validate_record,
    validate_records,
)
from scripts.verification_errors import VerificationError


class Response(BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def record(identifier: str, *, deactivated: bool = False) -> bytes:
    return json.dumps({
        "orcid-identifier": {
            "uri": f"https://orcid.org/{identifier}",
            "path": identifier,
            "host": "orcid.org",
        },
        "history": {
            "deactivation-date": {"value": 1} if deactivated else None,
        },
    }).encode()


class OrcidValidationTests(unittest.TestCase):
    def test_canonical_identifier_accepts_bare_ids_and_canonical_urls(self):
        identifier = "0000-0002-0201-310X"
        self.assertEqual(canonical_identifier(identifier), identifier)
        self.assertEqual(
            canonical_identifier(f"https://orcid.org/{identifier}"), identifier
        )
        self.assertEqual(
            canonical_identifier(f"https://orcid.org/{identifier}/"), identifier
        )
        self.assertIsNone(canonical_identifier(f"http://orcid.org/{identifier}"))
        self.assertIsNone(canonical_identifier(f"https://example.com/{identifier}"))

    def test_identifier_shaped_text_is_detected_inside_names(self):
        identifier = "0009-0009-9699-9712"
        self.assertTrue(contains_identifier(f"Idris Ali Shaik (ORCID {identifier})"))
        self.assertTrue(
            contains_identifier(f"Idris Ali Shaik https://orcid.org/{identifier}")
        )
        self.assertFalse(contains_identifier("Orcid Smith"))

    def test_checksum_accepts_numeric_and_x_check_digits(self):
        self.assertTrue(valid_checksum("0000-0002-1825-0097"))
        self.assertTrue(valid_checksum("0000-0002-1694-233X"))
        self.assertFalse(valid_checksum("0000-0002-1825-0098"))
        self.assertFalse(valid_checksum("0000-0000-0000-000x"))

    def test_current_record_is_accepted_without_retaining_profile_data(self):
        identifier = "0000-0002-1825-0097"
        opened = []

        def open_url(request, *, timeout):
            opened.append((request, timeout))
            return Response(record(identifier))

        receipt = validate_records(
            [identifier, identifier],
            checked_at="2026-08-31T12:00:00Z",
            open_url=open_url,
        )

        self.assertEqual(len(opened), 1)
        self.assertEqual(
            opened[0][0].full_url,
            f"https://pub.orcid.org/v3.0/{identifier}/record",
        )
        self.assertEqual(opened[0][0].headers["Accept"], "application/vnd.orcid+json")
        self.assertEqual(receipt, {
            "schema_version": 1,
            "checked_at": "2026-08-31T12:00:00Z",
            "registry": "https://orcid.org",
            "records": [{
                "orcid": identifier,
                "record_url": f"https://orcid.org/{identifier}",
            }],
        })

    def test_missing_and_deactivated_records_are_submitter_errors(self):
        identifier = "0000-0002-1694-233X"
        missing = HTTPError("url", 404, "not found", {}, None)

        def missing_record(*_args, **_kwargs):
            raise missing

        with self.assertRaisesRegex(VerificationError, "does not name a current") as caught:
            validate_record(identifier, open_url=missing_record)
        self.assertEqual(caught.exception.owner, "submitter")
        self.assertFalse(caught.exception.retryable)

        with self.assertRaisesRegex(VerificationError, "does not name a current"):
            validate_record(
                identifier,
                open_url=lambda *_args, **_kwargs: Response(record(identifier, deactivated=True)),
            )

    def test_registry_outage_is_retryable_and_owned_by_palomar(self):
        identifier = "0000-0002-1825-0097"

        def offline(*_args, **_kwargs):
            raise URLError("offline")

        with self.assertRaisesRegex(VerificationError, "could not be reached") as caught:
            validate_record(identifier, open_url=offline)
        self.assertEqual(caught.exception.owner, "palomar")
        self.assertTrue(caught.exception.retryable)

    def test_registry_must_return_the_requested_current_identifier(self):
        requested = "0000-0002-1694-233X"
        current = "0000-0002-1825-0097"
        with self.assertRaisesRegex(VerificationError, f"resolves to {current}"):
            validate_record(
                requested,
                open_url=lambda *_args, **_kwargs: Response(record(current)),
            )


if __name__ == "__main__":
    unittest.main()
