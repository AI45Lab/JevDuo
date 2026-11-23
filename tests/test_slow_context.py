"""Lossless Slow deltas across restart, interrupted delivery and compaction."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nexau.archs.llm.llm_config import LLMConfig
from nexau.archs.main_sub.config import AgentConfig
from nexau.core.messages import ImageBlock, Message, Role, TextBlock
from nexau.core.serializers.openai_chat import serialize_ump_to_openai_chat_payload
from northcua.agentinfra.context.adapters.ump import UMPContextProjector
from northcua.agentinfra.context.bootstrap import prepare_context_messages
from northcua.agentkernel.context.application.history import CheckpointHistoryAssembler
from northcua.agentkernel.context.domain.enums import RetentionClass
from northcua.agentkernel.context.domain.models import (
    ContextSummaryDocumentV1,
    ContextTokenEstimate,
    SummaryInvocationResult,
    SummaryItem,
)
from northcua.agentkit.orchestration.contracts import CycleRecord, SlowDecision, SubagentResult
from northcua.agentkit.orchestration.conversation import Conversation, event_message
from northcua.agents.desktop.runtime.slow_context import SlowContextEvents, pin_slow_references
from northcua.core.config import NorthCUAAppConfig


def payload(message):
    return json.loads(message.content[0].text)


def decision(goal="Preserve this exact assignment", *, observation="before"):
    return SlowDecision.model_validate({
        "action": "CONTINUE", "summary": "Inspect the file", "verification": "Read it back",
        "observation_id": observation, "contract": {
            "subgoal_id": "subgoal", "agent_id": "general", "goal": goal,
            "constraints": ["Never repeat an unconfirmed write"], "success_criteria": ["Readback matches"],
            "budget": {"max_model_calls": 2, "max_tool_calls": 1, "max_seconds": 60},
        },
    })


def result():
    return SubagentResult(
        subgoal_id="subgoal", attempt_id="attempt", outcome="blocked", exit_reason="unsafe_to_replay",
        summary=json.dumps({"unresolved_delivery": True, "tool_results": [
            {"command": "save /tmp/report", "delivery": "unknown", "unsafe_to_replay": True},
        ]}), evidence_refs=["after"], trace_ref="original-trace", criterion_claims=[],
    )


class SlowContextTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "slow.sqlite3"
        self.conversation = Conversation(self.path)
        self.conversation.extend([
            Message(role=Role.SYSTEM, content=[TextBlock(text="Exact task policy")]),
            event_message({"task": "Save report with exact constraints"}),
        ], event_id="initial")
        initial = SlowContextEvents(self.conversation)
        self.conversation.append(initial.current_state(
            {"event": "current_state", "observation": {"observation_id": "before"}}, result=None, notes=[],
        ))
        self.proposed = decision()
        self.conversation.append(Message(role=Role.ASSISTANT, content=[
            TextBlock(text=self.proposed.model_dump_json()),
        ]))

    def append_state(self, *, cycle_id="cycle", child=None, notes=None, current_decision=None):
        child = result() if child is None else child
        events = SlowContextEvents(self.conversation)
        events.append_cycle(CycleRecord(cycle_id=cycle_id, decision=current_decision or self.proposed,
                                       subagent_result=child))
        message = events.current_state(
            {"event": "current_state", "observation": {"observation_id": "after"}},
            result=child, notes=notes or [],
        )
        message.content.append(ImageBlock(url="data:image/png;base64,ZnJlc2g="))
        self.conversation.append(message)
        return payload(message)

    def test_result_and_decision_appear_once_with_append_stable_wire_prefix(self):
        before = self.conversation.snapshot()
        state = self.append_state()
        after = self.conversation.snapshot()
        self.assertEqual(before, after[:len(before)])
        wire_before = serialize_ump_to_openai_chat_payload(before)
        wire_after = serialize_ump_to_openai_chat_payload(after)
        self.assertEqual(wire_before, wire_after[:len(wire_before)])
        added = [payload(m) for m in after[len(before):]]
        stored = next(p for p in added if p.get("event") == "subagent_result")
        cycle = next(p["history"][0] for p in added if p.get("event") == "cycle_finished")
        self.assertEqual(stored["result"], result().model_dump(mode="json"))
        self.assertEqual(cycle["subagent_result_ref"], stored["ref"])
        self.assertEqual(state["previous_result_ref"], stored["ref"])
        self.assertNotIn("subagent_result", cycle)
        self.assertNotIn("decision", cycle)
        self.assertNotIn("previous_result", state)
        self.assertEqual(cycle["decision_ref"]["observation_id"], "before")
        self.assertEqual(sum(p.get("event") == "subagent_result" for p in added), 1)

    def test_restart_emits_only_changed_notes_and_versioned_results(self):
        entry = {"key": "delivery", "kind": "fact", "text": "Unknown write; verify first",
                 "reason": "receipt"}
        first = self.append_state(notes=[entry])
        self.conversation = Conversation(self.path)
        before = self.conversation.snapshot()
        second = self.append_state(notes=[entry])
        added = self.conversation.snapshot()[len(before):]
        self.assertEqual(len(added), 1)
        self.assertEqual(second["previous_result_ref"], first["previous_result_ref"])
        self.assertEqual(second["notes_refs"], first["notes_refs"])
        entry["text"] = "Observed no save; do not claim success"
        third = self.append_state(notes=[entry])
        self.assertNotEqual(third["notes_refs"], first["notes_refs"])
        changed = result().model_copy(update={"summary": "Fresh readback confirmed no successful write"})
        fourth = self.append_state(child=changed, notes=[])
        self.assertNotEqual(fourth["previous_result_ref"], first["previous_result_ref"])
        self.assertEqual(fourth["notes_refs"], [])
        events = [payload(m) for m in self.conversation.messages if m.metadata.get("jevduo_slow_event")]
        self.assertEqual(sum(p.get("event") == "context_protocol" for p in events), 1)
        self.assertEqual(sum(p.get("event") == "notes_removed" for p in events), 1)
        self.assertEqual(sum(p.get("event") == "subagent_result" for p in events), 2)

    def test_controller_budget_override_is_explicit_and_other_changes_remain_complete(self):
        assigned = self.proposed.model_copy(deep=True)
        assigned.contract.budget.max_model_calls = 50
        self.append_state(current_decision=assigned)
        cycles = [payload(m)["history"][0] for m in self.conversation.messages
                  if m.metadata.get("jevduo_slow_event") == "cycle_finished"]
        self.assertEqual(cycles[-1]["decision_ref"]["contract_budget_override"]["max_model_calls"], 50)
        changed = decision(goal="Different exact assignment")
        self.append_state(cycle_id="different", current_decision=changed)
        latest = next(payload(m)["history"][0] for m in reversed(self.conversation.messages)
                      if m.metadata.get("jevduo_slow_event") == "cycle_finished")
        self.assertEqual(latest["decision"], changed.model_dump(mode="json"))
        self.assertNotIn("decision_ref", latest)

    def test_ambiguous_earlier_decision_is_not_referenced(self):
        self.conversation.append(Message(role=Role.ASSISTANT, content=[
            TextBlock(text=decision(goal="Rejected proposal using the same IDs").model_dump_json()),
        ]))
        self.append_state()
        latest = next(payload(m)["history"][0] for m in reversed(self.conversation.messages)
                      if m.metadata.get("jevduo_slow_event") == "cycle_finished")
        self.assertEqual(latest["decision"], self.proposed.model_dump(mode="json"))

    def test_live_references_and_exact_assignment_survive_hard_trim(self):
        # Enough disposable old context that a hard guard must remove history.
        for index in range(10):
            self.conversation.append(event_message({"event": "old", "value": index}))
        state = self.append_state(notes=[{"key": "output", "text": "/tmp/report is not verified"}])
        raw = self.conversation.snapshot()
        messages = self.conversation.snapshot()
        pin_slow_references(messages)
        self.assertEqual(raw, self.conversation.snapshot())
        self.assertEqual(serialize_ump_to_openai_chat_payload(raw),
                         serialize_ump_to_openai_chat_payload(messages))
        segments = UMPContextProjector(policy_profile="generic", recent_iterations=1).project(messages)
        removed = frozenset(s.segment_id for s in segments if s.retention != RetentionClass.PINNED)
        self.assertTrue(removed)
        trimmed = CheckpointHistoryAssembler(None).assemble(segments, None, additionally_removed=removed)
        entries = [payload(m) for m in trimmed if m.role == Role.USER]
        refs = {p["ref"]: p for p in entries if "ref" in p}
        self.assertEqual(refs[state["previous_result_ref"]]["result"], result().model_dump(mode="json"))
        self.assertEqual(refs[state["notes_refs"][0]["ref"]]["entry"]["text"], "/tmp/report is not verified")
        self.assertTrue(any(m.role == Role.ASSISTANT and payload(m) == self.proposed.model_dump(mode="json")
                            for m in trimmed))
        current = [m for m in trimmed if m.metadata.get("jevduo_slow_state")][-1]
        self.assertEqual(current.content[-1].url, "data:image/png;base64,ZnJlc2g=")

    def test_missing_live_reference_fails_before_the_provider_and_legacy_prefix_stays_intact(self):
        self.append_state()
        messages = self.conversation.snapshot()
        messages = [m for m in messages if m.metadata.get("jevduo_slow_event") != "subagent_result"]
        with self.assertRaisesRegex(RuntimeError, "missing a referenced"):
            pin_slow_references(messages)
        legacy = Conversation()
        legacy.append(event_message({"event": "current_state", "previous_result": result().model_dump()}))
        before = legacy.snapshot()
        events = SlowContextEvents(legacy)
        events.current_state({"event": "current_state"}, result=result(), notes=[])
        self.assertEqual(before, legacy.snapshot()[:len(before)])


class SlowCheckpointTest(unittest.IsolatedAsyncioTestCase):
    async def test_summary_keeps_live_values_and_reuses_an_unchanged_checkpoint(self):
        conversation = Conversation()
        conversation.extend([
            Message(role=Role.SYSTEM, content=[TextBlock(text="Fixed task policy")]),
            event_message({"task": "Save report"}),
        ], event_id="initial")
        conversation.append(event_message({"event": "current_state", "observation_id": "before"}))
        conversation.append(Message(role=Role.ASSISTANT, content=[
            TextBlock(text=decision().model_dump_json()),
        ]))
        for index in range(25):
            conversation.append(event_message({"event": "old_diagnostic", "value": index}))
        notes = [{"key": "delivery", "text": "Write is unknown; never replay it"}]
        events = SlowContextEvents(conversation)
        events.append_cycle(CycleRecord(cycle_id="cycle", decision=decision(), subagent_result=result()))
        conversation.append(events.current_state({"event": "current_state", "observation_id": "after"},
                                                 result=result(), notes=notes))
        summaries = []

        class Estimator:
            def __init__(self, **kwargs):
                pass

            def estimate(self, messages, tools):
                count = len(messages) * 1000
                return ContextTokenEstimate(message_tokens=count, tool_tokens=0,
                                            total_tokens=count, estimator="fixture")

        class Summary:
            async def summarize(self, request):
                summaries.append(request)
                return SummaryInvocationResult(
                    invocation_id=request.invocation_id, model="fixture", provider="fixture",
                    document=ContextSummaryDocumentV1(goal="Save report", verified_facts=(SummaryItem(
                        text="Old diagnostics retained in artifacts",
                        source_segment_ids=tuple(s.segment_id for s in request.segments),
                    ),)),
                )

            async def close(self):
                pass

        config = NorthCUAAppConfig(agent_config_path="unused", context_management={
            "mode": "enforce", "budget": {"soft_trigger_ratio": .9, "target_ratio": .65,
                "safety_margin_ratio": 0, "safety_margin_min_tokens": 0, "recent_iterations": 1},
        })
        llm = LLMConfig(model="fixture", max_tokens=1000,
                        base_url="https://example.invalid/v1", api_key="fixture")
        agent = AgentConfig(llm_config=llm, max_context_tokens=30000)
        with tempfile.TemporaryDirectory() as directory:
            async def prepare(step):
                messages = conversation.snapshot()
                pin_slow_references(messages)
                return await prepare_context_messages(
                    app_config=config, agent_kind="orchestrator", user_id="fixture", session_id="fixture",
                    task_epoch="round-1", logical_agent_path="orchestrator", logical_step_id=str(step),
                    round_num=1, messages=messages, tools=[], agent_config=agent,
                    prepared_agent=SimpleNamespace(), primary_llm_config=llm,
                    artifact_root_override=Path(directory),
                )

            with (patch("northcua.agentinfra.context.bootstrap.runtime.NexAUTokenEstimator", Estimator),
                  patch("northcua.agentinfra.context.bootstrap.runtime._build_summary_client",
                        return_value=Summary())):
                first = await prepare(1)
                self.assertTrue(summaries)
                self.assertLess(len(first.messages), len(conversation.messages))
                entries = [payload(m) for m in first.messages if m.metadata.get("jevduo_slow_event")]
                self.assertTrue(any(e.get("result") == result().model_dump(mode="json") for e in entries))
                self.assertTrue(any(e.get("entry") == notes[0] for e in entries))
                count = len(summaries)
                conversation.append(Message(role=Role.ASSISTANT, content=[
                    TextBlock(text="Inspect fresh state"),
                ]))
                events = SlowContextEvents(conversation)
                conversation.append(events.current_state(
                    {"event": "current_state", "observation_id": "new"}, result=result(), notes=notes,
                ))
                second = await prepare(2)
                self.assertEqual(len(summaries), count)
                first_wire = serialize_ump_to_openai_chat_payload(first.messages)
                second_wire = serialize_ump_to_openai_chat_payload(second.messages)
                self.assertEqual(first_wire, second_wire[:len(first_wire)])
