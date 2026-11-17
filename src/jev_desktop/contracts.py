"""Interfaces for callers that supply their own model or desktop backend."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol

from .actions import DEFAULT_KEYS
from .text_request import TextRequest

Json = dict[str, Any]
TextSource = str | list[str] | Callable[[Json, list[Json]], str | None] | None


@dataclass(frozen=True)
class Scope:
    app: str | None = None
    root: str | None = None
    window_id: str | None = None


@dataclass
class Observation:
    nodes: list[Json]
    screen: Json


@dataclass(frozen=True)
class Limits:
    steps: int = 40
    calls: int = 80
    stalled_turns: int = 3

    def __post_init__(self):
        if any(type(n) is not int or n < 1 for n in (self.steps, self.calls, self.stalled_turns)):
            raise ValueError("budgets must be positive integers")


@dataclass(frozen=True)
class RunOptions:
    scope: Scope = field(default_factory=Scope)
    text: TextSource = None
    cursor: bool = False
    values: bool = True
    confirm_destructive: bool = False
    text_model: "TextModel | None" = None
    input_mode: str = "both"
    keys: tuple[str, ...] = DEFAULT_KEYS
    allowed_operations: tuple[str, ...] | None = None
    include_context: bool = False
    blocked_actions: tuple[dict, ...] = ()
    previous_blocker: dict | None = None

    def __post_init__(self):
        if self.input_mode not in ("both", "replace", "insert"):
            raise ValueError("input_mode must be both, replace or insert")
        if len(self.keys) > 100 or any(
            not isinstance(key, str) or not key or len(key) > 80 or any(c.isspace() for c in key)
            for key in self.keys
        ):
            raise ValueError("keys must contain at most 100 nonempty native key combinations")


class DesktopError(RuntimeError):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class TextModelError(RuntimeError):
    pass


class DecisionModel(Protocol):
    def ask(self, request: Json) -> Json: ...


class TextModel(Protocol):
    def generate(self, request: TextRequest) -> str | None: ...


class Desktop(Protocol):
    def run_context(self, goal: str, cursor: bool) -> AbstractContextManager: ...

    def observe(self, scope: Scope) -> Observation: ...

    def execute(self, scope: Scope, operation: str, node: Json | None, text: str | None) -> Json: ...

    def applications(self) -> list[Json]: ...

    def launch(self, application: Json) -> Json: ...

    def wait_for_window(self, launched: Json) -> Json: ...
