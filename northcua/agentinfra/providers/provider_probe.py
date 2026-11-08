"""Live provider compatibility probe harness for NorthCUA."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

import httpx
from nexau.archs.llm.llm_config import LLMConfig

from northcua.agentinfra.providers.provider_payloads import build_provider_request_payload
from northcua.agentinfra.providers.provider_probe_cases import ProviderProbeCase, default_probe_cases
from northcua.agentinfra.providers.provider_profiles import ProviderCapabilityProfile, resolve_provider_profile
from northcua.core.json_types import JsonObject

# pyright: reportMissingImports=false


logger = logging.getLogger(__name__)
_default_probe_cases = default_probe_cases


@dataclass(slots=True, frozen=True)
class ProviderProbeResult:
    """Structured result for one probe case.

    Attributes:
        case_id: Stable probe identifier.
        label: Human-readable probe label.
        provider_mode: NexAU api_type used for dispatch.
        status: Probe status classification.
        http_status: HTTP status code when a response was received.
        response_excerpt: Short non-secret response summary.
        tool_call_worked: Tool-call validation result when applicable.
        image_worked: Image input validation result when applicable.
        structured_output_valid: JSON validation result when applicable.
        request_path: Path to the persisted request artifact.
        response_path: Path to the persisted response artifact.
        error: Short error message for failed cases.
    """

    case_id: str
    label: str
    provider_mode: str
    status: str
    http_status: int | None = None
    response_excerpt: str = ""
    tool_call_worked: bool | None = None
    image_worked: bool | None = None
    structured_output_valid: bool | None = None
    request_path: str = ""
    response_path: str = ""
    error: str = ""


@dataclass(slots=True, frozen=True)
class ProviderProbeReport:
    """Structured provider probe suite report.

    Attributes:
        profile: Resolved provider capability profile.
        results: Per-case probe results.
    """

    profile: ProviderCapabilityProfile
    results: list[ProviderProbeResult]


def run_provider_probe_suite(
    *,
    llm_config: LLMConfig,
    output_dir: Path,
    timeout_seconds: float = 30.0,
) -> ProviderProbeReport:
    """Run text/tool/image/structured-output probes against one provider config.

    Args:
        llm_config: NexAU LLM config to probe.
        output_dir: Directory where request/response artifacts are written.
        timeout_seconds: Per-request HTTP timeout.

    Returns:
        Structured provider probe report.
    """

    output_dir.mkdir(parents=True, exist_ok=True)
    profile = resolve_provider_profile(llm_config)
    results: list[ProviderProbeResult] = []

    for case in default_probe_cases(llm_config.api_type):
        results.append(
            _run_provider_probe_case(
                llm_config=llm_config,
                output_dir=output_dir,
                timeout_seconds=timeout_seconds,
                case=case,
            )
        )

    report = ProviderProbeReport(profile=profile, results=results)
    _write_provider_probe_report(output_dir=output_dir, report=report)
    return report


def _run_provider_probe_case(
    *,
    llm_config: LLMConfig,
    output_dir: Path,
    timeout_seconds: float,
    case: ProviderProbeCase,
) -> ProviderProbeResult:
    case_dir = output_dir / case.case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    request_path = case_dir / "request.json"
    response_path = case_dir / "response.json"
    try:
        request_payload = _build_probe_request(llm_config=llm_config, case=case)
        request_path.write_text(json.dumps(request_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        http_response = _dispatch_provider_request(
            llm_config=llm_config,
            payload=request_payload,
            timeout_seconds=timeout_seconds,
        )
        response_payload: JsonObject = {
            "status_code": http_response.status_code,
            "body": _safe_json(http_response),
        }
        response_path.write_text(json.dumps(response_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        parsed_body = cast(JsonObject, response_payload["body"])
        return _success_probe_result(
            llm_config=llm_config,
            case=case,
            http_response=http_response,
            parsed_body=parsed_body,
            request_path=request_path,
            response_path=response_path,
        )
    except Exception as exc:
        logger.warning(
            "Provider probe case failed; preserving fail-open report entry.",
            extra={"case_id": case.case_id, "api_type": llm_config.api_type},
            exc_info=True,
        )
        request_path.write_text(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), encoding="utf-8")
        return _failure_probe_result(llm_config=llm_config, case=case, request_path=request_path, exc=exc)


def _build_probe_request(*, llm_config: LLMConfig, case: ProviderProbeCase) -> JsonObject:
    prepared = build_provider_request_payload(
        llm_config=llm_config,
        messages=case.messages,
        tool_definitions=case.tool_definitions or None,
        tool_choice=case.tool_choice,
    )
    request_payload = dict(prepared.request_payload)
    if case.extra_request_fields:
        request_payload.update(case.extra_request_fields)
    return request_payload


def _success_probe_result(
    *,
    llm_config: LLMConfig,
    case: ProviderProbeCase,
    http_response: httpx.Response,
    parsed_body: JsonObject,
    request_path: Path,
    response_path: Path,
) -> ProviderProbeResult:
    return ProviderProbeResult(
        case_id=case.case_id,
        label=case.label,
        provider_mode=llm_config.api_type,
        status="success" if http_response.is_success else "http_error",
        http_status=http_response.status_code,
        response_excerpt=_response_excerpt(parsed_body),
        tool_call_worked=_tool_call_worked(parsed_body, llm_config.api_type) if case.expects_tool_call else None,
        image_worked=http_response.is_success if case.is_image_probe else None,
        structured_output_valid=(
            _structured_output_valid(parsed_body, llm_config.api_type) if case.expects_json else None
        ),
        request_path=str(request_path),
        response_path=str(response_path),
        error="" if http_response.is_success else _http_error(parsed_body),
    )


def _failure_probe_result(
    *,
    llm_config: LLMConfig,
    case: ProviderProbeCase,
    request_path: Path,
    exc: Exception,
) -> ProviderProbeResult:
    return ProviderProbeResult(
        case_id=case.case_id,
        label=case.label,
        provider_mode=llm_config.api_type,
        status="unsupported" if isinstance(exc, ValueError) else "exception",
        response_excerpt=str(exc),
        tool_call_worked=None,
        image_worked=False if case.is_image_probe else None,
        structured_output_valid=False if case.expects_json else None,
        request_path=str(request_path),
        error=str(exc),
    )


def _write_provider_probe_report(*, output_dir: Path, report: ProviderProbeReport) -> None:
    (output_dir / "provider_probe_report.json").write_text(
        json.dumps(
            {
                "profile": asdict(report.profile),
                "results": [asdict(result) for result in report.results],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _dispatch_provider_request(
    *,
    llm_config: LLMConfig,
    payload: JsonObject,
    timeout_seconds: float,
) -> httpx.Response:
    url = _provider_request_url(llm_config)
    headers = _provider_headers(llm_config)
    with httpx.Client(timeout=timeout_seconds) as client:
        return client.post(url, headers=headers, json=payload)


def _provider_request_url(llm_config: LLMConfig) -> str:
    base_url = (llm_config.base_url or "").rstrip("/")
    if llm_config.api_type == "openai_chat_completion":
        if base_url.endswith("/chat/completions"):
            return base_url
        return f"{base_url}/chat/completions"
    if llm_config.api_type == "anthropic_chat_completion":
        if base_url.endswith("/messages"):
            return base_url
        if base_url.endswith("/v1"):
            return f"{base_url}/messages"
        return f"{base_url}/v1/messages"
    if llm_config.api_type == "openai_responses":
        if base_url.endswith("/responses"):
            return base_url
        return f"{base_url}/responses"
    raise ValueError(f"Unsupported api_type for provider probe: {llm_config.api_type}")


def _provider_headers(llm_config: LLMConfig) -> dict[str, str]:
    if llm_config.api_type == "anthropic_chat_completion":
        return {
            "x-api-key": llm_config.api_key or "",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
    return {
        "authorization": f"Bearer {llm_config.api_key or ''}",
        "content-type": "application/json",
    }


def _safe_json(response: httpx.Response) -> JsonObject:
    try:
        payload = response.json()
        return payload if isinstance(payload, dict) else {"value": payload}
    except Exception:
        logger.debug(
            "Provider probe response was not JSON; falling back to response text.",
            extra={"status_code": response.status_code},
            exc_info=True,
        )
        return {"text": response.text}


def _response_excerpt(payload: JsonObject) -> str:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message", {})
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                return content[:400]
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                return json.dumps(tool_calls[0], ensure_ascii=False)[:400]
    content = payload.get("content")
    if isinstance(content, list):
        text_parts = [
            str(block["text"]) for block in content if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        if text_parts:
            return "\n".join(text_parts)[:400]
        tool_blocks = [block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"]
        if tool_blocks:
            return json.dumps(tool_blocks[0], ensure_ascii=False)[:400]
    if isinstance(payload.get("output_text"), str):
        return str(payload.get("output_text"))[:400]
    return json.dumps(payload, ensure_ascii=False)[:400]


def _tool_call_worked(payload: JsonObject, api_type: str) -> bool:
    if api_type == "anthropic_chat_completion":
        content = payload.get("content")
        return bool(
            isinstance(content, list)
            and any(isinstance(block, dict) and block.get("type") == "tool_use" for block in content)
        )
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return False
    message = choices[0].get("message", {})
    return bool(isinstance(message, dict) and message.get("tool_calls"))


def _structured_output_valid(payload: JsonObject, api_type: str) -> bool:
    if api_type == "anthropic_chat_completion":
        text = _response_excerpt(payload)
    else:
        text = _response_excerpt(payload)
    cleaned = text.strip().strip("`")
    if cleaned.startswith("json"):
        cleaned = cleaned[4:].strip()
    try:
        parsed = json.loads(cleaned)
    except Exception:
        logger.debug("Provider probe structured-output response was not valid JSON.", exc_info=True)
        return False
    return isinstance(parsed, dict) and parsed.get("ok") is True and "label" in parsed


def _http_error(payload: JsonObject) -> str:
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return message
    if isinstance(error, str):
        return error
    return _response_excerpt(payload)
