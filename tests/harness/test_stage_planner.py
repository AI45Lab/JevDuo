"""Stage planning keeps search submission ahead of result expectations."""

from jevduo.planning.stage_planner import plan_stage

from .conftest import A11Y_XML, build_targets


class Client:
    def __init__(self):
        self.messages = None
        self.validator = None

    def json_request(self, messages, validate, *, role):
        self.messages = messages
        self.validator = validate
        value = {
            "done": False,
            "stage_goal": "Search",
            "milestones": [{
                "route": "gui",
                "current_goal": "Submit search",
                "expected_observation": "Results",
                "action_proposals": [{"operation": "keypress", "key_sequence": [["enter"]]}],
            }],
        }
        assert validate(value)
        return value


def test_stage_prompt_requires_submit_before_filtered_results(tmp_path):
    screenshot = tmp_path / "s.png"
    screenshot.write_bytes(b"img")
    client = Client()
    plan_stage(client, "Search", screenshot, build_targets(), [], A11Y_XML)
    assert "A populated search field alone is not an applied search" in client.messages[0]["content"]
    assert "scroll_up/scroll_down require target_name and target_role" in client.messages[0]["content"]
    assert "unbound_proposals and available_targets" in client.messages[0]["content"]


def test_invalid_scroll_gets_field_level_repair_feedback(tmp_path):
    screenshot = tmp_path / "s.png"
    screenshot.write_bytes(b"img")
    client = Client()
    plan_stage(client, "Search", screenshot, build_targets(), [], A11Y_XML)
    invalid = {
        "done": False,
        "stage_goal": "Scroll",
        "milestones": [{
            "route": "gui",
            "current_goal": "Scroll",
            "expected_observation": "More content",
            "action_proposals": [{"operation": "scroll_down"}],
        }],
    }
    assert client.validator(invalid) is False
    feedback = client.messages[-1]["content"]
    assert "milestones[0].action_proposals[0].target_name" in feedback
    assert "never emit a bare scroll operation" in feedback


def test_cli_proposal_gets_milestone_level_repair_feedback(tmp_path):
    screenshot = tmp_path / "s.png"
    screenshot.write_bytes(b"img")
    client = Client()
    plan_stage(client, "Search", screenshot, build_targets(), [], A11Y_XML)
    assert "CLI milestones never use action_proposals" in client.messages[0]["content"]
    invalid = {
        "done": False,
        "stage_goal": "Build",
        "milestones": [{
            "route": "cli",
            "current_goal": "Build output",
            "expected_observation": "Output exists",
            "action_proposals": [{"operation": "cli", "command": "python3 build.py", "timeout_s": 90}],
            "checks": [],
        }],
    }
    assert client.validator(invalid) is False
    feedback = client.messages[-1]["content"]
    assert "milestones[0].command" in feedback
    assert "not action_proposals with operation=cli" in feedback
    assert "Move operation=cli out of action_proposals" in feedback
