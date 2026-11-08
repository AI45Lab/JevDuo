"""Shared command-parameter keys and side-channel helpers for AgentKit adapters."""

from __future__ import annotations

from northcua_protocol.payloads import AGENTKIT_SETTINGS_PARAM, sanitize_agentkit_settings_side_channel

__all__ = ["AGENTKIT_SETTINGS_PARAM", "sanitize_agentkit_settings_side_channel"]
