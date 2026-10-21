import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.kernel_outcomes import con_ron_outcome
from scripts.verify_submission import ResourceExhausted, replay_con_ron


class CheckerOutcomesTests(unittest.TestCase):
    def test_exit_status_and_one_authentic_verdict_determine_the_outcome(self):
        cases = [
            (0, "con-ron: accepted 123 declarations (--verified)", "accepted"),
            (1, "con-ron: rejected: type mismatch [at theorem Foo] (--verified)", "rejected"),
            (2, "con-ron: declined: in-process model of I: reflexive member I (--verified)", "declined"),
            (3, "con-ron: error: fuel exhausted: whnf loop [at theorem Foo] (--verified)", "declined"),
            (2, "con-ron: declined: non-standard axiom [at axiom bad] (--verified)", "error"),
            (2, "con-ron: declined: unknown export format (--verified)", "error"),
            (3, "con-ron: error: kernel internal failure", "error"),
            (101, "con-ron: declined: in-process model of I", "error"),
            (0, "", "error"),
            (0, "con-ron: accepted 123 declarations (--trusted)", "error"),
            (1, "con-ron: declined: in-process model of I", "rejected"),
            (3, "con-ron: error: mismatch [at bad\ncon-ron: error: fuel exhausted: whnf]", "error"),
            (2, "con-ron: declined: unknown [at bad\ncon-ron: declined: in-process model of I]", "error"),
        ]
        for code, log, expected in cases:
            with self.subTest(code=code, log=log):
                result = con_ron_outcome(code, "", log)
                self.assertEqual(result["status"], expected)
                self.assertLessEqual(len(result["detail"]), 2000)

    def test_only_this_optional_sandbox_phase_may_turn_capacity_into_a_decline(self):
        for failure in (ResourceExhausted("authenticated OOM telemetry"),
                        subprocess.TimeoutExpired(["con-ron"], 5)):
            with tempfile.TemporaryDirectory() as directory, mock.patch(
                "scripts.verify_submission.sandboxed_run", side_effect=failure
            ) as run:
                root = Path(directory)
                result = replay_con_ron(binary=root / "con-ron", solution_export=root / "solution.export",
                                        scratch=root / "scratch", lean_prefix=root, environment={},
                                        tools={}, timeout=10)
                self.assertEqual(result["status"], "declined")
                self.assertEqual(result["reason"], "capacity")
                self.assertEqual(run.call_args.kwargs["writable_directories"], [])
                self.assertNotIn("nested_sandbox", run.call_args.kwargs)
