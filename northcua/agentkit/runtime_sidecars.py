"""Compatibility imports for AgentKit runtime-only sidecar payloads."""

from __future__ import annotations

from northcua_protocol.runtime_sidecars import RUNTIME_SIDECAR_KEY, attach_runtime_sidecar, split_runtime_sidecar

__all__ = ["RUNTIME_SIDECAR_KEY", "attach_runtime_sidecar", "split_runtime_sidecar"]
