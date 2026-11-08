"""Runtime invocation helpers for contract-backed tools."""

from __future__ import annotations

from pydantic import BaseModel

from northcua_protocol.capabilities import JsonObject
from northcua_runtime_shared.tool_contracts import ToolContract
from northcua_runtime_shared.validation import validate_command_against_tool


def validate_contract_parameters(contract: ToolContract, parameters: JsonObject) -> BaseModel | JsonObject:
    """Validate parameters against the contract-owned input schema.

    Typed contracts use their Pydantic params model. JSON-schema-only migration
    contracts fall back to the protocol descriptor validator so provider-safe
    schema simplification never weakens the runtime boundary.
    """

    if contract.params_model is None:
        validate_command_against_tool(
            tool_id=contract.tool_id,
            parameters=parameters,
            descriptor=contract.to_descriptor(),
        )
        return dict(parameters)
    return contract.params_model.model_validate(parameters)


__all__ = ["validate_contract_parameters"]
