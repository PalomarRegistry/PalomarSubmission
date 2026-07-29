import unittest

from scripts.landrun_passthrough import command_index


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


if __name__ == "__main__":
    unittest.main()
