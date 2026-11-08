"""Shared backend contracts and traversal helpers for accessibility capture."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from northcua.core.accessibility import AccessibilityCapabilities, AccessibilityCaptureRequest, AccessibilitySnapshot
from northcua.core.enums import (
    AccessibilityCaptureStatus,
    AccessibilityRawFormat,
    AccessibilityScope,
    AccessibilitySourceKind,
)


@dataclass(slots=True)
class AccessibilityTraversalBudget:
    """Traversal budget enforcing depth, breadth, and node-count limits.

    Attributes:
        max_depth: Maximum tree depth to traverse before marking output truncated.
        max_children_per_node: Maximum number of children visited for each node.
        max_nodes: Maximum number of nodes included in one normalized snapshot.
        visited_nodes: Number of nodes already accepted by this budget.
        truncated: Whether traversal skipped nodes because a limit was reached.
    """

    max_depth: int
    max_children_per_node: int
    max_nodes: int
    visited_nodes: int = 0
    truncated: bool = False

    def claim_node(self, depth: int) -> bool:
        """Reserve capacity for a node if traversal limits allow it.

        Args:
            depth: Zero-based depth of the node the caller wants to include.

        Returns:
            True when the node can be included; False when depth or node-count
            limits have been reached.
        """

        if depth > self.max_depth:
            self.truncated = True
            return False
        if self.visited_nodes >= self.max_nodes:
            self.truncated = True
            return False
        self.visited_nodes += 1
        return True

    def limit_children(self, child_count: int) -> range:
        """Return the child indexes allowed for traversal.

        Args:
            child_count: Total number of available children on the source node.

        Returns:
            Range of child indexes that should be visited.
        """

        if child_count > self.max_children_per_node:
            self.truncated = True
        return range(min(child_count, self.max_children_per_node))


class AccessibilityBackend(ABC):
    """Strategy interface for one platform accessibility backend."""

    platform_name: str
    source_kind: AccessibilitySourceKind | None

    def __init__(self, *, platform_name: str, source_kind: AccessibilitySourceKind | None) -> None:
        self.platform_name = platform_name
        self.source_kind = source_kind

    @abstractmethod
    def capabilities(self) -> AccessibilityCapabilities:
        """Return backend/platform capability metadata.

        Returns:
            Normalized capability metadata for the concrete backend.
        """

    @abstractmethod
    def capture(self, request: AccessibilityCaptureRequest) -> AccessibilitySnapshot:
        """Capture one normalized accessibility snapshot.

        Args:
            request: Capture scope, traversal limits, and timeout preferences.

        Returns:
            Normalized accessibility snapshot for the requested scope.
        """

    def unsupported_snapshot(self, *, scope: AccessibilityScope, message: str) -> AccessibilitySnapshot:
        """Build a normalized unsupported snapshot for the current backend.

        Args:
            scope: Requested capture scope.
            message: Human-readable reason the backend cannot capture.

        Returns:
            Failed snapshot with unsupported status.
        """

        return AccessibilitySnapshot.failed(
            status=AccessibilityCaptureStatus.UNSUPPORTED,
            platform_name=self.platform_name,
            source=self.source_kind,
            scope=scope,
            error=message,
        )

    def error_snapshot(self, *, scope: AccessibilityScope, message: str) -> AccessibilitySnapshot:
        """Build a normalized error snapshot for the current backend.

        Args:
            scope: Requested capture scope.
            message: Human-readable capture failure reason.

        Returns:
            Failed snapshot with error status.
        """

        return AccessibilitySnapshot.failed(
            status=AccessibilityCaptureStatus.ERROR,
            platform_name=self.platform_name,
            source=self.source_kind,
            scope=scope,
            error=message,
        )

    def permission_denied_snapshot(self, *, scope: AccessibilityScope, message: str) -> AccessibilitySnapshot:
        """Build a normalized permission-denied snapshot for the current backend.

        Args:
            scope: Requested capture scope.
            message: Human-readable permission failure reason.

        Returns:
            Failed snapshot with permission-denied status.
        """

        return AccessibilitySnapshot.failed(
            status=AccessibilityCaptureStatus.PERMISSION_DENIED,
            platform_name=self.platform_name,
            source=self.source_kind,
            scope=scope,
            error=message,
        )

    def default_capabilities(
        self,
        *,
        supported_scopes: list[AccessibilityScope],
        requires_permission: bool = False,
        permission_granted: bool = True,
        raw_formats: list[AccessibilityRawFormat] | None = None,
        message: str = "",
    ) -> AccessibilityCapabilities:
        """Build a standard capability response for a backend.

        Args:
            supported_scopes: Capture scopes the backend can currently serve.
            requires_permission: Whether OS-level accessibility permission is required.
            permission_granted: Whether required permission is currently available.
            raw_formats: Raw sidecar formats available from the backend.
            message: Optional diagnostic or setup hint.

        Returns:
            Normalized capability metadata.
        """

        return AccessibilityCapabilities(
            platform_name=self.platform_name,
            source=self.source_kind,
            supported_scopes=supported_scopes,
            requires_permission=requires_permission,
            permission_granted=permission_granted,
            raw_formats=list(raw_formats or []),
            message=message,
        )
