"""Resolve a task-safe route reference inside the Controller process.

Task metadata contains only ``policy_route_ref``.  Endpoint and credential
bytes stay in Controller-local, owner-only runtime files and are never sent to
the E2B device worker.
"""

from __future__ import annotations

import copy
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field, SecretStr, field_validator

POLICY_ROUTE_MANIFEST_ENV = "NORTHCUA_POLICY_ROUTE_MANIFEST"
_LLM_FALLBACKS_STORAGE_KEY = "__northcua_llm_fallbacks__"

_SUPPORTED_SAMPLING_PARAMS = frozenset(
    {
        "temperature",
        "top_p",
        "top_k",
        "max_new_tokens",
        "stop",
        "stop_token_ids",
        "skip_special_tokens",
        "no_stop_trim",
        "spaces_between_special_tokens",
        "sampling_seed",
        "min_new_tokens",
        "repetition_penalty",
    }
)


def _validate_sampling_params(value: dict[str, object]) -> dict[str, object]:
    unknown = set(value) - _SUPPORTED_SAMPLING_PARAMS
    if unknown:
        raise ValueError(f"unsupported task policy sampling parameters: {sorted(unknown)}")
    try:
        json.dumps(value, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("task policy sampling parameters must be finite JSON values") from exc
    maximum = value.get("max_new_tokens")
    if maximum is not None and (not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 1):
        raise ValueError("max_new_tokens must be a positive integer")
    return dict(value)


class PolicyRoute(BaseModel):
    """Resolved route material used only for one Controller-owned agent."""

    route_ref: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_type: str = "openai_chat_completion"
    api_key: SecretStr
    sampling_params: dict[str, object] = Field(default_factory=dict)

    _sampling_params = field_validator("sampling_params")(_validate_sampling_params)


class PolicyRouteRecord(BaseModel):
    """Non-secret manifest record pointing at an owner-only credential file."""

    route_ref: str = Field(min_length=1)
    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    api_type: str = "openai_chat_completion"
    credential_file: Path
    sampling_params: dict[str, object] = Field(default_factory=dict)

    _sampling_params = field_validator("sampling_params")(_validate_sampling_params)


class PolicyRouteManifest(BaseModel):
    """Reloadable Controller-local policy route manifest."""

    version: int = Field(default=1, ge=1)
    routes: dict[str, PolicyRouteRecord]


class PolicyRouteResolver(Protocol):
    """Resolve one opaque task route reference to Controller-only material."""

    def resolve(self, route_ref: str) -> PolicyRoute: ...


class FilePolicyRouteResolver:
    """Read a reloadable manifest and separate owner-only credential files."""

    def __init__(self, manifest_path: Path) -> None:
        self._manifest_path = manifest_path

    @classmethod
    def from_environment(cls) -> "FilePolicyRouteResolver | None":
        """Build from the Controller env, or return none when routing is unused."""

        raw = os.getenv(POLICY_ROUTE_MANIFEST_ENV, "").strip()
        return cls(Path(raw)) if raw else None

    def resolve(self, route_ref: str) -> PolicyRoute:
        """Resolve the exact route and read its credential without logging it."""

        _require_owner_only(self._manifest_path, label="policy route manifest")
        manifest = PolicyRouteManifest.model_validate_json(self._manifest_path.read_text(encoding="utf-8"))
        try:
            record = manifest.routes[route_ref]
        except KeyError as exc:
            raise LookupError(f"unknown policy route ref: {route_ref}") from exc
        if record.route_ref != route_ref:
            raise ValueError("policy route manifest key/ref mismatch")
        credential_path = record.credential_file
        if not credential_path.is_absolute():
            credential_path = self._manifest_path.parent / credential_path
        _require_owner_only(credential_path, label="policy route credential")
        credential = credential_path.read_text(encoding="utf-8").strip()
        if not credential:
            raise ValueError("policy route credential is empty")
        return PolicyRoute(
            route_ref=route_ref,
            base_url=record.base_url,
            model=record.model,
            api_type=record.api_type,
            api_key=SecretStr(credential),
            sampling_params=record.sampling_params,
        )


def apply_task_policy_route(
    app: object,
    metadata: Mapping[str, object],
    *,
    resolver: PolicyRouteResolver | None = None,
) -> bool:
    """Install one resolved route on this task's isolated AgentConfig."""

    raw_ref = metadata.get("policy_route_ref")
    if raw_ref is None:
        return False
    if not isinstance(raw_ref, str) or not raw_ref:
        raise ValueError("policy_route_ref must be a non-empty string")
    selected = resolver or FilePolicyRouteResolver.from_environment()
    if selected is None:
        raise RuntimeError("policy_route_ref requires a Controller policy route resolver")
    route = selected.resolve(raw_ref)
    if route.route_ref != raw_ref:
        raise ValueError("policy route resolver returned a different route ref")
    agent_config = getattr(app, "agent_config", None)
    llm_config = getattr(agent_config, "llm_config", None)
    if agent_config is None or llm_config is None:
        raise ValueError("policy routing requires an application AgentConfig with llm_config")
    routed = copy.deepcopy(llm_config)
    routed.base_url = route.base_url
    routed.api_key = route.api_key.get_secret_value()
    routed.model = route.model
    routed.api_type = route.api_type
    # A task route changes provider families. Provider-specific payload knobs
    # from the base app (for example Responses reasoning/include fields) must
    # not leak into a Chat Completions request. Keep only neutral capability
    # and context hints; the runtime payload adapter strips these before the
    # SDK call.
    neutral_extra_params = {
        key: value
        for key, value in dict(getattr(routed, "extra_params", {}) or {}).items()
        if key
        in {
            "image_transport",
            "max_context_tokens",
            "reasoning_replay_policy",
            "supports_images",
            "supports_structured_output",
            "supports_tool_calls",
            "tool_image_policy",
            "transcode_images_to_jpeg",
        }
    }
    routed.extra_params = neutral_extra_params
    _apply_sampling_params(routed, route.sampling_params)
    agent_config.llm_config = routed
    storage = dict(agent_config.global_storage or {})
    storage.pop(_LLM_FALLBACKS_STORAGE_KEY, None)
    agent_config.global_storage = storage
    return True


def _apply_sampling_params(llm_config: object, sampling_params: dict[str, object]) -> None:
    if not sampling_params:
        return
    direct = {
        "temperature": "temperature",
        "top_p": "top_p",
        "max_new_tokens": "max_tokens",
    }
    for source, target in direct.items():
        if source in sampling_params:
            setattr(llm_config, target, sampling_params[source])
    extra_body = {key: value for key, value in sampling_params.items() if key not in direct}
    if extra_body:
        extra_params = dict(getattr(llm_config, "extra_params", {}) or {})
        extra_params["extra_body"] = extra_body
        setattr(llm_config, "extra_params", extra_params)


def _require_owner_only(path: Path, *, label: str) -> None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"{label} does not exist: {path}") from exc
    if mode & 0o077:
        raise PermissionError(f"{label} must not be group/world accessible: {path}")


__all__ = [
    "FilePolicyRouteResolver",
    "POLICY_ROUTE_MANIFEST_ENV",
    "PolicyRoute",
    "PolicyRouteManifest",
    "PolicyRouteRecord",
    "PolicyRouteResolver",
    "apply_task_policy_route",
]
