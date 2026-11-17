"""Native tool inference through the existing model, without inline execution."""

from __future__ import annotations

import json

from nexau.core.messages import Message, Role, TextBlock, ToolUseBlock
from northcua_runtime_shared.tool_contracts import ToolContract, ToolPlacement

from northcua.agentkernel.tools.projection import contract_to_nexau_tool


def _reject_inline(*args, **kwargs):
    raise RuntimeError("Subagent tools must execute through the scoped runtime")


def configure_tools(config, definitions: list[dict]) -> None:
    config.tools = [
        contract_to_nexau_tool(
            ToolContract(
                tool_id=f"general.{item['name']}",
                public_name=item["name"],
                description=item["description"],
                json_schema=item["parameters"],
                namespace="desktop.general",
                agent_kind="general",
                placement=ToolPlacement.LOCAL_HANDLER,
                source=__name__,
            ),
            implementation=_reject_inline,
        )
        for item in definitions
    ]
    config.tool_call_mode = "structured"
    config.llm_config.extra_params["parallel_tool_calls"] = False
    config.max_iterations = 1
    config.before_tool_hooks = [_reject_inline]


def assistant_message(result) -> Message:
    state = result.trace_state
    if state.tool_executions:
        raise RuntimeError("Subagent model attempted inline tool execution")
    content = []
    metadata = {}
    if state.reasoning or state.reasoning_encrypted_content:
        item = {"type": "reasoning", "summary": []}
        if state.reasoning:
            item["summary"] = [{"type": "summary_text", "text": state.reasoning}]
        if state.reasoning_encrypted_content:
            item["encrypted_content"] = state.reasoning_encrypted_content
        metadata["reasoning"] = [item]
    if state.assistant_text or result.response_text:
        content.append(TextBlock(text=state.assistant_text or result.response_text))
    for call in state.assistant_tool_calls:
        arguments = json.loads(call.arguments)
        if not isinstance(arguments, dict) or not call.id:
            raise ValueError("Invalid subagent tool-call arguments or identity")
        content.append(ToolUseBlock(id=call.id, name=call.name, input=arguments, raw_input=call.arguments))
    return Message(role=Role.ASSISTANT, content=content, metadata=metadata)
