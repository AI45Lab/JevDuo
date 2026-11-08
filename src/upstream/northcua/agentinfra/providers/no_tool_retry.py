"""Opt-in, wire-level retries before any response enters the agent history.

Only instance-owned OpenAI Chat resources are wrapped, for the duration of one
Desktop actor step. No global SDK patch, auxiliary-model retry or tool replay.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from threading import Event


def has_tools(response):
    choices = getattr(response, "choices", None)
    if not choices or len(choices) != 1:
        raise ValueError("Expected exactly one completion choice")
    choice = choices[0]
    if choice.finish_reason == "content_filter":
        return None  # A provider refusal is not a no-tool retry opportunity.
    return bool(choice.message.tool_calls)


def _check_active(closed: Event | None):
    if closed is not None and closed.is_set():
        raise asyncio.CancelledError("The model-step retry scope has ended")


def retry_sync(call, kwargs, record, *, closed: Event | None = None):
    baseline = copy.deepcopy(kwargs)
    for attempt in range(1, 4):
        _check_active(closed)
        response = call(**copy.deepcopy(baseline))
        _check_active(closed)
        tool = has_tools(response)
        selected = tool is not False or attempt == 3
        record(attempt, response, selected)
        if selected:
            return response


async def retry_async(call, kwargs, record, *, closed: Event | None = None):
    baseline = copy.deepcopy(kwargs)
    for attempt in range(1, 4):
        _check_active(closed)
        response = await call(**copy.deepcopy(baseline))
        _check_active(closed)
        tool = has_tools(response)
        selected = tool is not False or attempt == 3
        record(attempt, response, selected)
        if selected:
            return response


def _recorder(root: Path, kwargs: dict, logical_step: int):
    root.mkdir(parents=True, exist_ok=True)
    group = uuid.uuid4().hex
    # Credentials/headers are not written into the request receipt.
    payload = {k: v for k, v in kwargs.items() if k not in {"extra_headers", "api_key"}}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    def record(attempt, response, selected):
        receipt = {
            "at": time.time(),
            "group": group,
            "logical_step": logical_step,
            "call": attempt,
            "request_sha256": digest,
            "selected_for_logical_round": selected,
            "response": response.model_dump(mode="json"),
        }
        fd = os.open(root / f"{group}-{attempt}.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(receipt, stream, ensure_ascii=False)

    return record


def _validate_request(args, kwargs):
    if args or kwargs.get("stream"):
        raise ValueError("Same-context retry requires non-streaming keyword requests")
    if kwargs.get("n", 1) != 1:
        raise ValueError("Same-context retry requires n=1")
    key = kwargs.get("prompt_cache_key")
    if key is not None and (not isinstance(key, str) or not key or len(key.encode()) > 64):
        raise ValueError("prompt_cache_key must be a nonempty string of at most 64 UTF-8 bytes")


def _wrap(original, *, model: str, root: Path, logical_step: int, closed: Event):
    if inspect.iscoroutinefunction(inspect.unwrap(original)):

        async def async_create(*args, **kwargs):
            if kwargs.get("model") != model:
                return await original(*args, **kwargs)
            _validate_request(args, kwargs)
            return await retry_async(original, kwargs, _recorder(root, kwargs, logical_step), closed=closed)
    else:

        def sync_create(*args, **kwargs):
            if kwargs.get("model") != model:
                return original(*args, **kwargs)
            _validate_request(args, kwargs)
            return retry_sync(original, kwargs, _recorder(root, kwargs, logical_step), closed=closed)

    wrapped = async_create if inspect.iscoroutinefunction(inspect.unwrap(original)) else sync_create
    wrapped._northcua_no_tool_retry = True
    return wrapped


@contextmanager
def scoped_no_tool_retry(agent, *, attempts: int, audit_dir: Path, logical_step: int):
    """Bind both real SDK paths, restore on success/error/cancellation.

    Preparation/compaction happens before this scope. The full wire kwargs
    (images, tools, parameters and cache key included) are copied for each call.
    Only the selected response is returned to NexAU hooks/history/tool parsing.
    API exceptions propagate; existing configured transport retries are separate.
    """
    if attempts == 1:
        yield
        return
    if attempts != 3:
        raise ValueError("no_tool_call_attempts must be 1 or 3")
    llm = agent.config.llm_config
    if llm.api_type != "openai_chat_completion":
        raise ValueError("Same-context retry currently supports OpenAI Chat only")
    caller = agent.executor.llm_caller
    clients = [
        getattr(agent, "openai_client", None),
        getattr(caller, "openai_client", None),
        getattr(caller, "async_openai_client", None),
    ]
    resources = []
    for client in clients:
        if client is not None:
            resource = client.chat.completions
            if all(resource is not existing for existing in resources):
                resources.append(resource)
    if not resources:
        raise ValueError("No owned Chat completion client to bind")
    if any(getattr(resource.create, "_northcua_no_tool_retry", False) for resource in resources):
        raise RuntimeError("Completion client already belongs to an active retry scope")
    originals = [(resource, resource.create, "create" in vars(resource)) for resource in resources]
    previous = getattr(agent, "_no_tool_call_attempts", None)
    closed = Event()
    try:
        agent._no_tool_call_attempts = attempts
        for resource, original, _ in originals:
            resource.create = _wrap(original, model=llm.model, root=audit_dir, logical_step=logical_step, closed=closed)
        yield
    finally:
        # Cancelling an asyncio.to_thread bridge does not stop its worker.
        # An in-flight SDK call may finish, but it must not issue more attempts.
        closed.set()
        for resource, original, had_instance_method in originals:
            if had_instance_method:
                resource.create = original
            else:
                del resource.create
        if previous is None:
            del agent._no_tool_call_attempts
        else:
            agent._no_tool_call_attempts = previous
