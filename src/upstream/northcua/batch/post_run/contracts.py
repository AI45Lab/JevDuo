"""Typed post-run hook contracts shared by batch and case-suite layers."""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator

from northcua.sandboxes.contracts import SandboxFileUploadSpec
from northcua_protocol.capabilities import JsonObject

_HOOK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ARTIFACT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_MAX_OUTPUT_CHARS = 8000


class PostRunHookKind(str, Enum):
    """Supported hook execution kinds."""

    COMMAND_JSON = "command_json"


class PostRunHookStatus(str, Enum):
    """Normalized post-run hook terminal status."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    TIMED_OUT = "timed_out"


class PostRunArtifactSpec(BaseModel):
    """One optional sandbox artifact collected after a hook runs.

    Attributes:
        remote_path: Absolute path inside the sandbox to copy after execution.
        name: Optional path-safe artifact name used in the local output folder.
        required: Whether a missing artifact should mark the hook result as
            errored instead of recording a non-fatal artifact error.
    """

    remote_path: str
    name: str = ""
    required: bool = False

    @model_validator(mode="after")
    def _validate_artifact(self) -> "PostRunArtifactSpec":
        if not self.remote_path.startswith("/"):
            raise ValueError("post-run artifact remote_path must be absolute")
        if "\x00" in self.remote_path or "\n" in self.remote_path or "\r" in self.remote_path:
            raise ValueError("post-run artifact remote_path contains unsafe characters")
        if self.name and not _ARTIFACT_NAME_PATTERN.fullmatch(self.name):
            raise ValueError("post-run artifact name must be path-safe")
        return self


class PostRunHookSpec(BaseModel):
    """Provider-neutral hook that runs inside the task sandbox after the agent.

    Attributes:
        hook_id: Stable path-safe hook identifier.
        kind: Hook execution contract currently used for result parsing.
        label: Optional human-readable command label.
        command: Legacy shell command executed inside the sandbox.
        argv: Argument-safe command used by external-runtime integrations.
        timeout_seconds: Per-hook command timeout.
        cwd: Optional sandbox working directory.
        env: Extra environment variables for the hook command.
        allow_command_failure: Whether non-zero exit codes can still be parsed.
        score_threshold: Minimum score that counts as passed.
        status_key: JSON key containing an explicit status string.
        score_key: JSON key containing the numeric score.
        passed_key: JSON key containing a boolean pass flag.
        artifacts: Optional sandbox artifacts to collect after the hook.
        metadata: Caller-defined metadata copied into the result.
    """

    hook_id: str
    kind: PostRunHookKind = PostRunHookKind.COMMAND_JSON
    label: str = ""
    command: str = ""
    argv: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=120.0, gt=0)
    infra_retries: int = Field(default=0, ge=0, le=5)
    infra_retry_backoff_seconds: float = Field(default=1.0, ge=0)
    cwd: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    allow_command_failure: bool = False
    score_threshold: float = 0.5
    status_key: str = "status"
    score_key: str = "score"
    passed_key: str = "passed"
    artifacts: list[PostRunArtifactSpec] = Field(default_factory=list)
    required_evidence_ids: list[str] = Field(default_factory=list)
    file_uploads: list[SandboxFileUploadSpec] = Field(default_factory=list)
    terminal_decision_remote_path: str = ""
    metadata: JsonObject = Field(default_factory=dict)

    @field_validator("hook_id")
    @classmethod
    def _validate_hook_id(cls, value: str) -> str:
        if not _HOOK_ID_PATTERN.fullmatch(value):
            raise ValueError("post-run hook_id must be path-safe")
        return value

    @field_validator("terminal_decision_remote_path")
    @classmethod
    def _validate_terminal_decision_path(cls, value: str) -> str:
        if value and not value.startswith("/"):
            raise ValueError("terminal_decision_remote_path must be absolute")
        return value

    @model_validator(mode="after")
    def _validate_command(self) -> "PostRunHookSpec":
        if bool(self.command.strip()) == bool(self.argv):
            raise ValueError("post-run hook requires exactly one command or argv")
        if "\x00" in self.command:
            raise ValueError("post-run hook command cannot contain NUL bytes")
        if self.argv and not self.argv[0]:
            raise ValueError("post-run hook executable cannot be empty")
        if any("\x00" in item for item in self.argv):
            raise ValueError("post-run hook argv entries must be NUL-free")
        if any(not item or "\x00" in item for item in self.required_evidence_ids):
            raise ValueError("post-run hook required evidence ids must be non-empty and NUL-free")
        if len(self.required_evidence_ids) != len(set(self.required_evidence_ids)):
            raise ValueError("post-run hook required evidence ids must be unique")
        return self


class PostRunHookResult(BaseModel):
    """Persisted result from one post-run hook.

    Attributes:
        hook_id: Stable hook identifier matching the spec.
        kind: Hook execution contract used to interpret the result.
        label: Human-readable command label.
        status: Normalized terminal status for the hook.
        passed: Optional boolean verdict parsed from output.
        score: Optional numeric score parsed from output.
        score_threshold: Threshold used to compute pass/fail.
        exit_code: Sandbox command exit code.
        timed_out: Whether the sandbox command hit its timeout.
        duration_seconds: Wall-clock hook runtime.
        stdout: Truncated standard output.
        stderr: Truncated standard error.
        error: Internal execution or parsing error, if any.
        result_json: Parsed JSON payload from stdout.
        artifacts: Local artifact paths by artifact name.
        artifact_errors: Collection errors by artifact name.
        metadata: Caller-defined metadata copied from the spec.
    """

    hook_id: str
    kind: PostRunHookKind = PostRunHookKind.COMMAND_JSON
    label: str = ""
    status: PostRunHookStatus
    passed: bool | None = None
    score: float | None = None
    score_threshold: float = 0.5
    exit_code: int = 0
    timed_out: bool = False
    duration_seconds: float = 0.0
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    result_json: JsonObject = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)
    artifact_errors: dict[str, str] = Field(default_factory=dict)
    required_evidence_ids: list[str] = Field(default_factory=list)
    metadata: JsonObject = Field(default_factory=dict)

    @field_validator("stdout", "stderr", "error")
    @classmethod
    def _limit_output(cls, value: str) -> str:
        if len(value) <= _MAX_OUTPUT_CHARS:
            return value
        return value[: _MAX_OUTPUT_CHARS - 3] + "..."
