import unittest
from pathlib import Path

from scripts.check_release_primitive_targets import release_problems, supported_release_tags

FIXTURES = Path(__file__).parent / "fixtures" / "primitive-targets"


class ReleasePrimitiveTargetTests(unittest.TestCase):
    def test_only_release_tags_at_or_above_the_minimum_are_checked(self):
        tags = [
            "v4.36.0", "v4.35.0-rc1", "v4.35.0", "v4.35.0-rc2", "v4.34.1", "v4.37.0-rc1",
            "nightly-2026-11-01", "v4.36.0-m1", "v4.35.0-rc10",
        ]
        self.assertEqual(
            supported_release_tags(tags, "v4.35.0-rc2"),
            ["v4.35.0-rc2", "v4.35.0-rc10", "v4.35.0", "v4.36.0", "v4.37.0-rc1"],
        )

    def test_released_lists_pass_and_an_unparseable_one_fails(self):
        for release in ("v4.36.0", "v4.37.0-rc1"):
            with self.subTest(release=release):
                text = (FIXTURES / f"{release}.lean").read_bytes()
                self.assertEqual(release_problems(release, text), [])
        broken = (FIXTURES / "v4.37.0-rc1.lean").read_bytes().replace(
            b"set_option linter.deprecated false in ``String.mk",
            b"set_option pp.all true in ``String.mk",
        )
        [problem] = release_problems("v9.9.9", broken)
        self.assertIn("unrecognised export target list", problem)

    def test_a_list_missing_an_expected_primitive_is_reported(self):
        text = (FIXTURES / "v4.36.0.lean").read_bytes().replace(b"    ``String.mk,\n", b"")
        [problem] = release_problems("v9.9.9", text)
        self.assertIn("lacks String.mk", problem)


if __name__ == "__main__":
    unittest.main()
