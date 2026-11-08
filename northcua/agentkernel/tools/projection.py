"""Projection factories from canonical tool contracts to NexAU tools."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import replace
from typing import Any, Literal

from nexau.archs.tool import Tool
from pydantic import BaseModel

from northcua.agentinfra.nexau_compat.adapters.tool import NexAUToolAdapter
from northcua_runtime_shared.tool_contracts import (
    DirectHandlerBinding,
    ToolContract,
    ToolDispatcher,
    ToolDispatchPolicy,
    attach_tool_route_metadata,
)
from northcua_runtime_shared.tool_contracts.runtime_metadata import attach_tool_contract_projection_metadata


def contract_to_nexau_tool(
    contract: ToolContract,
    *,
    implementation: Callable[..., Any] | str | None = None,
    source_name: str | None = None,
    formatter: str | Callable[..., object] | None = None,
    disable_parallel: bool = False,
    execution: Literal["async", "sync"] = "async",
    dispatcher: ToolDispatcher | None = None,
    policy: ToolDispatchPolicy | None = None,
    on_validation_error: Callable[[Exception], None] | None = None,
    extra_kwargs: dict[str, Any] | None = None,
) -> Tool:
    """Project one contract into a NexAU Tool.

    When ``implementation`` is callable, calls are validated through
    ``ToolDispatcher`` before the implementation can observe parameters.
    """

    selected_source = source_name or contract.server_name or None
    validated_implementation = None
    if callable(implementation):
        if not contract.all_bindings:
            contract = replace(contract, binding=DirectHandlerBinding(handler_path=_handler_path(implementation)))
        validated_implementation = _validated_implementation(
            contract=contract,
            implementation=implementation,
            dispatcher=dispatcher or ToolDispatcher(),
            policy=policy or ToolDispatchPolicy(),
            on_validation_error=on_validation_error,
            execution=execution,
        )
    tool = NexAUToolAdapter.create(
        name=contract.provider_name,
        description=contract.description,
        input_schema=contract.provider_parameters_schema,
        implementation=validated_implementation if callable(implementation) else implementation,
        formatter=formatter,
        disable_parallel=disable_parallel,
        extra_kwargs=extra_kwargs,
        source_name=selected_source,
    )
    attach_tool_contract_projection_metadata(tool, contract=contract)
    if selected_source:
        attach_tool_route_metadata(tool, server_name=selected_source)
    return tool


def _validated_implementation(
    *,
    contract: ToolContract,
    implementation: Callable[..., Any],
    dispatcher: ToolDispatcher,
    policy: ToolDispatchPolicy,
    on_validation_error: Callable[[Exception], None] | None,
    execution: Literal["async", "sync"],
) -> Callable[..., Any]:
    def _prepare(raw_parameters: dict[str, Any]) -> BaseModel | dict[str, object]:
        normalized_parameters = {str(key): value for key, value in raw_parameters.items()}
        try:
            envelope = dispatcher.prepare(contract, normalized_parameters, policy=policy)
        except Exception as exc:
            if on_validation_error is not None:
                on_validation_error(exc)
            raise
        return envelope.validated_parameters

    if execution == "sync":

        def _run_sync(**raw_parameters: Any) -> Any:
            result = _call_handler(implementation, _prepare(raw_parameters))
            if inspect.isawaitable(result):
                raise TypeError(f"{contract.tool_id} sync contract handler returned an awaitable")
            return result

        _run_sync.__name__ = f"{contract.tool_id}_nexau_contract_handler"
        _run_sync.__doc__ = contract.description
        return _run_sync

    async def _run(**raw_parameters: Any) -> Any:
        result = _call_handler(implementation, _prepare(raw_parameters))
        if inspect.isawaitable(result):
            return await result
        return result

    _run.__name__ = f"{contract.tool_id}_nexau_contract_handler"
    _run.__doc__ = contract.description
    return _run


def _call_handler(handler: Callable[..., Any], validated_parameters: BaseModel | dict[str, object]) -> Any:
    if _handler_wants_single_payload(handler, validated_parameters):
        return handler(validated_parameters)
    payload = _payload_dict(validated_parameters)
    return handler(**payload)


def _handler_wants_single_payload(
    handler: Callable[..., Any], validated_parameters: BaseModel | dict[str, object]
) -> bool:
    try:
        signature = inspect.signature(handler)
    except (TypeError, ValueError):
        return False
    parameters = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }
    ]
    if len(parameters) != 1:
        return False
    parameter = parameters[0]
    if parameter.name in {"params", "payload", "validated_parameters"}:
        return True
    annotation = parameter.annotation
    return (
        isinstance(validated_parameters, BaseModel)
        and isinstance(annotation, type)
        and issubclass(annotation, BaseModel)
    )


def _payload_dict(validated_parameters: BaseModel | dict[str, object]) -> dict[str, object]:
    if isinstance(validated_parameters, BaseModel):
        return dict(validated_parameters.model_dump(mode="json"))
    return dict(validated_parameters)


def _handler_path(handler: Callable[..., Any]) -> str:
    return f"{getattr(handler, '__module__', '')}:{getattr(handler, '__qualname__', getattr(handler, '__name__', 'handler'))}"


__all__ = ["contract_to_nexau_tool"]
