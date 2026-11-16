"""Scoped capture before NexAU drops the OpenAI Chat completion envelope."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from northcua.core.json_types import JsonObject


@dataclass
class _CaptureScope:
    callback: Callable[[JsonObject], None]
    active: bool = True
    callers: set[int] | None = None


_capture: ContextVar[_CaptureScope | None] = ContextVar("chat_response_capture", default=None)
_caller: ContextVar[int | None] = ContextVar("chat_response_caller", default=None)
_PROXY_MARKER = "__northcua_openai_chat_capture_proxy__"


def register_chat_capture_caller(caller: object) -> None:
    """Restrict a model-step capture to its caller, excluding context summarizers."""
    scope = _capture.get()
    if scope is not None and scope.active:
        if scope.callers is None:
            scope.callers = set()
        scope.callers.add(id(caller))


@contextmanager
def chat_capture_caller(caller: object) -> Iterator[None]:
    token = _caller.set(id(caller))
    try:
        yield
    finally:
        _caller.reset(token)


@contextmanager
def capture_chat_responses(callback: Callable[[JsonObject], None]) -> Iterator[None]:
    """Capture SDK JSON bodies only; headers and request credentials stay private."""
    scope = _CaptureScope(callback)
    token = _capture.set(scope)
    try:
        yield
    finally:
        scope.active = False  # Inherited child-task contexts must not retain a live capture.
        _capture.reset(token)


def wrap_openai_chat_client(client: object | None) -> object | None:
    """Delegate sync/async clients without changing completion objects or streams."""
    if client is None or getattr(client, _PROXY_MARKER, False):
        return client
    chat = getattr(client, "chat", None)
    if chat is None or getattr(chat, "completions", None) is None:
        return client
    return _ChatClient(client, chat)


class _ChatClient:
    __northcua_openai_chat_capture_proxy__ = True

    def __init__(self, client: object, chat: object) -> None:
        self._client = client
        self.chat = _ChatResource(chat)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class _ChatResource:
    def __init__(self, chat: Any) -> None:
        self._chat = chat
        self.completions = _ChatCompletions(chat.completions)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._chat, name)


class _ChatCompletions:
    def __init__(self, completions: Any) -> None:
        self._completions = completions

    def create(self, *args: Any, **kwargs: Any) -> Any:
        scope = None if kwargs.get("stream") else _capture.get()
        result = self._completions.create(*args, **kwargs)
        if scope is None or not scope.active:
            return result
        if scope.callers is not None and _caller.get() not in scope.callers:
            return result

        def record(response: Any) -> Any:
            if scope.active:
                scope.callback(response.model_dump(mode="json"))
            return response

        if not inspect.isawaitable(result):
            return record(result)

        async def await_result() -> Any:
            return await asyncio.to_thread(record, await result)

        return await_result()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._completions, name)
