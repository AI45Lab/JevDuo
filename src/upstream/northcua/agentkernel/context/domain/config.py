"""Typed application configuration for shared context management."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .enums import (
    CheckpointBackend,
    ContextManagementMode,
    FailurePolicy,
    SummaryModelSource,
    SummaryTracePayloadMode,
)


class _StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


ConfigT = TypeVar("ConfigT", bound=_StrictConfig)


class ContextBudgetConfig(_StrictConfig):
    soft_trigger_ratio: float = Field(default=0.70, gt=0, lt=1)
    target_ratio: float = Field(default=0.55, gt=0, lt=1)
    safety_margin_ratio: float = Field(default=0.08, ge=0, lt=1)
    safety_margin_min_tokens: int = Field(default=4096, ge=0)
    recent_iterations: int = Field(default=4, ge=1)

    @model_validator(mode="after")
    def _validate_ratios(self) -> "ContextBudgetConfig":
        if self.target_ratio >= self.soft_trigger_ratio:
            raise ValueError("target_ratio must be lower than soft_trigger_ratio")
        return self


class SummaryModelConfig(_StrictConfig):
    source: SummaryModelSource = SummaryModelSource.INHERIT_PRIMARY
    agent_config_path: str | None = None

    @model_validator(mode="after")
    def _validate_source(self) -> "SummaryModelConfig":
        if self.source == SummaryModelSource.AGENT_CONFIG and not (self.agent_config_path or "").strip():
            raise ValueError("summary.model.agent_config_path is required when source=agent_config")
        if self.source == SummaryModelSource.INHERIT_PRIMARY and self.agent_config_path is not None:
            raise ValueError("summary.model.agent_config_path is only valid when source=agent_config")
        return self


class ContextSummaryConfig(_StrictConfig):
    model: SummaryModelConfig = Field(default_factory=SummaryModelConfig)
    prompt_profile: Literal["northcua_v1"] = "northcua_v1"
    max_output_tokens: int = Field(default=2048, ge=128)
    timeout_seconds: float = Field(default=60.0, gt=0)
    total_deadline_seconds: float = Field(default=120.0, gt=0)
    max_chunks: int = Field(default=4, ge=1)
    max_calls: int = Field(default=6, ge=1)
    repair_attempts: int = Field(default=1, ge=0, le=1)
    minimum_reduction_ratio: float = Field(default=0.01, gt=0, lt=1)

    @model_validator(mode="after")
    def _validate_call_budget(self) -> "ContextSummaryConfig":
        minimum_calls = self.max_chunks + (1 if self.max_chunks > 1 else 0) + self.repair_attempts
        if self.max_calls < minimum_calls:
            raise ValueError("summary.max_calls is too small for chunks and repair_attempts")
        if self.total_deadline_seconds < self.timeout_seconds:
            raise ValueError("summary.total_deadline_seconds must be >= timeout_seconds")
        return self


class ContextFailureConfig(_StrictConfig):
    soft: FailurePolicy = FailurePolicy.CONTINUE_UNMODIFIED
    hard: FailurePolicy = FailurePolicy.ATOMIC_TRIM_THEN_ERROR

    @model_validator(mode="after")
    def _validate_stage_policies(self) -> "ContextFailureConfig":
        if self.soft not in {FailurePolicy.CONTINUE_UNMODIFIED, FailurePolicy.ERROR}:
            raise ValueError("failure.soft must be continue_unmodified or error")
        if self.hard not in {FailurePolicy.ATOMIC_TRIM_THEN_ERROR, FailurePolicy.ERROR}:
            raise ValueError("failure.hard must be atomic_trim_then_error or error")
        return self


class ContextCheckpointConfig(_StrictConfig):
    backend: CheckpointBackend = CheckpointBackend.ARTIFACT
    schema_version: int = Field(default=1, ge=1)


class ContextTracingConfig(_StrictConfig):
    payload: SummaryTracePayloadMode = SummaryTracePayloadMode.FULL_REDACTED


class ContextAgentOverrideConfig(_StrictConfig):
    mode: ContextManagementMode | None = None
    policy_profile: str | None = None
    budget: ContextBudgetConfig | None = None
    summary: ContextSummaryConfig | None = None
    failure: ContextFailureConfig | None = None

    @field_validator("mode", mode="before")
    @classmethod
    def _normalize_yaml_off(cls, value: object) -> object:
        return "off" if value is False else value


class ResolvedContextPolicy(_StrictConfig):
    mode: ContextManagementMode
    policy_profile: str
    policy_version: str = "northcua-context-v1"
    budget: ContextBudgetConfig
    summary: ContextSummaryConfig
    failure: ContextFailureConfig
    checkpoint: ContextCheckpointConfig
    tracing: ContextTracingConfig

    def fingerprint(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ContextManagementConfig(_StrictConfig):
    """Global defaults plus typed per-agent overrides."""

    mode: ContextManagementMode = ContextManagementMode.OFF
    policy_profile: str = "generic"
    budget: ContextBudgetConfig = Field(default_factory=ContextBudgetConfig)
    summary: ContextSummaryConfig = Field(default_factory=ContextSummaryConfig)
    failure: ContextFailureConfig = Field(default_factory=ContextFailureConfig)
    checkpoint: ContextCheckpointConfig = Field(default_factory=ContextCheckpointConfig)
    tracing: ContextTracingConfig = Field(default_factory=ContextTracingConfig)
    agent_overrides: dict[str, ContextAgentOverrideConfig] = Field(default_factory=dict)

    @field_validator("mode", mode="before")
    @classmethod
    def _normalize_yaml_off(cls, value: object) -> object:
        # PyYAML's YAML 1.1 resolver parses an unquoted `off` scalar as False.
        # Accept that documented spelling without treating True as `enforce`.
        return "off" if value is False else value

    def resolve(self, agent_kind: str) -> ResolvedContextPolicy:
        family = _builtin_agent_family(agent_kind)
        override = self.agent_overrides.get(agent_kind) or self.agent_overrides.get(family)
        return ResolvedContextPolicy(
            mode=override.mode if override and override.mode is not None else self.mode,
            policy_profile=(
                override.policy_profile if override and override.policy_profile else family or self.policy_profile
            ),
            budget=_merge_config(self.budget, override.budget if override else None),
            summary=_merge_config(self.summary, override.summary if override else None),
            failure=_merge_config(self.failure, override.failure if override else None),
            checkpoint=self.checkpoint,
            tracing=self.tracing,
        )


def _builtin_agent_family(agent_kind: str) -> str:
    normalized = agent_kind.strip().lower()
    if normalized == "evaluator" or normalized.endswith("_evaluator") or "evaluator" in normalized:
        return "evaluator"
    aliases = {
        "desktop": "desktop",
        "web": "web",
        "generic": "generic",
    }
    return aliases.get(normalized, "")


def _merge_config(base: ConfigT, patch: ConfigT | None) -> ConfigT:
    if patch is None:
        return base.model_copy(deep=True)
    payload = base.model_dump(mode="python")
    for field_name in patch.model_fields_set:
        patch_value = getattr(patch, field_name)
        base_value = getattr(base, field_name, None)
        if isinstance(base_value, _StrictConfig) and isinstance(patch_value, _StrictConfig):
            payload[field_name] = _merge_config(base_value, patch_value).model_dump(mode="python")
        else:
            payload[field_name] = patch_value
    return type(base).model_validate(payload)
