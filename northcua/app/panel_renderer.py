"""Shared terminal panel rendering scaffold for NorthCUA CLIs."""

from __future__ import annotations

import re
import textwrap
from abc import ABC, abstractmethod
from pathlib import Path

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class TerminalPanelRendererBase(ABC):
    """Abstract base class for human-friendly terminal panel renderers."""

    width = 88
    reset = "\033[0m"
    bold = "\033[1m"
    dim = "\033[2m"
    cyan = "\033[36m"
    blue = "\033[34m"
    magenta = "\033[35m"
    green = "\033[32m"
    yellow = "\033[33m"
    red = "\033[31m"
    gray = "\033[90m"

    @abstractmethod
    def render_header(self, **kwargs: object) -> str:
        """Render the top-level header for one CLI run.

        Args:
            **kwargs: Renderer-specific header fields.

        Returns:
            Styled terminal panel text.
        """

    @abstractmethod
    def render_footer(self, **kwargs: object) -> str:
        """Render the footer/summary for one CLI run.

        Args:
            **kwargs: Renderer-specific footer fields.

        Returns:
            Styled terminal panel text.
        """

    def render_step_start(self, step_index: int) -> str:
        """Render a lightweight pre-step banner before one round executes.

        Args:
            step_index: One-based step index.

        Returns:
            Styled one-line terminal banner.
        """

        step_label = self._style(f"Step {step_index}", self.bold, self.cyan)
        running = self._style("running...", self.dim, self.blue)
        return f"{self._style('›', self.cyan)} {step_label} {self._style('·', self.gray)} {running}"

    def _panel(self, *, title: str, lines: list[str]) -> str:
        inner_width = self.width - 4
        top = f"┌─ {title} " + "─" * max(0, self.width - self._visible_len(title) - 5) + "┐"
        body = [f"│ {self._pad_visible(line, inner_width)} │" for line in lines]
        bottom = "└" + "─" * (self.width - 2) + "┘"
        return "\n".join([top, *body, bottom])

    def _section_lines(self, title: str, lines: list[str], *, color: str) -> list[str]:
        inner_width = self.width - 4
        section_lines = [self._style(title, self.bold, color)]
        for line in lines:
            wrapped = textwrap.wrap(line, width=max(20, inner_width - 4)) or [""]
            section_lines.extend(f"  {self._format_inline_markdown(part)}" for part in wrapped)
        return section_lines

    def _path_lines(self, title: str, pairs: list[tuple[str, Path | str]], *, color: str) -> list[str]:
        return self._section_lines(
            title,
            [f"{label}: {value}" for label, value in pairs],
            color=color,
        )

    @classmethod
    def _format_inline_markdown(cls, text: str) -> str:
        """Render lightweight markdown-like inline styles for terminal output."""

        text = re.sub(
            r"`([^`]+)`",
            lambda match: cls._style(match.group(1), cls.cyan),
            text,
        )
        text = re.sub(
            r"\*\*([^*]+)\*\*",
            lambda match: cls._style(match.group(1), cls.bold),
            text,
        )
        return text

    @classmethod
    def _status_label(cls, status: str) -> str:
        color = {
            "FINISH": cls.green,
            "FAIL": cls.red,
            "CONTINUE": cls.yellow,
            "SUCCESS": cls.green,
            "ERROR": cls.red,
            "OK": cls.green,
        }.get(status.upper(), cls.cyan)
        return cls._style(status, cls.bold, color)

    @classmethod
    def _tool_status_label(cls, status: str) -> str:
        color = cls.red if status.lower() in {"error", "failed", "fail"} else cls.green
        return cls._style(f"[{status}]", cls.bold, color)

    @classmethod
    def _style(cls, text: str, *styles: str) -> str:
        prefix = "".join(styles)
        if not prefix:
            return text
        return f"{prefix}{text}{cls.reset}"

    @staticmethod
    def _visible_len(text: str) -> int:
        return len(ANSI_RE.sub("", text))

    @classmethod
    def _pad_visible(cls, text: str, width: int) -> str:
        return text + " " * max(0, width - cls._visible_len(text))

    @staticmethod
    def _tool_label(name: str, arguments: str) -> str:
        compact_arguments = " ".join(arguments.split())
        if not compact_arguments:
            return name
        if len(compact_arguments) > 64:
            compact_arguments = compact_arguments[:61] + "..."
        return f"{name} {compact_arguments}"
