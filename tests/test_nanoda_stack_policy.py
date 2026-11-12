"""The NanoDa worker stack is a maintainer profile field, not a submission field."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scripts.verify_submission as verifier
from scripts.submission_contract import load_formalization_metadata, submission_request
from scripts.verification_errors import VerificationError
from scripts.verification_profile import (
    PROFILE_PATH,
    VerificationProfileError,
    load_profile,
    nanoda_worker_stack_kib,
    profile_evidence,
)


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
    def load_hosted(self, **changes):
        document = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        document.update(changes)
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "verification-profile.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            with mock.patch("scripts.verification_profile.PROFILE_PATH", path):
                return load_profile("palomar-standard-v1")

    def test_the_published_profile_does_not_change_the_stack(self):
        profile = load_profile("palomar-standard-v1")
        self.assertNotIn("nanoda_worker_stack_kib", profile)
        self.assertIsNone(nanoda_worker_stack_kib(profile))
        evidence = verifier.verification_profile_evidence()
        self.assertNotIn("nanoda_worker_stack_kib", evidence)
        self.assertEqual(
            [name for name, _binary in verifier.PROTECTED_KERNELS],
            ["nanoda", "con-ron"],
        )

    def test_an_approved_value_is_recorded_and_a_bad_one_is_rejected(self):
        self.assertEqual(nanoda_worker_stack_kib({"nanoda_worker_stack_kib": 8192}), 8192)
        for value in (256, 65536, 24576):
            with self.subTest(value=value):
                loaded = self.load_hosted(nanoda_worker_stack_kib=value)
                self.assertEqual(loaded["nanoda_worker_stack_kib"], value)
                evidence = profile_evidence(loaded, digest="ab")
                self.assertEqual(evidence["nanoda_worker_stack_kib"], value)
                self.assertIs(type(evidence["nanoda_worker_stack_kib"]), int)
        for value in (255, 65537, True, "8192"):
            with self.subTest(value=value):
                with self.assertRaises(VerificationProfileError):
                    nanoda_worker_stack_kib({"nanoda_worker_stack_kib": value})
                with self.assertRaises(VerificationProfileError):
                    self.load_hosted(nanoda_worker_stack_kib=value)

    def test_a_submission_cannot_set_the_stack(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "comparator.json"
            path.write_text(
                json.dumps(comparator({"nanoda_worker_stack_kib": 8192})),
                encoding="utf-8",
            )
            with self.assertRaises(VerificationError) as caught:
                verifier.load_comparator_config(path)
        self.assertEqual(caught.exception.code, "comparator.stack_override")
        self.assertEqual(caught.exception.owner, "submitter")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "comparator.json"
            path.write_text(
                json.dumps(comparator({"notes": {"nanoda_worker_stack_kib": 8192}})),
                encoding="utf-8",
            )
            with self.assertRaises(VerificationError) as caught:
                verifier.load_comparator_config(path)
            self.assertEqual(caught.exception.code, "comparator.stack_override")

            formalization = Path(tmp) / "formalization.yaml"
            formalization.write_text("nanoda_worker_stack_kib: 8192\n", encoding="utf-8")
            with self.assertRaises(VerificationError) as caught:
                load_formalization_metadata(formalization)
            self.assertEqual(caught.exception.code, "formalization.stack_override")

            formalization.write_text(
                "project:\n  name: Example\n  nanoda_worker_stack_kib: 4096\n",
                encoding="utf-8",
            )
            with self.assertRaises(VerificationError) as caught:
                load_formalization_metadata(formalization)
            self.assertEqual(caught.exception.code, "formalization.stack_override")

        with self.assertRaises(VerificationError) as caught:
            submission_request({
                "inputs": {
                    "repository": "owner/repo",
                    "commit": "b" * 40,
                    "request_id": "abc123def456",
                    "options": json.dumps({
                        "comparator_config_path": "comparator.json",
                        "nanoda_worker_stack_kib": "8192",
                    }),
                }
            })
        self.assertIn("maintainer-owned", str(caught.exception))
        self.assertEqual(caught.exception.field, "options.nanoda_worker_stack_kib")

    def test_recorded_stack_is_not_passed_to_either_kernel(self):
        kernels = verifier.protected_kernels({
            "nanoda_bin": Path("/toolchain/bin/nanoda_bin"),
            "con-ron": Path("/toolchain/bin/con-ron"),
        })
        self.assertEqual(list(kernels), ["nanoda", "con-ron"])
        self.assertEqual(kernels["nanoda"], ["/toolchain/bin/nanoda_bin"])
        self.assertFalse(any("24576" in item for item in kernels["nanoda"] + kernels["con-ron"]))
        with tempfile.TemporaryDirectory() as raw, mock.patch(
            "scripts.verify_submission.sandboxed_run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as launched:
            root = Path(raw)
            verifier.judge_exports(
                lake=Path("/toolchain/bin/lake"),
                config=root / "protected.json",
                challenge_export=root / "challenge.export",
                solution_export=root / "solution.export",
                scratch=root / "judge",
                bwrap=Path("/opt/bwrap"),
                lean_prefix=Path("/toolchain"),
                environment={"PATH": "/usr/bin"},
                tools={},
                timeout=30,
            )
        argv = launched.call_args.args[0]
        environment = launched.call_args.kwargs["environment"]
        self.assertEqual(argv[1], "comparator")
        self.assertNotIn("24576", argv)
        self.assertNotIn("--stack", argv)
        self.assertTrue(all("24576" not in value for value in environment.values()))
        self.assertNotIn("nanoda_worker_stack_kib", environment)


if __name__ == "__main__":
    unittest.main()
