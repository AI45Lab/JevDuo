"""Provider-neutral contracts for a task on an externally owned runtime."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from northcua.submitter.specs import DesktopSubmitSpec, WebSubmitSpec
from northcua_protocol.capabilities import JsonObject
from northcua_protocol.payloads import TaskAccepted


class ExternalTaskDeviceBinding(BaseModel):
    """Scoped proof that a NorthCUA device belongs to the retained runtime."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    runtime_session_ref: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    bootstrap_operation_id: str = Field(min_length=1)
    bootstrap_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    registration_receipt_ref: str = Field(min_length=1)
    registration_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExternalTaskSpec(BaseModel):
    """Everything NorthCUA needs to execute one task without runtime custody."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    controller_url: str = Field(min_length=1)
    controller_token: SecretStr = Field(default_factory=lambda: SecretStr(""))
    trace_id: str = Field(min_length=1)
    attempt: int = Field(default=1, ge=1)
    output_dir: Path
    device: ExternalTaskDeviceBinding
    desktop: DesktopSubmitSpec | None = None
    web: WebSubmitSpec | None = None
    policy_route_ref: str = Field(min_length=1)
    source_projection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recovery_poll_seconds: float = Field(default=0.5, gt=0)

    @model_validator(mode="after")
    def _validate_agent_and_device(self) -> "ExternalTaskSpec":
        if (self.desktop is None) == (self.web is None):
            raise ValueError("external task requires exactly one desktop or web submit spec")
        submit = self.desktop or self.web
        if submit is not None and submit.target_device_id not in {"", self.device.device_id}:
            raise ValueError("external task target device differs from its runtime binding")
        return self


class ExternalTaskRecoverySpec(BaseModel):
    """Secret-free task configuration sufficient for process restart recovery."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    controller_url: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    attempt: int = Field(default=1, ge=1)
    output_dir: Path
    device: ExternalTaskDeviceBinding
    desktop: DesktopSubmitSpec | None = None
    web: WebSubmitSpec | None = None
    policy_route_ref: str = Field(min_length=1)
    source_projection_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recovery_poll_seconds: float = Field(default=0.5, gt=0)

    @classmethod
    def from_task_spec(cls, spec: ExternalTaskSpec) -> "ExternalTaskRecoverySpec":
        """Remove the Controller credential from a live task spec."""

        return cls.model_validate(spec.model_dump(mode="python", exclude={"controller_token"}))

    def restore(self, controller_token: SecretStr) -> ExternalTaskSpec:
        """Inject a freshly resolved credential without changing public identity."""

        return ExternalTaskSpec.model_validate({**self.model_dump(mode="python"), "controller_token": controller_token})


class ExternalTaskIntent(BaseModel):
    """Secret-free durable submit identity written before network side effects."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "northcua.external-task-intent/v2"
    task_id: str = Field(min_length=1)
    recovery_spec: ExternalTaskRecoverySpec
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_fingerprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted: TaskAccepted | None = None


class ExternalTaskFact(BaseModel):
    """Integrity-bound reference to one NorthCUA execution fact."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    ref: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExternalTaskTerminal(BaseModel):
    """Raw Controller terminal and evidence; no reward or cleanup authority."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    trace_id: str
    session_id: str
    state: str
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_fingerprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent_fact: ExternalTaskFact
    controller_terminal_fact: ExternalTaskFact
    execution_fact: ExternalTaskFact
    trace_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    public_error: str | None = None
    terminal_decision: JsonObject = Field(default_factory=dict)


class ExternalTaskObservationState(StrEnum):
    """Controller knowledge for an already-durable external task intent."""

    ABSENT = "absent"
    ACTIVE = "active"
    TERMINAL = "terminal"
    UNKNOWN = "unknown"


class ExternalTaskObservation(BaseModel):
    """Non-submitting recovery observation; UNKNOWN is always fail-closed."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    state: ExternalTaskObservationState
    controller_state: str | None = None
    terminal: ExternalTaskTerminal | None = None

    @model_validator(mode="after")
    def _terminal_matches_state(self) -> "ExternalTaskObservation":
        if (self.terminal is not None) != (self.state is ExternalTaskObservationState.TERMINAL):
            raise ValueError("external task observation terminal must match TERMINAL state")
        return self


__all__ = [
    "ExternalTaskDeviceBinding",
    "ExternalTaskFact",
    "ExternalTaskIntent",
    "ExternalTaskObservation",
    "ExternalTaskObservationState",
    "ExternalTaskRecoverySpec",
    "ExternalTaskSpec",
    "ExternalTaskTerminal",
]
