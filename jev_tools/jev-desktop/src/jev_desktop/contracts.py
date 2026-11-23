"""Interfaces for callers that supply their own model or desktop backend."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
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
    resources: tuple[str, ...] = ()
    allowed_operations: tuple[str, ...] | None = None
    include_context: bool = False
    blocked_actions: tuple[dict, ...] = ()
    previous_blocker: dict | None = None
    replan_safe_failures: bool = False
    reconcile_observed_popups: bool = False
    text_replans: int = 0
    reference_context: dict = field(default_factory=dict)
    allow_infeasible: bool = False
    follow_owned_dialogs: bool = False
    review_goal_scope: bool = False
    follow_application_windows: bool = False
    remember_closed_windows: bool = False
    replan_no_effect: bool = False
    verify_terminal: bool = False
    replan_changed_text_targets: bool = False
    startup_observation_retries: int = 0

    def __post_init__(self):
        if len(self.resources) > 16 or any(
            not isinstance(path, str) or not path
            or not (PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute())
            or any(ord(c) < 32 or ord(c) == 127 for c in path)
            for path in self.resources
        ):
            raise ValueError("resources must contain at most 16 caller-bound absolute paths without controls")
        if any(path in self.resources[:index] for index, path in enumerate(self.resources)):
            raise ValueError("resource choices must not contain duplicate literal paths")
        if type(self.startup_observation_retries) is not int or not 0 <= self.startup_observation_retries <= 10:
            raise ValueError("startup_observation_retries must be an integer between zero and ten")
        if type(self.text_replans) is not int or not 0 <= self.text_replans <= 3:
            raise ValueError("text_replans must be an integer between zero and three")
        if self.input_mode not in ("both", "replace", "insert"):
            raise ValueError("input_mode must be both, replace or insert")
        if len(self.keys) > 192 or any(
            not isinstance(key, str) or not key or len(key) > 80 or any(c.isspace() for c in key)
            for key in self.keys
        ):
            raise ValueError("keys must contain at most 192 nonempty native key combinations")


class DesktopError(RuntimeError):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class TextModelError(DesktopError):
    pass


class DecisionModel(Protocol):
    def ask(self, request: Json) -> Json: ...


class TextModel(Protocol):
    def generate(self, request: TextRequest) -> str | None: ...


class Desktop(Protocol):
    def run_context(self, goal: str, cursor: bool) -> AbstractContextManager: ...

    def observe(self, scope: Scope) -> Observation: ...

    def execute(self, scope: Scope, operation: str, node: Json | None, text: str | None) -> Json: ...

    def owned_dialog(self, scope: Scope) -> Scope | None: ...

    def application_windows(self, scope: Scope) -> list[Json]: ...

    def applications(self) -> list[Json]: ...

    def launch(self, application: Json) -> Json: ...

    def wait_for_window(self, launched: Json) -> Json: ...
