"""Tool placement resolution for local and remote evaluator runs."""

from __future__ import annotations

from dataclasses import dataclass

from northcua.core.enums import EvaluationToolPlacement

from ..spec import EvaluatorSpec, EvaluatorToolRef


class EvaluationToolPlacementError(ValueError):
    """Raised when an evaluator exposes tools that violate placement policy."""


@dataclass(slots=True, frozen=True)
class PlacementResolution:
    """Resolved tool partition for one evaluator run.

    Attributes:
        server_tools: Tools executed on the controller/server side.
        client_probe_tools: Read-only live tools executed on the client/device.
        client_mutating_tools: Mutating tools executed on the client/device.
        finalizer_tools: Server-side tools that terminate the evaluator.
    """

    server_tools: tuple[EvaluatorToolRef, ...]
    client_probe_tools: tuple[EvaluatorToolRef, ...]
    client_mutating_tools: tuple[EvaluatorToolRef, ...]
    finalizer_tools: tuple[EvaluatorToolRef, ...]

    @property
    def has_client_live_tools(self) -> bool:
        """Return whether any client-side live tool is present.

        Returns:
            True when probe or mutating client tools are present.
        """

        return bool(self.client_probe_tools or self.client_mutating_tools)


class ToolPlacementResolver:
    """Partition evaluator tools and enforce live-probe scheduling rules."""

    def resolve(self, spec: EvaluatorSpec, *, remote: bool) -> PlacementResolution:
        """Resolve tools for a local or remote evaluator execution.

        Args:
            spec: Evaluator spec to partition.
            remote: Whether the evaluator is running against a remote device.

        Returns:
            Tool placement partition.
        """

        del remote
        server: list[EvaluatorToolRef] = []
        probe: list[EvaluatorToolRef] = []
        mutating: list[EvaluatorToolRef] = []
        finalizers: list[EvaluatorToolRef] = []
        for tool in spec.visible_tools():
            if tool.placement == EvaluationToolPlacement.SERVER_ARTIFACT:
                server.append(tool)
            elif tool.placement == EvaluationToolPlacement.CLIENT_PROBE:
                probe.append(tool)
            elif tool.placement == EvaluationToolPlacement.CLIENT_MUTATING:
                mutating.append(tool)
            elif tool.placement == EvaluationToolPlacement.FINALIZER:
                finalizers.append(tool)
        resolution = PlacementResolution(
            server_tools=tuple(server),
            client_probe_tools=tuple(probe),
            client_mutating_tools=tuple(mutating),
            finalizer_tools=tuple(finalizers),
        )
        self._validate_live_tool_schedule(spec, resolution)
        return resolution

    def _validate_live_tool_schedule(self, spec: EvaluatorSpec, resolution: PlacementResolution) -> None:
        if not resolution.has_client_live_tools:
            return
        if any(not tool.readonly for tool in resolution.client_probe_tools):
            raise EvaluationToolPlacementError("CLIENT_PROBE evaluator tools must be read-only.")
        if any(tool.readonly for tool in resolution.client_mutating_tools):
            raise EvaluationToolPlacementError("CLIENT_MUTATING evaluator tools must be mutating.")
        if resolution.client_probe_tools and not spec.placement_policy.allow_client_live_probe:
            raise EvaluationToolPlacementError("Client-side evaluator probes are disabled by placement policy.")
        if resolution.client_mutating_tools and not spec.placement_policy.allow_client_mutating_tools:
            raise EvaluationToolPlacementError("Client-side mutating evaluator tools are disabled by placement policy.")


__all__ = ["EvaluationToolPlacementError", "PlacementResolution", "ToolPlacementResolver"]
