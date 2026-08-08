import unittest

from scripts.render_report import (
    AcceptedRenderPaths,
    PreparedRenderReport,
    RenderReportError,
    RenderSource,
    intake_report,
    parse_prepared_report,
)


class RenderReportTests(unittest.TestCase):
    def source(self) -> RenderSource:
        return RenderSource(
            repository="owner/repository",
            repository_url="https://github.com/owner/repository",
            commit="1" * 40,
            challenge_sha256="2" * 64,
            paths=AcceptedRenderPaths(
                project_path="examples/example",
                challenge_path="examples/example/Challenge.lean",
                solution_path="examples/example/Solution.lean",
                comparator_config_path="examples/example/comparator.json",
                lakefile_path="examples/example/lakefile.toml",
                lean_toolchain_path="lean-toolchain",
            ),
        )

    def report(self) -> dict:
        return PreparedRenderReport(
            source=self.source(),
            lean_toolchain="leanprover/lean4:v4.31.0",
            verso_commit="3" * 40,
            prepared_at="2026-08-08T00:00:00Z",
        ).as_dict()

    def test_current_prepared_report_round_trips_through_the_typed_boundary(self):
        report = self.report()

        parsed = parse_prepared_report(report)

        self.assertEqual(parsed.as_dict(), report)
        self.assertEqual(parsed.source.paths.challenge_path, report["source"]["challenge_path"])

    def test_legacy_schema_is_rejected_instead_of_defaulting_paths(self):
        report = self.report()
        report["schema_version"] = 1
        for field in (
            "project_path",
            "challenge_path",
            "solution_path",
            "comparator_config_path",
            "lakefile_path",
            "lean_toolchain_path",
        ):
            report["source"].pop(field)

        with self.assertRaisesRegex(
            RenderReportError,
            "schema_version 2 with the complete accepted path set",
        ):
            parse_prepared_report(report)

    def test_missing_path_is_rejected(self):
        report = self.report()
        report["source"].pop("solution_path")

        with self.assertRaisesRegex(RenderReportError, "missing solution_path"):
            parse_prepared_report(report)

    def test_blank_file_path_is_rejected(self):
        with self.assertRaisesRegex(
            RenderReportError, "source.challenge_path must be a nonempty string"
        ):
            AcceptedRenderPaths(
                project_path="",
                challenge_path="",
                solution_path="Solution.lean",
                comparator_config_path="comparator.json",
                lakefile_path="lakefile.toml",
                lean_toolchain_path="lean-toolchain",
            )

    def test_unexpected_source_field_is_rejected(self):
        report = self.report()
        report["source"]["legacy_path"] = "Challenge.lean"

        with self.assertRaisesRegex(RenderReportError, "unexpected legacy_path"):
            parse_prepared_report(report)

    def test_intake_failures_use_the_current_schema(self):
        self.assertEqual(intake_report("2026-08-08T00:00:00Z")["schema_version"], 2)


if __name__ == "__main__":
    unittest.main()
