"""AgentKit spec for the bundled generic web agent."""

from __future__ import annotations

from northcua.agentinfra.runtime.protocol import WebRunRequest
from northcua.agentkit.builtin_common import (
    ToolContractProjector,
    controller_args,
    serve_args,
)
from northcua.agentkit.spec import (
    AgentCliArgumentSpec,
    AgentCliCommandSpec,
    AgentCliSpec,
    AgentPresentationSpec,
    AgentRemoteSpec,
    AgentRuntimeSpec,
    AgentSessionSpec,
    AgentSpec,
    ObservationSpec,
    PromptSpec,
)
from northcua.core.config import WebAgentConfig
from northcua.core.protocol import WebTaskRequestPayload
from northcua_protocol.capabilities import Capability, PlatformKind

from .mcp.descriptors import web_agent_action_contracts, web_agent_backend_contracts, web_agent_probe_contracts


def _web_run_arguments() -> tuple[AgentCliArgumentSpec, ...]:
    """Return local/submit arguments shared by web run commands."""

    return (
        AgentCliArgumentSpec(("--app-config",), dest="app_config_path", default=None, help="Path to app.yaml"),
        AgentCliArgumentSpec(("--query",), dest="query", required=True, help="User web task/query"),
        AgentCliArgumentSpec(("--url",), dest="url", default="", help="Optional initial URL"),
        AgentCliArgumentSpec(
            ("--headed",),
            dest="headed",
            action="store_true",
            default=None,
            help="Show the browser window while the web agent runs; omitted uses web.headed from config",
        ),
        AgentCliArgumentSpec(("--max-steps",), dest="max_steps", type=int, default=0, help="Max steps"),
        AgentCliArgumentSpec(
            ("--max-session-steps",),
            dest="max_session_steps",
            type=int,
            default=0,
            help="Maximum cumulative session steps; 0 disables the session cap",
        ),
        AgentCliArgumentSpec(("--session-id",), dest="session_id", default="", help="Optional session id"),
    )


def _web_cli_spec() -> AgentCliSpec:
    """Build the web-agent CLI declaration."""

    return AgentCliSpec(
        agent_name="web",
        help="Generic web-agent commands",
        local_commands=(
            AgentCliCommandSpec(
                name="run",
                help="Run one web task locally",
                handler="northcua.cli.web_commands:run_web_command",
                arguments=(
                    *_web_run_arguments(),
                    AgentCliArgumentSpec(
                        ("--evaluate",),
                        dest="evaluate",
                        action="store_true",
                        default=None,
                        help="Run the Web round evaluator after the local agent run.",
                    ),
                    AgentCliArgumentSpec(
                        ("--no-evaluate",),
                        dest="evaluate",
                        action="store_false",
                        help="Skip the Web round evaluator after the local agent run.",
                    ),
                    AgentCliArgumentSpec(("--log-dir",), dest="log_dir", default=None, help="Artifact root override"),
                ),
            ),
            AgentCliCommandSpec(
                name="serve",
                help="Start the web-agent websocket server",
                handler="northcua.cli.web_commands:run_web_server_command",
                arguments=serve_args(),
            ),
            AgentCliCommandSpec(
                name="evaluate",
                help="Run the Web evaluator directly from an existing artifact directory",
                handler="northcua.cli.web_commands:evaluate_web_artifact_command",
                arguments=(
                    AgentCliArgumentSpec(
                        ("--app-config",), dest="app_config_path", default=None, help="Path to app.yaml"
                    ),
                    AgentCliArgumentSpec(
                        ("--artifact-dir",),
                        dest="artifact_dir",
                        required=True,
                        help="Web session artifact directory containing result.json and web_agent_trace.jsonl.",
                    ),
                    AgentCliArgumentSpec(
                        ("--round-num",),
                        dest="round_num",
                        type=int,
                        default=1,
                        help="Round number to evaluate. Web runs currently default to round 1.",
                    ),
                ),
            ),
        ),
        submit_command=AgentCliCommandSpec(
            name="submit",
            help="Submit one web task to a remote controller",
            handler="northcua.cli.web_commands:submit_web_command",
            arguments=(*_web_run_arguments(), *controller_args()),
        ),
    )


def web_agent_spec() -> AgentSpec:
    """Return the bundled generic web AgentKit spec.

    Returns:
        AgentKit spec that wires web CLI, browser runtime, presentation,
        session adapter, and remote runner metadata.
    """

    contracts = ToolContractProjector()
    action_contracts = web_agent_action_contracts()
    backend_contracts = web_agent_backend_contracts()
    probe_contracts = web_agent_probe_contracts()
    # Keep web-agent CLI, remote runner, prompt, session adapter, and tool
    # contracts in one declarative spec for AgentKit registry discovery.
    return AgentSpec(
        runtime=AgentRuntimeSpec(
            kind="web",
            display_name="NorthCUA Web Agent",
            platform=PlatformKind.BROWSER,
            capabilities=frozenset({Capability.BROWSER_PAGE}),
        ),
        cli=_web_cli_spec(),
        remote=AgentRemoteSpec(
            enabled=True,
            required_capabilities=frozenset({Capability.BROWSER_PAGE}),
            runner_factory="northcua_controller_plugins.agentkit_web_runner:build_web_remote_task_runner",
        ),
        prompt=PromptSpec(
            system_template="northcua/agents/web/prompt/templates/web_agent_system.j2",
            observation_template="northcua/agents/web/prompt/templates/web_agent_observation_user.j2",
        ),
        observation=ObservationSpec(
            default_probe_names=tuple(contract.tool_id for contract in probe_contracts),
            persist_all=True,
        ),
        presentation=AgentPresentationSpec(renderer_cls="northcua.app.terminal_renderer:WebAgentTerminalRenderer"),
        session=AgentSessionSpec(
            adapter_cls="northcua.agents.web.session.agentkit_adapter:WebAgentDeviceSessionAdapter",
            close_tool_name="web_close_session",
        ),
        settings_model=WebAgentConfig,
        request_model=WebRunRequest,
        task_payload_model=WebTaskRequestPayload,
        application_cls="northcua.agents.web:WebAgentApplication",
        facade_cls="northcua.agents.web.facade:WebAgentFacade",
        server_cls="northcua.agents.web.server:WebAgentWebSocketServer",
        artifact_store_cls="northcua.agents.web.artifacts:WebArtifactStore",
        tools=contracts.tool_specs((*action_contracts, *backend_contracts)),
        probes=contracts.probe_specs(
            probe_contracts,
            default=False,
            persist=True,
            prompt=False,
        ),
    )
