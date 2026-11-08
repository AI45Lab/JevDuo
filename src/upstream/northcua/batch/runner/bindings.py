"""Device binding helpers for sandbox-backed batch tasks."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Mapping

from northcua.batch.contracts import BatchRunConfig, BatchTaskSpec
from northcua.core.config import NorthCUAAppConfig
from northcua.core.coordinates import (
    CoordinatePolicy,
    CoordinateTarget,
    non_default_coordinate_policy_context_values,
    non_default_coordinate_policy_environment_values,
    policy_from_config,
    resolve_coordinate_policy,
)
from northcua.core.enums import CoordinateSpace
from northcua.runtime.mcp.desktop_native.image_encoding import (
    SCREENSHOT_FORMAT_ENV,
    SCREENSHOT_JPEG_QUALITY_ENV,
    SCREENSHOT_WEBP_QUALITY_ENV,
)
from northcua.sandboxes.contracts import SandboxDeviceBinding, SandboxProviderSpec
from northcua_protocol.capabilities import Capability, PlatformKind
from northcua_runtime_shared.desktop_surface.profiles import default_desktop_tool_profile

logger = logging.getLogger(__name__)
_SHELL_EXEC_TOOL_ID = "shell.exec"
_SHELL_EXEC_READONLY_TOOL_ID = "shell.exec_readonly"
_NON_DESKTOP_WORKER_TOOL_PREFIXES = ("cua_",)
_NON_DESKTOP_WORKER_TOOL_IDS = {"cua_page"}
_DEVICE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9-]+")
_DESKTOP_SCREENSHOT_ENV_KEYS = (
    SCREENSHOT_FORMAT_ENV,
    SCREENSHOT_JPEG_QUALITY_ENV,
    SCREENSHOT_WEBP_QUALITY_ENV,
)


def _stable_device_component(value: str, *, prefix_chars: int = 12, digest_chars: int = 8) -> str:
    """Return a readable device-id component with collision-resistant entropy.

    Args:
        value: Full task or sandbox identifier.
        prefix_chars: Number of readable slug characters to keep.
        digest_chars: Number of sha256 hex characters to append.

    Returns:
        Stable component safe for controller device ids.
    """

    slug = _DEVICE_COMPONENT_RE.sub("-", value).strip("-").lower() or "task"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:digest_chars]
    return f"{slug[:prefix_chars]}-{digest}"


class SandboxDeviceBindingFactory:
    """Build deterministic device ids and worker bindings for batch tasks."""

    def device_id_for_task(
        self,
        batch_id: str,
        task: BatchTaskSpec,
        spec: SandboxProviderSpec,
        *,
        attempt: int = 1,
    ) -> str:
        """Return a strict target device id for a task-scoped sandbox.

        Args:
            batch_id: Batch namespace used to avoid cross-batch collisions.
            task: Manifest task that will lease the sandbox.
            spec: Sandbox allocation spec; existing sandbox ids are preserved
                in the derived device id to make reconnects deterministic.
            attempt: One-based attempt number for task-scoped sandboxes. New
                attempts get distinct ids so controller readiness cannot match
                a stale websocket from an earlier sandbox.

        Returns:
            Device id that the controller task should target.
        """

        attempt_suffix = f"-a{max(attempt, 1)}"
        if spec.sandbox_id:
            return f"batch-{batch_id}-{_stable_device_component(spec.sandbox_id)}{attempt_suffix}"
        return f"batch-{batch_id}-{_stable_device_component(task.task_id)}{attempt_suffix}"

    def for_task(
        self,
        config: BatchRunConfig,
        task: BatchTaskSpec,
        *,
        sandbox_spec: SandboxProviderSpec,
        device_id: str,
        controller_url: str,
        token: str,
    ) -> SandboxDeviceBinding:
        """Build a worker binding for one task or pool slot.

        Args:
            config: Active batch runtime configuration.
            task: Manifest task being prepared.
            sandbox_spec: Provider/platform spec for the leased sandbox.
            device_id: Strict controller target id assigned to the worker.
            controller_url: Controller websocket URL passed to the worker.
            token: Device/submitter token passed to the worker.

        Returns:
            Device worker bootstrap binding with capabilities and metadata.
        """

        capabilities = set(task.required_capabilities) or self.default_capabilities(task.agent_kind)
        worker_environment = self.desktop_worker_environment(task.metadata)
        return SandboxDeviceBinding(
            device_id=device_id,
            display_name=f"{config.manifest.batch_id}:{task.task_id}",
            server_url=controller_url,
            token=token,
            platform=sandbox_spec.platform or PlatformKind.LINUX,
            capabilities=capabilities,
            agentkit_agent_kinds=self.agentkit_agent_kinds(task.agent_kind),
            allowed_tools=self.allowed_tools_for_capabilities(capabilities),
            python_executable=sandbox_spec.device_python,
            environment=worker_environment,
            metadata={
                "batch_id": config.manifest.batch_id,
                "batch_task_id": task.task_id,
                "sandbox_provider": sandbox_spec.provider,
                "sandbox_template": sandbox_spec.template,
                **worker_environment,
            },
        )

    @classmethod
    def desktop_coordinate_metadata(cls, metadata: dict[str, object]) -> dict[str, str]:
        """Return protocol metadata for the desktop coordinate policy.

        Args:
            metadata: Batch or task metadata containing optional app config paths.

        Returns:
            Non-default desktop coordinate policy metadata for controller/device handoff.
        """

        policy = cls._desktop_coordinate_policy(metadata)
        return non_default_coordinate_policy_context_values(CoordinateTarget.DESKTOP, policy)

    @classmethod
    def desktop_coordinate_environment(cls, metadata: dict[str, object]) -> dict[str, str]:
        """Return device-worker environment for the desktop coordinate policy.

        Args:
            metadata: Batch or task metadata containing optional app config paths.

        Returns:
            Non-default desktop coordinate policy environment variables.
        """

        policy = cls._desktop_coordinate_policy(metadata)
        return non_default_coordinate_policy_environment_values(CoordinateTarget.DESKTOP, policy)

    @classmethod
    def desktop_worker_environment(cls, metadata: dict[str, object]) -> dict[str, str]:
        """Return desktop runtime environment exported into sandbox workers.

        Args:
            metadata: Batch or task metadata containing runtime configuration paths.

        Returns:
            Environment variables propagated to sandbox-hosted desktop workers.
        """

        return {
            **cls.desktop_coordinate_environment(metadata),
            **cls.desktop_screenshot_environment(os.environ),
        }

    @staticmethod
    def desktop_screenshot_environment(environment: Mapping[str, str]) -> dict[str, str]:
        """Return configured screenshot encoding env for sandbox workers.

        Screenshot encoding happens inside remote device workers, so the batch
        runner must explicitly propagate operator-provided encoding knobs.

        Args:
            environment: Host environment mapping to read screenshot settings from.

        Returns:
            Non-empty screenshot encoding variables safe to pass to workers.
        """

        return {
            key: value.strip() for key in _DESKTOP_SCREENSHOT_ENV_KEYS if (value := environment.get(key, "")).strip()
        }

    @staticmethod
    def _desktop_coordinate_policy(metadata: dict[str, object]) -> CoordinatePolicy:
        config_path = str(metadata.get("controller_app_config_path") or metadata.get("app_config_path") or "").strip()
        if config_path:
            try:
                app_config = NorthCUAAppConfig.from_yaml(Path(config_path).expanduser())
                return policy_from_config(app_config.desktop.coordinate_policy)
            except Exception:
                logger.warning("failed to load desktop coordinate policy from app config", exc_info=True)
        return resolve_coordinate_policy(
            CoordinateTarget.DESKTOP,
            environment=os.environ,
            fallback_policy=CoordinatePolicy(input_space=CoordinateSpace.IMAGE_PIXELS),
        )

    @staticmethod
    def default_capabilities(agent_kind: str) -> set[Capability]:
        """Return built-in capability defaults for known agent kinds.

        Args:
            agent_kind: AgentKit kind from a batch manifest.

        Returns:
            Capabilities required by known built-in agents, or an empty set for
            custom agents that must declare their own requirements.
        """

        if agent_kind == "desktop":
            return {
                Capability.DESKTOP_INPUT,
                Capability.DESKTOP_SCREENSHOT,
                Capability.DESKTOP_WINDOW,
                Capability.A11Y_TREE,
            }
        if agent_kind == "web":
            return {Capability.BROWSER_PAGE}
        return set()

    @staticmethod
    def agentkit_agent_kinds(agent_kind: str) -> set[str]:
        """Return AgentKit delegates needed by a sandbox worker.

        Args:
            agent_kind: Agent kind from a batch manifest.

        Returns:
            AgentKit kinds to expose on the standalone worker. Desktop uses the
            dedicated desktop runtime adapter, so it must not also load the
            generic AgentKit adapter for ``desktop``; doing so duplicates the
            MCP tool ids advertised to the controller.
        """

        if agent_kind == "desktop":
            return set()
        return {agent_kind}

    @staticmethod
    def allowed_tools_for_capabilities(capabilities: set[Capability]) -> set[str]:
        """Return worker tool allowlist needed for optional high-risk capabilities.

        Args:
            capabilities: Capability set requested for the sandbox worker.

        Returns:
            Tool ids the worker may expose for the requested capabilities.
        """

        if Capability.SHELL_EXEC not in capabilities:
            return set()
        return {
            tool.tool_id
            for tool in default_desktop_tool_profile().tools
            if not tool.tool_id.startswith(_NON_DESKTOP_WORKER_TOOL_PREFIXES)
            and tool.tool_id not in _NON_DESKTOP_WORKER_TOOL_IDS
        } | {_SHELL_EXEC_TOOL_ID, _SHELL_EXEC_READONLY_TOOL_ID}
