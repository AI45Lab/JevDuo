"""Session-scoped runtime state for the generic web agent."""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Literal

from northcua.core.config import WebAgentConfig

from ..artifacts import WebArtifactStore
from ..backend import WebActionBackend
from ..domain.evaluation_artifacts import (
    WebToolArtifactAttributionSource,
    WebToolArtifactImageKind,
    WebToolImageArtifactEvent,
    WebToolInvocationArtifactEvent,
)
from ..domain.observations import WebObservationContext
from ..observation.ref_alias import WebObservationRefAliases, canonical_source_ref, is_source_ref_like

logger = logging.getLogger(__name__)

WebRefAliasApplyDecision = Literal["accepted", "ignored", "seeded", "idempotent"]


@dataclass(slots=True)
class RegisteredWebAgentSession:
    """Runtime state needed by in-memory MCP tools for one session.

    Attributes:
        session_id: NorthCUA session id used by MCP invocation context.
        client: Browser action backend bound to the session.
        artifact_store: Session artifact store used by MCP tools.
        settings: Web-agent settings used to configure the browser substrate.
    """

    session_id: str
    client: WebActionBackend
    artifact_store: WebArtifactStore
    settings: WebAgentConfig
    tool_invocation_artifact_events: list[WebToolInvocationArtifactEvent] = field(default_factory=list)
    tool_image_artifact_events: list[WebToolImageArtifactEvent] = field(default_factory=list)
    current_ref_aliases: WebObservationRefAliases = field(default_factory=WebObservationRefAliases)
    current_actionable_observation: WebObservationContext | None = None
    artifact_invocation_sequence: int = 0
    artifact_event_sequence: int = 0
    next_artifact_invocation_id: int = 0
    _artifact_action_context: ContextVar[str] = field(
        default_factory=lambda: ContextVar("web_tool_artifact_action", default=""),
        init=False,
        repr=False,
    )
    _artifact_invocation_context: ContextVar[int] = field(
        default_factory=lambda: ContextVar("web_tool_artifact_invocation_id", default=0),
        init=False,
        repr=False,
    )

    def begin_tool_artifact_invocation(self, action: str) -> tuple[Token[str], Token[int]]:
        """Mark image artifacts materialized during the active tool call."""

        self.next_artifact_invocation_id += 1
        self.artifact_invocation_sequence += 1
        invocation_id = self.next_artifact_invocation_id
        action_token = self._artifact_action_context.set(action)
        invocation_token = self._artifact_invocation_context.set(invocation_id)
        self.tool_invocation_artifact_events.append(
            WebToolInvocationArtifactEvent(
                sequence=self.artifact_invocation_sequence,
                invocation_id=invocation_id,
                action=action,
            )
        )
        return action_token, invocation_token

    def restore_tool_artifact_invocation(self, previous: tuple[Token[str], Token[int]]) -> None:
        """Restore the previous artifact attribution context."""

        action_token, invocation_token = previous
        self._artifact_invocation_context.reset(invocation_token)
        self._artifact_action_context.reset(action_token)

    def clear_tool_image_artifact_events(self) -> None:
        """Clear step-local image artifact events before a model step."""

        self.tool_invocation_artifact_events.clear()
        self.tool_image_artifact_events.clear()
        self.artifact_invocation_sequence = 0
        self.artifact_event_sequence = 0

    def update_ref_aliases(
        self,
        aliases: WebObservationRefAliases,
        *,
        observation: WebObservationContext | None = None,
    ) -> None:
        """Replace refs produced by a fresh local observation.

        Local browser observations are authoritative inside the process that
        owns the browser.  Each fresh observation advances the runtime-only
        alias epoch so stale remote side-channel payloads cannot roll the
        active map back later.
        """

        self.current_ref_aliases = aliases.model_copy(update={"alias_epoch": self.current_ref_aliases.alias_epoch + 1})
        self.current_actionable_observation = observation

    def clear_ref_aliases(self, *, observation_id: str = "") -> None:
        """Drop stale model-visible refs when no fresh observation is available."""

        self.current_ref_aliases = WebObservationRefAliases(
            observation_id=observation_id,
            alias_epoch=self.current_ref_aliases.alias_epoch + 1,
        )
        self.current_actionable_observation = None

    def apply_external_ref_aliases(self, aliases: WebObservationRefAliases) -> WebRefAliasApplyDecision:
        """Apply aliases received over a remote runtime side-channel.

        Remote controller/device synchronization is eventually consistent.  The
        latest browser observation must win over whichever side-channel payload
        happens to arrive last, so incoming maps are accepted only when their
        runtime-only epoch is newer, idempotent, or seeding an empty session.
        """

        current = self.current_ref_aliases
        if self._aliases_equivalent(current, aliases):
            self._log_ref_alias_decision(aliases=aliases, decision="idempotent")
            return "idempotent"
        if not current.aliases and not current.observation_id and current.alias_epoch == 0:
            self.current_ref_aliases = aliases
            self.current_actionable_observation = None
            self._log_ref_alias_decision(aliases=aliases, decision="seeded")
            return "seeded"
        if aliases.alias_epoch > current.alias_epoch:
            self.current_ref_aliases = aliases
            self.current_actionable_observation = None
            self._log_ref_alias_decision(aliases=aliases, decision="accepted")
            return "accepted"
        level = logging.WARNING if aliases.alias_epoch == current.alias_epoch else logging.DEBUG
        self._log_ref_alias_decision(aliases=aliases, decision="ignored", level=level)
        return "ignored"

    def _aliases_equivalent(self, left: WebObservationRefAliases, right: WebObservationRefAliases) -> bool:
        return (
            left.alias_epoch == right.alias_epoch
            and left.observation_id == right.observation_id
            and left.aliases == right.aliases
        )

    def _log_ref_alias_decision(
        self,
        *,
        aliases: WebObservationRefAliases,
        decision: WebRefAliasApplyDecision,
        level: int = logging.DEBUG,
    ) -> None:
        if not logger.isEnabledFor(level):
            return
        logger.log(
            level,
            "web_ref_alias_side_channel_%s",
            decision,
            extra={
                "session_id": self.session_id,
                "incoming_epoch": aliases.alias_epoch,
                "current_epoch": self.current_ref_aliases.alias_epoch,
                "incoming_observation_id": aliases.observation_id,
                "current_observation_id": self.current_ref_aliases.observation_id,
                "decision": decision,
            },
        )

    def resolve_model_ref(self, ref: object) -> str:
        """Resolve a model-visible ref alias to the browser source ref.

        Numeric refs are scoped to the most recent model-visible observation.
        Source refs are intentionally not accepted here; model-visible action
        tools must use the numeric refs from the latest observation.
        """

        value = str(ref or "").strip()
        if not value:
            raise ValueError("web element ref cannot be empty.")
        source = self.current_ref_aliases.alias_to_source.get(value)
        if source:
            return source
        if is_source_ref_like(value):
            raise ValueError(
                f"Source web ref {value!r} is internal-only; use a numeric ref from the latest observation."
            )
        if self.current_ref_aliases.alias_to_source:
            raise ValueError(f"Unknown or stale web ref {value!r}; use a numeric ref from the latest observation.")
        raise ValueError(f"Unknown or stale web ref {value!r}; use a numeric ref from the latest observation.")

    def project_source_ref(self, ref: object) -> str:
        """Return the current model-visible alias for a browser source ref."""

        if not is_source_ref_like(ref):
            return str(ref)
        source_ref = canonical_source_ref(ref)
        return self.current_ref_aliases.source_to_alias.get(source_ref, source_ref)

    def record_tool_image_artifact(
        self,
        *,
        raw_image_path: str = "",
        action_overlay_image_path: str = "",
        som_image_path: str = "",
        selected_image_path: str = "",
        selected_image_kind: WebToolArtifactImageKind = WebToolArtifactImageKind.NONE,
        source: WebToolArtifactAttributionSource = WebToolArtifactAttributionSource.EXPLICIT_PATH,
        metadata: dict[str, object] | None = None,
    ) -> None:
        """Record one image artifact materialized by the active tool call."""

        invocation_id = self._artifact_invocation_context.get()
        if invocation_id <= 0:
            return
        action = self._artifact_action_context.get()
        selected_path = selected_image_path or som_image_path or action_overlay_image_path or raw_image_path
        if selected_image_kind is WebToolArtifactImageKind.NONE:
            if som_image_path:
                selected_image_kind = WebToolArtifactImageKind.SOM
            elif action_overlay_image_path:
                selected_image_kind = WebToolArtifactImageKind.ACTION_OVERLAY
            elif raw_image_path:
                selected_image_kind = WebToolArtifactImageKind.RAW
        if not selected_path or selected_image_kind is WebToolArtifactImageKind.NONE:
            return
        self.artifact_event_sequence += 1
        self.tool_image_artifact_events.append(
            WebToolImageArtifactEvent(
                sequence=self.artifact_event_sequence,
                invocation_id=invocation_id,
                action=action,
                raw_image_path=raw_image_path,
                action_overlay_image_path=action_overlay_image_path,
                som_image_path=som_image_path,
                selected_image_path=selected_path,
                selected_image_kind=selected_image_kind,
                source=source,
                metadata={str(key): value for key, value in dict(metadata or {}).items()},
            )
        )
