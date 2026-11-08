"""AgentKit spec for the bundled desktop agent."""

from __future__ import annotations

import sys

from northcua.agentinfra.runtime.protocol import DesktopRunRequest
from northcua.agentkit.builtin_common import controller_args, serve_args, tool_specs_from_mcp_server_prefix
from northcua.agentkit.spec import (
    AgentCliArgumentSpec,
    AgentCliCommandSpec,
    AgentCliSpec,
    AgentPresentationSpec,
    AgentRemoteSpec,
    AgentRuntimeSpec,
    AgentSpec,
    ObservationSpec,
    PromptSpec,
)
from northcua.core.config import DesktopPolicyConfig
from northcua.core.protocol import DesktopTaskRequestPayload
from northcua_protocol.capabilities import Capability, PlatformKind

_DESKTOP_MCP_REGISTRATION_HOOK = "northcua.runtime.mcp.builtin_servers:ensure_builtin_mcp_servers_registered"
_DESKTOP_REMOTE_CAPABILITIES = frozenset(
    {Capability.DESKTOP_INPUT, Capability.DESKTOP_SCREENSHOT, Capability.DESKTOP_WINDOW}
)


def desktop_agent_spec() -> AgentSpec:
    """Return the bundled desktop AgentKit spec.

    Returns:
        AgentKit spec that wires desktop CLI, runtime, presentation, and remote
        runner metadata.
    """
    return AgentSpec(
        runtime=_desktop_runtime_spec(),
        cli=_desktop_cli_spec(),
        remote=_desktop_remote_spec(),
        prompt=PromptSpec(
            system_template="northcua/agents/desktop/prompt/templates/computer_using_agent_system.j2",
            observation_template="northcua/agents/desktop/prompt/templates/computer_using_agent_observation_user.j2",
        ),
        observation=ObservationSpec(default_probe_names=("desktop_screenshot", "desktop_a11y")),
        presentation=AgentPresentationSpec(renderer_cls="northcua.app.terminal_renderer:NorthCUATerminalRenderer"),
        settings_model=DesktopPolicyConfig,
        request_model=DesktopRunRequest,
        task_payload_model=DesktopTaskRequestPayload,
        application_cls="northcua.agents.desktop.application:DesktopApplication",
        facade_cls="northcua.agents.desktop.facade:NorthCUAAgentFacade",
        server_cls="northcua.agents.desktop.server:NorthCUAWebSocketServer",
        runner_cls=None,
        artifact_store_cls="northcua.runtime.artifacts.store:NorthCUAArtifactStore",
        tools=tool_specs_from_mcp_server_prefix("DesktopNative", registration_hook=_DESKTOP_MCP_REGISTRATION_HOOK),
    )


def _desktop_runtime_spec() -> AgentRuntimeSpec:
    """Return runtime metadata advertised to AgentKit."""

    return AgentRuntimeSpec(
        kind="desktop",
        display_name="NorthCUA Desktop",
        platform={"darwin": PlatformKind.MACOS, "win32": PlatformKind.WINDOWS}.get(sys.platform, PlatformKind.LINUX),
        capabilities=_DESKTOP_REMOTE_CAPABILITIES,
    )


def _desktop_cli_spec() -> AgentCliSpec:
    """Return desktop CLI command metadata."""

    return AgentCliSpec(
        agent_name="desktop",
        help="Desktop Computer Using Agent commands",
        local_commands=(
            _run_command_spec(),
            _serve_command_spec(),
            _evaluate_command_spec(),
        ),
        submit_command=_submit_command_spec(),
    )


def _run_command_spec() -> AgentCliCommandSpec:
    """Return local desktop run command metadata."""

    return AgentCliCommandSpec(
        name="run",
        help="Run one desktop CUA task locally",
        handler="northcua.cli.desktop_commands:run_task_command",
        arguments=(
            _app_config_arg(),
            AgentCliArgumentSpec(("--log-dir",), dest="log_dir", default=None, help="Artifact root override"),
            _query_arg(),
            _session_arg(),
            AgentCliArgumentSpec(("--max-steps",), dest="max_steps", type=int, default=100, help="Maximum steps"),
            _max_session_steps_arg(),
        ),
    )


def _serve_command_spec() -> AgentCliCommandSpec:
    """Return desktop websocket server command metadata."""

    return AgentCliCommandSpec(
        name="serve",
        help="Start the desktop websocket server",
        handler="northcua.cli.desktop_commands:run_server_command",
        arguments=serve_args(),
    )


def _evaluate_command_spec() -> AgentCliCommandSpec:
    """Return desktop evaluator command metadata."""

    return AgentCliCommandSpec(
        name="evaluate",
        help="Run the desktop evaluator from an existing artifact directory",
        handler="northcua.cli.desktop_commands:evaluate_artifact_command",
        arguments=(
            _app_config_arg(),
            AgentCliArgumentSpec(
                ("--artifact-dir",),
                dest="artifact_dir",
                required=True,
                help="Session artifact directory containing steps.jsonl",
            ),
            AgentCliArgumentSpec(
                ("--round-num",),
                dest="round_num",
                type=int,
                default=0,
                help="Round number to evaluate; defaults to latest",
            ),
            AgentCliArgumentSpec(
                ("--no-rollup",),
                dest="no_rollup",
                action="store_true",
                help="Skip session_evaluation.json rollup",
            ),
        ),
    )


def _submit_command_spec() -> AgentCliCommandSpec:
    """Return remote controller submit command metadata."""

    return AgentCliCommandSpec(
        name="submit",
        help="Submit one desktop task to a remote controller",
        handler="northcua.cli.desktop_commands:submit_task_command",
        arguments=(
            _app_config_arg(),
            _query_arg(),
            _session_arg(),
            AgentCliArgumentSpec(("--max-steps",), dest="max_steps", type=int, default=100, help="Max steps"),
            _max_session_steps_arg(),
            *controller_args(),
        ),
    )


def _desktop_remote_spec() -> AgentRemoteSpec:
    """Return controller-side desktop runner metadata."""

    return AgentRemoteSpec(
        enabled=True,
        required_capabilities=_DESKTOP_REMOTE_CAPABILITIES,
        runner_factory="northcua_controller_plugins.agentkit_desktop_runner:build_desktop_remote_task_runner",
    )


def _app_config_arg() -> AgentCliArgumentSpec:
    """Return the shared ``--app-config`` argument spec."""

    return AgentCliArgumentSpec(("--app-config",), dest="app_config_path", default=None, help="Path to app.yaml")


def _query_arg() -> AgentCliArgumentSpec:
    """Return the shared required query argument spec."""

    return AgentCliArgumentSpec(("--query",), dest="query", required=True, help="User task/query")


def _session_arg() -> AgentCliArgumentSpec:
    """Return the shared optional session-id argument spec."""

    return AgentCliArgumentSpec(("--session-id",), dest="session_id", default="", help="Optional session id")


def _max_session_steps_arg() -> AgentCliArgumentSpec:
    """Return the shared cumulative session cap argument spec."""

    return AgentCliArgumentSpec(
        ("--max-session-steps",),
        dest="max_session_steps",
        type=int,
        default=0,
        help="Maximum cumulative session steps; 0 disables the session cap",
    )
