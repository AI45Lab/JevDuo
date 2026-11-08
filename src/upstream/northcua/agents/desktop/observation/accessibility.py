"""Accessibility observation integration built on the generic probe framework."""

from __future__ import annotations

import logging

from northcua.app.logging import log_runtime_event
from northcua.core.config import AccessibilityObservationConfig, ProbeToolCallConfig
from northcua.core.enums import AccessibilityCaptureStatus, AccessibilityDiffMode, ObservationArtifactKind
from northcua.core.models import ObservationArtifact, ObservationContext, StepLogRecord
from northcua.runtime.accessibility import (
    AccessibilityArtifactPersister,
    AccessibilityCapabilities,
    AccessibilityDiffBuilder,
    AccessibilitySnapshot,
    AccessibilityStepRecord,
)

from .accessibility_capture_guard import AccessibilityCaptureGuardMixin, safe_exception_message
from .base import ProbeExecutor
from .sources import ProbeChainStateSource, StateSourceResult

logger = logging.getLogger("northcua.observation.accessibility")
_CAPABILITIES_CACHE: dict[tuple[str, str], AccessibilityCapabilities] = {}
_CAPABILITIES_LOGGED: set[tuple[str, str]] = set()


class AccessibilityCapabilitiesChecker:
    """Lazy per-session capability discovery for accessibility capture.

    Capability checks are cached by session/cache key and accessibility server
    name so each facade instance avoids repeatedly prompting or probing the
    same backend. Probe failures are handled by the caller because fail-open
    behavior depends on the active accessibility config.
    """

    def __init__(
        self, *, config: AccessibilityObservationConfig, probe_executor: ProbeExecutor, cache_key: str
    ) -> None:
        """Initialize the capability checker.

        Args:
            config: Accessibility observation configuration.
            probe_executor: Internal probe executor used to call capabilities.
            cache_key: Session-scoped key used for process-level caching.
        """
        self.config = config
        self.probe_executor = probe_executor
        self.cache_key = (cache_key, config.server_name)
        self._cached: AccessibilityCapabilities | None = None

    async def get(self, *, session_step: int) -> AccessibilityCapabilities:
        """Return cached capabilities, probing once on first access.

        Args:
            session_step: Current session step for probe records and logs.

        Returns:
            Parsed accessibility backend capabilities.
        """
        if self._cached is not None:
            return self._cached
        cached = _CAPABILITIES_CACHE.get(self.cache_key)
        if cached is not None:
            self._cached = cached
            return cached
        records = await self.probe_executor.execute_chain(
            "__a11y_capabilities__",
            session_step=session_step,
            steps=[
                ProbeToolCallConfig(
                    server=self.config.server_name,
                    tool=self.config.capabilities_tool_name,
                    arguments={},
                    adapter="generic_text",
                )
            ],
        )
        output = records[0].output if records else {}
        self._cached = AccessibilityCapabilities.model_validate(output if isinstance(output, dict) else {})
        _CAPABILITIES_CACHE[self.cache_key] = self._cached
        return self._cached


class AccessibilityObservationAugmentor(AccessibilityCaptureGuardMixin):
    """Augment one observation context with accessibility artifacts and diff.

    The augmentor preserves the platform's fail-open policy: when
    ``config.fail_open`` is true, accessibility failures are represented in the
    observation metadata and step accessibility record without aborting desktop
    execution. When fail-open is false, probe/capture exceptions propagate.
    """

    def __init__(
        self,
        *,
        config: AccessibilityObservationConfig,
        probe_source: ProbeChainStateSource,
        probe_executor: ProbeExecutor,
        artifact_persister: AccessibilityArtifactPersister,
        cache_key: str,
    ) -> None:
        """Initialize accessibility augmentation for one session.

        Args:
            config: Accessibility observation configuration.
            probe_source: Probe chain source used for accessibility capture.
            probe_executor: Probe executor used for capability discovery.
            artifact_persister: Persister for snapshots and diffs.
            cache_key: Session-scoped key used for capability caching.
        """
        self.config = config
        self.probe_source = probe_source
        self.probe_executor = probe_executor
        self.artifact_persister = artifact_persister
        self.diff_builder = AccessibilityDiffBuilder()
        self.capabilities_checker = AccessibilityCapabilitiesChecker(
            config=config, probe_executor=probe_executor, cache_key=cache_key
        )
        self.cache_key = (cache_key, config.server_name)
        self._capabilities_logged = _CAPABILITIES_LOGGED

    async def augment(
        self, *, observation_context: ObservationContext, session_step: int, previous_steps: list[StepLogRecord]
    ) -> ObservationContext:
        """Attach accessibility artifacts to an existing observation context.

        Args:
            observation_context: Observation context collected by the base
                desktop observation policy.
            session_step: Current session step number.
            previous_steps: Previously persisted step records used for diff
                lookup.

        Returns:
            The same observation context instance with accessibility fields and
            metadata updated when capture is enabled or fails open.

        Raises:
            Exception: Re-raises capability or capture failures when
                ``config.fail_open`` is disabled.
        """
        if not self.config.enabled:
            observation_context.accessibility = AccessibilityStepRecord(
                status=AccessibilityCaptureStatus.DISABLED, scope=self.config.scope, diff_mode=self.config.diff_mode
            )
            return observation_context
        capabilities = await self._ensure_capabilities(session_step=session_step)
        maybe_short_circuit = self._capabilities_prevent_capture(capabilities, session_step=session_step)
        if maybe_short_circuit is not None:
            observation_context.accessibility = maybe_short_circuit
            observation_context.observation_metadata["accessibility_status"] = maybe_short_circuit.status.value
            observation_context.observation_metadata["accessibility_error"] = maybe_short_circuit.error
            return observation_context
        log_runtime_event(
            logger,
            "a11y_capture_started",
            session_step=session_step,
            scope=self.config.scope,
            server_name=self.config.server_name,
            tool_name=self.config.capture_tool_name,
            platform_name=capabilities.platform_name if capabilities is not None else "",
            source=capabilities.source if capabilities is not None else None,
        )
        try:
            source_result = await self.probe_source.collect(session_step=session_step, previous_steps=previous_steps)
        except Exception as exc:
            if not self.config.fail_open:
                raise
            logger.debug("Accessibility capture failed; recording fail-open observation state.", exc_info=True)
            return self._mark_capture_error(
                observation_context, exc, capabilities=capabilities, session_step=session_step
            )
        step_record = source_result.accessibility
        if step_record is None:
            return observation_context
        snapshot = self.artifact_persister.load_snapshot(step_record.artifact_path)
        step_record = self._append_basic_diff(
            source_result, step_record, snapshot=snapshot, session_step=session_step, previous_steps=previous_steps
        )
        return self._apply_capture_result(
            observation_context, source_result, step_record, capabilities=capabilities, session_step=session_step
        )

    def _mark_capture_error(
        self,
        observation_context: ObservationContext,
        exc: Exception,
        *,
        capabilities: AccessibilityCapabilities | None,
        session_step: int,
    ) -> ObservationContext:
        """Persist fail-open capture errors onto the observation context."""
        error_message = safe_exception_message(exc)
        step_record = AccessibilityStepRecord(
            status=AccessibilityCaptureStatus.ERROR,
            scope=self.config.scope,
            diff_mode=self.config.diff_mode,
            error=error_message,
        )
        observation_context.accessibility = step_record
        observation_context.observation_metadata["accessibility_status"] = step_record.status.value
        observation_context.observation_metadata["accessibility_error"] = step_record.error
        log_runtime_event(
            logger,
            "a11y_capture_failed",
            level=logging.WARNING,
            session_step=session_step,
            scope=self.config.scope,
            status=step_record.status,
            platform_name=capabilities.platform_name if capabilities is not None else "",
            source=capabilities.source if capabilities is not None else None,
            error=step_record.error,
            error_type=type(exc).__name__,
            fail_open=True,
        )
        return observation_context

    def _append_basic_diff(
        self,
        source_result: StateSourceResult,
        step_record: AccessibilityStepRecord,
        *,
        snapshot: AccessibilitySnapshot | None,
        session_step: int,
        previous_steps: list[StepLogRecord],
    ) -> AccessibilityStepRecord:
        """Append an accessibility diff artifact when a valid baseline exists."""
        if (
            self.config.diff_mode != AccessibilityDiffMode.BASIC
            or snapshot is None
            or snapshot.status != AccessibilityCaptureStatus.SUCCESS
            or (snapshot.tree is None)
        ):
            return step_record
        previous_snapshot = self._previous_snapshot(previous_steps)
        diff = self.diff_builder.build(previous_snapshot, snapshot)
        if diff is None:
            return step_record
        diff_path = self.artifact_persister.persist_diff(diff, step_index=session_step)
        step_record = step_record.model_copy(update={"diff": diff, "diff_mode": self.config.diff_mode})
        source_result.artifacts.append(
            ObservationArtifact(
                kind=ObservationArtifactKind.ACCESSIBILITY_DIFF,
                role="accessibility_diff",
                source="accessibility_diff_builder",
                path=diff_path,
                metadata=diff.model_dump(mode="json"),
            )
        )
        log_runtime_event(
            logger,
            "a11y_diff_computed",
            session_step=session_step,
            scope=self.config.scope,
            artifact_path=diff_path,
            platform_name=snapshot.platform_name,
            source=snapshot.source,
            added_nodes=diff.added_nodes,
            removed_nodes=diff.removed_nodes,
            changed_nodes=diff.changed_nodes,
            focus_changed=diff.focus_changed,
        )
        return step_record

    def _apply_capture_result(
        self,
        observation_context: ObservationContext,
        source_result: StateSourceResult,
        step_record: AccessibilityStepRecord,
        *,
        capabilities: AccessibilityCapabilities | None,
        session_step: int,
    ) -> ObservationContext:
        """Merge successful accessibility capture output into observation state."""
        source_result.accessibility = step_record
        observation_context.current_artifacts.extend(source_result.artifacts)
        observation_context.accessibility = step_record
        observation_context.observation_metadata.update(source_result.metadata)
        observation_context.observation_metadata["accessibility_status"] = step_record.status.value
        observation_context.observation_metadata["accessibility_artifact_path"] = step_record.artifact_path
        if step_record.raw_artifact_path:
            observation_context.observation_metadata["accessibility_raw_artifact_path"] = step_record.raw_artifact_path
        log_runtime_event(
            logger,
            "a11y_capture_completed",
            session_step=session_step,
            scope=step_record.scope,
            source=step_record.source,
            status=step_record.status,
            platform_name=capabilities.platform_name if capabilities is not None else "",
            capture_ms=step_record.capture_ms,
            node_count=step_record.node_count,
            max_depth=step_record.max_depth,
            truncated=step_record.truncated,
            artifact_path=step_record.artifact_path,
            raw_artifact_path=step_record.raw_artifact_path,
            diff_artifact_path=next(
                (
                    artifact.path
                    for artifact in source_result.artifacts
                    if artifact.kind == ObservationArtifactKind.ACCESSIBILITY_DIFF
                ),
                "",
            ),
            error=step_record.error,
        )
        log_runtime_event(
            logger,
            "a11y_snapshot_persisted",
            level=logging.DEBUG,
            session_step=session_step,
            platform_name=capabilities.platform_name if capabilities is not None else "",
            source=step_record.source,
            artifact_path=step_record.artifact_path,
            raw_artifact_path=step_record.raw_artifact_path,
        )
        return observation_context

    def _previous_snapshot(self, previous_steps: list[StepLogRecord]) -> AccessibilitySnapshot | None:
        """Return the newest successful prior accessibility snapshot.

        Args:
            previous_steps: Previously persisted step records.

        Returns:
            Previous successful snapshot with a tree, or ``None`` when no
            usable baseline exists for diffing.
        """
        for step in reversed(previous_steps):
            if step.accessibility is None or not step.accessibility.artifact_path:
                continue
            snapshot = self.artifact_persister.load_snapshot(step.accessibility.artifact_path)
            if (
                snapshot is not None
                and snapshot.status == AccessibilityCaptureStatus.SUCCESS
                and (snapshot.tree is not None)
            ):
                return snapshot
        return None
