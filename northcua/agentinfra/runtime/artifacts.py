"""Shared artifact-store scaffold for internal agents.

The base classes centralize session directory creation and JSON/text writes so
concrete agent artifact stores only need to define their primary trace path.
"""

from __future__ import annotations

import hashlib
import json
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TypeVar

from northcua.core.config import ArtifactConfig
from northcua.core.config_base import (
    CONTEXT_CHECKPOINT_DIR_NAME,
    CONTEXT_COMPACTION_LOG_NAME,
    CONTEXT_SUMMARY_TRACE_LOG_NAME,
)

TStore = TypeVar("TStore", bound="AgentArtifactStoreBase")
_SESSION_ID_SAFE_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
_SESSION_ID_MAX_LENGTH = 128
_SESSION_ID_DIGEST_LENGTH = 64
_SESSION_ID_ENCODED_MARKER = "~"


def sanitize_session_id(session_id: str) -> str:
    """Return a filesystem/subprocess-safe session identifier.

    Session ids come from CLI and websocket payloads. Keeping only a compact
    filename-safe subset prevents path traversal while preserving ordinary
    safe ids. When normalization or truncation is required, a digest of the
    raw id prevents two distinct sessions from sharing an artifact namespace.

    Args:
        session_id: Raw user/controller supplied session id.

    Returns:
        Filename-safe, collision-resistant id capped to a predictable length.
    """

    raw = str(session_id or "")
    candidate = raw.strip()
    safe = _SESSION_ID_SAFE_CHARS.sub("_", candidate).strip("._-/\\")
    if not safe or safe in {".", ".."}:
        safe = "session"
    if safe == raw and len(safe) <= _SESSION_ID_MAX_LENGTH:
        return safe

    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    prefix_limit = _SESSION_ID_MAX_LENGTH - len(_SESSION_ID_ENCODED_MARKER) - _SESSION_ID_DIGEST_LENGTH - 1
    prefix = safe[:prefix_limit].rstrip("._-") or "session"
    return f"{_SESSION_ID_ENCODED_MARKER}{prefix}-{digest}"


class AgentArtifactStoreBase(ABC):
    """Disk-backed session artifact store base class.

    Attributes:
        root: Session-scoped artifact root directory.
        config: Artifact naming/layout config.
    """

    def __init__(self, root: Path, config: ArtifactConfig, *, session_id: str | None = None) -> None:
        """Initialize a session-scoped artifact store.

        Args:
            root: Session-scoped artifact root directory.
            config: Artifact naming/layout config.
            session_id: Logical protocol/session identifier. Direct store
                construction falls back to the root name for compatibility.
        """

        self.root = root
        self.config = config
        self._session_id = root.name if session_id is None else str(session_id)

    @classmethod
    def for_session(cls, root_dir: str | Path, session_id: str, config: ArtifactConfig) -> "AgentArtifactStoreBase":
        """Create one session-scoped artifact store.

        Args:
            root_dir: Parent artifact root directory.
            session_id: Session identifier used for the child directory.
            config: Artifact naming/layout config.

        Returns:
            Session-scoped artifact store.
        """

        logical_session_id = str(session_id)
        session_root = Path(root_dir) / sanitize_session_id(logical_session_id)
        session_root.mkdir(parents=True, exist_ok=True)
        return cls(session_root, config, session_id=logical_session_id)

    @property
    def session_id(self) -> str:
        """Return the session id encoded by the store root.

        Returns:
            Session identifier.
        """

        return self._session_id

    @property
    def storage_key(self) -> str:
        """Return the collision-resistant filesystem namespace key."""

        return self.root.name

    @property
    def runtime_log_path(self) -> Path:
        """Structured runtime log path shared by all agents.

        Returns:
            Runtime JSONL log path.
        """

        return self.root / self.config.runtime_log_name

    @property
    def llm_trace_log_path(self) -> Path:
        """Full-fidelity model trace JSONL shared by all agents.

        Returns:
            LLM trace JSONL path.
        """

        return self.root / self.config.llm_trace_log_name

    @property
    def context_summary_llm_trace_log_path(self) -> Path:
        """Return the isolated internal summary LLM trace path."""

        return self.root / CONTEXT_SUMMARY_TRACE_LOG_NAME

    @property
    def context_compaction_log_path(self) -> Path:
        """Return the context planning/checkpoint audit path."""

        return self.root / CONTEXT_COMPACTION_LOG_NAME

    @property
    def context_checkpoint_root(self) -> Path:
        """Return the durable per-agent checkpoint directory."""

        return self.root / CONTEXT_CHECKPOINT_DIR_NAME

    @property
    @abstractmethod
    def primary_trace_path(self) -> Path:
        """Return the primary trace-like artifact for this agent.

        Returns:
            Agent-specific primary trace path.
        """

    def append_jsonl(self, path: Path, payload: dict[str, object]) -> None:
        """Append a JSON object as one JSONL line.

        Args:
            path: JSONL file path.
            payload: JSON-serializable record payload.
        """

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.write("\n")

    def rewrite_jsonl(self, path: Path, payloads: list[dict[str, object]]) -> None:
        """Rewrite a JSONL file with an exact ordered payload set.

        Args:
            path: JSONL file path.
            payloads: Ordered JSON-serializable record payloads.
        """

        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for payload in payloads:
                json.dump(payload, handle, ensure_ascii=False)
                handle.write("\n")

    def write_json(self, path: Path, payload: object) -> None:
        """Persist one pretty-printed JSON payload.

        Args:
            path: JSON file path.
            payload: JSON-serializable payload.
        """

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def write_text(self, path: Path, text: str) -> None:
        """Persist a UTF-8 text payload.

        Args:
            path: Text file path.
            text: UTF-8 text payload.
        """

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class ArtifactStoreFactory:
    """Factory for session-scoped artifact stores."""

    @staticmethod
    def create_session_store(
        store_cls: type[TStore],
        *,
        root_dir: str | Path,
        session_id: str,
        config: ArtifactConfig,
    ) -> TStore:
        """Instantiate a typed session-scoped artifact store.

        Args:
            store_cls: Concrete artifact store class.
            root_dir: Parent artifact root directory.
            session_id: Session identifier used for the child directory.
            config: Artifact naming/layout config.

        Returns:
            Typed session-scoped artifact store.
        """

        return store_cls.for_session(root_dir=root_dir, session_id=session_id, config=config)


class BrowserArtifactStoreBase(AgentArtifactStoreBase, ABC):
    """Reserved browser/web artifact-store scaffold for future agents."""
