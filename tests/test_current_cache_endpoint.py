"""Renderer cache migration retains a fixed official-host chain and old archives."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.render_challenge import download_mathlib_cache


class CurrentCacheEndpointTests(unittest.TestCase):
    def test_current_official_endpoint_is_tried_before_legacy_azure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache, urls = root / "cache", []
            digest = "0123456789abcdef"
            def download(command, **kwargs):
                config = (root / "mathlib-cache-download/download.conf").read_text()
                url = json.loads(next(line[6:] for line in config.splitlines() if line.startswith("url = ")))
                urls.append(url)
                if url.startswith("https://cache.mathlib.org/mathlib4-master/"):
                    (cache / (digest + ".ltar")).write_bytes(b"official archive")
                    return subprocess.CompletedProcess(command, 0, "", "")
                return subprocess.CompletedProcess(command, 22, "", "404")
            with patch("scripts.render_challenge.sandboxed_run", side_effect=download):
                count, _ = download_mathlib_cache(
                    {digest}, cache, trusted_work=root,
                    environment={"HOME": str(root / "home"), "TMPDIR": str(root / "tmp")},
                    curl=Path("/usr/bin/curl"), env_tool=Path("/usr/bin/env"),
                    executable_paths=[], tools={})
            self.assertEqual(count, 1)
            self.assertEqual(urls, ["https://cache.mathlib.org/mathlib4-master/f/" + digest + ".ltar"])

    def test_legacy_azure_archive_remains_reachable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, urls = Path(tmp), []
            cache, digest = Path(tmp) / "cache", "0123456789abcdef"
            def download(command, **kwargs):
                config = (root / "mathlib-cache-download/download.conf").read_text()
                url = json.loads(next(line[6:] for line in config.splitlines() if line.startswith("url = ")))
                urls.append(url)
                if url.startswith("https://lakecache.blob.core.windows.net/mathlib4/f/"):
                    (cache / (digest + ".ltar")).write_bytes(b"legacy archive")
                    return subprocess.CompletedProcess(command, 0, "", "")
                return subprocess.CompletedProcess(command, 22, "", "404")
            with patch("scripts.render_challenge.sandboxed_run", side_effect=download):
                count, _ = download_mathlib_cache(
                    {digest}, cache, trusted_work=root,
                    environment={"HOME": str(root / "home"), "TMPDIR": str(root / "tmp")},
                    curl=Path("/usr/bin/curl"), env_tool=Path("/usr/bin/env"),
                    executable_paths=[], tools={})
            self.assertEqual(count, 1)
            self.assertEqual(len(urls), 4)
            self.assertTrue(all(url.startswith((
                "https://cache.mathlib.org/mathlib4",
                "https://lakecache.blob.core.windows.net/mathlib4")) for url in urls))

