"""Observation state sources for prompt-facing current desktop state."""

from __future__ import annotations

from northcua.core.config import ProbeChainConfig
from northcua.core.enums import ObservationArtifactKind
from northcua.core.image_payloads import image_data_url_from_path
from northcua.core.json_types import JsonObject
from northcua.core.models import ObservationArtifact, StepLogRecord

from .adapters import ProbeAdapterRegistry
from .base import ProbeExecutor, StateSource, StateSourceResult


class ToolResultStateSource(StateSource):
    """Reuse the newest raw tool-result screenshot as clean current state.

    Selection walks persisted steps newest-first and tool results newest-first.
    Raw screenshots are clean desktop state and can seed the next observation.
    Selected or overlay aliases are history-only: they can be shown as prior
    action evidence, but they must not become the next clean screenshot or the
    overlay would recursively accumulate across steps.
    """

    async def collect(
        self,
        *,
        session_step: int,
        previous_steps: list[StepLogRecord],
    ) -> StateSourceResult:
        """Collect current state from the newest image-bearing tool result.

        Args:
            session_step: Current session step number, unused by this source.
            previous_steps: Previously persisted step records.

        Returns:
            State-source result using the newest eligible tool image, preferring
            raw clean screenshots for current state and preserving selected
            image aliases only as history evidence.
        """

        del session_step
        for step in reversed(previous_steps):
            for tool_result in reversed(step.tool_results):
                raw_image_url = tool_result.raw_image_url or image_data_url_from_path(tool_result.raw_image_path)
                if raw_image_url:
                    artifact_url = "" if tool_result.raw_image_path else tool_result.raw_image_url
                    artifact_metadata: JsonObject = {
                        "selected_image_kind": "raw",
                        "tool_name": tool_result.name,
                        "source_image_path": tool_result.source_image_path,
                    }
                    artifact_metadata.update(tool_result.metadata)
                    artifact = ObservationArtifact(
                        kind=ObservationArtifactKind.IMAGE,
                        role="current_clean",
                        source="latest_tool_result",
                        path=tool_result.raw_image_path,
                        url=artifact_url,
                        metadata=artifact_metadata,
                    )
                    metadata: JsonObject = {
                        "current_state_source": "latest_tool_result:raw",
                        "selected_image_kind": "raw",
                        "source_image_path": tool_result.source_image_path,
                    }
                    metadata.update(tool_result.metadata)
                    return StateSourceResult(
                        artifacts=[artifact],
                        current_clean_screenshot_path=tool_result.raw_image_path,
                        current_clean_screenshot_url=raw_image_url,
                        selected_history_image_path=tool_result.raw_image_path,
                        selected_history_image_url=raw_image_url,
                        metadata=metadata,
                    )
                selected_image_url = tool_result.image_url or image_data_url_from_path(tool_result.image_path)
                if tool_result.image_path and selected_image_url:
                    artifact_url = "" if tool_result.image_path else tool_result.image_url
                    artifact_metadata: JsonObject = {
                        "selected_image_kind": tool_result.selected_image_kind.value,
                        "tool_name": tool_result.name,
                        "source_image_path": tool_result.source_image_path,
                    }
                    artifact_metadata.update(tool_result.metadata)
                    artifact = ObservationArtifact(
                        kind=ObservationArtifactKind.IMAGE,
                        role="history",
                        source="latest_tool_result",
                        path=tool_result.image_path,
                        url=artifact_url,
                        metadata=artifact_metadata,
                    )
                    metadata: JsonObject = {
                        "history_image_source": f"latest_tool_result:{tool_result.selected_image_kind.value}",
                        "selected_image_kind": tool_result.selected_image_kind.value,
                        "source_image_path": tool_result.source_image_path,
                    }
                    metadata.update(tool_result.metadata)
                    return StateSourceResult(
                        artifacts=[artifact],
                        selected_history_image_path=tool_result.image_path,
                        selected_history_image_url=selected_image_url,
                        metadata=metadata,
                    )
        return StateSourceResult()


class ProbeChainStateSource(StateSource):
    """Collect current state from a configured internal probe chain.

    Adapters own artifact normalization, while this source performs only a
    stable left-to-right merge of adapted fields so probe-chain order remains
    the source of truth for conflicting current-state values.
    """

    def __init__(
        self,
        *,
        chain_name: str,
        chain: ProbeChainConfig,
        probe_executor: ProbeExecutor,
        adapter_registry: ProbeAdapterRegistry,
    ) -> None:
        """Initialize a state source backed by one probe chain.

        Args:
            chain_name: Configured probe chain name.
            chain: Probe chain configuration.
            probe_executor: Executor used to run chain steps.
            adapter_registry: Registry used to normalize probe outputs.
        """

        self.chain_name = chain_name
        self.chain = chain
        self.probe_executor = probe_executor
        self.adapter_registry = adapter_registry

    async def collect(
        self,
        *,
        session_step: int,
        previous_steps: list[StepLogRecord],
    ) -> StateSourceResult:
        """Collect current state by executing and adapting probe steps.

        Args:
            session_step: Current session step number.
            previous_steps: Previously persisted step records, unused here.

        Returns:
            Merged state-source result from all probe adapters.
        """

        del previous_steps
        records = await self.probe_executor.execute_chain(
            self.chain_name,
            session_step=session_step,
            steps=self.chain.steps,
        )
        result = StateSourceResult()
        for record, step in zip(records, self.chain.steps, strict=False):
            adapted = self.adapter_registry.get(step.adapter).adapt(record, session_step=session_step)
            result.artifacts.extend(adapted.artifacts)
            if adapted.accessibility is not None:
                result.accessibility = adapted.accessibility
            if adapted.current_clean_screenshot_url:
                result.current_clean_screenshot_path = adapted.current_clean_screenshot_path
                result.current_clean_screenshot_url = adapted.current_clean_screenshot_url
            result.metadata.update(adapted.metadata)
        if result.current_clean_screenshot_url and "current_state_source" not in result.metadata:
            result.metadata["current_state_source"] = f"probe_chain:{self.chain_name}"
        return result
