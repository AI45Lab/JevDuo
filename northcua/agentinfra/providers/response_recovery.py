"""Bounded same-request recovery; no tools are executed by this retry loop."""

import asyncio
from asyncio import sleep
import logging
import random
from contextlib import contextmanager

logger = logging.getLogger(__name__)


def truncated_reasoning_only(response, max_tokens: int) -> bool:
    if response is None or max_tokens <= 0:
        return False
    if str(getattr(response, "content", None) or "").strip() or getattr(response, "tool_calls", None):
        return False
    if not str(getattr(response, "reasoning_content", None) or "").strip():
        return False
    try:
        return int(getattr(getattr(response, "usage", None), "completion_tokens", 0)) >= max_tokens
    except (TypeError, ValueError):
        return False


def retryable(exc: Exception) -> bool:
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code == 429 or 500 <= code < 600
    if type(exc).__name__ in {
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "RateLimitError",
        "ServiceUnavailableError",
        "TimeoutException",
    }:
        return True
    return any(
        marker in str(exc).lower()
        for marker in (
            "no response content or tool calls",
            "internal server error",
            "connection reset",
            "temporarily unavailable",
            "timed out",
            "timeout",
            "rate limit",
        )
    )


async def recover_response(call, *, retries: int, max_tokens: int, attempt_timeout: float = 3600):
    """Initial attempt plus at most ``retries`` retries; terminal text is valid.

    Retries remain inside one logical step, with the same messages/tools. The
    provider SDK's own configured retry count is separate, as in the original
    harness. Cancellation and the hard per-attempt deadline are not swallowed.
    """
    for attempt in range(retries + 1):
        try:
            response = await asyncio.wait_for(call(), timeout=attempt_timeout)
            if not truncated_reasoning_only(response, max_tokens):
                return response
            if attempt == retries:
                raise RuntimeError("Reasoning-only response hit the configured completion token limit")
        except TimeoutError:
            raise
        except Exception as exc:
            if attempt == retries or not retryable(exc):
                raise
        delay = min(2.0 * 2**attempt, 30.0)
        delay += random.uniform(0.0, min(1.0, delay * 0.1))
        # Do not log messages, reasoning, credentials, or raw exception URLs.
        logger.warning("model_response_retry attempt=%s delay_seconds=%.3f", attempt + 1, delay)
        await sleep(delay)
    raise AssertionError("unreachable retry state")


@contextmanager
def response_recovery_scope(agent, *, enabled: bool):
    previous = getattr(agent, "_northcua_response_recovery", None)
    try:
        agent._northcua_response_recovery = enabled
        yield
    finally:
        if previous is None:
            del agent._northcua_response_recovery
        else:
            agent._northcua_response_recovery = previous
