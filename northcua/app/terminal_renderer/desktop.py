"""Desktop terminal renderer for task progress and artifact summaries."""

from __future__ import annotations

from pathlib import Path

from northcua.agentinfra.runtime.protocol import DesktopRunResult
from northcua.app.task_runner import TaskRunResult
from northcua.core.models import StepLogRecord

from .base import AgentTerminalRendererBase


class NorthCUATerminalRenderer(AgentTerminalRendererBase):
    """Render task headers, step blocks, and footers for desktop CUA runs."""

    def render_header(
        self,
        *,
        context: object | None = None,
        session_id: str = "",
        log_dir: Path | str | None = None,
        query: str = "",
    ) -> str:
        """Render the terminal header for a desktop task run.

        Args:
            session_id: Active run session identifier.
            log_dir: Artifact directory for the run.
            query: User task/query text.

        Returns:
            Styled terminal panel text.
        """

        if context is not None:
            session_id = str(getattr(context, "session_id", session_id))
            log_dir = getattr(context, "log_dir", log_dir)
            query = str(getattr(context, "query", query))
        log_dir = Path(str(log_dir or "."))
        return self._panel(
            title="NorthCUA Task",
            lines=self._section_lines(
                "Run",
                [
                    f"Session: {session_id}",
                    f"Trace Dir: {log_dir}",
                    f"Query: {query}",
                ],
                color=self.blue,
            ),
        )

    def render_step(self, step_log: StepLogRecord) -> str:
        """Render a single desktop step record.

        Args:
            step_log: Structured step log emitted by the desktop agent loop.

        Returns:
            Styled terminal panel text.
        """

        thought = self._thought_text(step_log)
        lines: list[str] = []
        lines.extend(self._section_lines("Thought", [thought or "(none)"], color=self.magenta))
        lines.append("")
        lines.extend(
            self._section_lines(
                "Assistant",
                [f"↳ {step_log.assistant_text or '(empty)'}"],
                color=self.green,
            )
        )
        lines.append("")
        lines.extend(self._tool_lines(step_log))
        status = self._status_label(step_log.status.value)
        return self._panel(title=f"Step {step_log.session_step} [{status}]", lines=lines)

    @staticmethod
    def _thought_text(step_log: StepLogRecord) -> str:
        reasoning = (step_log.llm_reasoning or step_log.llm_trace.reasoning or "").strip()
        if reasoning:
            return reasoning
        thinking = (step_log.llm_thinking or step_log.llm_trace.thinking or "").strip()
        if thinking:
            return thinking
        return ""

    def render_footer(
        self,
        *,
        context: object | None = None,
        result: TaskRunResult | DesktopRunResult,
        step_log_path: Path,
        runtime_log_path: Path,
        evaluation_log_path: Path,
    ) -> str:
        """Render the terminal footer for a completed desktop task run.

        Args:
            result: Runtime result from local or facade execution.
            step_log_path: Path to the per-step JSONL log.
            runtime_log_path: Path to the runtime event log.
            evaluation_log_path: Path to the evaluation log.

        Returns:
            Styled terminal panel text.
        """

        del context
        if isinstance(result, DesktopRunResult):
            status = result.terminal_status or "UNKNOWN"
            step_count = result.step_count
            hit_step_limit = result.hit_step_limit
            terminal_decision = result.terminal_decision
        else:
            status = result.terminal_status.value if result.terminal_status is not None else "UNKNOWN"
            step_count = len(result.steps)
            hit_step_limit = result.hit_step_limit
            terminal_decision = result.steps[-1].terminal_decision if result.steps else {}
        lines = self._section_lines(
            "Summary",
            [
                f"Terminal: {status}",
                f"Steps: {step_count}",
                f"Trace Dir: {step_log_path.parent}",
                f"Step Log: {step_log_path}",
                f"Runtime Log: {runtime_log_path}",
                f"Eval: {evaluation_log_path}",
            ],
            color=self.blue,
        )
        lines.extend(self._terminal_decision_lines(terminal_decision))
        if hit_step_limit:
            lines.extend(
                self._section_lines(
                    "Warning",
                    ["Reached step limit before terminal state."],
                    color=self.yellow,
                )
            )
        return self._panel(title="NorthCUA Summary", lines=lines)

    def _tool_lines(self, step_log: StepLogRecord) -> list[str]:
        lines = [self._style("Tools", self.bold, self.cyan)]
        results_by_call_id = {result.tool_call_id: result for result in step_log.tool_results}
        if not step_log.assistant_tool_calls:
            lines.append(f"  {self._style('(none)', self.gray)}")
            return lines
        for call in step_log.assistant_tool_calls:
            call_label = self._tool_label(call.name, call.arguments)
            lines.append(f"  {self._style('•', self.cyan)} {self._format_inline_markdown(call_label)}")
            result = results_by_call_id.get(call.id)
            if result is None:
                result_label = self._style("Result", self.bold, self.yellow)
                lines.append(f"    {self._style('└─', self.gray)} {result_label} {self._style('[pending]', self.gray)}")
                continue
            result_text = result.content or "(none)"
            status = self._tool_status_label(result.status)
            result_label = self._style("Result", self.bold, self.yellow)
            lines.extend(self._section_wrapped_text(f"{result_label} {status}: {result_text}", indent="    "))
        assistant_call_ids = {call.id for call in step_log.assistant_tool_calls}
        unmatched = [result for result in step_log.tool_results if result.tool_call_id not in assistant_call_ids]
        for result in unmatched:
            result_label = self._style("Result", self.bold, self.yellow)
            lines.extend(
                self._section_wrapped_text(
                    f"{result.name} -> {result_label} {self._tool_status_label(result.status)}: {result.content or '(none)'}",
                    indent="  ",
                )
            )
        return lines

    @staticmethod
    def _tool_label(name: str, arguments: str) -> str:
        compact_arguments = " ".join(arguments.split())
        if not compact_arguments:
            return name
        if len(compact_arguments) > 64:
            compact_arguments = compact_arguments[:61] + "..."
        return f"{name} {compact_arguments}"
