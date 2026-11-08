"""History replay helpers for NorthCUA rounds."""

from __future__ import annotations

from northcua.agents.desktop.prompt import HistoryTurn as PromptHistoryTurn
from northcua.core.models import StepLogRecord


class HistoryReplayService:
    """Convert step logs into prompt replay turns."""

    def build_turns(self, step_logs: list[StepLogRecord], history_window_rounds: int) -> list[PromptHistoryTurn]:
        """Transform recent step logs into chronological history turns.

        Args:
            step_logs: Persisted step logs available for replay.
            history_window_rounds: Number of recent steps to retain.

        Returns:
            Prompt history turns in chronological order.
        """

        retained_steps = list(step_logs)[-max(history_window_rounds, 0) :]
        turns: list[PromptHistoryTurn] = []
        for step in retained_steps:
            turns.append(
                PromptHistoryTurn(
                    assistant_text=step.assistant_text,
                    reasoning=step.llm_reasoning,
                    reasoning_encrypted_content=step.llm_reasoning_encrypted_content,
                    assistant_tool_calls=[
                        {
                            "id": call.id,
                            "type": call.type,
                            "function": {
                                "name": call.name,
                                "arguments": call.arguments,
                            },
                        }
                        for call in step.assistant_tool_calls
                    ],
                    tool_results=[
                        {
                            "tool_call_id": result.tool_call_id,
                            "name": result.name,
                            "content": result.content,
                            "raw_image_path": result.raw_image_path,
                            "raw_image_url": result.raw_image_url,
                            "overlay_image_path": result.overlay_image_path,
                            "overlay_image_url": result.overlay_image_url,
                            "image_path": result.image_path,
                            "image_url": result.image_url,
                            "selected_image_kind": result.selected_image_kind.value,
                            "source_image_path": result.source_image_path,
                            "is_error": result.is_error,
                        }
                        for result in step.tool_results
                    ],
                )
            )
        return turns
