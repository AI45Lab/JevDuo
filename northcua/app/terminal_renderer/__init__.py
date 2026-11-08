"""Public terminal renderer package entrypoint with lazy renderer exports."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .desktop import NorthCUATerminalRenderer
    from .evaluator import NorthCUAEvaluatorTerminalRenderer
    from .web import WebAgentTerminalRenderer

_LAZY_RENDERERS: dict[str, tuple[str, str]] = {
    "NorthCUAEvaluatorTerminalRenderer": (
        "northcua.app.terminal_renderer.evaluator",
        "NorthCUAEvaluatorTerminalRenderer",
    ),
    "NorthCUATerminalRenderer": ("northcua.app.terminal_renderer.desktop", "NorthCUATerminalRenderer"),
    "WebAgentTerminalRenderer": ("northcua.app.terminal_renderer.web", "WebAgentTerminalRenderer"),
}

__all__ = [
    "NorthCUAEvaluatorTerminalRenderer",
    "NorthCUATerminalRenderer",
    "WebAgentTerminalRenderer",
]


def __getattr__(name: str) -> object:
    """Load terminal renderer classes only when callers request them.

    Args:
        name: Public renderer class name.

    Returns:
        Renderer class exported by this package.

    Raises:
        AttributeError: If ``name`` is not a known renderer export.
    """

    try:
        module_name, attr_name = _LAZY_RENDERERS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name), attr_name)
    globals()[name] = value
    return value
