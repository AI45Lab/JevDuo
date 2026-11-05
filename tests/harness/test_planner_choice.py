"""Synthetic API-choice tests; no live model or GUI calls."""

import json
from unittest.mock import Mock, patch

from PIL import Image

from jevduo.components import build_default
from jevduo.config import SessionConfig
from jevduo.models.planner_choice import PlannerChoicePolicy
from jevduo.models.ranking import ranking_dict
from jevduo.models.slow import SlowModelClient
from jevduo_eval.entry import build_session

from .conftest import build_space, build_targets
from .test_slow_transport import _Response


def test_api_selector_sees_the_same_offered_actions_and_image(tmp_path):
    targets = build_targets()
    space = build_space(targets)
    expected = space.candidates()[-1]
    screenshot = tmp_path / "screen.png"
    Image.new("RGB", (16, 16)).save(screenshot)
    client = Mock()

    def choose(messages, validate, **kw):
        assert kw["role"] == "gui_choice"
        request = json.loads(messages[1]["content"][0]["text"])
        assert request["option_ids"] == [c.id for c in space.candidates()]
        assert len(request["options"]) == len(space.candidates())
        assert messages[1]["content"][1]["type"] == "image_url"
        assert not validate({"choice_id": "invented-action"})
        result = {"choice_id": expected.id}
        assert validate(result)
        return result

    client.json_request.side_effect = choose
    ranking = PlannerChoicePolicy(client).rank(stage_goal="Current goal", intent="Current intent",
        active_window="Editor", targets=targets, action_space=space, recent_actions=[], screenshot=screenshot)
    assert ranking.winner is expected and ranking.used_image
    record = ranking_dict(ranking)
    assert record["score_type"] == "api_choice_id" and "score" not in record["action_ranked"][0]


def test_planner_backend_does_not_construct_or_discover_jev(tmp_path):
    config = SessionConfig(execution_mode="goal", fast_backend="planner")
    with patch("jevduo.components.JevOmniPolicy") as jev:
        components = build_default(client=Mock(), endpoint="", config=config)
        assert isinstance(components.selector, PlannerChoicePolicy)
        with patch("jevduo_eval.entry.resolve_fast_model_endpoint") as discover:
            session = build_session({"benchmark": "osworld21", "output_root": str(tmp_path),
                "harness_config": {"execution_mode": "goal", "fast_backend": "planner"},
                "secrets": {"slow_model": {"base_url": "http://synthetic"}}})
        discover.assert_not_called()
        assert isinstance(session.components.selector, PlannerChoicePolicy)
    jev.assert_not_called()


def test_gui_choice_uses_bounded_low_reasoning_without_changing_planner(tmp_path):
    config = SessionConfig(execution_mode="goal", slow_reasoning_effort="max", slow_max_tokens=32768)
    client = SlowModelClient({"base_url": "http://synthetic"}, config, tmp_path / "audit.jsonl")
    with patch("urllib.request.urlopen", return_value=_Response()) as opener:
        client.json_request([{"role": "user", "content": "choose"}], bool, role="gui_choice")
    payload = json.loads(opener.call_args.args[0].data)
    assert payload["reasoning_effort"] == "low" and payload["max_tokens"] == 2048
    assert client.config.slow_reasoning_effort == "max"
