"""Desktop MCP bridge runtime adapter for standalone device workers.

The adapter intentionally exposes the same tool ids as NorthCUA's registered
desktop MCP servers. This keeps the remote harness data plane aligned with the
desktop agent's real tool surface instead of maintaining a second simplified
tool namespace.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import re
from collections.abc import Callable
from typing import Protocol, cast

from pydantic import BaseModel

from northcua_protocol.artifacts import ArtifactKind, ArtifactRef, ArtifactScope, RetentionPolicy
from northcua_protocol.capabilities import Capability, ToolDescriptor
from northcua_protocol.payloads import CommandRequest, CommandResult
from northcua_protocol.state import CommandState
from northcua_runtime_shared import (
    RuntimeHealth,
    apply_desktop_command_metadata,
    build_registered_mcp_server,
    desktop_tool_server_name,
    ensure_desktop_mcp_servers_registered,
    is_registered_mcp_server,
    normalize_mcp_tool_result,
)
from northcua_runtime_shared.desktop_surface.profiles import DesktopToolProfile, default_desktop_tool_profile
from northcua_runtime_shared.tool_contracts import ToolContract, ToolDispatcher, ToolDispatchPolicy

_IMAGE_DATA_URL_RE = re.compile(r"^data:(image/[-+.a-zA-Z0-9]+);base64,(.+)$")
_MAX_INLINE_ARTIFACT_BYTES = 4_000_000
logger = logging.getLogger(__name__)


class _McpServer(Protocol):
    """Protocol for the subset of FastMCP server methods used by the worker."""

    async def call_tool(self, tool_name: str, parameters: dict[str, object]) -> object:
        """Execute one tool through the backing MCP server.

        Args:
            tool_name: Registered MCP tool id.
            parameters: JSON object passed to the MCP tool.

        Returns:
            Raw MCP/FastMCP tool result.
        """
        ...


NormalizeResult = Callable[[object], dict[str, object] | str]
ApplyCommandMetadata = Callable[[dict[str, object]], None]


class DesktopRuntimeAdapter:
    """Desktop worker adapter backed by the same MCP tool ids as NorthCUA."""

    def __init__(
        self,
        *,
        servers_by_name: dict[str, object] | None = None,
        tool_contracts: list[ToolContract] | None = None,
        tool_server_names: dict[str, str] | None = None,
        tool_profile: DesktopToolProfile | None = None,
        normalize_result: NormalizeResult | None = None,
        apply_command_metadata: ApplyCommandMetadata | None = None,
    ) -> None:
        self._injected_servers_by_name = dict(servers_by_name or {})
        self._normalize_result = normalize_result
        self._apply_command_metadata = apply_command_metadata
        self._servers_by_name: dict[str, _McpServer] = {}
        self._tool_server_by_name: dict[str, _McpServer] = {}
        self._tool_dispatcher = ToolDispatcher()
        if tool_profile is not None and tool_contracts is not None:
            raise ValueError("Pass either tool_profile or tool_contracts, not both")
        if tool_profile is None:
            tool_profile = default_desktop_tool_profile()
            if tool_contracts is not None:
                mapping = dict(tool_server_names or {})
                tool_profile = DesktopToolProfile(
                    contracts=tuple(tool_contracts),
                    server_by_tool_id={
                        contract.tool_id: mapping.get(contract.tool_id)
                        or desktop_tool_server_name(contract.tool_id)
                        or ""
                        for contract in tool_contracts
                    },
                )
        self._contracts = list(tool_profile.contracts)
        self._server_name_by_tool_id = {
            tool_id: server_name for tool_id, server_name in tool_profile.server_by_tool_id.items() if server_name
        }
        self._capabilities = {contract.capability for contract in self._contracts}

    @property
    def capabilities(self) -> set[Capability]:
        """Return capabilities exposed by the desktop MCP surface.

        Returns:
            Coarse capabilities derived from real registered desktop tool descriptors.
        """

        return set(self._capabilities)

    @property
    def tool_contracts(self) -> list[ToolContract]:
        """Return desktop tool contracts selected by the runtime profile."""

        return list(self._contracts)

    @property
    def tools(self) -> list[ToolDescriptor]:
        """Return desktop tool descriptors.

        Returns:
            Copy of the authoritative desktop MCP tool descriptors.
        """

        return [contract.to_descriptor() for contract in self._contracts]

    async def healthcheck(self) -> RuntimeHealth:
        """Check whether the desktop MCP bridge can build every advertised tool.

        Returns:
            Runtime health keyed by advertised desktop capabilities.
        """

        try:
            if not self._tool_server_by_name:
                await self._ensure_servers()
        except Exception as exc:  # pragma: no cover - depends on local GUI/runtime setup
            logger.debug("desktop MCP bridge healthcheck failed", exc_info=True)
            return RuntimeHealth(
                healthy=False,
                message=str(exc),
                capability_health={capability: False for capability in self.capabilities},
            )
        capability_health = {capability: True for capability in self.capabilities}
        message = ""
        return RuntimeHealth(healthy=True, message=message, capability_health=capability_health)

    async def execute(self, command: CommandRequest) -> CommandResult:
        """Execute one desktop MCP command through its registered server.

        Args:
            command: Controller-issued command request targeting a desktop tool id.

        Returns:
            Command result with normalized output and extracted artifacts.
        """

        contract = next((item for item in self._contracts if item.tool_id == command.tool_id), None)
        if contract is None:
            return self._failure(command, f"unknown desktop tool_id: {command.tool_id}")
        try:
            envelope = self._tool_dispatcher.prepare(
                contract,
                dict(command.parameters),
                policy=ToolDispatchPolicy(max_allowed_risk=None),
            )
            validated_parameters = envelope.validated_parameters
            if isinstance(validated_parameters, BaseModel):
                validated_parameters = dict(validated_parameters.model_dump(mode="json"))
            else:
                validated_parameters = dict(validated_parameters)
            server = (await self._ensure_servers())[command.tool_id]
            self._apply_runtime_metadata(command)
            normalized = self._normalize(await server.call_tool(contract.provider_name, validated_parameters))
            normalized, artifacts = self._extract_artifacts(command.command_id, normalized)
            return CommandResult(
                command_id=command.command_id,
                session_id=command.session_id,
                lease_id=command.lease_id,
                device_id=command.device_id,
                lease_generation=command.lease_generation,
                status=CommandState.SUCCEEDED,
                result=normalized,
                artifacts=artifacts,
            )
        except Exception as exc:  # pragma: no cover - runtime boundary
            logger.warning(
                "desktop MCP command failed",
                extra={
                    "device_id": command.device_id,
                    "session_id": command.session_id,
                    "lease_id": command.lease_id,
                    "command_id": command.command_id,
                    "tool_id": command.tool_id,
                },
                exc_info=True,
            )
            return self._failure(command, str(exc))

    async def _ensure_servers(self) -> dict[str, _McpServer]:
        if self._tool_server_by_name:
            return self._tool_server_by_name
        if self._injected_servers_by_name:
            self._servers_by_name = {
                name: cast(_McpServer, server) for name, server in self._injected_servers_by_name.items()
            }
        else:
            self._servers_by_name = await self._build_registered_servers()
        for contract in self._contracts:
            server_name = self._server_name_by_tool_id.get(contract.tool_id) or desktop_tool_server_name(
                contract.tool_id
            )
            if not server_name:
                continue
            server = self._servers_by_name.get(server_name)
            if server is not None:
                self._tool_server_by_name[contract.tool_id] = server
        missing = sorted(
            contract.tool_id for contract in self._contracts if contract.tool_id not in self._tool_server_by_name
        )
        if missing:
            raise RuntimeError("desktop MCP surface is incomplete for standalone execution: " + ", ".join(missing))
        return self._tool_server_by_name

    def _apply_runtime_metadata(self, command: CommandRequest) -> None:
        metadata = dict(command.metadata)
        if self._apply_command_metadata is not None:
            self._apply_command_metadata(metadata)
        elif not self._injected_servers_by_name:
            apply_desktop_command_metadata(metadata)

    async def _build_registered_servers(self) -> dict[str, _McpServer]:
        ensure_desktop_mcp_servers_registered()
        server_names = tuple(
            dict.fromkeys(
                server_name
                for contract in self._contracts
                if (
                    server_name := self._server_name_by_tool_id.get(contract.tool_id)
                    or desktop_tool_server_name(contract.tool_id)
                )
                is not None
            )
        )
        return {
            server_name: cast(
                _McpServer,
                build_registered_mcp_server(
                    server_name=server_name,
                    process_name=f"northcua-device:{server_name}",
                ),
            )
            for server_name in server_names
            if is_registered_mcp_server(server_name)
        }

    def _normalize(self, result: object) -> dict[str, object] | str:
        if self._normalize_result is not None:
            normalized = self._normalize_result(result)
        else:
            normalized = normalize_mcp_tool_result(result)
        if isinstance(normalized, dict):
            return self._with_inline_screenshot_urls(normalized)
        return normalized if isinstance(normalized, (dict, str)) else str(normalized)

    @staticmethod
    def _with_inline_screenshot_urls(result: dict[str, object]) -> dict[str, object]:
        """Attach data URLs for worker-local screenshot paths before dispatch.

        Desktop action MCP tools return local ``screenshot_path`` values to
        match local NorthCUA execution. In remote sandbox mode those paths are
        private to the worker, so the worker must also attach a data URL that
        the controller can extract into protocol artifacts and rehydrate into
        the same prompt-facing shape local runs use.
        """

        normalized = dict(result)
        path = normalized.get("screenshot_path") or normalized.get("image_path")
        existing_url = normalized.get("screenshot_url") or normalized.get("image_url")
        if existing_url or not isinstance(path, str) or not path or not os.path.exists(path):
            return normalized
        if os.path.getsize(path) > _MAX_INLINE_ARTIFACT_BYTES:
            raise ValueError("worker-local desktop screenshot exceeds max inline artifact bytes")
        with open(path, "rb") as handle:
            encoded = base64.b64encode(handle.read()).decode("ascii")
        mime_type = _image_mime_type_for_path(path)
        normalized["screenshot_url"] = f"data:{mime_type};base64,{encoded}"
        normalized.setdefault("screenshot_format", "local_path")
        return normalized

    @staticmethod
    def _extract_artifacts(
        command_id: str, result: dict[str, object] | str
    ) -> tuple[dict[str, object] | str, list[ArtifactRef]]:
        if not isinstance(result, dict):
            return result, []
        normalized_result = dict(result)
        artifacts: list[ArtifactRef] = []
        for key, value in result.items():
            if not isinstance(value, str):
                continue
            match = _IMAGE_DATA_URL_RE.match(value)
            if match is None:
                continue
            mime_type, encoded = match.groups()
            data = DesktopRuntimeAdapter._decode_inline_artifact(encoded)
            artifact = ArtifactRef.from_inline_bytes(
                artifact_id=f"{command_id}-{key}",
                kind=ArtifactKind.SCREENSHOT,
                data=data,
                mime_type=mime_type,
                artifact_scope=ArtifactScope.COMMAND,
                retention_policy=RetentionPolicy.TASK,
            )
            artifacts.append(artifact)
            normalized_result[key] = ""
            normalized_result[f"{key}_artifact_id"] = artifact.artifact_id
            normalized_result[f"{key}_artifact_sha256"] = artifact.sha256
            normalized_result[f"{key}_artifact_mime_type"] = artifact.mime_type
        return normalized_result, artifacts

    @staticmethod
    def _decode_inline_artifact(encoded: str) -> bytes:
        # Estimate decoded bytes before allocating so a malicious device result
        # cannot force the controller/worker to materialize an unbounded blob.
        estimated_size = (len(encoded) * 3) // 4
        if estimated_size > _MAX_INLINE_ARTIFACT_BYTES:
            raise ValueError("inline desktop artifact exceeds max bytes")
        data = base64.b64decode(encoded, validate=True)
        if len(data) > _MAX_INLINE_ARTIFACT_BYTES:
            raise ValueError("inline desktop artifact exceeds max bytes")
        return data

    @staticmethod
    def _failure(command: CommandRequest, error: str) -> CommandResult:
        return CommandResult(
            command_id=command.command_id,
            session_id=command.session_id,
            lease_id=command.lease_id,
            device_id=command.device_id,
            lease_generation=command.lease_generation,
            status=CommandState.FAILED,
            error=error,
        )


def _image_mime_type_for_path(path: str) -> str:
    mime_type = mimetypes.guess_type(path)[0]
    if isinstance(mime_type, str) and mime_type.startswith("image/"):
        return mime_type
    return "image/png"
