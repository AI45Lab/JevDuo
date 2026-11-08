"""Public contracts for one NorthCUA-managed task.

The contracts deliberately describe one task only.  They do not expose the
batch scheduler, retry policy, or training-framework concepts.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, SecretStr, model_validator

from northcua.evaluation.script import ScriptEvaluatorResult, ScriptEvaluatorSpec
from northcua.sandboxes.contracts import (
    SandboxCommandSpec,
    SandboxDeviceBinding,
    SandboxProviderSpec,
)
from northcua.submitter.ports import ControllerClientPort
from northcua.submitter.specs import DesktopSubmitSpec, WebSubmitSpec


class ManagedRuntimeTrustBinding(BaseModel):
    """Release-pinned identity for the sandbox adapter that observed a live lease.

    The values are public certificate coordinates. Credentials and signing keys
    never cross this one-task contract.
    """

    provider_ref: str = Field(min_length=1)
    provider_kind: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    provider_build_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    descriptor_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    conformance_profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    conformance_report_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolved_runtime_ref: str = Field(min_length=1)
    resolved_runtime_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_spec_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    sdk_name: str = ""
    sdk_version: str = ""


class ManagedTaskSpec(BaseModel):
    """Everything NorthCUA needs to own one sandbox-backed task lifecycle."""

    controller_url: str = Field(min_length=1)
    controller_token: SecretStr = Field(default_factory=lambda: SecretStr(""))
    trace_id: str = ""
    batch_id: str = "managed-task"
    attempt: int = Field(default=1, ge=1)
    output_dir: Path
    sandbox: SandboxProviderSpec
    device: SandboxDeviceBinding
    desktop: DesktopSubmitSpec | None = None
    web: WebSubmitSpec | None = None
    evaluator: ScriptEvaluatorSpec
    policy_route_ref: str = Field(min_length=1)
    runtime_trust: ManagedRuntimeTrustBinding | None = None
    startup_commands: list[SandboxCommandSpec] = Field(default_factory=list)
    teardown_commands: list[SandboxCommandSpec] = Field(default_factory=list)
    readiness_timeout_seconds: float = Field(default=600.0, gt=0)
    recovery_poll_seconds: float = Field(default=0.5, gt=0)
    close_grace_seconds: float = Field(default=30.0, gt=0)

    @model_validator(mode="after")
    def _exactly_one_agent(self) -> "ManagedTaskSpec":
        if (self.desktop is None) == (self.web is None):
            raise ValueError("managed task requires exactly one desktop or web submit spec")
        trust = self.runtime_trust
        if trust is not None:
            if trust.provider_kind != self.sandbox.provider:
                raise ValueError("managed runtime trust provider differs from sandbox provider")
            if trust.resolved_runtime_ref != self.sandbox.template:
                raise ValueError("managed runtime trust ref differs from sandbox template")
        return self


class ManagedArtifactFact(BaseModel):
    """Integrity-bound reference to a small durable NorthCUA fact."""

    ref: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ManagedTaskTerminal(BaseModel):
    """Terminal task, script verdict, and cleanup receipts returned together."""

    task_id: str
    trace_id: str
    session_id: str
    state: str
    terminal_payload: ManagedArtifactFact
    execution_fact: ManagedArtifactFact
    script_evaluation_fact: ManagedArtifactFact
    cleanup_receipt: ManagedArtifactFact
    runtime_custody_fact: ManagedArtifactFact
    script_evaluation: ScriptEvaluatorResult
    trace_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    public_error: str | None = None


__all__ = [
    "ControllerClientPort",
    "ManagedArtifactFact",
    "ManagedRuntimeTrustBinding",
    "ManagedTaskSpec",
    "ManagedTaskTerminal",
]
