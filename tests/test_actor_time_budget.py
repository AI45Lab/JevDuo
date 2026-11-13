import unittest

from appcfg import execution_harness


class ActorTimeBudgetTest(unittest.TestCase):
    def test_global_time_is_tightened_without_mutating_source_or_call_limits(self):
        original = {"orchestration": {"enabled": True, "max_seconds": 10800,
                                       "max_model_calls": 1300, "subagent_steps_per_cycle": 50}}
        local = execution_harness(original, 8700)
        self.assertEqual(local["orchestration"]["max_seconds"], 8700)
        self.assertEqual(original["orchestration"]["max_seconds"], 10800)
        self.assertEqual(local["orchestration"]["max_model_calls"], 1300)
        self.assertEqual(local["orchestration"]["subagent_steps_per_cycle"], 50)

    def test_never_expands_a_shorter_configured_limit_and_clamps_to_one_second(self):
        original = {"orchestration": {"enabled": True, "max_seconds": 30}}
        self.assertEqual(execution_harness(original, 100)["orchestration"]["max_seconds"], 30)
        self.assertEqual(execution_harness(original, -5)["orchestration"]["max_seconds"], 1)
