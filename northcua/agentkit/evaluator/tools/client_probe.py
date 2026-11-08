"""Standard read-only client probe tool declarations for evaluators."""

from __future__ import annotations

from northcua.core.enums import EvaluationToolPlacement
from northcua_runtime_shared.desktop_surface.action_contracts import desktop_action_contracts
from northcua_runtime_shared.desktop_surface.data_contracts import desktop_data_contracts
from northcua_runtime_shared.device_shell_contracts import SHELL_EXEC_READONLY_CONTRACT
from northcua_runtime_shared.tool_contracts import (
    EvaluatorClientBinding,
    ToolAudience,
    ToolContract,
    ToolContractKind,
    ToolPlacement,
    fingerprint_json,
)

from ..spec import EvaluatorToolRef


def _tool_placement(placement: EvaluationToolPlacement) -> ToolPlacement:
    if placement == EvaluationToolPlacement.CLIENT_MUTATING:
        return ToolPlacement.CLIENT_MUTATING
    return ToolPlacement.CLIENT_PROBE


def _client_evaluator_tool(
    *,
    name: str,
    description: str,
    placement: EvaluationToolPlacement,
    dispatch_name: str,
    server_name: str = "",
    readonly: bool | None = None,
    timeout_seconds: float = 10.0,
) -> EvaluatorToolRef:
    target_contract = _target_runtime_contract(server_name=server_name, tool_id=dispatch_name)
    resolved_readonly = target_contract.is_readonly if readonly is None else readonly
    target_fingerprint = fingerprint_json(target_contract.parameters_schema)
    contract = ToolContract(
        tool_id=name,
        public_name=name,
        description=description,
        namespace="evaluator.client",
        agent_kind="evaluator",
        kind=ToolContractKind.PROBE if resolved_readonly else ToolContractKind.ACTION,
        audience=ToolAudience.EVALUATOR,
        placement=_tool_placement(placement),
        capability=target_contract.capability,
        json_schema=target_contract.parameters_schema,
        timeout_seconds=timeout_seconds or target_contract.timeout_seconds,
        side_effect=not resolved_readonly,
        readonly=resolved_readonly,
        requires_focus=target_contract.requires_focus,
        risk_level=target_contract.risk_level,
        max_payload_bytes=target_contract.max_payload_bytes,
        binding=EvaluatorClientBinding(
            dispatch_name=target_contract.provider_name,
            server_name=target_contract.server_name or server_name,
            placement=placement.value,
            target_tool_id=target_contract.tool_id,
            target_schema_fingerprint=target_fingerprint,
        ),
        profiles=("evaluator.desktop.round",),
        aliases=(target_contract.provider_name,) if target_contract.provider_name != name else (),
        source="northcua.agentkit.evaluator.tools.client_probe",
        audit_tags=(f"target_contract:{target_contract.contract_id}", f"target_schema:{target_fingerprint}"),
    )
    return EvaluatorToolRef.from_contract(contract, placement=placement)


def _target_runtime_contract(*, server_name: str, tool_id: str) -> ToolContract:
    for contract in _EVALUATOR_CLIENT_TARGET_CONTRACTS:
        if contract.tool_id == tool_id and (not server_name or contract.server_name == server_name):
            return contract
    raise ValueError(f"Unknown evaluator client target runtime contract: {server_name}.{tool_id}")


_EVALUATOR_CLIENT_TARGET_CONTRACTS: tuple[ToolContract, ...] = (
    *desktop_data_contracts(),
    *desktop_action_contracts(),
    SHELL_EXEC_READONLY_CONTRACT,
)


def standard_client_probe_tools() -> tuple[EvaluatorToolRef, ...]:
    """Return reusable read-only live probe tool specs.

    These declarations become executable only when the evaluator runtime is
    given a local implementation or a remote controller/device dispatcher. They
    are intentionally opt-in: placement allows client probes by default, while
    `allow_client_live_probe` and sync scheduling still gate actual execution.

    Returns:
        Tuple of standard client-side read-only evaluator tool specs.
    """

    return (
        _client_evaluator_tool(
            name="client_screenshot",
            description=(
                "Capture a read-only screenshot from the evaluated client/device. "
                "Use only in sync/manual evaluators with explicit live-probe permission."
            ),
            placement=EvaluationToolPlacement.CLIENT_PROBE,
            dispatch_name="capture_desktop_screenshot",
            server_name="DesktopNativeDataExecutor",
            readonly=True,
            timeout_seconds=15.0,
        ),
        _client_evaluator_tool(
            name="gui_inspect",
            description=(
                "Inspect open windows on the evaluated client/device without clicking or typing. "
                "Use this when screenshots are ambiguous."
            ),
            placement=EvaluationToolPlacement.CLIENT_PROBE,
            dispatch_name="get_desktop_app_target_info",
            server_name="DesktopNativeDataExecutor",
            readonly=True,
            timeout_seconds=10.0,
        ),
        _client_evaluator_tool(
            name="client_bash_readonly",
            description=(
                "Run a tightly-scoped allowlisted read-only command on the evaluated client/device. "
                "Only simple cmd argv commands such as pwd, ls, cat, stat, grep, head, tail, wc, ps, and uname are allowed."
            ),
            placement=EvaluationToolPlacement.CLIENT_PROBE,
            dispatch_name="shell.exec_readonly",
            server_name="DeviceShellExecutor",
            readonly=True,
            timeout_seconds=10.0,
        ),
    )


def standard_client_gui_tools() -> tuple[EvaluatorToolRef, ...]:
    """Return reusable client-side GUI tool specs for evaluator runs.

    These tools can mutate the evaluated desktop and are only intended for
    explicitly configured synchronous evaluators.

    Returns:
        Tuple of GUI-capable evaluator tool specs.
    """

    return (
        _client_evaluator_tool(
            name="client_gui_screenshot",
            description="Capture a fresh screenshot from the evaluated desktop.",
            placement=EvaluationToolPlacement.CLIENT_PROBE,
            dispatch_name="screenshot",
            server_name="DesktopNativeActionExecutor",
            readonly=True,
            timeout_seconds=15.0,
        ),
        _client_evaluator_tool(
            name="client_gui_click",
            description="Click a coordinate on the evaluated desktop.",
            placement=EvaluationToolPlacement.CLIENT_MUTATING,
            dispatch_name="click",
            server_name="DesktopNativeActionExecutor",
            readonly=False,
        ),
        _client_evaluator_tool(
            name="client_gui_type",
            description="Type literal text into the focused desktop element.",
            placement=EvaluationToolPlacement.CLIENT_MUTATING,
            dispatch_name="type",
            server_name="DesktopNativeActionExecutor",
            readonly=False,
        ),
        _client_evaluator_tool(
            name="client_gui_keypress",
            description="Press a key or key chord on the evaluated desktop.",
            placement=EvaluationToolPlacement.CLIENT_MUTATING,
            dispatch_name="keypress",
            server_name="DesktopNativeActionExecutor",
            readonly=False,
        ),
        _client_evaluator_tool(
            name="client_gui_wait",
            description="Wait briefly and capture the post-wait desktop state.",
            placement=EvaluationToolPlacement.CLIENT_MUTATING,
            dispatch_name="wait",
            server_name="DesktopNativeActionExecutor",
            readonly=False,
        ),
    )


__all__ = ["standard_client_gui_tools", "standard_client_probe_tools"]
