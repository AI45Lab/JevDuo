"""Contract-backed readonly shell MCP server for device/evaluator fallback."""

from __future__ import annotations

import asyncio

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from northcua.runtime.mcp.contracts import register_contract_handlers
from northcua.runtime.mcp.registry import MCPFactoryContext, NorthCUAMCPRegistry
from northcua_runtime_shared.device_shell_contracts import SHELL_EXEC_READONLY_CONTRACT
from northcua_runtime_shared.shell_readonly import readonly_shell_argv

DEVICE_SHELL_EXECUTOR_NAME = "DeviceShellExecutor"


@NorthCUAMCPRegistry.register_decorator(
    DEVICE_SHELL_EXECUTOR_NAME,
    tool_contracts=[SHELL_EXEC_READONLY_CONTRACT],
)
def create_device_shell_server(context: MCPFactoryContext) -> FastMCP:
    """Create a readonly shell MCP server for controller-side command dispatch."""

    del context
    mcp = FastMCP(DEVICE_SHELL_EXECUTOR_NAME)
    register_contract_handlers(
        mcp,
        [SHELL_EXEC_READONLY_CONTRACT],
        {"shell.exec_readonly": _shell_exec_readonly},
    )
    return mcp


async def _shell_exec_readonly(cmd: str) -> dict[str, object]:
    """Execute a readonly allowlisted shell probe."""

    argv = readonly_shell_argv(cmd)
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(
        process.communicate(),
        timeout=SHELL_EXEC_READONLY_CONTRACT.timeout_seconds,
    )
    result = {
        "returncode": process.returncode,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }
    if process.returncode != 0:
        raise ToolError(f"readonly shell command failed: {result}")
    return result


__all__ = ["DEVICE_SHELL_EXECUTOR_NAME", "create_device_shell_server"]
