"""Anthropic client compatibility for legacy thinking-signature retries."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Iterator
from typing import Any

_INVALID_SIGNATURE_MARKERS = (
    "invalid `signature` in `thinking`",
    "invalid signature in thinking",
    'invalid "signature" in "thinking"',
)
_PROXY_MARKER = "__northcua_legacy_anthropic_client_proxy__"


class LegacyAnthropicSignatureError(Exception):
    """Carry a signature 400 across public NexAU's internal repair layer."""

    def __init__(self, original: Exception) -> None:
        super().__init__(str(original))
        self.original = original


def wrap_anthropic_client(client: object | None) -> object | None:
    """Return a delegating client that promotes only invalid-signature 400s."""

    if client is None or getattr(client, _PROXY_MARKER, False):
        return client
    messages = getattr(client, "messages", None)
    if messages is None:
        return client
    return _AnthropicClientProxy(client, messages)


def _promote_if_invalid_signature(error: Exception) -> None:
    status_code = getattr(error, "status_code", None)
    message = f"{getattr(error, 'message', '') or ''} {error}".lower()
    if status_code == 400 and any(marker in message for marker in _INVALID_SIGNATURE_MARKERS):
        raise LegacyAnthropicSignatureError(error) from error


class _AnthropicClientProxy:
    """Delegate an Anthropic client while replacing its messages resource."""

    __northcua_legacy_anthropic_client_proxy__ = True

    def __init__(self, client: object, messages: object) -> None:
        self._client = client
        self.messages = _AnthropicMessagesProxy(messages)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class _AnthropicMessagesProxy:
    """Translate the one error public NexAU otherwise repairs internally."""

    def __init__(self, messages: object) -> None:
        self._messages = messages

    def create(self, *args: object, **kwargs: object) -> object:
        try:
            result = self._messages.create(*args, **kwargs)  # type: ignore[attr-defined]
        except Exception as exc:
            _promote_if_invalid_signature(exc)
            raise
        if not inspect.isawaitable(result):
            return result

        async def await_result() -> object:
            try:
                return await result
            except Exception as exc:
                _promote_if_invalid_signature(exc)
                raise

        return await_result()

    def stream(self, *args: object, **kwargs: object) -> object:
        try:
            stream = self._messages.stream(*args, **kwargs)  # type: ignore[attr-defined]
        except Exception as exc:
            _promote_if_invalid_signature(exc)
            raise
        return _AnthropicStreamProxy(stream)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._messages, name)


class _AnthropicStreamProxy(Iterator[object], AsyncIterator[object]):
    """Translate signature errors from sync or async stream lifecycles."""

    def __init__(self, stream: object) -> None:
        self._stream = stream
        self._active = stream
        self._iterator: Iterator[object] | None = None
        self._async_iterator: AsyncIterator[object] | None = None

    def __enter__(self) -> "_AnthropicStreamProxy":
        try:
            self._active = self._stream.__enter__()  # type: ignore[attr-defined]
            return self
        except Exception as exc:
            _promote_if_invalid_signature(exc)
            raise

    def __exit__(self, *args: object) -> object:
        try:
            return self._stream.__exit__(*args)  # type: ignore[attr-defined]
        except Exception as exc:
            _promote_if_invalid_signature(exc)
            raise

    def __iter__(self) -> Iterator[object]:
        try:
            self._iterator = iter(self._active)  # type: ignore[arg-type]
            return self
        except Exception as exc:
            _promote_if_invalid_signature(exc)
            raise

    def __next__(self) -> object:
        try:
            iterator = self._iterator or iter(self._active)  # type: ignore[arg-type]
            self._iterator = iterator
            return next(iterator)
        except Exception as exc:
            _promote_if_invalid_signature(exc)
            raise

    async def __aenter__(self) -> "_AnthropicStreamProxy":
        try:
            self._active = await self._stream.__aenter__()  # type: ignore[attr-defined]
            return self
        except Exception as exc:
            _promote_if_invalid_signature(exc)
            raise

    async def __aexit__(self, *args: object) -> object:
        try:
            return await self._stream.__aexit__(*args)  # type: ignore[attr-defined]
        except Exception as exc:
            _promote_if_invalid_signature(exc)
            raise

    def __aiter__(self) -> AsyncIterator[object]:
        try:
            self._async_iterator = self._active.__aiter__()  # type: ignore[attr-defined]
            return self
        except Exception as exc:
            _promote_if_invalid_signature(exc)
            raise

    async def __anext__(self) -> object:
        try:
            iterator = self._async_iterator or self._active.__aiter__()  # type: ignore[attr-defined]
            self._async_iterator = iterator
            return await iterator.__anext__()
        except Exception as exc:
            _promote_if_invalid_signature(exc)
            raise

    def __getattr__(self, name: str) -> Any:
        return getattr(self._active, name)


__all__ = ["LegacyAnthropicSignatureError", "wrap_anthropic_client"]
