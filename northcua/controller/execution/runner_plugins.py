"""Controller task-runner plugin registry and entry-point discovery."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import entry_points
from threading import RLock
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from northcua.controller.execution.runner_registry import RunnerRegistry
    from northcua.controller.execution.sdk import ControllerSDK


@dataclass(slots=True, frozen=True)
class ControllerRunnerRegistrationContext:
    """Context passed to controller runner plugins at startup time.

    Attributes:
        app_config_path: Root app config path used by agent runner factories.
        command_timeout_seconds: Default command timeout exposed to runners.
        artifact_root_dir_override: Optional artifact root for controller-owned runs.
    """

    app_config_path: str
    command_timeout_seconds: float
    artifact_root_dir_override: str | None = None


class ControllerRunnerPlugin(Protocol):
    """Callable plugin that registers controller task runners."""

    def __call__(
        self,
        runner_registry: "RunnerRegistry",
        sdk: "ControllerSDK",
        context: ControllerRunnerRegistrationContext,
    ) -> None:
        """Register one or more task runners on the provided registry.

        Args:
            runner_registry: Mutable controller runner registry.
            sdk: Controller SDK used by runners to send remote commands.
            context: Startup context shared with runner plugins.
        """


_plugin_lock = RLock()


@dataclass(slots=True, frozen=True)
class _RegisteredRunnerPlugin:
    plugin: ControllerRunnerPlugin
    source: str


_plugins_by_name: dict[str, _RegisteredRunnerPlugin] = {}
_plugins_loaded = False


def register_controller_runner_plugin(
    name: str,
    plugin: ControllerRunnerPlugin,
    *,
    source: str | None = None,
) -> None:
    """Register one controller runner plugin by name.

    Args:
        name: Stable plugin name used for replacement in tests.
        plugin: Callable that registers one or more runners.
    """

    with _plugin_lock:
        resolved_source = (
            source or f"{plugin.__module__}.{getattr(plugin, '__qualname__', plugin.__class__.__qualname__)}"
        )
        _plugins_by_name[name] = _RegisteredRunnerPlugin(plugin=plugin, source=resolved_source)


def clear_controller_runner_plugins() -> None:
    """Clear registered runner plugins.

    Tests use this to exercise plugin discovery deterministically.
    """

    global _plugins_loaded
    with _plugin_lock:
        _plugins_by_name.clear()
        _plugins_loaded = False


def load_controller_runner_plugins() -> None:
    """Load controller runner plugins from entry points exactly once."""

    global _plugins_loaded
    with _plugin_lock:
        if _plugins_loaded:
            return
        for entry_point in entry_points(group="northcua.controller.runners"):
            plugin = entry_point.load()
            source = getattr(entry_point, "value", None)
            register_controller_runner_plugin(
                getattr(entry_point, "name", "plugin"),
                plugin,
                source=source if isinstance(source, str) and source else None,
            )
        _plugins_loaded = True


def populate_runner_registry(
    *,
    runner_registry: "RunnerRegistry",
    sdk: "ControllerSDK",
    context: ControllerRunnerRegistrationContext,
) -> None:
    """Populate one runner registry from all discovered plugins.

    Args:
        runner_registry: Mutable registry to populate.
        sdk: Controller SDK passed to runner plugins.
        context: Startup context passed to runner plugins.
    """

    load_controller_runner_plugins()
    for registered in _plugins_by_name.values():
        with runner_registry.registration_scope(registered.source):
            registered.plugin(runner_registry, sdk, context)
