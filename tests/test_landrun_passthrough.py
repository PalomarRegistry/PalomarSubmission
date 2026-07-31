import unittest

from scripts.landrun_passthrough import adapted_arguments, command_index


class LandrunPassthroughTests(unittest.TestCase):
    def test_comparator_arguments(self) -> None:
        arguments = [
            "--best-effort",
            "--ro",
            "/",
            "--rw",
            "/dev",
            "-ldd",
            "-add-exec",
            "--env",
            "PATH",
            "--rox",
            "/toolchain",
            "/tools/lean4export",
            "Challenge",
            "--",
            "Namespace.theorem",
        ]
        self.assertEqual(command_index(arguments), 11)

    def test_rejects_unknown_option(self) -> None:
        with self.assertRaises(ValueError):
            command_index(["--surprise", "lean"])

    def test_adds_exactly_one_landrun_delimiter(self) -> None:
        arguments = ["--ro", "/source", "/usr/bin/lean", "Challenge", "--", "theorem"]
        self.assertEqual(
            adapted_arguments(arguments),
            [
                "--ro",
                "/source",
                "--env",
                "GIT_CONFIG_GLOBAL=/dev/null",
                "--env",
                "GIT_CONFIG_NOSYSTEM=1",
                "--env",
                "GIT_TERMINAL_PROMPT=0",
                "--",
                "/usr/bin/lean",
                "Challenge",
                "--",
                "theorem",
            ],
        )
        already_delimited = ["--ro", "/source", "--", "/usr/bin/lean", "Challenge"]
        adapted = adapted_arguments(already_delimited)
        self.assertEqual(adapted.count("--"), 1)
        self.assertEqual(adapted[-3:], ["--", "/usr/bin/lean", "Challenge"])
        self.assertIn("GIT_CONFIG_GLOBAL=/dev/null", adapted)
        self.assertIn("GIT_CONFIG_NOSYSTEM=1", adapted)
        self.assertIn("GIT_TERMINAL_PROMPT=0", adapted)


if __name__ == "__main__":
    unittest.main()
