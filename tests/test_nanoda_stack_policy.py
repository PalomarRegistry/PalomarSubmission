"""The NanoDa worker stack is a profile field, not a submission field."""
import json
import tempfile
import unittest
from pathlib import Path

from scripts.verification_errors import VerificationError
from scripts.verification_profile import VerificationProfileError, load_profile, nanoda_worker_stack_kib
from scripts.verify_submission import load_comparator_config


def comparator(extra):
    body = {
        "challenge_module": "Challenge",
        "solution_module": "Solution",
        "theorem_names": ["main"],
        "permitted_axioms": ["propext"],
    }
    body.update(extra)
    return body


class NanodaStackPolicyTests(unittest.TestCase):
    def test_the_published_profile_does_not_change_the_stack(self):
        profile = load_profile()
        self.assertNotIn("nanoda_worker_stack_kib", profile)
        self.assertIsNone(nanoda_worker_stack_kib(profile))

    def test_an_approved_value_is_recorded_and_a_bad_one_is_rejected(self):
        self.assertEqual(nanoda_worker_stack_kib({"nanoda_worker_stack_kib": 8192}), 8192)
        for value in [255, 65537, True, "8192"]:
            with self.subTest(value=value):
                with self.assertRaises(VerificationProfileError):
                    nanoda_worker_stack_kib({"nanoda_worker_stack_kib": value})

    def test_a_submission_cannot_set_the_stack(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "comparator.json"
            path.write_text(json.dumps(comparator({"nanoda_worker_stack_kib": 8192})), encoding="utf-8")
            with self.assertRaises(VerificationError) as caught:
                load_comparator_config(path)
        self.assertEqual(caught.exception.code, "comparator.stack_override")
        self.assertEqual(caught.exception.owner, "submitter")


if __name__ == "__main__":
    unittest.main()
