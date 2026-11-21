import copy
import unittest

from jev_desktop import AgentDesktop, JevLoop, RunOptions, Scope
from jev_desktop.startup_observation import empty_document, empty_document_tree
from support import Desktop, Model, Transport, node, success


class StartingDesktop(Desktop):
    def __init__(self, ready_after=None):
        super().__init__([node(role="webarea", name="", available_actions=["SetFocus"], states=["focused"])])
        self.ready_after = ready_after

    def observe(self, scope):
        seen = super().observe(scope)
        seen.screen["context"] = [{"role": "window", "name": "Test"}, {"role": "webarea", "states": ["focused"]}]
        if self.ready_after is not None and len(self.observations) > self.ready_after:
            seen.nodes = [node(role="textfield", name="Loaded field", available_actions=["SetValue"])]
            seen.screen["context"].append({"role": "textfield", "name": "Loaded field"})
        return seen


class StartupObservationTests(unittest.TestCase):
    def test_native_observation_retains_empty_document_evidence_before_candidate_filtering(self):
        reads = []
        def reply(args):
            if args[0] == "list-surfaces":
                return success({"surfaces": []})
            self.assertEqual(args[0], "snapshot")
            reads.append(args)
            tree = {"role": "window", "children": [{"role": "webarea"}]}
            if len(reads) > 1:
                tree["children"][0]["children"] = [node(name="Loaded")]
            return success({"app": "Editor", "window": {"id": "w1", "title": "Test"}, "complete": True, "tree": tree})
        transport = Transport(reply)
        desktop = AgentDesktop(transport=transport)
        model = Model("DONE")
        events = list(JevLoop(desktop, model, sleep=lambda _: None).run("Inspect", RunOptions(startup_observation_retries=3)))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual(len(reads), 2)
        self.assertEqual(len(model.requests), 1)
        self.assertIn("Loaded", str(model.requests[0]))
        self.assertTrue(all(args[0] in {"snapshot", "list-surfaces"} for _, args, _ in transport.calls))

    def test_raw_document_structure_survives_candidate_filtering_without_actions_or_refs(self):
        tree = {"role": "window", "children": [{"role": "webarea", "states": ["focused"]}]}
        self.assertTrue(empty_document_tree(tree))
        seen = StartingDesktop().observe(Scope())
        seen.nodes = []
        self.assertFalse(empty_document(seen))
        seen.screen["empty_document_structure"] = empty_document_tree(tree)
        self.assertTrue(empty_document(seen))
        tree["children"][0]["children"] = [{"role": "button", "name": "Ready"}]
        self.assertFalse(empty_document_tree(tree))

    def test_readiness_retry_does_not_spend_a_model_call_or_dispatch_input(self):
        desktop = StartingDesktop(ready_after=1)
        model = Model("DONE")
        delays = []
        events = list(JevLoop(desktop, model, sleep=delays.append).run("Inspect", RunOptions(startup_observation_retries=3)))
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual(len(desktop.observations), 2)
        self.assertEqual(len(model.requests), 1)
        self.assertIn("Loaded field", str(model.requests[0]))
        self.assertEqual(delays, [0.5])
        self.assertEqual(desktop.actions, [])

    def test_a_permanently_empty_document_has_a_bounded_grace_then_a_real_decision(self):
        for count in (0, 3):
            with self.subTest(count=count):
                desktop = StartingDesktop()
                model = Model("BLOCKED")
                events = list(JevLoop(desktop, model, sleep=lambda _: None).run("Inspect", RunOptions(startup_observation_retries=count)))
                self.assertEqual(events[-1]["stop"], "blocked")
                self.assertEqual(len(desktop.observations), count + 1)
                self.assertEqual(sum(e.get("diagnostic") == "empty_startup_document" for e in events), count)
                self.assertEqual(len(model.requests), 1)
                self.assertEqual(desktop.actions, [])

    def test_content_partial_reads_and_unknown_surfaces_are_not_empty_startup_documents(self):
        seen = StartingDesktop().observe(Scope())
        self.assertTrue(empty_document(seen))
        for changes in ({"complete": False}, {"root": "@s:e1"}, {"surface": "menu"}, {"unclassified_surfaces": [{"id": "unknown"}]}):
            candidate = copy.deepcopy(seen)
            candidate.screen.update(changes)
            self.assertFalse(empty_document(candidate))
        for changes in ({"children_count": 1}, {"value": "loaded"}, {"subtree_truncated": True}, {"children": [node()]}, {"role": "textfield"}):
            candidate = copy.deepcopy(seen)
            candidate.nodes[0].update(changes)
            self.assertFalse(empty_document(candidate))
        candidate = copy.deepcopy(seen)
        candidate.screen["context"].append({"role": "button", "name": "Loaded"})
        self.assertFalse(empty_document(candidate))

    def test_retry_option_rejects_invalid_budgets(self):
        for value in (-1, 11, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RunOptions(startup_observation_retries=value)
