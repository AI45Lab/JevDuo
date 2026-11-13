"""agent-desktop commands pinned to NorthCUA's current OSWorld environment."""

from __future__ import annotations

import json
import time

import httpx
from jev_desktop.desktop import CATALOG
from jev_desktop.transport import DesktopConfig, execution_result, uncertain
from pydantic import Field

from northcua.agentkit.orchestration.contracts import ContractModel
from northcua.core.config import DesktopPolicyConfig


class JevCommand(ContractModel):
    operation: str = "cli"
    argv: list[str] = Field(default_factory=list)
    timeout: float = Field(default=30, gt=0, le=305, allow_inf_nan=False)


def snapshot_timeout(argv: list[str]) -> int | None:
    args = list(argv)
    while args:
        if args[0] in ("--session", "--agent-id") and len(args) >= 2:
            args = args[2:]
        elif args[0] == "--headed":
            args = args[1:]
        else:
            break
    if not args or args[0] != "snapshot":
        return None
    values = []
    for index, arg in enumerate(args[1:], 1):
        if arg == "--timeout-ms":
            if index + 1 >= len(args):
                raise ValueError("snapshot timeout is missing")
            values.append(args[index + 1])
        elif arg.startswith("--timeout-ms="):
            values.append(arg.partition("=")[2])
    if not values:
        return None
    if len(values) != 1:
        raise ValueError("snapshot timeout must be specified once")
    value = int(values[0])
    if not 1 <= value <= 300000:
        raise ValueError("snapshot timeout must be between 1 and 300000 ms")
    return value


def cleanup_command(argv: list[str]) -> bool:
    args = list(argv)
    while len(args) >= 2 and args[0] in ("--session", "--agent-id"):
        args = args[2:]
    return (
        args == ["clipboard-clear"]
        or args == ["cursor-overlay", "disable"]
        or (len(args) == 2 and args[0] == "clipboard-set")
    )


class JevEnvironment:
    def __init__(self, desktop: DesktopPolicyConfig, config: DesktopConfig):
        if desktop.backend != "osworld":
            raise ValueError("gui_jev currently requires desktop.backend=osworld")
        self.endpoint = f"http://{desktop.osworld.vm_ip}:{desktop.osworld.server_port}/execute"
        self.config = config

    async def execute(self, arguments: dict, *, cleanup: bool = False) -> dict:
        command = JevCommand.model_validate(arguments)
        if cleanup:
            if command.operation != "cli" or not cleanup_command(command.argv):
                raise ValueError("gui_jev cleanup permits only clipboard restoration and cursor disable")
        if command.operation == "cli":
            executable, argv = self.config.binary, command.argv
        elif command.operation == "catalog" and not command.argv and not cleanup:
            executable, argv = "python3", ["-c", CATALOG]
        else:
            raise ValueError("unknown_agent_desktop_operation")
        native_ms = snapshot_timeout(argv) if command.operation == "cli" else None
        timeout = max(command.timeout, native_ms / 1000 + 5) if native_ms is not None else command.timeout
        diagnostic = {
            "command": "snapshot" if native_ms is not None else command.operation,
            "requested_timeout_seconds": command.timeout,
            "timeout_seconds": timeout,
        }
        if native_ms is not None:
            diagnostic["snapshot_timeout_ms"] = native_ms
        started = time.monotonic()
        phase = "transport"
        try:
            # One POST only: losing a response never authorizes replaying a write.
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                async with client.stream(
                    "POST",
                    self.endpoint,
                    json={"command": [executable, *argv], "shell": False},
                    timeout=timeout,
                ) as response:
                    diagnostic["http_status"] = response.status_code
                    response.raise_for_status()
                    phase = "response_read"
                    payload = bytearray()
                    async for chunk in response.aiter_bytes():
                        payload.extend(chunk)
                        if len(payload) > 64 * 1024 * 1024:
                            phase = "response_limit"
                            raise ValueError("execution response exceeds 64 MiB")
            diagnostic["response_bytes"] = len(payload)
            phase = "response_json"
            envelope = json.loads(payload)
            phase = "execution_envelope"
            output, returncode = envelope["output"], envelope["returncode"]
            if not isinstance(output, str) or type(returncode) is not int:
                raise ValueError("invalid execution envelope")
            diagnostic["returncode"] = returncode
            phase = "native_output"
            result = execution_result(output, returncode)
            diagnostic["outcome"] = "success" if result["ok"] else "native_error"
            if native_ms is not None and result["ok"]:
                data = result.get("data", {})
                if isinstance(data, dict):
                    for key in ("identity_unproven", "complete", "truncated"):
                        if type(data.get(key)) is bool:
                            diagnostic[key] = data[key]
        except (httpx.HTTPError, ValueError, KeyError, TypeError, UnicodeError) as error:
            result = uncertain("REMOTE_EXECUTION_UNCONFIRMED")
            diagnostic.update(
                outcome="unconfirmed",
                phase=phase,
                error_type=type(error).__name__,
                failure_kind=(
                    "timeout"
                    if isinstance(error, httpx.TimeoutException)
                    else "http_status"
                    if isinstance(error, httpx.HTTPStatusError)
                    else "transport"
                    if isinstance(error, httpx.HTTPError)
                    else phase
                ),
            )
        diagnostic["elapsed_seconds"] = round(time.monotonic() - started, 3)
        result["execution_diagnostic"] = diagnostic
        return result
