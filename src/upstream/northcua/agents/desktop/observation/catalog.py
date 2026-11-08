"""Built-in observation probe chain catalog."""

from __future__ import annotations


from northcua.core.config import (
    AccessibilityObservationConfig,
    DesktopPolicyConfig,
    ProbeChainConfig,
    ProbeToolCallConfig,
)


def built_in_probe_chains(desktop_policy: DesktopPolicyConfig) -> dict[str, ProbeChainConfig]:
    """Return the fixed initial/fallback screenshot probe.

    Args:
        desktop_policy: Desktop observation policy used to decide whether
            screenshot probes capture all screens or only the primary screen.

    Returns:
        The screenshot call used when no clean tool-result image exists.
    """

    all_screens = not desktop_policy.primary_screen_only
    return {
        "desktop_snapshot_default": ProbeChainConfig(
            steps=[
                ProbeToolCallConfig(
                    server="DesktopNativeDataExecutor",
                    tool="capture_desktop_screenshot",
                    arguments={"all_screens": all_screens},
                    adapter="desktop_screenshot",
                ),
            ]
        ),
    }


def default_accessibility_probe_chain(a11y_policy: AccessibilityObservationConfig) -> ProbeChainConfig:
    """Return the default internal accessibility capture chain.

    Args:
        a11y_policy: Accessibility policy that supplies backend server/tool
            names, capture scope, limits, and raw-payload behavior.

    Returns:
        Probe chain containing the configured accessibility capture tool call.
    """

    return ProbeChainConfig(
        steps=[
            ProbeToolCallConfig(
                server=a11y_policy.server_name,
                tool=a11y_policy.capture_tool_name,
                arguments={
                    "scope": a11y_policy.scope.value,
                    "max_depth": a11y_policy.max_depth,
                    "max_children_per_node": a11y_policy.max_children_per_node,
                    "max_nodes": a11y_policy.max_nodes,
                    "include_raw_payload": a11y_policy.include_raw_payload,
                    "capture_timeout_seconds": a11y_policy.capture_timeout_seconds,
                },
                adapter="accessibility_tree",
            )
        ]
    )
