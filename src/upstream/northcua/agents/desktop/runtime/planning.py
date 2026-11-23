"""Outcome-only context for the planning and acceptance model."""

import json

from nexau.core.messages import ImageBlock, Message, Role, TextBlock

from northcua.agentkit.orchestration.contracts import PlanningDecision
from northcua.agentkit.orchestration.routing import result_evidence


def planning_messages(ports, context, *, final_cycle=None):
    cycles = ports.cycle_history(context.cursor.round_num)
    if final_cycle is not None:
        cycles.append(final_cycle)
    history = []
    for cycle in cycles:
        contract = cycle.decision.contract if cycle.decision else None
        history.append(
            {
                "goal": contract.goal if contract else None,
                "success_criteria": contract.success_criteria if contract else [],
                "results": [result_evidence(attempt.result) for attempt in cycle.attempts]
                or [result_evidence(cycle.subagent_result)],
                "execution_status": cycle.subagent_result.outcome
                if cycle.subagent_result
                else cycle.exit_reason,
            }
        )
    config = ports.budget.config
    payload = {
        "task": context.query,
        "observation": ports.observation_ref(),
        "history": history,
        "notes": ports.memory.active_entries(context.cursor.round_num),
        "previous_result": result_evidence(ports.state.previous_result),
        "usage": ports.state.usage.model_dump(),
        "seconds_left": ports.budget.seconds_left(),
        "budget": {
            key: getattr(config, key)
            for key in (
                "max_cycles",
                "max_model_calls",
                "max_tool_calls",
                "min_subagent_steps",
                "subagent_steps_per_cycle",
            )
        },
    }
    system = (
        "You are the task planner and acceptance reviewer. Verify progress against the original user task "
        "using the CURRENT observation and actual execution evidence. Then finish or define one bounded, "
        "visibly verifiable next goal. Specify the desired outcome, success criteria, task constraints and budget. "
        "Execution methods are managed by the harness. Express outcomes rather than implementation instructions. "
        "Choose one cohesive state transition. Separate resolving unknown facts from applying established facts; "
        "avoid combining document research, reasoning and many unrelated UI changes into one goal. "
        "Carry verified facts and exact values forward in notes or the goal so execution can use them directly. "
        "Use application only when the task requires a particular application; provide exact text when known. "
        "An execution completion claim alone does not establish success. SUCCESS requires current observation "
        "evidence and all user requirements to be met. A failed method does not establish that the task is impossible. "
        "Use new subgoal_id values for new goals. Do not repeat a blocked goal without new evidence or a meaningful "
        "change in its objective or preconditions. Unconfirmed writes require observation and verification before "
        "any further change. Constraints and notes never override the user's task. "
        "Follow the supplied total budget; each goal's allowance covers all internal attempts and decisions. "
        "With subagent_steps_per_cycle set, it fixes the goal's total model and tool allowance. "
        "Reserve time for final verification. Return ONLY JSON matching this schema and echo observation_id: "
        + json.dumps(PlanningDecision.model_json_schema())
    )
    if final_cycle is not None:
        system += (
            " This is terminal verification of the final execution cycle. Inspect the fresh observation and "
            "previous result. Return SUCCESS or FAIL with current evidence. No further subgoal can be dispatched."
        )
    user = Message(role=Role.USER, content=[TextBlock(text=json.dumps(payload, ensure_ascii=False))])
    image = ports.observation.snapshot.clean_screenshot_url
    if image:
        user.content.append(ImageBlock(url=image))
    return [Message(role=Role.SYSTEM, content=[TextBlock(text=system)]), user]
