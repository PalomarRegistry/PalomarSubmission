"""A reproduction recipe keeps pins and drops submission-private fields."""
import unittest

from scripts.reproduction_recipe import RecipeError, reproduction_recipe


def report():
    return {
        "schema_version": 2,
        "source": {
            "repository": "example/proofs",
            "repository_url": "https://github.com/example/proofs",
            "commit": "a" * 40,
            "project_path": "formalization",
        },
        "lean_toolchain": "leanprover/lean4:v4.22.0",
        "tool_digests": {"lean": "b" * 64, "nanoda_bin": "c" * 64},
        "verification_profile": {"id": "palomar-standard-v1"},
        "bwrap_source_tag": "v0.11.0",
        "comparator": {"theorem_names": ["Demo.main"]},
        "submission": {"authorization": {"evidence": "secret-token"}},
        "orcid_validation": {"secret": "do-not-copy"},
    }


class ReproductionRecipeTests(unittest.TestCase):
    def test_recipe_names_pins_and_omits_private_fields(self):
        text = reproduction_recipe(report())
        self.assertIn("example/proofs", text)
        self.assertIn("a" * 40, text)
        self.assertIn("formalization", text)
        self.assertIn("v4.22.0", text)
        self.assertIn("Demo.main", text)
        self.assertNotIn("secret-token", text)
        self.assertNotIn("do-not-copy", text)
        self.assertIn("not an official Palomar verification", text)

    def test_mismatched_source_url_is_refused(self):
        document = report()
        document["source"]["repository_url"] = "https://github.com/other/proofs"
        with self.assertRaises(RecipeError):
            reproduction_recipe(document)

    def test_unknown_schema_is_refused(self):
        document = report()
        document["schema_version"] = 1
        with self.assertRaises(RecipeError):
            reproduction_recipe(document)

    def test_parent_project_path_is_refused(self):
        document = report()
        document["source"]["project_path"] = "../outside"
        with self.assertRaises(RecipeError):
            reproduction_recipe(document)


if __name__ == "__main__":
    unittest.main()
