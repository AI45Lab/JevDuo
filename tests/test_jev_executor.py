"""Mocked local-control contracts; no model, game or VM is executed here."""

import asyncio
import io
import time
from types import SimpleNamespace

from PIL import Image

from jev_gui.bindings import LocalBindings
from jev_gui.choice import LocalChoicePolicy
from jev_gui.executor import LocalExecutor, LocalFrame
from jev_gui.policy import local_candidates


def target(**changes):
    values = dict(id="email", name="Email", description="", role="entry", parent_context=("Form",),
                  x=20, y=20, width=100, height=20, value="", input_type="", semantic_key="form/email",
                  operations=("click", "type_text"), checked=None, source="a11y", date_order=())
    return SimpleNamespace(**(values | changes))


def test_input_values_are_bound_to_labels_not_cross_product():
    candidates = local_candidates("Fill contact information", [target(), target(id="name", name="Name")],
                                  inputs=[{"field": "Email", "text": "a@example.test"}])
    writes = [c for c in candidates if c.operation == "type_text"]
    assert len(writes) == 1 and writes[0].metadata["field"] == "email"
    assert [name for name, _ in writes[0].metadata["commands"]] == ["click", "keypress", "type"]
    assert not [c for c in local_candidates("Fill", [target(input_type="date")],
        inputs=[{"field": "Email", "text": "2025-11-26"}]) if c.operation == "type_text"]
    # A native reported zero is not proof that a visually blank field is filled.
    assert any(c.operation == "type_text" for c in local_candidates("Fill Email", [target(value="0")],
               inputs=[{"field": "Email", "text": "0"}]))
    assert not any(c.operation == "type_text" for c in local_candidates("Compose message",
        [target(name="Total")], inputs=[{"field": "To", "text": "recipient@example.test"}]))
    many = [target(id=f"button{i}", role="push_button", name="Other") for i in range(50)] + [target()]
    assert any(c.operation == "type_text" for c in local_candidates("Fill the form", many,
               inputs=[{"field": "Email", "text": "a@example.test"}]))
    # Nearby labels must not compete with a different control's authoritative name.
    fields = [target(id="to", name="To"), target(id="subject", name="Subject", nearby_text="To")]
    choices = local_candidates("Fill", fields, inputs=[{"field": "To", "text": "recipient@example.test"}])
    assert [c.target_id for c in choices if c.operation == "type_text"] == ["to"]


def test_delegation_records_each_command_and_refreshes_after_last_action():
    calls, observations, events = [], [], []

    async def observe(reactive):
        observations.append(reactive)
        image = io.BytesIO()
        Image.new("RGB", (200, 100)).save(image, "PNG")
        return LocalFrame(image.getvalue(), f"frame{len(observations)}", time.monotonic(),
                          200, 100, [target(value="a@example.test" if calls else "")])

    async def perform(name, params, reactive):
        calls.append((name, params))
        return {"action_status": "acknowledged"}

    def choose(**kwargs):
        choices = kwargs["candidates"]
        winner = next(c for c in choices if c.operation == ("type_text" if not calls else "done"))
        return winner, {"latency_ms": 1, "used_image": True}

    runner = LocalExecutor(SimpleNamespace(choose=choose), observe, perform, events.append)
    result = asyncio.run(runner.run({"goal": "Fill Email", "inputs": [
        {"field": "Email", "text": "a@example.test"}], "max_actions": 5}))
    assert result["actions"] == 3 and result["stop_reason"] == "completion_requested"
    assert [name for name, _ in calls] == ["click", "keypress", "type"]
    assert calls[0][1] == {"x": 350.0, "y": 300.0}
    assert result["screenshot_path"] == "frame3" and len(events) == 2
    assert result["input_readback"][0]["status"] == "verified"


def test_expired_game_choices_never_send_inputs():
    sent, events = [], []

    async def observe(reactive):
        return LocalFrame(b"mock", "old", time.monotonic() - 2, 100, 100, [])

    async def perform(*args):
        sent.append(args)

    def choose(**kwargs):
        return kwargs["candidates"][1], {"latency_ms": 1, "used_image": True}

    result = asyncio.run(LocalExecutor(SimpleNamespace(choose=choose), observe, perform, events.append).run(
        {"goal": "Play normally", "mode": "reactive", "allowed_keys": ["space"], "max_actions": 20}))
    assert result["stop_reason"] == "reactive_latency_exceeded" and not sent
    assert len(events) == 3 and all(e["action_discarded"] for e in events)


def test_factory_and_inline_choice_make_one_request_without_staging(tmp_path):
    endpoint = tmp_path / "endpoint.json"
    endpoint.write_text('{"base_url":"http://mock.invalid"}')
    runner = LocalExecutor(LocalChoicePolicy(str(endpoint)), None, None, None)
    seen = []

    class Opener:
        def open(self, request, **kwargs):
            import json

            payload = json.loads(request.data)
            seen.append(payload)
            return io.BytesIO(b'{"scores":[0.2,0.8],"used_image":true}')

    runner.selector.opener = Opener()
    image = io.BytesIO()
    Image.new("RGB", (64, 64)).save(image, "PNG")
    choices = local_candidates("game", [], reactive=True)[:2]
    winner, ranking = runner.selector.choose(goal="game", screenshot=image.getvalue(),
        candidates=choices, recent=[], reactive=True)
    assert len(seen) == 1 and seen[0]["image_path"].startswith("data:image/jpeg;base64,")
    assert "image_data" not in seen[0]
    assert winner is choices[1] and ranking["used_image"]


def test_scroll_progress_is_not_stopped_as_a_duplicate_and_clock_is_ignored():
    def frame(color, clock=False):
        image = Image.new("RGB", (200, 100), color)
        if clock:
            image.paste("white", (0, 0, 200, 20))
        out = io.BytesIO()
        image.save(out, "PNG")
        return LocalFrame(out.getvalue(), "mock", 0, 200, 100, [], "same")

    assert not LocalExecutor._ui_changed(frame("black"), frame("black", clock=True))
    assert LocalExecutor._ui_changed(frame("black"), frame("gray"))


def test_unbound_or_ambiguous_fields_cannot_submit_or_cross_write():
    fields = [target(id="one"), target(id="two", y=60)]
    send = target(id="send", role="push_button", name="Send", operations=("click",))
    candidates = local_candidates("Fill and send", fields + [send],
        inputs=[{"field": "Email", "text": "a@example.test"}], allowed_keys=["enter", "tab"])
    assert not any(c.operation == "type_text" or c.id in {"click:send", "key:enter", "done"}
                   for c in candidates)
    assert any(c.id == "key:tab" for c in candidates)
    scoped = local_candidates("Fill", fields, inputs=[
        {"field": "Email", "target_id": "two", "text": "a@example.test"}])
    assert [c.id for c in scoped if c.operation == "type_text"] == ["type:two:0"]
    missing = local_candidates("Fill and send", [send], inputs=[{"field": "Body", "text": "Hello"}])
    assert not any(c.id in {"click:send", "done"} for c in missing)
    duplicate = LocalBindings([{"field": "Email", "text": value} for value in ("first", "second")])
    duplicate.observe([target()], "form")
    assert not duplicate.targets and all(row["status"] == "ambiguous" for row in duplicate.rows)


def test_readback_requires_our_write_and_unique_same_surface_identity():
    binding = LocalBindings([{"field": "Email", "text": "0"}])
    binding.observe([target(value="0")], "form")
    assert binding.pending  # A stale A11y value alone must not skip writing.
    binding.wrote(0)
    binding.observe([target(id="new-snapshot", name="Changed name", value="0")], "form")
    assert not binding.pending
    binding.observe([target(value="other")], "form")
    assert binding.rows[0]["status"] == "readback_mismatch"


def test_explicit_target_id_rebinds_after_refresh_but_not_across_surfaces():
    binding = LocalBindings([{"field": "Unlabelled body", "text": "Exact", "target_id": "old:1"}])
    binding.observe([target(id="old:1", name="Quoted message")], "form")
    binding.observe([target(id="new:1", name="Quoted message")], "form")
    assert binding.targets[0].id == "new:1"
    binding.observe([target(id="new:2", name="Quoted message")], "another-form")
    assert not binding.targets


def test_new_delegation_reuses_only_acknowledged_and_currently_matching_inputs():
    history = {}
    inputs = [{"field": "Email", "text": "0"}]
    first = LocalBindings(inputs, history)
    first.observe([target(value="0")], "form")
    assert first.pending and not history
    first.wrote(0)
    first.observe([target(value="0")], "form")
    resumed = LocalBindings(inputs, history)
    resumed.observe([target(id="new", value="0")], "form")
    assert not resumed.pending
    resumed.observe([target(value="different")], "form")
    assert resumed.pending
    another = LocalBindings(inputs, history)
    another.observe([target(value="0")], "other-form")
    assert another.pending


def test_only_real_capabilities_and_valid_bounds_become_actions():
    choices = local_candidates("Open Data", [
        target(id="passive", role="list_item", operations=(), x=0, y=0),
        target(id="text", role="text", operations=()),
        target(id="button", role="push_button", operations=("click",)),
        target(id="offscreen", operations=("click",), x=1000)], screen_size=(200, 100))
    ids = {c.id for c in choices}
    assert "click:button" in ids
    assert not ids & {"click:passive", "click:text", "click:offscreen", "open:passive"}


def test_opening_file_chooser_is_not_mistaken_for_final_submission():
    upload = target(id="upload", role="push_button", name="Upload", operations=("click",))
    save_as = target(id="save_as", role="menu_item", name="Save As", operations=("click",))
    request = {"inputs": [{"field": "File name", "text": "/tmp/result.txt"}]}
    choices = local_candidates("Choose file and upload", [upload, save_as], **request)
    assert {c.id for c in choices} >= {"click:upload", "click:save_as"}
    assert not any(c.metadata.get("commit") for c in choices)
    guarded = local_candidates("Confirm upload", [upload], commit_targets=["Upload"], **request)
    assert not any(c.id == "click:upload" for c in guarded)


def test_wait_keeps_visual_target_and_failed_click_is_not_reoffered():
    events, calls = [], []
    image = io.BytesIO()
    Image.new("RGB", (200, 100)).save(image, "PNG")

    async def observe(reactive):
        return LocalFrame(image.getvalue(), "mock.png", time.monotonic(), 200, 100, [], "form")

    async def perform(operation, parameters, reactive):
        calls.append(operation)
        return {"action_status": "acknowledged"}

    def choose(**kwargs):
        ids = {c.id: c for c in kwargs["candidates"]}
        if not calls:
            return ids["observe"], {}
        if len(calls) == 1:
            assert "visual:0" in ids
            return ids["visual:0"], {}
        assert "visual:0" not in ids
        return ids["blocked"], {}

    failures = {}
    runner = LocalExecutor(SimpleNamespace(choose=choose), observe, perform, events.append,
                           failed_actions=failures)
    goal = {"goal": "Open foreground app", "visual_targets": [{"label": "dock icon", "x": 20, "y": 50}]}
    result = asyncio.run(runner.run(goal))
    assert calls == ["wait", "click"] and result["goal_status"] == "blocked"
    assert failures
    # A fresh delegation on the unchanged surface must not resurrect that click.
    result = asyncio.run(runner.run(goal))
    assert calls == ["wait", "click"] and result["actions"] == 0


def test_mismatched_readback_returns_before_send_without_replaying_input():
    calls = []
    image = io.BytesIO()
    Image.new("RGB", (200, 100)).save(image, "PNG")

    async def observe(reactive):
        return LocalFrame(image.getvalue(), "mock.png", time.monotonic(), 200, 100,
            [target(value="wrong" if calls else ""), target(
                id="send", role="push_button", name="Send", operations=("click",))], "form")

    async def perform(operation, parameters, reactive):
        calls.append(operation)
        return {"action_status": "acknowledged"}

    def choose(**kwargs):
        assert not any(c.id == "click:send" for c in kwargs["candidates"])
        return next(c for c in kwargs["candidates"] if c.operation == "type_text"), {}

    result = asyncio.run(LocalExecutor(SimpleNamespace(choose=choose), observe, perform, lambda e: None).run(
        {"goal": "Fill and send", "inputs": [{"field": "Email", "text": "expected"}]}))
    assert calls == ["click", "keypress", "type"]
    assert result["stop_reason"] == "input_readback_mismatch" and result["goal_status"] == "blocked"


def test_verified_inputs_unlock_one_submission_then_require_outer_review():
    calls = []
    image = io.BytesIO()
    Image.new("RGB", (200, 100)).save(image, "PNG")

    async def observe(reactive):
        controls = [target(value="expected" if calls else ""), target(
            id="send", role="push_button", name="Send", operations=("click",))]
        return LocalFrame(image.getvalue(), "mock.png", time.monotonic(), 200, 100, controls, "form")

    async def perform(operation, parameters, reactive):
        calls.append(operation)
        return {"action_status": "acknowledged"}

    def choose(**kwargs):
        if not calls:
            return next(c for c in kwargs["candidates"] if c.operation == "type_text"), {}
        assert not any(c.operation == "type_text" for c in kwargs["candidates"])
        return next(c for c in kwargs["candidates"] if c.id == "click:send"), {}

    result = asyncio.run(LocalExecutor(SimpleNamespace(choose=choose), observe, perform, lambda e: None).run(
        {"goal": "Fill and send", "inputs": [{"field": "Email", "text": "expected"}]}))
    assert calls == ["click", "keypress", "type", "click"]
    assert result["stop_reason"] == "submission_acknowledged" and result["goal_status"] == "needs_review"


def test_unacknowledged_macro_stops_before_typing_without_replay():
    calls = []
    image = io.BytesIO()
    Image.new("RGB", (200, 100)).save(image, "PNG")

    async def observe(reactive):
        return LocalFrame(image.getvalue(), "mock.png", time.monotonic(), 200, 100, [target()], "form")

    async def perform(operation, parameters, reactive):
        calls.append(operation)
        return {"action_status": "acknowledged" if len(calls) == 1 else "unknown"}

    def choose(**kwargs):
        return next(c for c in kwargs["candidates"] if c.operation == "type_text"), {}

    result = asyncio.run(LocalExecutor(SimpleNamespace(choose=choose), observe, perform, lambda e: None).run(
        {"goal": "Fill", "inputs": [{"field": "Email", "text": "expected"}]}))
    assert calls == ["click", "keypress"] and result["actions"] == 2
    assert result["stop_reason"] == "execution_not_acknowledged"


def test_unambiguous_fields_progress_before_returning_other_field_ambiguity():
    calls = []
    image = io.BytesIO()
    Image.new("RGB", (200, 100)).save(image, "PNG")

    async def observe(reactive):
        return LocalFrame(image.getvalue(), "mock.png", time.monotonic(), 200, 100, [
            target(value="expected" if calls else ""), target(id="body1", name="Body", y=50),
            target(id="body2", name="Body", y=75)], "form")

    async def perform(operation, parameters, reactive):
        calls.append(operation)
        return {"action_status": "acknowledged"}

    def choose(**kwargs):
        return next(c for c in kwargs["candidates"] if c.operation == "type_text"), {}

    result = asyncio.run(LocalExecutor(SimpleNamespace(choose=choose), observe, perform, lambda e: None).run(
        {"goal": "Fill", "inputs": [{"field": "Email", "text": "expected"},
                                      {"field": "Body", "text": "body text"}]}))
    assert calls == ["click", "keypress", "type"]
    assert result["stop_reason"] == "input_binding_ambiguous"
    assert result["input_readback"][0]["status"] == "verified"
