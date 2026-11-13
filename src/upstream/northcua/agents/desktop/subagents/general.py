"""General desktop fallback inspired by OSWorld's Kimi GUI/hybrid agent."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

from nexau.core.messages import ImageBlock, Message, Role, TextBlock, ToolResultBlock, ToolUseBlock
from PIL import Image

from northcua.agentkit.orchestration.contracts import (
    SubagentRequest,
    SubagentResult,
    SubagentRuntime,
    SubagentSpec,
)
from northcua.core.config import DesktopPolicyConfig
from northcua.core.config_base import GeneralSubagentConfig

from .general_environment import TOOL_MODELS, GeneralEnvironment


def observation_message(observation: dict) -> Message:
    path = Path(observation["screenshot_path"])
    with Image.open(path) as image:
        width, height = image.size
        mime = Image.MIME[image.format]
        image.verify()
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return Message(
        role=Role.USER,
        content=[
            TextBlock(text=json.dumps({"observation": observation, "screen_size": [width, height]})),
            ImageBlock(url=f"data:{mime};base64,{encoded}"),
        ],
    )


def active_screenshots(messages: list[Message]) -> list[Message]:
    """Keep three recent images and all actual assistant/tool result pairs."""
    image_messages = [
        index
        for index, message in enumerate(messages)
        if any(isinstance(block, ImageBlock) for block in message.content)
    ]
    remove = set(image_messages[:-3])
    return [
        message.model_copy(
            update={"content": [block for block in message.content if not isinstance(block, ImageBlock)]}
        )
        if index in remove
        else message
        for index, message in enumerate(messages)
    ]


class GeneralSubagent:
    def __init__(self, config: GeneralSubagentConfig, desktop: DesktopPolicyConfig):
        self.config = config
        self.environment = GeneralEnvironment(desktop, mode=config.mode)
        capabilities = ("screenshot_gui", "coordinate_actions")
        if config.mode == "hybrid":
            capabilities += ("shell", "filesystem")
        self.spec = SubagentSpec(
            agent_id="general",
            description=(
                f"General {config.mode} fallback for tasks outside gui_jev's semantic GUI operations: "
                "canvas/drawing, coordinate clicking, dragging, hotkeys and text. "
                "Requires allowed_tools=[execute_pyautogui_code]; "
                + (
                    "also allow run_command for file inspection, Bash/Python scripts and result verification "
                    "inside the current task VM. "
                    if config.mode == "hybrid"
                    else ""
                )
                + "agent_input must be {}. Use gui_jev for supported semantic GUI operations; "
                "delegate a bounded recovery subgoal here when needed."
            ),
            capabilities=capabilities,
            required_tools=("execute_pyautogui_code",),
        )

    async def run(self, request: SubagentRequest, runtime: SubagentRuntime) -> SubagentResult:
        tools = []
        for name, (model, description) in TOOL_MODELS.items():
            if name == "run_command" and self.config.mode != "hybrid":
                continue
            if name in {"execute_pyautogui_code", "run_command", "wait_for_state"}:
                if name not in request.contract.allowed_tools:
                    continue
            tools.append({"name": name, "description": description, "parameters": model.model_json_schema()})
        system = (
            "You are the general desktop subagent. Complete only this bounded subgoal, respecting the "
            "original "
            "task, success criteria and constraints. Work in the current task VM, using the requested native "
            "application when its final state matters. Use the supplied screenshots as visual evidence. "
            "Coordinates are absolute pixels of the ORIGINAL screenshot (see screen_size), regardless of any "
            "normalized-coordinate convention used by the parent. Return exactly ONE primary native tool "
            "call per turn: an action, take_screenshot, or finish_task. Only an empty take_screenshot "
            "after one action may be a second call. "
            "GUI code accepts straight-line pyautogui calls with literal arguments, plus pyperclip.copy and "
            "time.sleep; no loops, assignments, arbitrary Python or nested calls. Batch only actions whose "
            "targets do not depend on an unseen intermediate screen. Use run_command for Bash/Python/file "
            "operations when that tool is available. Real tool output and a fresh screenshot follow "
            "each action automatically. An appended empty take_screenshot request shares that same "
            "fresh capture; never request a second action in the same turn. "
            "A nonzero exit code or truncated output does not establish success. Never repeat a write after "
            "unknown delivery or timeout. finish_task reports a SUBGOAL claim; the parent verifies it. "
            "If blocked, finish with failure and explain the exact blocker. Reserve time for verification."
        )
        messages = [
            Message(role=Role.SYSTEM, content=[TextBlock(text=system)]),
            Message(role=Role.USER, content=[TextBlock(text=request.model_dump_json())]),
            observation_message(request.observation),
        ]
        observation = request.observation
        tool_results = []

        def result(outcome: str, reason: str, summary: str, *, unresolved: bool = False):
            return SubagentResult(
                subgoal_id=request.contract.subgoal_id,
                attempt_id=request.attempt_id,
                outcome=outcome,
                exit_reason=reason,
                summary=json.dumps(
                    {
                        "message": summary,
                        "unresolved_delivery": unresolved,
                        "tool_results": tool_results[-3:],
                    },
                    ensure_ascii=False,
                ),
                evidence_refs=[observation["observation_id"]],
                criterion_claims=request.contract.success_criteria if outcome == "achieved" else [],
                last_observation=observation,
            )

        try:
            while runtime.remaining_budget().max_model_calls:
                remaining = runtime.remaining_budget()
                ending = remaining.max_model_calls == 1 or remaining.max_tool_calls == 0
                if ending:
                    messages.append(Message(role=Role.USER, content=[TextBlock(text=(
                        "Your execution budget is ending. Call finish_task now with an explicit handoff: "
                        "verified findings, exact artifacts, completed writes, unresolved delivery, "
                        "remaining work and current GUI position. Use failure if the subgoal is incomplete. "
                        "Do not perform another action or repeat research."
                    ))]))
                surface = [t for t in tools if t["name"] == "finish_task"] if ending else tools
                reply = await runtime.infer_tools(active_screenshots(messages), surface)
                messages.append(reply)
                calls = [block for block in reply.content if isinstance(block, ToolUseBlock)]
                if not calls:
                    return result("blocked", "no_tool_call", "Model returned no native tool call")
                appended_screenshot = None
                if (not ending and len(calls) == 2
                        and calls[0].name in {"execute_pyautogui_code", "run_command", "wait_for_state"}
                        and calls[1].name == "take_screenshot" and calls[0].id != calls[1].id):
                    try:
                        TOOL_MODELS["take_screenshot"][0].model_validate(calls[1].input)
                    except ValueError:
                        pass
                    else:
                        appended_screenshot = calls[1]
                if len(calls) != 1 and appended_screenshot is None:
                    # One message per receipt: the Chat serializer otherwise keeps
                    # only the first ToolResultBlock and loses the other feedback.
                    messages.extend(
                        Message(
                            role=Role.TOOL,
                            content=[
                                ToolResultBlock(
                                    tool_use_id=call.id,
                                    content=json.dumps({
                                        "ok": False, "delivery": "not_sent",
                                        "error": "invalid_tool_call_count", "call_count": len(calls),
                                        "message": "Not executed: return exactly one tool call. "
                                                   "Screenshots follow execution automatically. "
                                                   "This is a model protocol error, not a transport failure.",
                                    }),
                                    is_error=True,
                                ),
                            ],
                        )
                        for call in calls
                    )
                    continue
                call = calls[0]
                try:
                    if call.name not in {item["name"] for item in surface}:
                        raise ValueError("tool_not_available")
                    arguments = TOOL_MODELS[call.name][0].model_validate(call.input)
                    if call.name == "finish_task":
                        return result(
                            "achieved" if arguments.status == "success" else "yielded" if ending else "blocked",
                            "criteria_claimed" if arguments.status == "success" else
                            "model_budget_exhausted" if ending else "model_reported_blocker",
                            arguments.answer,
                        )
                    if call.name == "take_screenshot":
                        output = {"ok": True, "observation_requested": True}
                    else:
                        output = await runtime.execute(call.name, arguments.model_dump())
                except (ValueError, SyntaxError) as error:
                    output = {"ok": False, "error": str(error), "delivery": "not_sent"}
                messages.append(
                    Message(
                        role=Role.TOOL,
                        content=[
                            ToolResultBlock(
                                tool_use_id=call.id,
                                content=json.dumps(output, ensure_ascii=False),
                                is_error=not output.get("ok", False),
                            )
                        ],
                    )
                )
                evidence = dict(output)
                for key in ("stdout", "stderr"):
                    if isinstance(evidence.get(key), str) and len(evidence[key]) > 4096:
                        evidence[key] = evidence[key][:4096]
                        evidence[f"{key}_truncated"] = True
                tool_results.append({"tool_call_id": call.id, "tool": call.name, "result": evidence})
                if output.get("unsafe_to_replay"):
                    if appended_screenshot is not None:
                        messages.append(Message(role=Role.TOOL, content=[ToolResultBlock(
                            tool_use_id=appended_screenshot.id, is_error=True,
                            content=json.dumps({"ok": False, "delivery": "not_sent",
                                                "error": "action_requires_parent_reconciliation"}),
                        )]))
                    # Do not spend another model call deciding whether to retry a
                    # possibly delivered action. Return the exact feedback to Slow.
                    return result(
                        "blocked",
                        "unsafe_to_replay",
                        json.dumps(output, ensure_ascii=False),
                        unresolved=True,
                    )
                observation = await runtime.observe()
                if appended_screenshot is not None:
                    # Acknowledge both IDs before the next user/image message.
                    # Only the unique action was executed; use its ordinary
                    # post-action observation instead of another capture.
                    messages.append(Message(role=Role.TOOL, content=[ToolResultBlock(
                        tool_use_id=appended_screenshot.id,
                        content=json.dumps({"ok": True, "observation_requested": True,
                                            "observation_id": observation["observation_id"]}),
                    )]))
                messages.append(observation_message(observation))
            return result("yielded", "model_budget_exhausted", "Return to parent for fresh verification")
        except (Exception, asyncio.CancelledError) as error:
            # The dispatcher accepts this partial result, including through
            # asyncio.timeout's cancellation cause. Cover tool/observation and
            # budget-check failures as well as model waits; never replay here.
            cancelled = isinstance(error, asyncio.CancelledError)
            error.subagent_result = result(
                "yielded" if cancelled else "failed", type(error).__name__,
                "Execution interrupted; verify retained partial results"
                if cancelled or not str(error) else str(error),
            )
            raise
