"""Default provider compatibility probe cases."""

from __future__ import annotations

from dataclasses import dataclass

from nexau.archs.tool.tool import StructuredToolDefinitionLike
from nexau.core.messages import ImageBlock, Message, Role, TextBlock

from northcua.core.json_types import JsonObject

# pyright: reportMissingImports=false


_PROBE_IMAGE_DATA_URL = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


@dataclass(slots=True, frozen=True)
class ProviderProbeCase:
    """One compatibility probe scenario.

    Attributes:
        case_id: Stable identifier used for artifact directory names.
        label: Human-readable probe label.
        messages: Prompt messages sent for the probe.
        tool_definitions: Structured tool definitions included with the probe.
        tool_choice: Provider-native tool choice override.
        extra_request_fields: Extra provider request fields for this case.
        expects_tool_call: Whether success requires a returned tool call.
        expects_json: Whether success requires parseable JSON content.
        is_image_probe: Whether the case exercises image input handling.
    """

    case_id: str
    label: str
    messages: list[Message]
    tool_definitions: list[StructuredToolDefinitionLike]
    tool_choice: JsonObject | str | None = None
    extra_request_fields: JsonObject | None = None
    expects_tool_call: bool = False
    expects_json: bool = False
    is_image_probe: bool = False


def default_probe_cases(api_type: str) -> list[ProviderProbeCase]:
    """Return text/tool/image/JSON probe cases for a provider mode.

    Args:
        api_type: NexAU provider API mode being probed.

    Returns:
        Probe cases with provider-specific tool-choice and structured-output
        request fields.
    """

    return [
        ProviderProbeCase(
            case_id="text_ping",
            label="text-only ping",
            messages=[Message(role=Role.USER, content=[TextBlock(text="Reply with the single word pong.")])],
            tool_definitions=[],
        ),
        ProviderProbeCase(
            case_id="tool_ping",
            label="required tool call",
            messages=[
                Message(
                    role=Role.USER,
                    content=[TextBlock(text="You must call ping_tool with text='pong'. Do not answer normally.")],
                ),
            ],
            tool_definitions=_ping_tool_definition(),
            tool_choice=_tool_choice_for_api(api_type),
            expects_tool_call=True,
        ),
        ProviderProbeCase(
            case_id="image_probe",
            label="image input",
            messages=[
                Message(
                    role=Role.USER,
                    content=[
                        TextBlock(text="Look at the image and reply with a short color description."),
                        ImageBlock(url=_PROBE_IMAGE_DATA_URL, detail="high"),
                    ],
                ),
            ],
            tool_definitions=[],
            is_image_probe=True,
        ),
        ProviderProbeCase(
            case_id="structured_output",
            label="JSON contract",
            messages=[
                Message(
                    role=Role.USER,
                    content=[TextBlock(text='Return strict JSON only: {"ok": true, "label": "pong"}')],
                ),
            ],
            tool_definitions=[],
            expects_json=True,
            extra_request_fields=_structured_output_fields(api_type),
        ),
    ]


def _tool_choice_for_api(api_type: str) -> JsonObject | str:
    """Return provider-native tool-choice forcing for the ping tool probe."""

    if api_type in {"openai_chat_completion", "openai_responses"}:
        return "required"
    return {"type": "auto"}


def _ping_tool_definition() -> list[StructuredToolDefinitionLike]:
    """Return the single tool used to validate provider tool-call payloads."""

    return [
        {
            "name": "ping_tool",
            "description": "Echo a ping payload.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                },
                "required": ["text"],
            },
        }
    ]


def _structured_output_fields(api_type: str) -> JsonObject | None:
    """Return structured-output request fields for providers that support them."""

    if api_type != "openai_chat_completion":
        return None
    return {
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "probe_result",
                "schema": {
                    "type": "object",
                    "properties": {
                        "ok": {"type": "boolean"},
                        "label": {"type": "string"},
                    },
                    "required": ["ok", "label"],
                    "additionalProperties": False,
                },
                "strict": True,
            },
        }
    }
