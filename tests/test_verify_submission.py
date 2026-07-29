import json
import tempfile
import unittest
from pathlib import Path

from scripts.verify_submission import (
    VerificationError,
    direct_imports,
    github_repository,
    load_comparator_config,
    normalize_repository,
    parse_issue_body,
)


class VerifySubmissionTests(unittest.TestCase):
    def test_issue_form_sections(self):
        body = """### Repository URL

https://github.com/example/result

### Commit SHA

0123456789012345678901234567890123456789

### Existing Palomar ID (updates only)

_No response_
"""
        parsed = parse_issue_body(body)
        self.assertEqual(parsed["repository_url"], "https://github.com/example/result")
        self.assertEqual(parsed["existing_id"], "")

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

    def test_imports(self):
        source = """import Mathlib
public import TauCeti.Topology
-- import NotReal
"""
        self.assertEqual(direct_imports(source), ["Mathlib", "TauCeti.Topology"])

    def test_comparator_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "comparator.json"
            path.write_text(
                json.dumps(
                    {
                        "challenge_module": "Challenge",
                        "solution_module": "Solution",
                        "theorem_names": ["headline"],
                        "definition_names": [],
                        "permitted_axioms": ["propext", "Quot.sound", "Classical.choice"],
                        "enable_nanoda": False,
                    }
                )
            )
            self.assertEqual(load_comparator_config(path)["theorem_names"], ["headline"])


if __name__ == "__main__":
    unittest.main()
