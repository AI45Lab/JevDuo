"""Cross-platform CLI MCP server aligned with the old CommandLineExecutor."""

from __future__ import annotations

import logging
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from northcua.runtime.mcp.contracts import register_contract_handlers
from northcua.runtime.mcp.registry import MCPFactoryContext, NorthCUAMCPRegistry
from northcua_runtime_shared.tool_contracts import McpBinding, ToolContract

logger = logging.getLogger(__name__)

COMMAND_LINE_EXECUTOR_NAME = "CommandLineExecutor"


class ExecCommandParams(BaseModel):
    """Input parameters for the guarded local command executor."""

    bash_command: str = Field(
        min_length=1,
        description=(
            "Application launch command or guarded shell command. Supports simple fallback chains such as "
            "`open -a Calculator || open -a Notes`."
        ),
    )


EXEC_COMMAND_CONTRACT = ToolContract(
    tool_id="exec",
    description=(
        "Launch an application or run a guarded local shell command. Supports simple fallback chains while "
        "blocking clearly unsafe download, escalation, and destructive patterns."
    ),
    namespace="runtime",
    agent_kind="desktop",
    params_model=ExecCommandParams,
    timeout_seconds=30.0,
    side_effect=True,
    requires_focus=False,
    max_payload_bytes=64_000,
    binding=McpBinding(server_name=COMMAND_LINE_EXECUTOR_NAME),
)


@dataclass(slots=True, frozen=True)
class CommandSecurityPolicy:
    """Minimal dangerous-pattern policy for command execution.

    Attributes:
        dangerous_patterns: Regex patterns blocked before process launch.
    """

    dangerous_patterns: tuple[re.Pattern[str], ...] = field(
        default_factory=lambda: (
            re.compile(r"Invoke-Expression|IEX\b", re.IGNORECASE),
            re.compile(r"Invoke-WebRequest|IWR\b|Invoke-RestMethod|IRM\b", re.IGNORECASE),
            re.compile(r"Start-Process\b", re.IGNORECASE),
            re.compile(r"New-Object\s+.*Net\.WebClient", re.IGNORECASE),
            re.compile(r"DownloadString|DownloadFile", re.IGNORECASE),
            re.compile(r"\bAdd-Type\b", re.IGNORECASE),
            re.compile(r"\b(cmd|powershell|pwsh)(\.exe)?\s+[/-]", re.IGNORECASE),
            re.compile(r"[|;&`]\s*(bash|sh|cmd|powershell|pwsh)", re.IGNORECASE),
            re.compile(r"\bNew-Service\b|\bsc\.exe\b", re.IGNORECASE),
            re.compile(r"\breg(\.exe)?\s+(add|delete|import)", re.IGNORECASE),
            re.compile(r"\bschtasks(\.exe)?\b", re.IGNORECASE),
            re.compile(r"\bnet\s+(user|localgroup)\b", re.IGNORECASE),
            re.compile(r"\bSet-ExecutionPolicy\b", re.IGNORECASE),
            re.compile(r"\bRemove-Item\b.*-Recurse", re.IGNORECASE),
            re.compile(r"\brm\s+-rf\b", re.IGNORECASE),
            re.compile(r"[`$]\(", re.IGNORECASE),
            re.compile(r"\bcurl\b|\bwget\b", re.IGNORECASE),
            re.compile(r"\brdp\b|\bmstsc\b", re.IGNORECASE),
            re.compile(r">{1,2}\s*[/\\]", re.IGNORECASE),
        )
    )

    def validate(self, command: str) -> None:
        """Raise ToolError when a command violates the policy.

        Args:
            command: Command string to validate.

        Raises:
            ToolError: If the command is empty, malformed, or blocked.
        """

        if not command or not command.strip():
            raise ToolError("Bash command cannot be empty.")
        for segment in split_fallback_segments(command):
            try:
                tokens = shlex.split(segment)
            except ValueError as exc:
                raise ToolError(f"Invalid command syntax: {segment}") from exc
            if not tokens:
                raise ToolError("Command blocked because one fallback segment is empty.")
        for pattern in self.dangerous_patterns:
            if pattern.search(command):
                logger.warning(
                    "Blocked CLI command matching dangerous pattern %s: %s",
                    pattern.pattern,
                    command[:200],
                )
                raise ToolError(
                    "Command blocked by security policy. The command contains shell "
                    "behavior or patterns that are considered unsafe."
                )


def split_fallback_segments(command: str) -> list[str]:
    """Split a command string on top-level `||` while respecting quotes.

    Args:
        command: Raw command string.

    Returns:
        Non-empty fallback command segments.
    """

    segments: list[str] = []
    current: list[str] = []
    quote_char = ""
    escape_next = False
    index = 0

    while index < len(command):
        char = command[index]

        if escape_next:
            current.append(char)
            escape_next = False
            index += 1
            continue

        if char == "\\":
            current.append(char)
            escape_next = True
            index += 1
            continue

        if quote_char:
            current.append(char)
            if char == quote_char:
                quote_char = ""
            index += 1
            continue

        if char in {"'", '"'}:
            quote_char = char
            current.append(char)
            index += 1
            continue

        if char == "|" and index + 1 < len(command) and command[index + 1] == "|":
            segment = "".join(current).strip()
            if segment:
                segments.append(segment)
            current = []
            index += 2
            continue

        current.append(char)
        index += 1

    tail = "".join(current).strip()
    if tail:
        segments.append(tail)
    return segments


def segment_requires_shell(segment: str) -> bool:
    """Return whether a segment contains shell operators requiring parsing.

    Args:
        segment: Single fallback command segment.

    Returns:
        True when the segment should be launched through a shell.
    """

    quote_char = ""
    escape_next = False
    previous_non_space = ""

    for char in segment:
        if escape_next:
            escape_next = False
            if not char.isspace():
                previous_non_space = char
            continue
        if char == "\\":
            escape_next = True
            continue
        if quote_char:
            if char == quote_char:
                quote_char = ""
            continue
        if char in {"'", '"'}:
            quote_char = char
            continue
        if char in {"|", ";", ">", "<"}:
            return True
        if char == "&" and previous_non_space == "&":
            return True
        if not char.isspace():
            previous_non_space = char

    return False


@dataclass(slots=True)
class CommandLauncher:
    """Execute the first successful segment in a fallback chain.

    Attributes:
        launch_wait_seconds: Delay after a launch attempt before returning.
    """

    launch_wait_seconds: float = 5.0

    def launch(self, command: str) -> str:
        """Run a command or fallback chain and return a textual tool result.

        Args:
            command: Command string, optionally containing fallback segments.

        Returns:
            Plain-text launch confirmation.

        Raises:
            ToolError: If no segment can be launched.
        """

        last_error: Exception | None = None
        for segment in split_fallback_segments(command):
            try:
                if segment_requires_shell(segment):
                    subprocess.Popen(segment, shell=True)
                else:
                    subprocess.Popen(shlex.split(segment), shell=False)
                time.sleep(self.launch_wait_seconds)
                return f"Executed command: {segment}"
            except Exception as exc:  # pragma: no cover - defensive boundary
                logger.warning("CLI launch segment failed: %s (%s)", segment, exc)
                last_error = exc
        if last_error is not None:
            raise ToolError(f"Failed to launch application: {last_error}")
        raise ToolError("No executable command segments were found.")


@NorthCUAMCPRegistry.register_decorator(COMMAND_LINE_EXECUTOR_NAME, tool_contracts=[EXEC_COMMAND_CONTRACT])
def create_cli_mcp_server(context: MCPFactoryContext) -> FastMCP:
    """Create the local CLI MCP server instance.

    Args:
        context: MCP factory context supplied by the registry.

    Returns:
        FastMCP server exposing the guarded CLI execution tool.
    """

    del context
    mcp = FastMCP("NorthCUA CLI MCP Server")
    policy = CommandSecurityPolicy()
    launcher = CommandLauncher()

    def _exec_handler(params: ExecCommandParams) -> str:
        """Validate and launch a command or fallback chain.

        Args:
            params: Validated command parameters from the canonical contract.

        Returns:
            Plain-text command execution confirmation.
        """

        policy.validate(params.bash_command)
        return launcher.launch(params.bash_command)

    register_contract_handlers(mcp, [EXEC_COMMAND_CONTRACT], {"exec": _exec_handler})

    return mcp
