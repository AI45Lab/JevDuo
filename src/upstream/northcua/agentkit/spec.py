"""Declarative contracts for low-code NorthCUA agents."""

from __future__ import annotations

import argparse
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path

from pydantic import BaseModel

from northcua_protocol.capabilities import Capability, PlatformKind, ToolRiskLevel
from northcua_runtime_shared.tool_contracts import ToolContract

AsyncCliHandler = Callable[[argparse.Namespace], Awaitable[int]]


class _StoreAppConfigAliasAction(argparse.Action):
    """Store ``--app-config`` under both old and new namespace names."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | object,
        option_string: str | None = None,
    ) -> None:
        del parser, option_string
        setattr(namespace, self.dest, values)
        setattr(namespace, "app_config", values)


class ToolExecutionMode(str, Enum):
    """Where model-visible tools should execute."""

    LOCAL_IN_PROCESS = "local_in_process"
    REMOTE_DISPATCH = "remote_dispatch"


@dataclass(slots=True, frozen=True)
class ToolUseRef:
    """AgentKit use-site reference for an owned tool contract.

    Tool facts live on ``contract``. This ref carries only the local handler and
    model-visibility use policy needed by AgentKit runtimes.
    """

    contract: ToolContract
    handler: Callable[..., object] | None = None
    model_visible: bool = True

    @classmethod
    def from_contract(
        cls,
        contract: ToolContract,
        *,
        handler: Callable[..., object] | None = None,
        model_visible: bool | None = None,
    ) -> "ToolUseRef":
        """Create an AgentKit tool ref from the canonical contract."""

        visible = contract.model_visible if model_visible is None else model_visible
        return cls(contract=contract, handler=handler, model_visible=visible)

    def __post_init__(self) -> None:
        """Validate use-site policy against the canonical contract."""

        if self.model_visible is True and not self.contract.model_visible:
            raise ValueError("ToolUseRef.model_visible cannot expose a non-model-visible ToolContract")

    @property
    def tool_id(self) -> str:
        """Return the stable controller/device tool id.

        Returns:
            The unprefixed id; device adapters add an agent prefix when needed.
        """

        return self.name

    @property
    def name(self) -> str:
        return self.contract.tool_id

    @property
    def description(self) -> str:
        return self.contract.description

    @property
    def params_model(self) -> type[BaseModel] | None:
        return self.contract.params_model

    @property
    def is_model_visible(self) -> bool:
        return self.model_visible and self.contract.model_visible

    @property
    def capability(self) -> Capability:
        return self.contract.capability

    @property
    def server_name(self) -> str:
        return self.contract.server_name

    @property
    def registration_hook(self) -> str:
        return self.contract.registration_hook

    @property
    def timeout_seconds(self) -> float:
        return self.contract.timeout_seconds

    @property
    def side_effect(self) -> bool:
        return self.contract.side_effect

    @property
    def requires_focus(self) -> bool:
        return self.contract.requires_focus

    @property
    def risk_level(self) -> ToolRiskLevel:
        return self.contract.risk_level

    @property
    def parameters_schema(self) -> dict[str, object]:
        """Return the contract-owned JSON schema for the tool parameters."""

        return dict(self.contract.parameters_schema)


@dataclass(slots=True, frozen=True)
class ProbeUseRef:
    """AgentKit use-site reference for an owned probe contract."""

    contract: ToolContract
    handler: Callable[..., object] | None = None
    default: bool = True
    persist: bool = True
    prompt: bool = True

    @classmethod
    def from_contract(
        cls,
        contract: ToolContract,
        *,
        handler: Callable[..., object] | None = None,
        default: bool = True,
        persist: bool = True,
        prompt: bool = True,
    ) -> "ProbeUseRef":
        """Create an AgentKit probe ref from the canonical contract."""

        return cls(
            handler=handler,
            contract=contract,
            default=default,
            persist=persist,
            prompt=prompt,
        )

    @property
    def tool_id(self) -> str:
        """Return the stable controller/device tool id.

        Returns:
            The unprefixed id; device adapters add an agent prefix when needed.
        """

        return self.name

    @property
    def name(self) -> str:
        return self.contract.tool_id

    @property
    def description(self) -> str:
        return self.contract.description

    @property
    def params_model(self) -> type[BaseModel] | None:
        return self.contract.params_model

    @property
    def capability(self) -> Capability:
        return self.contract.capability

    @property
    def server_name(self) -> str:
        return self.contract.server_name

    @property
    def registration_hook(self) -> str:
        return self.contract.registration_hook

    @property
    def timeout_seconds(self) -> float:
        return self.contract.timeout_seconds

    @property
    def parameters_schema(self) -> dict[str, object]:
        """Return the contract-owned JSON schema for the probe parameters."""

        return dict(self.contract.parameters_schema)


@dataclass(slots=True, frozen=True)
class AgentRuntimeSpec:
    """Runtime identity and platform metadata for one agent.

    Attributes:
        kind: Stable registry key and task payload agent kind.
        display_name: Human-readable name used in terminal panels.
        platform: Primary execution platform advertised to controller/device.
        capabilities: Additional capabilities the agent may require or expose.
        enabled: Whether the agent participates in generated CLI/registry output.
    """

    kind: str
    display_name: str
    platform: PlatformKind
    capabilities: frozenset[Capability] = field(default_factory=frozenset)
    enabled: bool = True


@dataclass(slots=True, frozen=True)
class AgentCliArgumentSpec:
    """Declarative CLI argument used by generated adapters.

    Attributes:
        flags: Option strings passed to argparse, such as ``("--query",)``.
        dest: Namespace attribute populated by argparse.
        help: Help text shown in generated CLI output.
        required: Whether argparse should require the option.
        default: Default value when the option is absent.
        type: Optional string conversion callable for argparse.
        action: Optional argparse action name or custom action.
    """

    flags: tuple[str, ...]
    dest: str = ""
    help: str = ""
    required: bool = False
    default: object = None
    type: Callable[[str], object] | None = None
    action: str | None = None

    def add_to(self, parser: argparse.ArgumentParser) -> None:
        """Register this argument on an argparse parser.

        Args:
            parser: Parser for a generated local or submit subcommand.
        """

        kwargs: dict[str, object] = {}
        if self.dest:
            kwargs["dest"] = self.dest
        if self.help:
            kwargs["help"] = self.help
        if self.required:
            kwargs["required"] = self.required
        if self.default is not None:
            kwargs["default"] = self.default
        if self.type is not None:
            kwargs["type"] = self.type
        if self.action is not None:
            kwargs["action"] = self.action
        elif self.dest == "app_config_path":
            kwargs["action"] = _StoreAppConfigAliasAction
        parser.add_argument(*self.flags, **kwargs)  # pyright: ignore[reportArgumentType]
        if self.dest == "app_config_path":
            parser.set_defaults(app_config=self.default)


@dataclass(slots=True, frozen=True)
class AgentCliCommandSpec:
    """One generated local or submit command.

    Attributes:
        name: Subcommand name such as ``run`` or ``serve``.
        help: Help text shown for the generated subcommand.
        handler: Import path or async callable invoked by the CLI adapter.
        arguments: Declarative argparse arguments appended to the subcommand.
        enabled: Whether this command should be registered.
    """

    name: str
    help: str
    handler: str | AsyncCliHandler
    arguments: tuple[AgentCliArgumentSpec, ...] = ()
    enabled: bool = True


@dataclass(slots=True, frozen=True)
class AgentCliSpec:
    """Generated CLI surface for one agent.

    Attributes:
        agent_name: CLI namespace under ``northcua local`` and ``submit``.
        help: Help text for the agent namespace.
        local_commands: Generated commands under ``northcua local <agent>``.
        submit_command: Optional command under ``northcua submit <agent>``.
    """

    agent_name: str
    help: str
    local_commands: tuple[AgentCliCommandSpec, ...] = ()
    submit_command: AgentCliCommandSpec | None = None


@dataclass(slots=True, frozen=True)
class AgentRemoteSpec:
    """Controller/device remote execution metadata.

    Attributes:
        enabled: Whether controller runner registration is generated.
        required_capabilities: Device capabilities needed before leasing.
        runner_factory: Import path to the controller-side runner factory.
        command_timeout_seconds: Default timeout for remote tool dispatch.
    """

    enabled: bool = False
    required_capabilities: frozenset[Capability] = field(default_factory=frozenset)
    runner_factory: str | None = None
    command_timeout_seconds: float = 30.0


@dataclass(slots=True, frozen=True)
class PromptSpec:
    """Prompt template metadata for documentation and generic runners.

    Attributes:
        system_template: Optional path or inline identifier for system text.
        observation_template: Optional path or inline identifier for observations.
    """

    system_template: str = ""
    observation_template: str = ""


@dataclass(slots=True, frozen=True)
class ObservationSpec:
    """Default observation policy metadata for documentation and generic runners.

    Attributes:
        default_probe_names: Probe names that generic loops collect each step.
        persist_all: Whether observations should be persisted by default.
        prompt_all: Whether observations should be projected into prompts.
    """

    default_probe_names: tuple[str, ...] = ()
    persist_all: bool = True
    prompt_all: bool = False


@dataclass(slots=True, frozen=True)
class AgentPresentationSpec:
    """Terminal presentation metadata for one low-code agent.

    Attributes:
        renderer_cls: Import path or class for the terminal renderer.
        panel_projector_cls: Optional projector that converts runner records
            into renderer-facing panel records.
        templates: Template names consumed by the default renderer.
    """

    renderer_cls: str | type[object] | None = None
    panel_projector_cls: str | type[object] | None = None
    templates: Mapping[str, str] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class AgentSessionSpec:
    """Session lifecycle metadata for low-code device/session adapters.

    Attributes:
        adapter_cls: Import path or class that owns platform session resources.
        close_tool_name: Unprefixed generated tool name for session teardown.
    """

    adapter_cls: str | type[object] | None = None
    close_tool_name: str = "close_session"


@dataclass(slots=True, frozen=True)
class ArtifactLayoutSpec:
    """Common artifact filenames and directories for low-code agents.

    Attributes:
        result_name: Final typed result file name.
        trace_name: Agent-level step trace file name.
        runtime_log_name: Runtime event log file name.
        llm_trace_name: Provider request/response trace file name.
        screenshot_dir: Directory for screenshot artifacts.
        dom_dir: Directory for DOM, a11y, or snapshot artifacts.
        replay_name: Replay evidence file name for evaluators.
        error_table_name: Structured issue/error table file name.
    """

    result_name: str = "result.json"
    trace_name: str = "trace.jsonl"
    runtime_log_name: str = "runtime.jsonl"
    llm_trace_name: str = "llm_trace.jsonl"
    screenshot_dir: str = "screenshots"
    dom_dir: str = "dom_snapshots"
    replay_name: str = "replay_evidence.json"
    error_table_name: str = "error_table.json"


@dataclass(slots=True, frozen=True)
class AgentSkillSpec:
    """Folder-based skill defaults for an AgentKit agent.

    Attributes:
        enabled: Whether AgentKit should merge this spec/config skill block
            into the underlying NexAU ``AgentConfig``.
        paths: Default skill folder paths. Relative paths are resolved by the
            config loader against the agent or app config directory.
        asset_tools_enabled: Whether AgentKit should expose read-only skill
            asset tools for this agent.
        asset_read_max_bytes: Maximum bytes returned by one asset read tool
            call before truncation or binary summary.
        allowed_file_extensions: Lower-case file suffixes readable as text.
            Empty string represents extensionless files.
        allow_binary_summary: Whether binary or disallowed files may return a
            size/checksum summary instead of failing.
        allow_missing: Whether missing configured skill folders are skipped.
            Production configs should usually keep this false.
    """

    enabled: bool = True
    paths: tuple[str, ...] = ()
    asset_tools_enabled: bool = False
    asset_read_max_bytes: int = 64 * 1024
    allowed_file_extensions: frozenset[str] = frozenset(
        {
            "",
            ".css",
            ".csv",
            ".html",
            ".ini",
            ".j2",
            ".jinja",
            ".js",
            ".json",
            ".md",
            ".py",
            ".sh",
            ".svg",
            ".toml",
            ".ts",
            ".txt",
            ".xml",
            ".yaml",
            ".yml",
        }
    )
    allow_binary_summary: bool = True
    allow_missing: bool = False


@dataclass(slots=True, frozen=True)
class AgentSpec:
    """One complete low-code agent declaration.

    Attributes:
        runtime: Stable identity, display name, platform, and enable flag.
        cli: Generated local and submit command declarations.
        remote: Controller/device runner metadata.
        prompt: Prompt template metadata for generic runners and docs.
        observation: Probe selection and prompt/persist defaults.
        presentation: Terminal renderer and panel projection metadata.
        session: Device/session lifecycle adapter metadata.
        artifacts: Common artifact layout for generic agents.
        skills: Folder-based skill defaults and optional asset tools.
        settings_model: Pydantic model for ``agents.<kind>.settings``.
        request_model: Optional local request model used by richer facades.
        result_model: Optional typed result model for public API responses.
        task_payload_model: Optional controller task payload validator.
        application_cls: Optional application class import path or object.
        facade_cls: Optional facade class import path or object.
        server_cls: Optional websocket server class import path or object.
        runner_cls: Optional local runner class import path or object.
        artifact_store_cls: Optional artifact store class import path or object.
        tools: Model-visible or device-executable action declarations.
        probes: Runtime observation declarations.
    """

    runtime: AgentRuntimeSpec
    cli: AgentCliSpec
    remote: AgentRemoteSpec = field(default_factory=AgentRemoteSpec)
    prompt: PromptSpec = field(default_factory=PromptSpec)
    observation: ObservationSpec = field(default_factory=ObservationSpec)
    presentation: AgentPresentationSpec = field(default_factory=AgentPresentationSpec)
    session: AgentSessionSpec = field(default_factory=AgentSessionSpec)
    artifacts: ArtifactLayoutSpec = field(default_factory=ArtifactLayoutSpec)
    skills: AgentSkillSpec = field(default_factory=AgentSkillSpec)
    settings_model: type[BaseModel] | None = None
    request_model: type[object] | None = None
    result_model: type[object] | None = None
    task_payload_model: type[BaseModel] | None = None
    application_cls: str | type[object] | None = None
    facade_cls: str | type[object] | None = None
    server_cls: str | type[object] | None = None
    runner_cls: str | type[object] | None = None
    artifact_store_cls: str | type[object] | None = None
    tools: tuple[ToolUseRef, ...] = ()
    probes: tuple[ProbeUseRef, ...] = ()

    @property
    def kind(self) -> str:
        """Return the stable agent kind.

        Returns:
            Registry key used by CLI, task payloads, and remote runners.
        """

        return self.runtime.kind

    @property
    def display_name(self) -> str:
        """Return the human-readable agent name.

        Returns:
            Name shown in terminal panels and generated help.
        """

        return self.runtime.display_name

    @property
    def enabled(self) -> bool:
        """Return whether this agent is enabled.

        Returns:
            True when generated registries and CLIs should expose the agent.
        """

        return self.runtime.enabled

    def all_device_tools(self) -> tuple[ToolUseRef | ProbeUseRef, ...]:
        """Return all device-executable tools and probes.

        Returns:
            Tool and probe declarations in the order registered by the agent.
        """

        return (*self.tools, *self.probes)

    def validate_settings(self, raw_settings: Mapping[str, object] | None = None) -> BaseModel | dict[str, object]:
        """Validate agent-specific settings against ``settings_model`` when present.

        Args:
            raw_settings: Raw config mapping from ``agents.<kind>.settings``.

        Returns:
            A Pydantic settings instance for typed agents, otherwise a plain
            copy of the provided mapping.
        """

        settings = dict(raw_settings or {})
        if self.settings_model is None:
            return settings
        return self.settings_model.model_validate(settings)


class ToolBundle:
    """Base class for low-code tool bundles.

    Subclasses declare methods with ``@cua_tool``. The registry can inspect the
    class without instantiating it, which keeps command surfaces cheap to load.
    """

    @classmethod
    def declared_tools(cls) -> tuple[ToolUseRef, ...]:
        """Return tool metadata declared on this bundle.

        Returns:
            Tool specs with handlers rebound to an instance of the bundle.
        """

        specs: list[ToolUseRef] = []
        instance = cls()
        for value in cls.__dict__.values():
            spec = getattr(value, "__northcua_tool_ref__", None)
            if isinstance(spec, ToolUseRef):
                bound_handler = getattr(instance, value.__name__, spec.handler)
                specs.append(replace(spec, handler=bound_handler))
        return tuple(specs)


class ProbeBundle:
    """Base class for low-code probe bundles."""

    @classmethod
    def declared_probes(cls) -> tuple[ProbeUseRef, ...]:
        """Return probe metadata declared on this bundle.

        Returns:
            Probe specs with handlers rebound to an instance of the bundle.
        """

        specs: list[ProbeUseRef] = []
        instance = cls()
        for value in cls.__dict__.values():
            spec = getattr(value, "__northcua_probe_ref__", None)
            if isinstance(spec, ProbeUseRef):
                bound_handler = getattr(instance, value.__name__, spec.handler)
                specs.append(replace(spec, handler=bound_handler))
        return tuple(specs)


class SessionAdapter:
    """Default per-session platform adapter.

    Rich agents can subclass this to own browser, mobile, VM, or app sessions.
    The default implementation is intentionally inert so simple handler-only
    agents can use AgentKit without lifecycle boilerplate.
    """

    def __init__(
        self,
        *,
        agent_kind: str = "",
        session_id: str = "",
        settings: BaseModel | Mapping[str, object] | None = None,
        artifact_root: str | Path | None = None,
    ) -> None:
        """Initialize session metadata shared by local and device adapters.

        Args:
            agent_kind: Agent kind that owns this adapter instance.
            session_id: Session identifier for a browser, desktop, VM, or device.
            settings: Typed or raw settings passed from AgentKit config.
            artifact_root: Root directory for session-owned artifacts.
        """

        self.agent_kind = agent_kind
        self.session_id = session_id
        self.settings = settings
        self.artifact_root = Path(artifact_root) if artifact_root is not None else None

    async def start(self) -> None:
        """Start or attach to the platform session."""

    async def close(self) -> None:
        """Close resources owned by this session adapter."""

    def ensure_device_session(self, *, command: object, artifact_root: Path) -> None:
        """Ensure a device-side MCP session exists before a tool executes.

        Args:
            command: Remote command request about to be executed.
            artifact_root: Device-side artifact root available to the session.
        """

        del command, artifact_root

    async def close_device_session(self, *, command: object, artifact_root: Path) -> object:
        """Close a device-side session and return a tool-result payload.

        Args:
            command: Remote close command that selected this session.
            artifact_root: Device-side artifact root available during cleanup.

        Returns:
            Tool-result payload sent back through the device data plane.
        """

        del command, artifact_root
        await self.close()
        return {"status": "ok", "text": f"closed {self.agent_kind or 'agent'} session"}


class ObservationProvider:
    """Default observation provider for generic stepwise agents."""

    async def collect(self, *, session: SessionAdapter, step_index: int) -> Mapping[str, object]:
        """Collect prompt/artifact-facing observation data.

        Args:
            session: Active session adapter for the current agent session.
            step_index: One-based step index within the current round.

        Returns:
            Mapping that prompt projectors and artifact writers may consume.
        """

        del session, step_index
        return {}


class PromptProjector:
    """Default prompt projector for generic stepwise agents."""

    def project(
        self, *, query: str, observation: Mapping[str, object], history: tuple[object, ...] = ()
    ) -> list[object]:
        """Project an observation and history into model messages.

        Args:
            query: User task for the current round.
            observation: Latest observation collected before the step.
            history: Prior step records or message fragments retained by policy.

        Returns:
            Provider-neutral message objects for the model step executor.
        """

        del history
        return [{"role": "user", "content": f"{query}\n\nObservation: {dict(observation)}"}]


class TerminalPolicy:
    """Default terminal policy for generic stepwise agents."""

    def should_stop(self, *, step_index: int, max_steps: int, result: object | None = None) -> bool:
        """Return whether a generic loop should stop.

        Args:
            step_index: One-based step index just completed or about to run.
            max_steps: Maximum allowed steps for the round.
            result: Optional latest step result for policies that inspect status.

        Returns:
            True when the generic loop should terminate.
        """

        del result
        return step_index >= max_steps
