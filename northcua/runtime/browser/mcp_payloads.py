"""Payload helpers shared by the Playwright browser MCP server."""

from __future__ import annotations

from typing import Sequence
from urllib.parse import urlparse

from fastmcp.exceptions import ToolError

from .backend import BrowserElementInfo, BrowserSnapshotResult, BrowserTabInfo


def _render_tabs_markdown(tabs: Sequence[BrowserTabInfo]) -> str:
    """Render browser tabs in the tool's markdown response format."""

    if not tabs:
        return "No browser tabs are open."
    lines: list[str] = []
    for tab in tabs:
        marker = "*" if tab.is_active else " "
        lines.append(f"- [{tab.index}] {marker} {tab.title or '(untitled)'} - {tab.url or '(blank)'}")
    return "\n".join(lines)


def _render_snapshot_markdown(
    snapshot: BrowserSnapshotResult,
    ref_map: dict[str, BrowserElementInfo],
) -> str:
    """Render a browser snapshot and element ref map as markdown."""

    lines = [
        "# Browser Snapshot",
        f"- URL: {snapshot.url or '(blank)'}",
        f"- Title: {snapshot.title or '(untitled)'}",
    ]
    for ref, element in ref_map.items():
        indent = "  " * max(element.depth, 0)
        label = f'{element.role} "{element.name}"' if element.name else element.role
        lines.append(f"{indent}- [{ref}] {label} <{element.tag_name}>")
    return "\n".join(lines)


def _normalize_url(url: str) -> str:
    """Normalize user-provided URLs for browser navigation."""

    value = str(url or "").strip()
    if not value:
        raise ToolError("URL is required.")
    if urlparse(value).scheme:
        return value
    if value.startswith("localhost"):
        return "http://" + value
    return "https://" + value
