"""Evaluator-facing wrappers for Web-agent live probes."""

from __future__ import annotations

from northcua.agentkit.evaluator.spec import EvaluatorToolRef
from northcua.agents.web.domain.actions import WebAgentToolName
from northcua.agents.web.mcp.descriptors import web_agent_action_contracts
from northcua.core.enums import EvaluationToolPlacement
from northcua_runtime_shared.tool_contracts import (
    EvaluatorClientBinding,
    ToolAudience,
    ToolContract,
    ToolContractKind,
    ToolPlacement,
    fingerprint_json,
)

_READ_ONLY_WEB_ACTION_WRAPPERS: dict[str, str] = {
    WebAgentToolName.SNAPSHOT.value: "client_browser_snapshot",
    WebAgentToolName.GET_TEXT.value: "client_browser_get_text",
    WebAgentToolName.LIST_TABS.value: "client_browser_tabs",
    WebAgentToolName.GET_URL.value: "client_browser_get_url",
    WebAgentToolName.SCREENSHOT.value: "client_browser_screenshot",
}
_EVALUATOR_BROWSER_SKIP_ACTIONS = {WebAgentToolName.NOTE.value}


def web_browser_client_tool_refs(*, allow_mutating: bool = False) -> tuple[EvaluatorToolRef, ...]:
    """Return evaluator client wrappers bound to the active Web-agent session.

    Raw Playwright ``browser_*`` contracts are intentionally not exposed here.
    The evaluator talks to the same ``web_*`` MCP action executor as the Web
    agent, so local and remote probes are pinned to the evaluated browser
    session/device instead of creating a second browser context. Mutating
    tools are exposed only when explicitly requested by evaluator config.
    """

    refs: list[EvaluatorToolRef] = []
    for contract in web_agent_action_contracts():
        if contract.provider_name in _EVALUATOR_BROWSER_SKIP_ACTIONS:
            continue
        readonly = contract.is_readonly
        if not readonly and not allow_mutating:
            continue
        provider_name = _wrapper_name_for_contract(contract)
        if provider_name is None:
            continue
        wrapper = _wrapper_contract(
            contract,
            provider_name=provider_name,
            description=(
                f"Evaluator-bound wrapper for {contract.provider_name}; "
                "executes in the evaluated Web agent browser session."
            ),
            schema=contract.parameters_schema,
            readonly=readonly,
        )
        refs.append(
            EvaluatorToolRef.from_contract(
                wrapper,
                placement=(
                    EvaluationToolPlacement.CLIENT_PROBE if readonly else EvaluationToolPlacement.CLIENT_MUTATING
                ),
            )
        )
    return tuple(refs)


def _wrapper_name_for_contract(contract: ToolContract) -> str | None:
    if contract.provider_name in _READ_ONLY_WEB_ACTION_WRAPPERS:
        return _READ_ONLY_WEB_ACTION_WRAPPERS[contract.provider_name]
    if not contract.provider_name.startswith("web_"):
        return None
    return f"client_browser_{contract.provider_name.removeprefix('web_')}"


def _wrapper_contract(
    contract: ToolContract,
    *,
    provider_name: str,
    description: str,
    schema: dict[str, object],
    readonly: bool,
) -> ToolContract:
    placement = ToolPlacement.CLIENT_PROBE if readonly else ToolPlacement.CLIENT_MUTATING
    target_fingerprint = fingerprint_json(contract.parameters_schema)
    dispatch_name = _controller_dispatch_name(contract)
    return ToolContract(
        tool_id=provider_name,
        public_name=provider_name,
        description=description,
        namespace="evaluator.web.client_browser",
        agent_kind="evaluator",
        kind=ToolContractKind.PROBE if readonly else ToolContractKind.ACTION,
        audience=ToolAudience.EVALUATOR,
        placement=placement,
        capability=contract.capability,
        json_schema=schema,
        timeout_seconds=contract.timeout_seconds,
        side_effect=not readonly,
        readonly=readonly,
        requires_focus=contract.requires_focus,
        risk_level=contract.risk_level,
        max_payload_bytes=contract.max_payload_bytes,
        binding=EvaluatorClientBinding(
            server_name=contract.server_name,
            dispatch_name=dispatch_name,
            placement=(
                EvaluationToolPlacement.CLIENT_PROBE.value
                if readonly
                else EvaluationToolPlacement.CLIENT_MUTATING.value
            ),
            target_tool_id=contract.tool_id,
            target_schema_fingerprint=target_fingerprint,
        ),
        profiles=("evaluator.web.round",),
        source="northcua.agents.web.evaluation.tools.browser_probe",
        audit_tags=(f"target_contract:{contract.contract_id}", f"target_schema:{target_fingerprint}"),
    )


def _controller_dispatch_name(contract: ToolContract) -> str:
    """Return the controller/device allowlist name for a target Web tool."""

    if contract.namespace:
        return f"{contract.namespace}.{contract.tool_id}"
    return contract.provider_name
