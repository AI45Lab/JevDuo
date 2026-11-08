"""Typed controller artifact records used during materialization."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class ControllerArtifactRow:
    """One artifact record loaded from the controller SQLite store.

    Attributes:
        original_artifact_id: Producer-side artifact id before controller
            storage rewrites.
        source: Controller source label, such as ``task_output`` or
            ``command_output``.
        kind: High-level artifact category persisted by the controller.
        mime_type: MIME type used to select a materialized file extension.
        payload: Decoded raw artifact bytes.
    """

    original_artifact_id: str
    source: str
    kind: str
    mime_type: str
    payload: bytes
