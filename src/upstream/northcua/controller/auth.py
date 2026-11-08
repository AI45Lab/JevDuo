"""Controller authentication and safety policy enforcement."""

from __future__ import annotations

import hmac
import json

from northcua.controller.config import ControllerSecurityConfig
from northcua_protocol.artifacts import StorageKind
from northcua_protocol.capabilities import Capability, DeviceDescriptor, JsonObject, ToolDescriptor
from northcua_protocol.payloads import (
    COMMAND_RUNTIME_SIDE_CHANNEL_KEYS,
    CommandRequest,
    TaskRequest,
    sanitize_command_runtime_side_channels,
)
from northcua_runtime_shared.validation import validate_command_against_tool


class ControllerAuth:
    """Validate device registration and command safety policy."""

    def __init__(self, config: ControllerSecurityConfig) -> None:
        self.config = config

    def validate_device_hello(self, descriptor: DeviceDescriptor, token: str) -> None:
        """Validate device token and allowlist.

        Args:
            descriptor: Device descriptor advertised during DEVICE_HELLO.
            token: Bearer token supplied by the device worker.

        Raises:
            PermissionError: If token or device allowlist checks fail.
        """

        expected_token = self.config.device_token or self.config.token
        if not self.config.insecure_dev_no_auth and not expected_token:
            raise PermissionError("device token is required")
        if not self.config.insecure_dev_no_auth and not hmac.compare_digest(str(token), str(expected_token)):
            raise PermissionError("device token rejected")
        if self.config.allowed_device_ids and descriptor.device_id not in self.config.allowed_device_ids:
            raise PermissionError("device id is not allowlisted")

    def validate_task_request(self, request: TaskRequest, *, skip_submitter_token: bool = False) -> None:
        """Validate submitter token and requested capability allowlist.

        Args:
            request: Task request received from a submitter or self-submitting device.
            skip_submitter_token: Whether a prior authenticated device connection
                has already established authority for this task.

        Raises:
            PermissionError: If token or capability allowlist checks fail.
            ValueError: If metadata or inline artifacts exceed configured limits.
        """

        expected_token = self.config.submitter_token or self.config.token
        if not skip_submitter_token and not self.config.insecure_dev_no_auth and not expected_token:
            raise PermissionError("submitter token is required")
        if (
            not skip_submitter_token
            and not self.config.insecure_dev_no_auth
            and not hmac.compare_digest(str(request.token), str(expected_token))
        ):
            raise PermissionError("submitter token rejected")
        if self.config.allowed_capabilities:
            disallowed = request.required_capabilities.difference(self.config.allowed_capabilities)
            if disallowed:
                raise PermissionError(f"capability is not allowlisted: {sorted(item.value for item in disallowed)}")
        metadata_bytes = len(json.dumps(request.metadata, ensure_ascii=False).encode("utf-8"))
        if metadata_bytes > self.config.max_task_metadata_bytes:
            raise ValueError("task metadata exceeds controller max_task_metadata_bytes")
        request_bytes = len(request.model_dump_json().encode("utf-8"))
        if request_bytes > self.config.max_envelope_bytes:
            raise ValueError("task request exceeds controller max_envelope_bytes")
        for artifact in request.input_artifacts:
            if artifact.storage_kind != StorageKind.INLINE:
                raise ValueError(
                    "controller_blob task input artifacts are not accepted until "
                    "controller fetch/materialization exists"
                )
            if artifact.size_bytes > self.config.max_envelope_bytes:
                raise ValueError("inline task artifact exceeds controller max_envelope_bytes")

    def validate_submitter_read_token(self, token: str) -> None:
        """Validate a submitter token for read-only task status APIs.

        Args:
            token: Bearer token supplied by the status/list caller.

        Raises:
            PermissionError: If the token is required and does not match.
        """

        expected_token = self.config.submitter_token or self.config.token
        if self.config.insecure_dev_no_auth:
            return
        if not expected_token:
            raise PermissionError("submitter token is required")
        if not hmac.compare_digest(str(token), str(expected_token)):
            raise PermissionError("submitter token rejected")

    def validate_device_self_submission(
        self,
        request: TaskRequest,
        *,
        device_id: str,
        device_capabilities: set[Capability],
    ) -> None:
        """Validate tokenless self-submission coming from an authenticated device.

        Args:
            request: Task request submitted on the device websocket.
            device_id: Authenticated device id bound to the websocket.
            device_capabilities: Capabilities advertised by that device.

        Raises:
            PermissionError: If the device targets another sandbox or requests
                capabilities it does not advertise.
        """

        if request.target_device_id and request.target_device_id != device_id:
            raise PermissionError("device connection cannot target a different device without submitter token")
        disallowed = request.required_capabilities.difference(device_capabilities)
        if disallowed:
            raise PermissionError(
                "device connection cannot request capabilities it does not advertise: "
                f"{sorted(capability.value for capability in disallowed)}"
            )

    def validate_command(self, command: CommandRequest, *, descriptor: ToolDescriptor | None = None) -> None:
        """Validate command tool allowlist, payload size, and optional schema.

        Args:
            command: Command request about to be dispatched to a device.
            descriptor: Optional tool descriptor used for JSON-schema checks.

        Raises:
            PermissionError: If the command tool id is not allowlisted.
            ValueError: If parameters are too large or fail descriptor validation.
        """

        if self.config.allowed_tools and command.tool_id not in self.config.allowed_tools:
            raise PermissionError("tool is not allowlisted by controller")
        payload_bytes = len(json.dumps(command.parameters, ensure_ascii=False).encode("utf-8"))
        if payload_bytes > self.config.max_envelope_bytes:
            raise ValueError("command payload exceeds controller max_envelope_bytes")
        sanitize_command_runtime_side_channels(command.parameters)
        if descriptor is not None:
            if self.config.allowed_capabilities and descriptor.capability not in self.config.allowed_capabilities:
                raise PermissionError(f"tool capability is not allowlisted: {descriptor.capability.value}")
            validate_command_against_tool(
                tool_id=command.tool_id,
                parameters=_command_schema_parameters(command.parameters),
                descriptor=descriptor,
                max_payload_bytes=self.config.max_envelope_bytes,
            )


def _command_schema_parameters(parameters: JsonObject) -> JsonObject:
    """Return tool-visible parameters for descriptor schema validation.

    Runtime-only side channels are carried inside ``CommandRequest.parameters``
    so remote adapters can bind the command to the evaluated session/device.
    They are not part of the model-visible tool contract, so controller-side
    descriptor validation must ignore them while preserving the original
    command payload for dispatch.
    """

    return {key: value for key, value in parameters.items() if key not in COMMAND_RUNTIME_SIDE_CHANNEL_KEYS}
