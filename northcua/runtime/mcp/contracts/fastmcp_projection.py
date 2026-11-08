"""FastMCP projection and registration helpers for typed contracts."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from fastmcp import FastMCP
from fastmcp.tools import FunctionTool
from pydantic import BaseModel

from northcua_runtime_shared.tool_contracts import (
    ToolContract,
    ToolDispatcher,
    ToolDispatchPolicy,
    ToolSet,
)


def contract_schema_by_tool_id(contracts: Iterable[ToolContract]) -> dict[str, dict[str, object]]:
    """Return protocol schemas keyed by tool id."""

    return {contract.tool_id: contract.parameters_schema for contract in contracts}


def provider_schema_by_tool_id(contracts: Iterable[ToolContract]) -> dict[str, dict[str, object]]:
    """Return provider-safe schemas keyed by tool id."""

    return {contract.tool_id: contract.provider_parameters_schema for contract in contracts}


def register_contract_handlers(
    mcp: FastMCP,
    contracts: Iterable[ToolContract],
    handlers: Mapping[str, Callable[..., Any]],
    *,
    policy: ToolDispatchPolicy | None = None,
    dispatcher: ToolDispatcher | None = None,
    error_handler: Callable[[str, Exception], Any] | None = None,
    fail_on_extra_handlers: bool = True,
) -> None:
    """Register FastMCP tools from canonical contracts and handler map.

    FastMCP receives only projected metadata. Runtime calls always pass through
    ``ToolDispatcher.prepare`` before handler code can observe parameters.
    """

    contract_list = tuple(contracts)
    contracts_by_id = _contracts_by_id(contract_list)
    handler_ids = set(handlers)
    missing_handlers = sorted(set(contracts_by_id) - handler_ids)
    if missing_handlers:
        raise ValueError(f"Missing handlers for contract tools: {missing_handlers}")
    extra_handlers = sorted(handler_ids - set(contracts_by_id))
    if fail_on_extra_handlers and extra_handlers:
        raise ValueError(f"Handlers declared without matching contracts: {extra_handlers}")

    selected_dispatcher = dispatcher or ToolDispatcher()
    selected_policy = policy or ToolDispatchPolicy()
    for contract in contract_list:
        handler = handlers[contract.tool_id]
        mcp.add_tool(
            FunctionTool(
                name=contract.provider_name,
                version=contract.version,
                description=contract.description,
                parameters=contract.parameters_schema,
                output_schema=_output_schema(contract),
                fn=_fastmcp_handler(
                    contract=contract,
                    handler=handler,
                    dispatcher=selected_dispatcher,
                    policy=selected_policy,
                    error_handler=error_handler,
                ),
                return_type=None,
                timeout=contract.timeout_seconds,
                meta={
                    "northcua_contract_id": contract.contract_id,
                    "northcua_tool_id": contract.tool_id,
                    "northcua_contract_version": contract.version,
                },
            )
        )


def register_mcp_toolset(
    mcp: FastMCP,
    toolset: ToolSet,
    *,
    handlers: Mapping[str, Callable[..., Any]] | None = None,
    policy: ToolDispatchPolicy | None = None,
    dispatcher: ToolDispatcher | None = None,
    error_handler: Callable[[str, Exception], Any] | None = None,
    fail_on_extra_handlers: bool = True,
) -> None:
    """Register an owned toolset on a FastMCP server from its contracts."""

    merged_handlers = dict(toolset.handlers)
    if handlers:
        overlap = sorted(set(merged_handlers) & set(handlers))
        if overlap:
            raise ValueError(f"Toolset {toolset.name!r} received duplicate handler overrides: {overlap}")
        merged_handlers.update(handlers)
    register_contract_handlers(
        mcp,
        toolset.contracts,
        merged_handlers,
        policy=policy,
        dispatcher=dispatcher,
        error_handler=error_handler,
        fail_on_extra_handlers=fail_on_extra_handlers,
    )


def _contracts_by_id(contracts: Iterable[ToolContract]) -> dict[str, ToolContract]:
    contracts_by_id: dict[str, ToolContract] = {}
    public_names: dict[str, str] = {}
    for contract in contracts:
        if contract.tool_id in contracts_by_id:
            raise ValueError(f"Duplicate contract tool_id {contract.tool_id!r}")
        contracts_by_id[contract.tool_id] = contract
        previous_tool_id = public_names.get(contract.provider_name)
        if previous_tool_id is not None:
            raise ValueError(
                f"Duplicate FastMCP public tool name {contract.provider_name!r} "
                f"for {previous_tool_id!r} and {contract.tool_id!r}"
            )
        public_names[contract.provider_name] = contract.tool_id
    return contracts_by_id


def _fastmcp_handler(
    *,
    contract: ToolContract,
    handler: Callable[..., Any],
    dispatcher: ToolDispatcher,
    policy: ToolDispatchPolicy,
    error_handler: Callable[[str, Exception], Any] | None,
) -> Callable[..., Any]:
    async def _run(**raw_parameters: Any) -> Any:
        try:
            normalized_parameters = {str(key): value for key, value in raw_parameters.items()}
            envelope = dispatcher.prepare(contract, normalized_parameters, policy=policy)
            result = _call_handler(handler, envelope.validated_parameters)
            if inspect.isawaitable(result):
                return await result
            return result
        except Exception as exc:
            if error_handler is None:
                raise
            result = error_handler(contract.tool_id, exc)
            if inspect.isawaitable(result):
                return await result
            return result

    _run.__name__ = f"{contract.tool_id}_contract_handler"
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


def _output_schema(contract: ToolContract) -> dict[str, object] | None:
    if contract.result_model is None:
        return None
    return contract.result_model.model_json_schema()


__all__ = [
    "contract_schema_by_tool_id",
    "provider_schema_by_tool_id",
    "register_contract_handlers",
    "register_mcp_toolset",
]
