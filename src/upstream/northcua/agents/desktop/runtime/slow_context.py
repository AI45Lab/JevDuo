"""Lossless, append-only references for the Slow agent's incremental context."""

from __future__ import annotations

import json

from nexau.core.messages import Message, Role, TextBlock

from northcua.agentkit.orchestration.contracts import CycleRecord, SlowDecision, SubagentResult
from northcua.agentkit.orchestration.conversation import Conversation

_EVENT = "jevduo_slow_event"
_REF = "jevduo_slow_ref"
_STATE = "jevduo_slow_state"
_PROTOCOL = (
    "Results and note updates are supplied once with a conversation-local ref. "
    "cycle_finished and current_state reference those records. "
    "decision_ref identifies the latest earlier assistant decision with its observation_id, "
    "action and subgoal_id; "
    "contract_budget_override, when present, is the actual assigned budget. "
    "current_state.notes_refs lists all active notes; an absent key is inactive. "
    "previous_result_ref names the child result to verify using the current observation. "
    "Referenced current results and active notes remain available through context compaction."
)


def _payload(message: Message) -> dict:
    return json.loads(next(block.text for block in message.content if isinstance(block, TextBlock)))


def _message(payload: dict, *, kind: str, ref: str | None = None) -> Message:
    metadata = {_EVENT: kind}
    if ref is not None:
        metadata[_REF] = ref
    return Message(
        role=Role.USER,
        content=[
            TextBlock(
                text=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            )
        ],
        metadata=metadata,
    )


class SlowContextEvents:
    """Rebuild reference indexes from durable messages without changing their prefix."""

    def __init__(self, conversation: Conversation) -> None:
        self.conversation = conversation
        self.results: dict[str, tuple[dict, str]] = {}
        self.notes: dict[str, tuple[dict, str]] = {}
        self.decisions: list[SlowDecision] = []
        for message in conversation.messages:
            if message.role == Role.ASSISTANT:
                try:
                    self.decisions.append(SlowDecision.model_validate(_payload(message)))
                except (ValueError, StopIteration):
                    pass  # Rejected raw decisions remain in history, but cannot supply a reference.
            kind = message.metadata.get(_EVENT)
            if kind == "subagent_result":
                payload = _payload(message)
                self.results[payload["result"]["attempt_id"]] = (payload["result"], payload["ref"])
            elif kind == "note_updated":
                payload = _payload(message)
                self.notes[payload["entry"]["key"]] = (payload["entry"], payload["ref"])
            elif kind == "notes_removed":
                for key in _payload(message)["keys"]:
                    self.notes.pop(key, None)
        protocol = _message({"event": "context_protocol", "instructions": _PROTOCOL}, kind="protocol")
        protocol.metadata["northcua_working_memory"] = True
        conversation.append(protocol, event_id="slow-context-protocol:v2")

    def result_ref(self, result: SubagentResult | None) -> str | None:
        if result is None:
            return None
        payload = result.model_dump(mode="json")
        previous = self.results.get(result.attempt_id)
        if previous is not None and previous[0] == payload:
            return previous[1]
        ref = f"slow-event-{self.conversation.sequence + 1}"
        self.conversation.append(
            _message(
                {"event": "subagent_result", "ref": ref, "result": payload},
                kind="subagent_result",
                ref=ref,
            )
        )
        self.results[result.attempt_id] = (payload, ref)
        return ref

    def _decision_ref(self, decision: SlowDecision) -> dict | None:
        ref: dict = {"observation_id": decision.observation_id, "action": decision.action}
        if decision.contract is not None:
            ref["subgoal_id"] = decision.contract.subgoal_id
        for previous in reversed(self.decisions):
            if previous.observation_id != decision.observation_id or previous.action != decision.action:
                continue
            if (previous.contract.subgoal_id if previous.contract else None) != ref.get("subgoal_id"):
                continue
            if previous == decision:
                return ref
            # Dispatch raises a proposed child budget to the configured minimum.
            # Represent only that exact controller change; all other differences
            # require the complete recovered decision instead of an ambiguous ref.
            if previous.contract is not None and decision.contract is not None:
                proposed = decision.model_copy(deep=True)
                assert proposed.contract is not None
                proposed.contract.budget = previous.contract.budget
                if proposed == previous:
                    ref["contract_budget_override"] = decision.contract.budget.model_dump(mode="json")
                    return ref
            return None
        return None

    def append_cycle(self, cycle: CycleRecord) -> None:
        if f"cycle:{cycle.cycle_id}" in self.conversation.events:
            return
        payload = cycle.model_dump(mode="json", exclude={"subagent_result"})
        payload["subagent_result_ref"] = self.result_ref(cycle.subagent_result)
        for name in ("decision", "final_verification"):
            decision = getattr(cycle, name)
            ref = self._decision_ref(decision) if decision is not None else None
            if ref is not None:
                del payload[name]
                payload[f"{name}_ref"] = ref
        self.conversation.append(
            _message(
                {"event": "cycle_finished", "history": [payload]},
                kind="cycle_finished",
            ),
            event_id=f"cycle:{cycle.cycle_id}",
        )

    def notes_refs(self, entries: list[dict[str, str]]) -> list[dict[str, str]]:
        active = {entry["key"] for entry in entries}
        removed = [key for key in self.notes if key not in active]
        if removed:
            self.conversation.append(
                _message(
                    {"event": "notes_removed", "keys": removed},
                    kind="notes_removed",
                )
            )
            for key in removed:
                del self.notes[key]
        refs = []
        for entry in entries:
            key = entry["key"]
            previous = self.notes.get(key)
            if previous is None or previous[0] != entry:
                ref = f"slow-event-{self.conversation.sequence + 1}"
                self.conversation.append(
                    _message(
                        {"event": "note_updated", "ref": ref, "entry": entry},
                        kind="note_updated",
                        ref=ref,
                    )
                )
                self.notes[key] = (dict(entry), ref)
            refs.append({"key": key, "ref": self.notes[key][1]})
        return refs

    def current_state(self, payload: dict, *, result: SubagentResult | None, notes: list[dict[str, str]]) -> Message:
        payload = {
            **payload,
            "previous_result_ref": self.result_ref(result),
            "notes_refs": self.notes_refs(notes),
        }
        message = _message(payload, kind="current_state")
        message.metadata[_STATE] = True
        return message


def pin_slow_references(messages: list[Message]) -> None:
    """Keep live referenced values verbatim; only caller-owned metadata is changed."""
    state = next((message for message in reversed(messages) if message.metadata.get(_STATE)), None)
    if state is None:
        return  # Legacy states already carry their current result/notes in the pinned observation.
    payload = _payload(state)
    required = {entry["ref"] for entry in payload["notes_refs"]}
    if payload["previous_result_ref"] is not None:
        required.add(payload["previous_result_ref"])
    found = set()
    current_result = None
    decision_ref = None
    contract_inlined = False
    for message in messages:
        ref = message.metadata.get(_REF)
        if ref in required:
            message.metadata["northcua_working_memory"] = True
            found.add(ref)
            if ref == payload["previous_result_ref"]:
                current_result = _payload(message)["result"]
        if message.metadata.get(_EVENT) == "cycle_finished":
            cycle = _payload(message)["history"][0]
            if (
                payload["previous_result_ref"] is not None
                and cycle["subagent_result_ref"] == payload["previous_result_ref"]
            ):
                message.metadata["northcua_working_memory"] = True
                decision_ref = cycle.get("decision_ref")
                contract_inlined = cycle.get("decision") is not None
    if found != required:
        raise RuntimeError("Slow context is missing a referenced current result or active note")
    if current_result is None or contract_inlined:
        return
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role != Role.ASSISTANT:
            continue
        try:
            decision = SlowDecision.model_validate(_payload(message))
        except (ValueError, StopIteration):
            continue
        if decision.contract is None or decision.contract.subgoal_id != current_result["subgoal_id"]:
            continue
        if decision_ref is not None and (
            decision.observation_id != decision_ref["observation_id"] or decision.action != decision_ref["action"]
        ):
            continue
        # The projector groups an assistant with its preceding user observation.
        # Pin both separately so the assigned contract remains exact at compaction.
        message.metadata["northcua_working_memory"] = True
        if index and messages[index - 1].role == Role.USER:
            messages[index - 1].metadata["northcua_working_memory"] = True
        break
