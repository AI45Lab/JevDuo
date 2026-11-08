"""Core observation framework contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from northcua.agentkernel.json_types import JsonObject
from northcua.core.accessibility import AccessibilityStepRecord
from northcua.core.config import ProbeToolCallConfig
from northcua.core.models import ObservationArtifact


@dataclass(slots=True)
class ProbeExecutionRecord:
    """One executed internal runtime probe tool call.

    Attributes:
        chain_name: Probe chain that produced this record.
        step_index: Zero-based index within the chain.
        server_name: MCP server name used for the call.
        tool_name: MCP tool name used for the call.
        arguments: Tool arguments sent to the probe.
        output: Normalized probe output.
        source: Stable source label for downstream artifacts.
    """

    chain_name: str
    step_index: int
    server_name: str
    tool_name: str
    arguments: JsonObject
    output: JsonObject | str | None
    source: str


@dataclass(slots=True)
class ProbeAdapterResult:
    """Normalized adapter output from one probe execution.

    Attributes:
        artifacts: Observation artifacts emitted by the adapter.
        current_clean_screenshot_path: Local current screenshot path.
        current_clean_screenshot_url: Current screenshot data URL.
        accessibility: Accessibility summary captured by the adapter.
        metadata: Additional JSON-serializable adapter metadata.
    """

    artifacts: list[ObservationArtifact] = field(default_factory=list)
    current_clean_screenshot_path: str = ""
    current_clean_screenshot_url: str = ""
    accessibility: AccessibilityStepRecord | None = None
    metadata: JsonObject = field(default_factory=dict)


@dataclass(slots=True)
class StateSourceResult:
    """Normalized result produced by one state source.

    Attributes:
        artifacts: Observation artifacts emitted by the source.
        current_clean_screenshot_path: Local current screenshot path.
        current_clean_screenshot_url: Current screenshot data URL.
        selected_history_image_path: Image path selected for history replay.
        selected_history_image_url: Image URL selected for history replay.
        accessibility: Accessibility summary captured by the source.
        metadata: Additional JSON-serializable source metadata.
    """

    artifacts: list[ObservationArtifact] = field(default_factory=list)
    current_clean_screenshot_path: str = ""
    current_clean_screenshot_url: str = ""
    selected_history_image_path: str = ""
    selected_history_image_url: str = ""
    accessibility: AccessibilityStepRecord | None = None
    metadata: JsonObject = field(default_factory=dict)


class ProbeResultAdapter(ABC):
    """Convert one probe execution result into normalized observation artifacts."""

    @abstractmethod
    def adapt(
        self,
        execution: ProbeExecutionRecord,
        *,
        session_step: int,
    ) -> ProbeAdapterResult:
        """Return normalized output for one probe call.

        Args:
            execution: Probe execution record to adapt.
            session_step: Current session step number.

        Returns:
            Normalized adapter result.
        """


class ProbeExecutor(ABC):
    """Execute named internal probe chains."""

    @abstractmethod
    async def execute_chain(
        self, chain_name: str, *, session_step: int, steps: list[ProbeToolCallConfig]
    ) -> list[ProbeExecutionRecord]:
        """Execute a chain and return normalized probe execution records.

        Args:
            chain_name: Configured probe chain name.
            session_step: Current session step number.
            steps: Probe tool-call steps to execute.

        Returns:
            Probe execution records in step order.
        """


class StateSource(ABC):
    """Build normalized current-round state from one source."""

    @abstractmethod
    async def collect(self, *, session_step: int, previous_steps: list[object]) -> StateSourceResult:
        """Return a normalized current-round state snapshot.

        Args:
            session_step: Current session step number.
            previous_steps: Previously persisted step records.

        Returns:
            Normalized state-source result.
        """
