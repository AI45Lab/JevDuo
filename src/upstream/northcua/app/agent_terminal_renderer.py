"""Shared terminal-renderer scaffold for typed agent step events."""

from __future__ import annotations

import re
import textwrap
import unicodedata
from collections.abc import Mapping

from northcua.app.panel_renderer import ANSI_RE, TerminalPanelRendererBase


class AgentTerminalRendererBase(TerminalPanelRendererBase):
    """Shared renderer helpers for agent terminal UIs."""

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
            wrapped = self._wrap_plain_text(line, width=max(20, inner_width - 4))
            section_lines.extend(f"  {self._format_inline_markdown(part)}" for part in wrapped)
        return section_lines

    def _section_wrapped_text(self, text: str, *, indent: str) -> list[str]:
        wrapped = self._wrap_text(text, width=max(20, self.width - len(indent) - 4)) or [""]
        lines: list[str] = []
        for index, part in enumerate(wrapped):
            prefix = indent if index == 0 else " " * len(indent)
            lines.append(f"{prefix}{self._format_inline_markdown(part)}")
        return lines

    def _terminal_decision_lines(
        self,
        decision: object,
        *,
        title: str = "Decision",
        color: str | None = None,
    ) -> list[str]:
        """Render shared agent terminal-decision metadata when present.

        Args:
            decision: Mapping payload, ``AgentTerminalDecision``-like object, or
                an empty value.
            title: Section title shown above the decision fields.
            color: Optional ANSI color override.

        Returns:
            Formatted section lines, or an empty list when no decision is
            available.
        """

        payload = self._terminal_decision_payload(decision)
        if not payload:
            return []
        detail_lines: list[str] = []
        for label, key in (
            ("Action", "action"),
            ("Reason", "reason"),
            ("Success", "success"),
            ("Retryable", "retryable"),
            ("Summary", "summary"),
            ("Answer", "final_answer"),
        ):
            value = payload.get(key)
            if value not in (None, "", {}, []):
                detail_lines.append(f"{label}: {value}")
        return self._section_lines(title, detail_lines or ["(empty)"], color=color or self.cyan)

    @staticmethod
    def _terminal_decision_payload(decision: object) -> Mapping[str, object]:
        if isinstance(decision, Mapping):
            return decision
        to_json = getattr(decision, "to_json", None)
        if callable(to_json):
            value = to_json()
            if isinstance(value, Mapping):
                return value
        return {}

    def _wrap_text(self, text: str, *, width: int) -> list[str]:
        return textwrap.wrap(text, width=width)

    @staticmethod
    def _visible_len(text: str) -> int:
        visible = 0
        for char in ANSI_RE.sub("", text):
            if unicodedata.combining(char):
                continue
            if unicodedata.east_asian_width(char) in {"W", "F"}:
                visible += 2
            else:
                visible += 1
        return visible

    @staticmethod
    def _wrap_plain_text(text: str, *, width: int) -> list[str]:
        normalized = " ".join(text.split())
        if not normalized:
            return [""]
        limit = max(8, width)
        lines: list[str] = []
        current = ""

        for token in re.findall(r"\S+\s*", normalized):
            token_text = token.rstrip()
            if not token_text:
                continue
            candidate = f"{current} {token_text}".strip() if current else token_text
            if AgentTerminalRendererBase._visible_len(candidate) <= limit:
                current = candidate
                continue
            if current:
                lines.append(current)
                current = ""
            if AgentTerminalRendererBase._visible_len(token_text) <= limit:
                current = token_text
                continue
            current = AgentTerminalRendererBase._split_long_token(token_text, limit, lines)

        if current:
            lines.append(current)
        return lines or [""]

    @staticmethod
    def _split_long_token(token: str, width: int, lines: list[str]) -> str:
        current = ""
        for char in token:
            candidate = current + char
            if AgentTerminalRendererBase._visible_len(candidate) > width and current:
                lines.append(current)
                current = char
            else:
                current = candidate
        return current
