"""Synthetic development tests; these are not benchmark completion results."""

from dataclasses import replace

import pytest

from tandem.config import SessionConfig
from tandem.gui.goal_candidates import foreground_tree, goal_candidates
from tandem.memory.context import planning_context
from tandem.memory.state import TaskMemory
from tandem.models.ranking import Ranking
from tandem.protocol.models import FinishAction
from tandem.runtime.goal_stage import ObservationRequired
from tandem.runtime.session import TandemSession
from tandem.verification.goal_verifier import valid_result, verify_goal

from .conftest import (
    A11Y_XML,
    FixedSelector,
    build_space,
    build_targets,
    gui_receipt,
    make_components,
    make_observation,
)


@pytest.fixture
def components():
    return make_components(lambda *a, **kw: None, lambda *a, **kw: None, FixedSelector())


def requirements():
    return [{"id": "R1", "requirement": "Edit and save the document", "output_path": None,
             "kind": "delivery", "units": ["Requested content saved and inspected"],
             "unit_fact_keys": [[]], "facts_needed": []}]


def plan():
    return {"done": False, "work_item_id": "R1:unit:1", "route": "gui", "purpose": "produce",
            "goal": "Complete the local form", "completion_criteria": "Form completed",
            "method": "Fill the current form", "input_fact_keys": [], "inputs": [], "guidance": "",
            "max_actions": 24, "command": ""}


def result(**overrides):
    value = {"stage_outcome": "met", "unit_met": True, "delivery_state": "verified",
             "summary": "The requested saved content was inspected", "remaining": [],
             "evidence_refs": ["A2"], "facts": [], "materials": [], "requirement_checks": [],
             "artifacts": [], "invalidated_work_item_ids": []}
    value.update(overrides)
    return value


def test_multiple_actions_then_one_stage_verification(tmp_path, components):
    calls = {"plan": 0, "stage_verify": 0, "terminal_verify": 0}

    def planner(*args, **kwargs):
        calls["plan"] += 1
        return plan() if calls["plan"] == 1 else {
            "done": True, "task_status": "completed", "current_goal": "All original requirements met"}

    def stage_verifier(*args, **kwargs):
        calls["stage_verify"] += 1
        assert len(kwargs["history"]) == 2
        return result()

    def verifier(*args, **kwargs):
        calls["terminal_verify"] += 1
        return {"task_status": "completed", "outcome": "achieved"}

    operations = iter(["click", "keypress", "done"])

    def rank(**kwargs):
        operation = next(operations)
        winner = next(c for c in kwargs["action_space"].candidates() if c.operation == operation)
        return Ranking(winner, [], [], 0.0, 0.0, 1.0, True)

    components.planner = planner
    components.verifier = verifier
    components.stage_verifier = stage_verifier
    components.memory_factory = TaskMemory
    components.selector.rank = rank
    session = TandemSession(components, config=SessionConfig(execution_mode="goal"))
    session.begin("Edit and save", requirements())
    obs = make_observation(tmp_path, 0)
    with pytest.raises(ObservationRequired, match="planner_returned"):
        session.act(obs)
    assert session.act(obs).kind == "gui"
    assert session.act(make_observation(tmp_path, 1, receipt=gui_receipt())).kind == "gui"
    assert calls["stage_verify"] == 0
    with pytest.raises(ObservationRequired, match="stage_verification_returned"):
        session.act(make_observation(tmp_path, 2, receipt=gui_receipt()))
    assert calls == {"plan": 1, "stage_verify": 1, "terminal_verify": 0}
    assert session.goal.memory.work_items["R1:unit:1"]["status"] == "done"
    assert isinstance(session.act(make_observation(tmp_path, 2)), FinishAction)
    assert calls["terminal_verify"] == 1
    assert not any(e.get("route") == "gui_verified" for e in session.events)


def test_missing_receipt_never_replays(tmp_path, components):
    components.planner = lambda *a, **kw: plan()
    components.memory_factory = TaskMemory
    session = TandemSession(components, config=SessionConfig(execution_mode="goal"))
    session.begin("Edit", requirements())
    obs = make_observation(tmp_path, 0)
    with pytest.raises(ObservationRequired):
        session.act(obs)
    session.act(obs)
    with pytest.raises(RuntimeError, match="not replayed"):
        session.act(make_observation(tmp_path, 1))


def test_unregistered_evidence_cannot_complete_unit():
    memory = TaskMemory("Edit", requirements())
    memory.start(plan(), 0)
    memory.apply(result(evidence_refs=["invented"]), {"A2": {}})
    assert memory.work_items["R1:unit:1"]["status"] != "done"
    assert memory.work_items["R1:unit:1"]["delivery_state"] == "none"


def test_source_coverage_accumulates_across_stages():
    memory = TaskMemory("Edit", requirements())
    for part, summary in [("page 1", "First-page requirements"), ("page 2", "Second-page data")]:
        memory.start(plan(), 0)
        memory.apply(result(stage_outcome="uncertain", unit_met=False, delivery_state="none",
                            materials=[{"source_ref": "/source.pdf", "covered_parts": [part],
                                        "summary": summary, "evidence_refs": ["A2"]}]), {"A2": {}})
    material = memory.materials["/source.pdf"]
    assert material["covered_parts"] == ["page 1", "page 2"]
    assert "First-page requirements" in material["summary"] and "Second-page data" in material["summary"]
    assert memory.stats["source_parts"] == 2


def test_stage_only_note_does_not_erase_confirmed_task_fact():
    memory = TaskMemory("Edit", requirements())
    fact = {"key": "source", "value": "Original requirement", "status": "confirmed", "scope": "task",
            "source_ref": "/source.pdf", "evidence_refs": ["A2"]}
    uncertain = {**fact, "value": "Page no longer visible", "scope": "stage", "status": "uncertain"}
    for version in [fact, uncertain]:
        memory.start(plan(), 0)
        memory.apply(result(unit_met=False, delivery_state="none", facts=[version]), {"A2": {}})
    assert memory.facts["source"]["value"] == "Original requirement"


def test_reworded_unknown_fact_is_not_semantic_progress():
    memory = TaskMemory("Edit", requirements())
    for text in ["Not observed", "Still not available"]:
        memory.start(plan(), 0)
        fact = {"key": "unknown", "value": text, "status": "uncertain", "scope": "stage",
                "source_ref": "current UI", "evidence_refs": ["A2"]}
        memory.apply(result(stage_outcome="uncertain", unit_met=False, delivery_state="none", facts=[fact]),
                     {"A2": {}})
    assert memory.stats["new_facts"] == 0
    assert memory.stats["stages_without_progress"] == 2


def test_result_rejects_non_string_evidence_ids():
    assert not valid_result(result(facts=[{"key": "bad", "value": "x", "status": "confirmed",
                                          "scope": "task", "source_ref": "x", "evidence_refs": [{}]}]))


def test_requirement_schema_retry_explains_missing_requirement_field():
    from tandem.planning.goal_planner import goal_requirements

    class Client:
        def json_request(self, messages, validate, **kwargs):
            incomplete = dict(requirements()[0])
            incomplete.pop("requirement")
            assert not validate({"requirements": [incomplete]})
            assert "requirement (plain text, mandatory)" in messages[-1]["content"]
            response = {"requirements": requirements()}
            assert validate(response)
            return response

    assert goal_requirements(Client(), "Edit")[0]["requirement"] == "Edit and save the document"


def test_stage_verifier_reads_full_source_and_retries_missing_array(tmp_path):
    import json

    memory = TaskMemory("Edit", requirements())
    memory.start({**plan(), "purpose": "prepare"}, 0)
    shot = tmp_path / "source.png"
    shot.write_bytes(b"synthetic frame")
    full = tmp_path / "output.json"
    full.write_text(json.dumps({"stdout": "SOURCE HEADER followed by full material", "stderr": ""}))

    class Client:
        config = SessionConfig()

        def json_request(self, messages, validate, **kwargs):
            context = json.loads(messages[-1]["content"][0]["text"])
            assert context["actual_actions"][0]["stdout"].startswith("SOURCE HEADER")
            incomplete = result()
            incomplete.pop("invalidated_work_item_ids")
            assert not validate(incomplete)
            assert "invalidated_work_item_ids" in messages[-1]["content"]
            source = result(unit_met=False, delivery_state="none", progress_kind="source", observed_errors=[],
                            unit_assessment={"work_item_id": "R1:unit:1", "status": "not_assessed",
                                             "evidence_refs": ["A2"], "remaining": []})
            assert validate(source)
            return source

    verify_goal(Client(), "Edit", shot, memory=memory, end_reason="cli_returned", evidence={"A2": {}},
                history=[{"stdout": "tail only", "screenshot": str(shot), "full_output_ref": str(full)}])


def test_schema_retry_does_not_increase_reasoning_effort(tmp_path, monkeypatch):
    import json

    from tandem.models.slow import SlowModelClient

    payloads = []
    responses = iter([{"unexpected": True}, {"done": True}])

    class Response:
        status, headers = 200, {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps(next(responses))}}]}).encode()

    def open_request(request, **kwargs):
        payloads.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    monkeypatch.setattr("tandem.models.slow.time.sleep", lambda seconds: None)
    client = SlowModelClient({"base_url": "http://synthetic"}, SessionConfig(), tmp_path / "audit.jsonl")
    client.json_request([], lambda value: "done" in value, role="plan_stage")
    assert [p.get("reasoning_effort") for p in payloads] == [None, None]


def test_visible_shell_app_menu_filters_stale_background_active_window():
    tree = '''<desktop-frame>
      <application name="browser"><frame name="Page - Browser" active="true"/></application>
      <application name="gnome-shell"><menu name="Editor" showing="true" screencoord="(90, 0)"/>
        <push-button name="Browser"/></application>
      <application name="editor"><frame name="Document - Editor" active="true"/></application>
    </desktop-frame>'''
    filtered = foreground_tree(tree)
    assert 'name="Page - Browser"' not in filtered
    assert 'name="Document - Editor"' in filtered
    assert 'name="Browser"' in filtered  # Dock remains available for an explicit app switch.


def test_diagnostic_success_does_not_become_delivery():
    memory = TaskMemory("Edit", requirements())
    memory.start(plan(), 0)
    memory.apply(result(unit_met=False, delivery_state="none", remaining=["No output was generated"]),
                 {"A2": {}})
    assert memory.work_items["R1:unit:1"]["status"] == "pending"
    assert memory.stats["delivery_advances"] == 0


def test_delivery_unit_can_prepare_but_cannot_commit_delivery_in_preparation():
    from tandem.planning.goal_planner import valid_goal

    memory = TaskMemory("Edit", requirements())
    preparation = {**plan(), "purpose": "prepare"}
    assert valid_goal(preparation, memory, allow_cli=True)
    memory.start(preparation, 0)
    memory.apply(result(), {"A2": {}})
    assert memory.work_items["R1:unit:1"]["status"] == "pending"
    assert memory.work_items["R1:unit:1"]["delivery_state"] == "none"


def test_facts_survive_replanning_and_repeat_does_not_reset_progress():
    memory = TaskMemory("Edit", requirements())
    fact = {"key": "source.quota", "value": "1400000 USD/year", "status": "confirmed", "scope": "task",
            "source_ref": "memo.pdf page 2", "evidence_refs": ["A2"]}
    update = result(unit_met=False, delivery_state="none", facts=[fact])
    for _ in range(2):
        memory.start(plan(), 0)
        memory.apply(update, {"A2": {}})
    assert memory.stats["new_facts"] == 1
    assert memory.stats["stages_without_progress"] == 2  # An unregistered note is not required progress.
    assert memory.facts["source.quota"]["value"] == fact["value"]
    assert planning_context(memory)["fact_index"][0]["key"] == fact["key"]


def test_uncertain_stage_cannot_promote_verified():
    memory = TaskMemory("Edit", requirements())
    memory.start(plan(), 0)
    memory.apply(result(stage_outcome="uncertain"), {"A2": {}})
    assert memory.work_items["R1:unit:1"]["delivery_state"] == "none"


def test_goal_space_keeps_dynamic_clicks_and_usable_scrolls():
    targets = build_targets()
    space = goal_candidates(build_space(targets), targets, A11Y_XML, [])
    assert len(space.target_heads["click"]) > 1
    assert space.controls["done"].id == "STAGE_DONE"
    assert all("scroll_x" in c.metadata for c in space.target_heads["scroll"])


def test_stage_result_contract():
    assert valid_result(result())
    assert not valid_result({**result(), "unit_met": "yes"})


def test_preparation_reuses_registered_confirmed_facts():
    req = requirements()[0]
    req.update(facts_needed=[{"key": "source.quota", "description": "Annual quota"}])
    req["unit_fact_keys"] = [["source.quota"]]
    memory = TaskMemory("Edit", [req])
    p = {**plan(), "work_item_id": "R1:prepare", "purpose": "prepare"}
    memory.start(p, 0)
    fact = {"key": "source.quota", "value": "1400000 USD", "status": "confirmed", "scope": "task",
            "source_ref": "memo.pdf p2", "evidence_refs": ["A2"]}
    memory.apply(result(delivery_state="none", facts=[fact]), {"A2": {}})
    assert memory.work_items["R1:prepare"]["status"] == "done"
    assert memory.ready_items()[0]["id"] == "R1:unit:1"


def test_component_factory_selects_goal_mode():
    from tandem.components import build_default
    from tandem.planning.goal_planner import plan_goal

    built = build_default(client=object(), endpoint="http://unused",
                          config=SessionConfig(execution_mode="goal"))
    assert built.planner.func is plan_goal
    assert built.memory_factory is TaskMemory


def test_initial_empty_memory_is_per_session(components):
    components.memory_factory = TaskMemory
    a = TandemSession(components, config=SessionConfig(execution_mode="goal"))
    b = TandemSession(replace(components), config=SessionConfig(execution_mode="goal"))
    assert a.goal.memory is not b.goal.memory


def test_verifier_sends_control_deltas_not_repeated_trees(tmp_path):
    import json
    from unittest.mock import Mock

    memory = TaskMemory("Edit", requirements())
    memory.start(plan(), 0)
    shot = tmp_path / "synthetic.png"
    shot.write_bytes(b"synthetic, no actual model invocation")
    history = []
    for step in range(24):
        controls = [{"id": f"snapshot-{step}-{i}", "role": "button", "name": f"Button {i}",
                     "x": i * 10, "y": 100, "semantic_key": "long ancestry" * 500,
                     "parent_context": ["repeated browser ancestors" * 300]} for i in range(40)]
        history.append({"evidence_ref": f"A{step}", "kind": "gui", "operation": "click",
                        "returncode": 0, "observed_change": True, "observed_controls": controls,
                        "screenshot": str(shot)})

    def capture(messages, validate, **kwargs):
        text = messages[-1]["content"][0]["text"]
        assert len(text) < 48000
        context = json.loads(text)
        assert len(context["actual_actions"]) == 24
        assert len(context["actual_actions"][0]["control_changes"]) == 40
        assert "control_changes" not in context["actual_actions"][1]
        assert "semantic_key" not in text and "parent_context" not in text
        assert kwargs["role"] == "verify_stage"
        return result()

    client = Mock(config=SessionConfig(), json_request=Mock(side_effect=capture))
    verify_goal(client, "Edit", shot, memory=memory, history=history,
                end_reason="stage_action_limit", evidence={"A23": {}})


def test_long_source_excerpt_is_explicit_and_full_output_is_preserved(tmp_path):
    import json
    from unittest.mock import Mock

    memory = TaskMemory("Edit", requirements())
    memory.start({**plan(), "purpose": "prepare"}, 0)
    shot = tmp_path / "synthetic.png"
    shot.write_bytes(b"synthetic frame")
    full = tmp_path / "receipt.json"
    original = "SOURCE START\n" + "source body\n" * 20000 + "SOURCE END"
    full.write_text(json.dumps({"stdout": original, "stderr": ""}))

    def capture(messages, validate, **kwargs):
        text = messages[-1]["content"][0]["text"]
        assert len(text) < 48000
        action = json.loads(text)["actual_actions"][0]
        assert action["stdout"].startswith("SOURCE START") and action["stdout"].endswith("SOURCE END")
        assert action["output_omitted_chars"]["stdout"] > 0
        assert action["full_output_ref"] == str(full)
        return result(unit_met=False, delivery_state="none")

    client = Mock(config=SessionConfig(), json_request=Mock(side_effect=capture))
    check = verify_goal(client, "Edit", shot, memory=memory, end_reason="cli_returned",
                        evidence={"A2": {}}, history=[{"evidence_ref": "A2", "kind": "cli",
                        "returncode": 0, "stdout": "tail", "screenshot": str(shot),
                        "full_output_ref": str(full)}])
    assert check["check_mode"] == "source"
    assert json.loads(full.read_text())["stdout"] == original


@pytest.mark.parametrize("reason", ["execution_error", "repeated_action_without_observable_progress"])
def test_known_failure_does_not_call_model_or_claim_completion(reason):
    from unittest.mock import Mock

    memory = TaskMemory("Edit", requirements())
    memory.start(plan(), 0)
    client = Mock()
    check = verify_goal(client, "Edit", None, memory=memory, end_reason=reason, evidence={"A2": {}},
                        history=[{"kind": "gui", "observed_change": False, "returncode": 1}])
    client.json_request.assert_not_called()
    assert check["check_mode"] == "local_failure" and check["unit_met"] is False
    assert check["delivery_state"] == "none" and valid_result(check)
    memory.apply(check, {"A2": {}})
    assert memory.work_items["R1:unit:1"]["status"] == "pending"


def test_save_requires_semantic_check_even_without_screen_change(tmp_path):
    from unittest.mock import Mock

    memory = TaskMemory("Edit", requirements())
    memory.start({**plan(), "purpose": "save"}, 0)
    shot = tmp_path / "synthetic.png"
    shot.write_bytes(b"synthetic frame")
    client = Mock(config=SessionConfig(), json_request=Mock(return_value=result()))
    check = verify_goal(client, "Edit", shot, memory=memory, end_reason="stage_action_limit",
                        evidence={"A2": {}}, history=[{"kind": "gui", "observed_change": False,
                        "returncode": 0, "screenshot": str(shot)}])
    client.json_request.assert_called_once()
    assert check["check_mode"] == "delivery"
