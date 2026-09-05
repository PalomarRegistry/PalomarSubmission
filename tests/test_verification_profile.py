import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.verification_profile import (
    PROFILE_PATH,
    VerificationProfileError,
    check_host,
    load_profile,
)


class VerificationProfileTests(unittest.TestCase):
    def test_checked_in_profile_is_closed_and_current(self):
        profile = load_profile()
        self.assertEqual(profile["id"], "palomar-standard-v1")
        self.assertEqual(profile["runner"]["label"], "ubuntu-24.04")
        self.assertEqual(
            profile,
            json.loads(PROFILE_PATH.read_text(encoding="utf-8")),
        )

    def test_host_capacity_is_checked_before_candidate_execution(self):
        profile = load_profile()
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "scripts.verification_profile.platform.machine", return_value="x86_64"
        ), mock.patch(
            "scripts.verification_profile.host_memory_bytes",
            return_value=profile["limits"]["memory_max_bytes"] - 1,
        ), self.assertRaisesRegex(VerificationProfileError, "profile requires"):
            check_host(profile, Path(temporary))


if __name__ == "__main__":
    unittest.main()
