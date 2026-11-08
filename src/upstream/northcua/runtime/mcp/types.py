"""Shared typed aliases for MCP JSON-style payload boundaries.

These aliases intentionally describe the existing wire shapes without adding
runtime validation or changing payload keys. Keep them broad unless a caller
already enforces a stricter schema at the MCP boundary.
"""

from __future__ import annotations

from typing import TypeAlias

JSONPayload: TypeAlias = dict[str, object]
MCPServerConfigPayload: TypeAlias = dict[str, object]
MCPToolInputSchema: TypeAlias = dict[str, object]
MCPToolResultPayload: TypeAlias = dict[str, object]
DesktopNativeActionResultPayload: TypeAlias = dict[str, object]
DesktopNativeDataURL: TypeAlias = str
DesktopNativeScreenshotPayload: TypeAlias = dict[str, object]
DesktopNativeMetadataPayload: TypeAlias = dict[str, object]
