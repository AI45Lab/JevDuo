"""Shared dispatch envelope for contract-backed tool calls."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from northcua_protocol.capabilities import JsonObject, ToolRiskLevel
from northcua_runtime_shared.tool_contracts.bindings import ToolBinding, binding_summary
from northcua_runtime_shared.tool_contracts.contracts import ToolContract, ToolVisibility
from northcua_runtime_shared.tool_contracts.fingerprints import normalized_json
from northcua_runtime_shared.validation import validate_command_against_tool


@dataclass(frozen=True, slots=True)
class ToolDispatchEnvelope:
    """Validated dispatch metadata for one tool invocation."""

    tool_id: str
    binding: ToolBinding
    validated_parameters: BaseModel | JsonObject
    route: JsonObject
    audit: JsonObject


@dataclass(frozen=True, slots=True)
class ToolDispatchPolicy:
    """Call-site policy gates applied before execution."""

    allow_internal: bool = False
    allow_operator: bool = False
    readonly_required: bool = False
    max_allowed_risk: ToolRiskLevel | None = ToolRiskLevel.HIGH
    max_payload_bytes: int | None = None


class ToolDispatcher:
    """Prepare validated dispatch envelopes from canonical contracts.

    This class intentionally stops before executing transport-specific work.
    MCP, device, evaluator, and sandbox adapters consume the returned envelope.
    """

    def prepare(
        self,
        contract: ToolContract,
        parameters: JsonObject | None = None,
        *,
        binding: ToolBinding | None = None,
        policy: ToolDispatchPolicy | None = None,
    ) -> ToolDispatchEnvelope:
        """Validate a call and select a route binding."""

        selected_policy = policy or ToolDispatchPolicy()
        _enforce_visibility(contract, selected_policy)
        _enforce_readonly(contract, selected_policy)
        _enforce_risk(contract, selected_policy)
        raw_parameters = dict(parameters or {})
        _enforce_payload(contract, raw_parameters, selected_policy)
        selected_binding = _select_binding(contract, requested_binding=binding)
        validated_parameters = _validate_contract_parameters(contract, raw_parameters)
        route: JsonObject = dict(binding_summary(selected_binding))
        return ToolDispatchEnvelope(
            tool_id=contract.tool_id,
            binding=selected_binding,
            validated_parameters=validated_parameters,
            route=route,
            audit={
                "tool_id": contract.tool_id,
                "contract_id": contract.contract_id,
                "visibility": contract.visibility.value,
                "readonly": contract.is_readonly,
                "risk_level": contract.risk_level.value,
                "binding_kind": selected_binding.kind.value,
            },
        )


def _enforce_visibility(contract: ToolContract, policy: ToolDispatchPolicy) -> None:
    if contract.visibility == ToolVisibility.INTERNAL_ONLY and not policy.allow_internal:
        raise PermissionError(f"Tool {contract.tool_id!r} is internal-only")
    if contract.visibility == ToolVisibility.OPERATOR_ONLY and not policy.allow_operator:
        raise PermissionError(f"Tool {contract.tool_id!r} is operator-only")


def _enforce_readonly(contract: ToolContract, policy: ToolDispatchPolicy) -> None:
    if policy.readonly_required and not contract.is_readonly:
        raise PermissionError(f"Tool {contract.tool_id!r} is mutating but readonly dispatch was required")


def _enforce_risk(contract: ToolContract, policy: ToolDispatchPolicy) -> None:
    if policy.max_allowed_risk is None:
        return
    if _risk_rank(contract.risk_level) > _risk_rank(policy.max_allowed_risk):
        raise PermissionError(
            f"Tool {contract.tool_id!r} risk level {contract.risk_level.value!r} exceeds "
            f"allowed maximum {policy.max_allowed_risk.value!r}"
        )


def _risk_rank(risk_level: ToolRiskLevel) -> int:
    return {
        ToolRiskLevel.LOW: 0,
        ToolRiskLevel.MEDIUM: 1,
        ToolRiskLevel.HIGH: 2,
    }[risk_level]


def _enforce_payload(contract: ToolContract, parameters: JsonObject, policy: ToolDispatchPolicy) -> None:
    policy_limit = policy.max_payload_bytes if policy.max_payload_bytes is not None else contract.max_payload_bytes
    limit = min(contract.max_payload_bytes, policy_limit)
    payload_size = len(normalized_json(parameters).encode("utf-8"))
    if payload_size > limit:
        raise ValueError(f"Tool {contract.tool_id!r} payload exceeds {limit} bytes")


def _select_binding(contract: ToolContract, *, requested_binding: ToolBinding | None = None) -> ToolBinding:
    bindings = contract.all_bindings
    if not bindings:
        raise LookupError(f"Tool {contract.tool_id!r} has no execution binding")
    if requested_binding is not None:
        if requested_binding not in bindings:
            raise LookupError(f"Tool {contract.tool_id!r} binding is not declared by its ToolContract")
        return requested_binding
    return bindings[0]


def _validate_contract_parameters(contract: ToolContract, parameters: JsonObject) -> BaseModel | JsonObject:
    if contract.params_model is not None:
        return contract.params_model.model_validate(parameters)
    validate_command_against_tool(
        tool_id=contract.tool_id,
        parameters=parameters,
        descriptor=contract.to_descriptor(),
    )
    return dict(parameters)


__all__ = ["ToolDispatchEnvelope", "ToolDispatchPolicy", "ToolDispatcher"]
