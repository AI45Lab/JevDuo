"""Web-agent terminal renderer for generic browser tasks."""

from __future__ import annotations

import json
from pathlib import Path

from northcua.agentinfra.runtime.protocol import WebRunResult
from northcua.agents.web.domain.panel_models import WebStepPanelRecord

from .base import AgentTerminalRendererBase


class WebAgentTerminalRenderer(AgentTerminalRendererBase):
    """Render generic web-agent progress panels for the CLI."""

    def render_header(
        self,
        *,
        context: object | None = None,
        session_id: str = "",
        log_dir: Path | str | None = None,
        query: str = "",
        url: str = "",
    ) -> str:
        """Render the terminal header for a web-agent run.

        Args:
            context: Optional generic render context supplied by AgentKit.
            session_id: Session id used when no context is provided.
            log_dir: Artifact directory shown to the user.
            query: User task text.
            url: Optional initial URL opened by the runner.

        Returns:
            Rich-compatible panel text for the start of a web-agent run.
        """

        if context is not None:
            session_id = str(getattr(context, "session_id", session_id))
            log_dir = getattr(context, "log_dir", log_dir)
            query = str(getattr(context, "query", query))
            extras = getattr(context, "extras", {})
            if isinstance(extras, dict):
                url = str(extras.get("url") or url)
        log_dir = Path(str(log_dir or "."))
        lines = [f"Session: {session_id}", f"Trace Dir: {log_dir}", f"Query: {query}"]
        if url:
            lines.append(f"URL: {url}")
        return self._panel(
            title="NorthCUA Web Agent",
            lines=self._section_lines("Run", lines, color=self.blue),
        )

    def render_step(self, step: WebStepPanelRecord) -> str:
        """Render one web-agent step panel.

        Args:
            step: Runtime step projected into the web terminal panel model.

        Returns:
            Rich-compatible panel text containing model text, tools, and state.
        """

        lines: list[str] = []
        if step.reasoning:
            lines.extend(self._section_lines("Reasoning", [step.reasoning], color=self.magenta))
            lines.append("")
        lines.extend(self._section_lines("Assistant", [step.assistant_text or "(empty)"], color=self.green))
        lines.append("")
        observation_lines = [
            f"URL: {step.url or '(unknown)'}",
            f"Observation: {step.observation_summary or '(none)'}",
        ]
        if step.summary:
            observation_lines.append(f"Summary: {step.summary}")
        lines.extend(self._section_lines("Observation", observation_lines, color=self.cyan))
        lines.append("")
        tool_summary = step.tool_summary or "(none)"
        result_status = "success" if step.success else "error"
        lines.extend(
            self._section_lines(
                "Tool",
                [
                    *self._tool_call_lines(step),
                    *self._executed_tool_lines(step),
                    f"Tool Summary: {tool_summary}",
                    f"Result: {result_status}",
                ],
                color=self.yellow if step.success else self.red,
            )
        )
        status = self._status_label(str(step.status.value if hasattr(step.status, "value") else step.status))
        return self._panel(title=f"Step {step.step} [{status}]", lines=lines)

    @staticmethod
    def _tool_call_lines(step: WebStepPanelRecord) -> list[str]:
        if not step.assistant_tool_calls:
            return ["Model call: (none)"]
        lines: list[str] = []
        for call in step.assistant_tool_calls:
            arguments = call.arguments
            try:
                parsed = json.loads(arguments) if arguments else {}
                if isinstance(parsed, dict):
                    arguments = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
            except json.JSONDecodeError:
                pass
            if len(arguments) > 120:
                arguments = f"{arguments[:117]}..."
            lines.append(f"Model call: {call.name} {arguments}".strip())
        return lines

    @staticmethod
    def _executed_tool_lines(step: WebStepPanelRecord) -> list[str]:
        if not step.tools:
            return ["Executed: (none)"]
        return [f"Executed: {tool.tool_name} [{'error' if tool.is_error else 'ok'}]" for tool in step.tools]

    def render_footer(
        self,
        *,
        context: object | None = None,
        result: WebRunResult,
        result_path: Path,
        trace_path: Path,
        log_dir: Path,
    ) -> str:
        """Render the terminal footer for a completed web-agent run.

        Args:
            context: Optional generic render context supplied by AgentKit.
            result: Typed run result returned by the web facade.
            result_path: Final result JSON path.
            trace_path: Step trace JSONL path.
            log_dir: Artifact directory for this run.

        Returns:
            Rich-compatible summary panel text for the completed run.
        """

        del context
        lines = self._section_lines(
            "Summary",
            [
                f"Status: {result.status}",
                f"Terminal: {result.terminal_status or '(unknown)'}",
                f"Steps: {result.step_count}",
                f"Success: {result.success}",
                f"Answer: {result.final_answer or '(none)'}",
                f"Trace Dir: {log_dir}",
                f"Result JSON: {result_path}",
                f"Trace: {trace_path}",
            ],
            color=self.blue,
        )
        lines.extend(self._terminal_decision_lines(result.terminal_decision))
        if result.tool_errors:
            lines.extend(self._section_lines("Tool Errors", list(result.tool_errors), color=self.red))
        if result.hit_step_limit:
            lines.extend(self._section_lines("Warning", ["Reached step limit before done."], color=self.yellow))
        return self._panel(title="NorthCUA Web Agent Summary", lines=lines)
