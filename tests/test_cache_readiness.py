"""Cache readiness separates an unanswered host from a missing object."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.render_challenge import MATHLIB_CACHE_BASE_URLS, cache_readiness, download_mathlib_cache
from scripts.verification_errors import VerificationError


class CacheReadinessTests(unittest.TestCase):
    def test_unavailable_host_is_not_a_miss(self):
        report = cache_readiness(
            {"0123456789abcdef"},
            [{"base_url": MATHLIB_CACHE_BASE_URLS[0], "curl_exit": 6, "hits": []}],
        )
        self.assertEqual(report["objects"], [{"digest": "0123456789abcdef", "status": "unknown"}])
        self.assertFalse(report["predicts_build_resources"])

    def test_http_failure_on_a_responding_host_is_absence(self):
        report = cache_readiness(
            {"0123456789abcdef"},
            [{"base_url": MATHLIB_CACHE_BASE_URLS[0], "curl_exit": 22, "hits": []}],
        )
        self.assertEqual(report["objects"][0]["status"], "absent")

    def test_untrusted_endpoint_is_rejected(self):
        with self.assertRaises(VerificationError):
            cache_readiness(
                set(),
                [{"base_url": "https://evil.example/mathlib4", "curl_exit": 0, "hits": []}],
            )

    def test_download_records_a_miss_without_calling_a_foreign_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"

            def download(command, **kwargs):
                config = (root / "mathlib-cache-download/download.conf").read_text()
                self.assertNotIn("evil.example", config)
                return subprocess.CompletedProcess(command, 22, "", "404")

            with mock.patch("scripts.render_challenge.sandboxed_run", side_effect=download):
                download_mathlib_cache(
                    {"0123456789abcdef"}, cache, trusted_work=root,
                    environment={"HOME": str(root / "home"), "TMPDIR": str(root / "tmp")},
                    curl=Path("/usr/bin/curl"), env_tool=Path("/usr/bin/env"),
                    executable_paths=[], tools={},
                )
            report = json.loads((root / "mathlib-cache-readiness.json").read_text())
            self.assertEqual(report["objects"][0]["status"], "absent")
            self.assertTrue(all(
                attempt["base_url"] in MATHLIB_CACHE_BASE_URLS for attempt in report["attempts"]
            ))


if __name__ == "__main__":
    unittest.main()
