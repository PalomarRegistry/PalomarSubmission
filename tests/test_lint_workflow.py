import re
import tomllib
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


class LintWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.safe_load(
            (ROOT / ".github/workflows/ci.yml").read_text()
        )
        self.job = self.workflow["jobs"]["lint"]

    def test_rule_set_is_explicit(self):
        configuration = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(
            configuration["tool"]["ruff"]["lint"]["select"],
            ["E", "F", "I", "UP", "B"],
        )

    def test_lint_dependency_is_one_exact_hash_locked_package(self):
        lock = (ROOT / "requirements-lint.txt").read_text()
        requirements = re.findall(r"^(?!\s|#)([A-Za-z0-9_-]+)==([^\s\\]+)", lock, re.M)
        self.assertEqual(requirements, [("ruff", "0.16.2")])
        self.assertGreaterEqual(lock.count("--hash=sha256:"), 1)

    def test_required_job_has_only_the_lint_authority_and_dependencies(self):
        self.assertEqual(self.job["permissions"], {"contents": "read"})
        self.assertEqual(self.job["timeout-minutes"], 5)
        self.assertNotIn("env", self.job)
        self.assertEqual(
            [step.get("uses") for step in self.job["steps"] if "uses" in step],
            [
                "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
            ],
        )
        checkout, python, install, lint = self.job["steps"]
        self.assertEqual(checkout["with"]["persist-credentials"], False)
        self.assertEqual(python["with"]["python-version"], "3.11.10")
        self.assertIn("--require-hashes", install["run"])
        self.assertIn("--no-deps", install["run"])
        self.assertIn("--only-binary=:all:", install["run"])
        self.assertIn("-r requirements-lint.txt", install["run"])
        self.assertEqual(lint["run"], "python -m ruff check .")


if __name__ == "__main__":
    unittest.main()
