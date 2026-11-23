import unittest

from jev_desktop import JevLoop, RunOptions
from support import Desktop, answer, node


class FirstOfferedModel:
    def ask(self, request):
        question = next(iter(request["questions"]))
        choices = request["questions"][question]["criteria"]
        choice = (
            next(iter(choices))
            if question == "operation"
            else next((key for key in choices if key.isdecimal()), "DONE")
        )
        return {"answers": {question: answer(choice)}}


class NoEffectNavigationTests(unittest.TestCase):
    def test_a_no_op_opener_is_withheld_so_an_alternate_route_can_reach_the_goal(self):
        desktop = Desktop([node(name="Open details"), node(name="Toggle sidebar", ref_id="@s1:e2")])
        desktop.result = {"ok": True, "delivery": "delivered_unverified"}
        desktop.installed = False

        def effect():
            clicked = desktop.actions[-1][2]["name"]
            if clicked == "Toggle sidebar":
                desktop.nodes = [node(name="Install", ref_id="@s2:e1")]
            elif clicked == "Install":
                desktop.installed = True
                desktop.nodes = [node(name="Installed", available_actions=[])]

        desktop.on_execute = effect
        events = list(
            JevLoop(desktop, FirstOfferedModel(), sleep=lambda _: None).run(
                "Install the extension",
                RunOptions(replan_no_effect=True),
            )
        )
        self.assertTrue(desktop.installed)
        self.assertEqual(events[-1]["stop"], "done")
        self.assertEqual(
            [action[2]["name"] for action in desktop.actions], ["Open details", "Toggle sidebar", "Install"]
        )

    def test_default_still_stops_when_three_clicks_have_no_visible_effect(self):
        desktop = Desktop([node(name="Open details")])
        desktop.result = {"ok": True, "delivery": "delivered_unverified"}
        events = list(JevLoop(desktop, FirstOfferedModel(), sleep=lambda _: None).run("goal"))
        self.assertIn("3 turns that changed nothing", events[-1]["stop"])
        self.assertEqual(len(desktop.actions), 3)
