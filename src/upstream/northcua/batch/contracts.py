"""Typed contracts shared by manifest parsing, scheduling, resume, and reporting."""

import hashlib
import uuid
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from northcua.batch.post_run.contracts import PostRunHookResult, PostRunHookSpec
from northcua.batch.preparation.contracts import PreparedSandboxPolicy
from northcua.reliability.contracts import FailureAction
from northcua.sandboxes.contracts import (
    DEFAULT_DEVICE_READINESS_TIMEOUT_SECONDS,
    SandboxCommandSpec,
    SandboxProviderSpec,
)
from northcua.telemetry.contracts import FailureCategory
from northcua_protocol.artifacts import ArtifactRef
from northcua_protocol.capabilities import Capability, JsonObject
from northcua_protocol.state import TaskState


class BatchTaskStatus(str, Enum):
    """Local batch task lifecycle state."""

    PENDING = "pending"
    STARTING_SANDBOX = "starting_sandbox"
    BOOTSTRAPPING_DEVICE = "bootstrapping_device"
    WAITING_DEVICE = "waiting_device"
    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    EVALUATION_PENDING = "evaluation_pending"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELED = "canceled"
    INCOMPLETE = "incomplete"


class BatchDefaults(BaseModel):
    """Default values shared by tasks in one manifest.

    Attributes:
        controller_url: Controller websocket URL used by local submitters.
        device_controller_url: Controller websocket URL passed to sandbox
            device workers. This can differ from ``controller_url`` when a
            managed local controller is submitted to through localhost but
            remote sandboxes must connect through a public tunnel.
        token: Submitter/device token.
        concurrency: Maximum concurrent tasks.
        sandbox: Default sandbox provider spec.
        startup_commands: Default commands run before worker bootstrap.
        pre_submit_commands: Default commands run after device readiness and
            before controller submission.
        teardown_commands: Default commands run before sandbox release.
        timeout_seconds: Default task wait timeout.
        max_retries: Default task retry count.
    """

    controller_url: str = "ws://127.0.0.1:8765"
    device_controller_url: str = ""
    token: str = ""
    concurrency: int = 1
    sandbox: SandboxProviderSpec = Field(default_factory=SandboxProviderSpec)
    startup_commands: list[SandboxCommandSpec] = Field(default_factory=list)
    pre_submit_commands: list[SandboxCommandSpec] = Field(default_factory=list)
    teardown_commands: list[SandboxCommandSpec] = Field(default_factory=list)
    timeout_seconds: float = 0
    max_retries: int = 0


class BatchTaskSpec(BaseModel):
    """One task in a batch manifest.

    Attributes:
        task_id: Stable task id; generated when omitted.
        agent_kind: AgentKit agent kind to run through the controller.
        request: Canonical user request sent to the controller.
        query: Convenience alias used when ``request`` is omitted.
        url: Optional web-agent initial URL.
        max_steps: Optional round/query step cap.
        max_session_steps: Optional cumulative session step cap.
        metadata: Small structured metadata merged into controller task metadata.
        input_artifacts: Inline/controller artifact refs attached to the task.
        required_capabilities: Device capabilities required by the task.
        sandbox: Per-task sandbox override.
        startup_commands: Per-task startup commands appended after defaults.
        pre_submit_commands: Per-task commands appended after defaults once
            the sandbox device is ready, immediately before controller submit.
        post_run_hooks: Per-task benchmark hooks run after the controller
            reaches a terminal result and before sandbox release.
        teardown_commands: Per-task teardown commands appended after defaults.
        timeout_seconds: Per-task wait timeout; zero means no timeout.
        max_retries: Per-task retry count; negative means use manifest default.
    """

    task_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    agent_kind: str
    request: str = ""
    query: str = ""
    url: str = ""
    max_steps: int = 0
    max_session_steps: int = 0
    metadata: JsonObject = Field(default_factory=dict)
    input_artifacts: list[ArtifactRef] = Field(default_factory=list)
    required_capabilities: set[Capability] = Field(default_factory=set)
    sandbox: SandboxProviderSpec | None = None
    startup_commands: list[SandboxCommandSpec] = Field(default_factory=list)
    pre_submit_commands: list[SandboxCommandSpec] = Field(default_factory=list)
    post_run_hooks: list[PostRunHookSpec] = Field(default_factory=list)
    teardown_commands: list[SandboxCommandSpec] = Field(default_factory=list)
    timeout_seconds: float = 0
    max_retries: int = -1

    @model_validator(mode="after")
    def _normalize_request(self) -> "BatchTaskSpec":
        """Promote query aliases into the canonical request field."""

        if not self.request and self.query:
            self.request = self.query
        if not self.request:
            raise ValueError("batch task requires request or query")
        return self


class BatchManifest(BaseModel):
    """Root YAML/JSONL batch manifest.

    Attributes:
        version: Manifest schema version.
        batch_id: Stable batch id used for output directories and metadata.
        defaults: Shared run defaults.
        tasks: Ordered task list.
    """

    version: str = "v1"
    batch_id: str = Field(default_factory=lambda: f"batch-{uuid.uuid4().hex[:12]}")
    defaults: BatchDefaults = Field(default_factory=BatchDefaults)
    tasks: list[BatchTaskSpec]

    @model_validator(mode="after")
    def _validate_tasks(self) -> "BatchManifest":
        """Ensure the manifest contains a non-empty set of unique task ids."""

        if not self.tasks:
            raise ValueError("batch manifest requires at least one task")
        seen: set[str] = set()
        duplicates: set[str] = set()
        for task in self.tasks:
            spec = task.sandbox or self.defaults.sandbox
            if spec.provider == "local-process":
                if task.agent_kind != "web" or spec.desktop or spec.platform.value != "browser":
                    raise ValueError("local-process batches require Web tasks and platform: browser, desktop: false")
                if spec.allocation_strategy.value != "ephemeral_per_task":
                    raise ValueError("local-process batches require ephemeral_per_task allocation")
                if any(
                    (
                        self.defaults.startup_commands,
                        self.defaults.pre_submit_commands,
                        self.defaults.teardown_commands,
                        task.startup_commands,
                        task.pre_submit_commands,
                        task.teardown_commands,
                        task.post_run_hooks,
                        spec.file_uploads,
                    )
                ):
                    raise ValueError("local-process batches do not support guest commands, hooks or uploads")
            if task.task_id in seen:
                duplicates.add(task.task_id)
            seen.add(task.task_id)
        if duplicates:
            raise ValueError(f"batch manifest task_id values must be unique: {sorted(duplicates)}")
        return self


class BatchRunConfig(BaseModel):
    """Runtime options for executing a batch.

    Attributes:
        manifest: Validated manifest.
        output_dir: Local directory for batch logs and reports.
        controller_url: Optional CLI override for the submitter controller URL.
        device_controller_url: Optional CLI override for the sandbox device
            worker controller URL.
        token: Optional CLI override for submitter/device auth.
        concurrency: Optional CLI override for maximum concurrent tasks.
        sandbox_create_concurrency: Optional cap for concurrent sandbox create
            operations. Zero means the runner may apply provider-aware defaults.
        controller_submit_concurrency: Optional cap for concurrent controller
            submit websocket openings. Zero means unconstrained.
        controller_status_concurrency: Optional cap for concurrent controller
            status websocket requests. Zero derives a conservative cap from
            submit concurrency.
        controller_submit_jitter_seconds: Deterministic per-task delay applied
            inside the submit cap before opening a controller submitter socket.
        device_readiness_timeout_seconds: Maximum time to wait for sandbox
            device workers to become ready after bootstrap.
        infra_retry_backoff_seconds: Delay sequence for retryable infra
            failures. Empty means retry immediately inside the current worker.
        dry_run: Validate and plan without creating sandboxes or submitting tasks.
        runner_id: Stable local runner id used for SQLite task leases.
        lease_timeout_seconds: Local task lease timeout for crash takeover.
        persist_token: Whether ``run_config.json`` may persist the submitter
            token for future ``batch resume``. Public experiment runs disable
            this and require the caller to provide the token again.
        telemetry_experiment_id: Optional experiment id used to correlate
            case telemetry across batch, controller, and evaluator reports.
        telemetry_controller_db_path: Optional controller DB path used when a
            managed local controller should receive authoritative telemetry.
        prepared_sandbox_policy: Optional background prepared sandbox queue
            policy. Disabled by default.
        identity_manifest: Optional immutable manifest snapshot used to verify
            the identity of an existing run when the runtime manifest carries
            narrowly scoped retry-budget overrides.
        identity_manifest_sha256: SHA256 of the exact persisted manifest bytes
            used for resume identity validation.
    """

    manifest: BatchManifest
    output_dir: Path = Path("logs/batch")
    controller_url: str = ""
    device_controller_url: str = ""
    token: str = ""
    concurrency: int = 0
    sandbox_create_concurrency: int = Field(default=0, ge=0)
    controller_submit_concurrency: int = Field(default=0, ge=0)
    controller_status_concurrency: int = Field(default=0, ge=0)
    controller_submit_jitter_seconds: float = Field(default=0.0, ge=0.0)
    device_readiness_timeout_seconds: float = Field(default=DEFAULT_DEVICE_READINESS_TIMEOUT_SECONDS, gt=0)
    infra_retry_backoff_seconds: list[float] = Field(default_factory=list)
    dry_run: bool = False
    runner_id: str = Field(default_factory=lambda: f"runner-{uuid.uuid4().hex}")
    lease_timeout_seconds: float = 120.0
    persist_token: bool = True
    telemetry_experiment_id: str = ""
    telemetry_controller_db_path: Path | None = None
    prepared_sandbox_policy: PreparedSandboxPolicy = Field(default_factory=PreparedSandboxPolicy)
    identity_manifest: BatchManifest | None = Field(default=None, exclude=True, repr=False)
    identity_manifest_sha256: str = Field(default="", exclude=True, repr=False)


def _token_fingerprint(token: str) -> str:
    """Return a non-secret runtime identity fingerprint for a token."""

    if not token:
        return ""
    return hashlib.sha256(f"northcua-batch-token-v1:{token}".encode("utf-8")).hexdigest()


def _effective_run_token(config: "BatchRunConfig") -> str:
    """Return the token that runtime submitters/device workers will use."""

    return config.token or config.manifest.defaults.token


class BatchRunResumeConfig(BaseModel):
    """Runtime overrides persisted beside a batch for later resume.

    Attributes:
        controller_url: CLI/submitter controller URL override used by the original run.
        device_controller_url: Sandbox device worker URL override used by the
            original run.
        token: Submitter/device token override used by the original run.
        concurrency: CLI concurrency override used by the original run.
        sandbox_create_concurrency: Sandbox create concurrency override used
            by the original run.
        controller_submit_concurrency: Controller submit websocket opening cap
            used by the original run.
        controller_status_concurrency: Controller status websocket request cap
            used by the original run.
        controller_submit_jitter_seconds: Per-task deterministic submit jitter
            used by the original run.
        device_readiness_timeout_seconds: Persisted device worker readiness
            timeout used by sandbox acquisition paths.
        infra_retry_backoff_seconds: Persisted delay sequence for retryable
            infra failures.
        persist_token: Whether future resume writes may persist token again.
        telemetry_experiment_id: Experiment id used to correlate telemetry.
        telemetry_controller_db_path: Optional controller DB path used by an
            injected telemetry sink.
        prepared_sandbox_policy: Persisted prepared sandbox queue policy.
    """

    controller_url: str = ""
    device_controller_url: str = ""
    token: str = ""
    token_fingerprint: str = ""
    concurrency: int = 0
    sandbox_create_concurrency: int = 0
    controller_submit_concurrency: int = 0
    controller_status_concurrency: int = 0
    controller_submit_jitter_seconds: float = 0.0
    device_readiness_timeout_seconds: float = DEFAULT_DEVICE_READINESS_TIMEOUT_SECONDS
    infra_retry_backoff_seconds: list[float] = Field(default_factory=list)
    persist_token: bool = True
    telemetry_experiment_id: str = ""
    telemetry_controller_db_path: Path | None = None
    prepared_sandbox_policy: PreparedSandboxPolicy = Field(default_factory=PreparedSandboxPolicy)

    @classmethod
    def from_run_config(cls, config: BatchRunConfig) -> "BatchRunResumeConfig":
        """Create a resumable runtime snapshot from the active run config.

        Args:
            config: Runtime configuration used for the original batch run.

        Returns:
            Serializable subset of CLI overrides needed to reconnect on resume.
        """

        effective_token = _effective_run_token(config)
        return cls(
            controller_url=config.controller_url,
            device_controller_url=config.device_controller_url,
            token=effective_token if config.persist_token else "",
            token_fingerprint=_token_fingerprint(effective_token),
            concurrency=config.concurrency,
            sandbox_create_concurrency=config.sandbox_create_concurrency,
            controller_submit_concurrency=config.controller_submit_concurrency,
            controller_status_concurrency=config.controller_status_concurrency,
            controller_submit_jitter_seconds=config.controller_submit_jitter_seconds,
            device_readiness_timeout_seconds=config.device_readiness_timeout_seconds,
            infra_retry_backoff_seconds=list(config.infra_retry_backoff_seconds),
            persist_token=config.persist_token,
            telemetry_experiment_id=config.telemetry_experiment_id,
            telemetry_controller_db_path=config.telemetry_controller_db_path,
            prepared_sandbox_policy=config.prepared_sandbox_policy,
        )

    def merge_overrides(
        self,
        *,
        controller_url: str = "",
        token: str = "",
        concurrency: int = 0,
        sandbox_create_concurrency: int = 0,
        controller_submit_concurrency: int = 0,
        controller_status_concurrency: int = 0,
        controller_submit_jitter_seconds: float = 0.0,
        device_readiness_timeout_seconds: float = 0.0,
        infra_retry_backoff_seconds: list[float] | None = None,
        device_controller_url: str = "",
        telemetry_controller_db_path: Path | None = None,
        prepared_sandbox_policy: PreparedSandboxPolicy | None = None,
    ) -> "BatchRunResumeConfig":
        """Return a copy with explicit resume CLI overrides applied.

        Args:
            controller_url: Optional submitter URL supplied to ``batch resume``.
            token: Optional submitter/device token supplied to ``batch resume``.
            concurrency: Optional concurrency override supplied to ``batch resume``.
            sandbox_create_concurrency: Optional sandbox create cap supplied to
                ``batch resume``.
            controller_submit_concurrency: Optional controller submit cap
                supplied to ``batch resume``.
            controller_status_concurrency: Optional controller status cap
                supplied to ``batch resume``.
            controller_submit_jitter_seconds: Optional controller submit jitter
                supplied to ``batch resume``.
            device_readiness_timeout_seconds: Optional device worker readiness
                timeout supplied to ``batch resume``.
            infra_retry_backoff_seconds: Optional retry backoff sequence for
                retryable infra failures.
            device_controller_url: Optional device URL supplied to
                ``batch resume``.
            telemetry_controller_db_path: Optional local controller DB supplied
                to ``batch resume`` for DB-first status reconciliation.

        Returns:
            Runtime snapshot where non-empty resume values override persisted
            values from the original run.
        """

        return self.model_copy(
            update={
                "controller_url": controller_url or self.controller_url,
                "device_controller_url": device_controller_url or self.device_controller_url,
                "token": token or self.token,
                "token_fingerprint": _token_fingerprint(token) or self.token_fingerprint,
                "concurrency": concurrency or self.concurrency,
                "sandbox_create_concurrency": sandbox_create_concurrency or self.sandbox_create_concurrency,
                "controller_submit_concurrency": (controller_submit_concurrency or self.controller_submit_concurrency),
                "controller_status_concurrency": (controller_status_concurrency or self.controller_status_concurrency),
                "controller_submit_jitter_seconds": (
                    controller_submit_jitter_seconds or self.controller_submit_jitter_seconds
                ),
                "device_readiness_timeout_seconds": (
                    device_readiness_timeout_seconds or self.device_readiness_timeout_seconds
                ),
                "infra_retry_backoff_seconds": (
                    list(infra_retry_backoff_seconds)
                    if infra_retry_backoff_seconds is not None
                    else list(self.infra_retry_backoff_seconds)
                ),
                "prepared_sandbox_policy": prepared_sandbox_policy or self.prepared_sandbox_policy,
                "persist_token": self.persist_token,
                "telemetry_experiment_id": self.telemetry_experiment_id,
                "telemetry_controller_db_path": telemetry_controller_db_path or self.telemetry_controller_db_path,
            }
        )


class BatchTaskState(BaseModel):
    """Persisted local state for one batch task.

    Attributes:
        task_id: Local batch task id.
        controller_task_id: Controller task id when submitted.
        trace_id: Controller trace id.
        session_id: Controller session id.
        sandbox_id: Provider sandbox id.
        device_id: Strict target device id.
        status: Local batch status.
        attempts: Number of attempts made so far.
        terminal_state: Controller terminal task state.
        error: Terminal error or last failure summary.
        recovery_action: Last recovery action selected by experiment resume.
        last_failure_category: Last normalized failure bucket.
        last_reconciled_at: UTC timestamp for the latest controller reconcile.
        last_reconciled_status: Controller task state observed during reconcile.
        cleanup_status: Cleanup/janitor outcome associated with the task.
        post_run_results: Benchmark/reward hook results collected before
            sandbox release. These do not alter control-plane task status.
        retry_budget_remaining: Whole-case retry budget still available.
        next_attempt_after: Unix timestamp before which retryable failed rows
            should not be claimed. Zero means immediately eligible.
        reconcile_unknown_count: Consecutive controller status reconciliation
            misses/errors while preserving a prior controller task id.
        terminal_decision: Shared agent terminal decision returned by the
            controller, when available.
    """

    task_id: str
    controller_task_id: str = ""
    trace_id: str = ""
    session_id: str = ""
    sandbox_id: str = ""
    device_id: str = ""
    status: BatchTaskStatus = BatchTaskStatus.PENDING
    attempts: int = 0
    terminal_state: TaskState | None = None
    error: str = ""
    recovery_action: FailureAction | None = None
    last_failure_category: FailureCategory | None = None
    last_reconciled_at: str = ""
    last_reconciled_status: str = ""
    cleanup_status: str = ""
    post_run_results: list[PostRunHookResult] = Field(default_factory=list)
    retry_budget_remaining: int = 0
    next_attempt_after: float = 0.0
    reconcile_unknown_count: int = 0
    terminal_decision: JsonObject = Field(default_factory=dict)
    agent_terminal: JsonObject = Field(default_factory=dict)
    evaluation_status: str = ""
    evaluation_error: JsonObject = Field(default_factory=dict)
    evaluation_attempts: int = 0
    evaluation_projection_level: str = ""


class BatchRunResult(BaseModel):
    """Summary returned by the batch runner.

    Attributes:
        batch_id: Batch id.
        total: Number of tasks in the manifest.
        succeeded: Number of tasks that finished successfully.
        failed: Number of tasks that failed or were interrupted.
        canceled: Number of tasks canceled locally.
        incomplete: Number of tasks left in non-terminal/non-success states.
        output_dir: Local output directory containing reports and artifacts.
        task_states: Final state for each task.
    """

    batch_id: str
    total: int
    succeeded: int
    failed: int
    canceled: int
    incomplete: int = 0
    output_dir: str
    task_states: list[BatchTaskState]
