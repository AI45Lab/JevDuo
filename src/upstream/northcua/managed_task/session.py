"""Sandbox-backed execution of exactly one NorthCUA Controller task."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar, TypedDict, cast

from pydantic import BaseModel, ConfigDict, Field

from northcua.controller.storage.file_artifacts import file_artifact_ref
from northcua.evaluation.mode import EXTERNAL_SCRIPT_EVALUATION_MODE
from northcua.evaluation.script import (
    ScriptEvaluatorResult,
    ScriptEvaluatorRunner,
    ScriptEvaluatorStatus,
)
from northcua.managed_task.contracts import (
    ControllerClientPort,
    ManagedArtifactFact,
    ManagedTaskSpec,
    ManagedTaskTerminal,
)
from northcua.sandboxes.contracts import SandboxFailureReason, SandboxLease, SandboxLeaseState
from northcua.sandboxes.lifecycle.task_lease import SandboxTaskLeaseManager
from northcua.sandboxes.provider.registry import SandboxProviderRegistry, default_sandbox_provider_registry
from northcua.submitter.client import ControllerSubmitterClient
from northcua_protocol.artifacts import ArtifactKind
from northcua_protocol.capabilities import Capability
from northcua_protocol.payloads import TaskAccepted, TaskCancel, TaskFinished, TaskStatusSnapshot
from northcua_protocol.state import TaskState

ControllerClientFactory = Callable[[str], ControllerClientPort]


class _ControllerSubmitPayload(TypedDict):
    agent_kind: str
    request: str
    target_device_id: str
    required_capabilities: set[Capability]
    metadata: dict[str, object]


class ManagedTaskCustodyRecord(BaseModel):
    """Secret-free durable custody needed to resume one managed task."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)
    schema_version: str = "northcua.managed-task-custody/v1"
    task_id: str = Field(min_length=1)
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lease: SandboxLease
    accepted: TaskAccepted | None = None
    released: bool = False
    lease_acquired_at: str = ""
    controller_accepted_at: str = ""
    release_started_at: str = ""
    release_finished_at: str = ""
    runtime_session_observation: ManagedArtifactFact | None = None
    provider_release_observation: ManagedArtifactFact | None = None


class ManagedTaskFactory:
    """Create cancellation-safe sessions without allocating a sandbox yet."""

    def __init__(
        self,
        *,
        provider_registry: SandboxProviderRegistry | None = None,
        controller_client_factory: ControllerClientFactory | None = None,
    ) -> None:
        self._providers = provider_registry or default_sandbox_provider_registry()
        self._clients = controller_client_factory or _default_controller_client

    async def open(self, spec: ManagedTaskSpec) -> "ManagedTaskSession":
        """Return an inert task session; ``submit`` owns the first side effect."""

        provider = self._providers.create(spec.sandbox.provider)
        return ManagedTaskSession(
            spec=spec,
            manager=SandboxTaskLeaseManager(provider),
            controller_client_factory=self._clients,
        )

    async def recover(self, spec: ManagedTaskSpec, *, task_id: str) -> "ManagedTaskSession":
        """Reconnect one task from its durable sandbox and Controller custody facts."""

        session = await self.open(spec)
        await session.recover(task_id=task_id)
        return session


class ManagedTaskSession:
    """Acquire, submit, recover, evaluate, collect, and release one task."""

    def __init__(
        self,
        *,
        spec: ManagedTaskSpec,
        manager: SandboxTaskLeaseManager,
        controller_client_factory: ControllerClientFactory,
    ) -> None:
        self._spec = spec
        self._manager = manager
        self._clients = controller_client_factory
        self._client: ControllerClientPort | None = None
        self._lease: SandboxLease | None = None
        self._accepted: TaskAccepted | None = None
        self._terminal: ManagedTaskTerminal | None = None
        self._task_id = ""
        self._released = False
        self._lease_acquired_at = ""
        self._controller_accepted_at = ""
        self._release_started_at = ""
        self._release_finished_at = ""
        self._runtime_session_observation: ManagedArtifactFact | None = None
        self._provider_release_observation: ManagedArtifactFact | None = None

    async def submit(self, *, task_id: str) -> TaskAccepted:
        """Acquire one sandbox and idempotently submit the caller's task id."""

        if self._accepted is not None:
            if task_id != self._accepted.task_id:
                raise ValueError("managed task session cannot change task_id")
            return self._accepted
        if self._task_id and self._task_id != task_id:
            raise ValueError("managed task session cannot change task_id")
        self._task_id = task_id
        if self._lease is None:
            self._lease = await self._manager.acquire(
                batch_id=self._spec.batch_id,
                task_id=task_id,
                spec=self._spec.sandbox,
                binding=self._spec.device,
                startup_commands=self._spec.startup_commands,
                readiness_timeout_seconds=self._spec.readiness_timeout_seconds,
                bootstrap_log_dir=self._task_output_dir / "bootstrap-logs",
                on_acquired=self._record_acquired_lease,
            )
        submit = self._submit_payload(self._lease.device_id)
        self._client = self._clients(self._spec.controller_url)
        await self._client.connect()
        try:
            self._accepted = await self._client.submit_task(
                **submit,
                token=self._spec.controller_token.get_secret_value(),
                task_id=task_id,
                trace_id=self._spec.trace_id,
            )
        except Exception as exc:
            self._accepted = await self._recover_duplicate_acceptance(exc, submit=submit)
        if self._accepted.task_id != task_id:
            raise ValueError("controller ignored the managed task id")
        if not self._controller_accepted_at:
            self._controller_accepted_at = _utc_now_iso()
        self._persist_custody()
        return self._accepted

    async def recover(self, *, task_id: str) -> None:
        """Restore an acquired lease and optional Controller acceptance after restart."""

        if self._task_id and self._task_id != task_id:
            raise ValueError("managed task session cannot change task_id")
        self._task_id = task_id
        custody = self._read_custody()
        if custody.spec_sha256 != self._spec_sha256():
            raise ValueError("managed task recovery specification differs from durable custody")
        self._lease_acquired_at = custody.lease_acquired_at
        self._controller_accepted_at = custody.controller_accepted_at
        self._release_started_at = custody.release_started_at
        self._release_finished_at = custody.release_finished_at
        self._runtime_session_observation = custody.runtime_session_observation
        self._provider_release_observation = custody.provider_release_observation
        if custody.released:
            self._lease = custody.lease
            self._accepted = custody.accepted
            self._released = True
            return
        self._lease = await self._manager.reconnect(custody.lease, spec=self._spec.sandbox)
        self._accepted = custody.accepted
        if self._accepted is None:
            statuses = await self._with_fresh_client(
                lambda client: client.task_statuses(
                    [task_id],
                    token=self._spec.controller_token.get_secret_value(),
                )
            )
            snapshot = next((item for item in statuses if item.task_id == task_id and item.session_id), None)
            if snapshot is not None:
                self._accepted = TaskAccepted(
                    task_id=snapshot.task_id,
                    trace_id=snapshot.trace_id,
                    session_id=snapshot.session_id,
                    device_id=snapshot.device_id,
                )
                if not self._controller_accepted_at:
                    self._controller_accepted_at = _utc_now_iso()
        self._persist_custody()

    @property
    def has_controller_acceptance(self) -> bool:
        """Whether durable custody proves Controller accepted this task."""

        return self._accepted is not None

    async def abandon_before_accept(self, *, reason: str) -> ManagedTaskTerminal:
        """Release a recovered sandbox when Controller never accepted its task."""

        if self._accepted is not None or self._lease is None:
            raise RuntimeError("pre-accept abandonment requires an unaccepted recovered sandbox")
        evaluation = ScriptEvaluatorResult(
            hook_id=self._spec.evaluator.hook_id,
            status=ScriptEvaluatorStatus.ERROR,
            error=reason,
            metadata={"training_disposition": "quarantine", "phase": "before_controller_acceptance"},
        )
        terminal_payload = self._write_fact(
            "controller-terminal",
            {"task_id": self._task_id, "state": "infra_failed", "reason": reason, "accepted": False},
        )
        execution = self._write_fact(
            "execution-fact",
            {"task_id": self._task_id, "state": "infra_failed", "reason": reason, "accepted": False},
        )
        evaluation_fact = self._write_fact("script-evaluation", evaluation.model_dump(mode="json"))
        cleanup = await self._release(failure=True)
        runtime_custody = self._runtime_custody_fact(cleanup)
        terminal = ManagedTaskTerminal(
            task_id=self._task_id,
            trace_id=self._spec.trace_id,
            session_id="pre-accept",
            state="failed",
            terminal_payload=terminal_payload,
            execution_fact=execution,
            script_evaluation_fact=evaluation_fact,
            cleanup_receipt=cleanup,
            runtime_custody_fact=runtime_custody,
            script_evaluation=evaluation,
            public_error=reason,
        )
        self._terminal = terminal
        return terminal

    async def _recover_duplicate_acceptance(
        self,
        exc: Exception,
        *,
        submit: _ControllerSubmitPayload,
    ) -> TaskAccepted:
        """Recover a lost acceptance only when the durable request is identical."""

        if "task_id already exists" not in str(exc) or self._client is None:
            raise exc
        snapshots = await self._client.task_statuses(
            [self._task_id],
            token=self._spec.controller_token.get_secret_value(),
        )
        snapshot = next((item for item in snapshots if item.task_id == self._task_id), None)
        if snapshot is None:
            raise RuntimeError("duplicate task id has no durable Controller snapshot") from exc
        expected = (
            self._spec.trace_id,
            submit["agent_kind"],
            submit["request"],
            submit["target_device_id"],
            submit["metadata"],
        )
        actual = (
            snapshot.trace_id,
            snapshot.agent_kind,
            snapshot.request,
            snapshot.device_id,
            snapshot.metadata,
        )
        if actual != expected or not snapshot.session_id:
            raise RuntimeError("duplicate task id belongs to a different managed request") from exc
        return TaskAccepted(
            task_id=snapshot.task_id,
            trace_id=snapshot.trace_id,
            session_id=snapshot.session_id,
            device_id=snapshot.device_id,
        )

    async def wait_terminal(self, *, timeout_seconds: float) -> ManagedTaskTerminal:
        """Wait, run the script evaluator while E2B is alive, then release it."""

        if self._terminal is not None:
            return self._terminal
        if self._accepted is None or self._lease is None:
            raise RuntimeError("managed task must be submitted before waiting")
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        finished = await self._wait_or_recover(deadline)
        evaluation = await ScriptEvaluatorRunner(output_dir=self._task_output_dir).run_one(
            hook=self._spec.evaluator,
            manager=self._manager,
            lease=self._lease,
            task_id=self._task_id,
            attempt=self._spec.attempt,
        )
        terminal_fact = self._write_fact("controller-terminal", finished.model_dump(mode="json"))
        execution_fact = self._write_fact(
            "execution-fact",
            {
                "task_id": finished.task_id,
                "trace_id": finished.trace_id,
                "session_id": finished.session_id,
                "state": finished.state.value,
                "device_id": finished.device_id,
                "lease_id": finished.lease_id,
                "result": finished.result,
                "error": finished.error,
                "terminal_decision": finished.terminal_decision,
                "artifact_refs": [artifact.model_dump(mode="json") for artifact in finished.artifacts],
            },
        )
        evaluation_fact = self._write_fact("script-evaluation", evaluation.model_dump(mode="json"))
        cleanup = await self._release(failure=finished.state is not TaskState.FINISHED)
        runtime_custody = self._runtime_custody_fact(cleanup)
        public_error = finished.error or evaluation.error or None
        if evaluation.status in {ScriptEvaluatorStatus.ERROR, ScriptEvaluatorStatus.TIMED_OUT} and not public_error:
            public_error = f"script evaluator {evaluation.status.value}"
        self._terminal = ManagedTaskTerminal(
            task_id=finished.task_id,
            trace_id=finished.trace_id,
            session_id=finished.session_id,
            state=finished.state.value,
            terminal_payload=terminal_fact,
            execution_fact=execution_fact,
            script_evaluation_fact=evaluation_fact,
            cleanup_receipt=cleanup,
            runtime_custody_fact=runtime_custody,
            script_evaluation=evaluation,
            artifact_refs=tuple(artifact.uri for artifact in finished.artifacts),
            public_error=public_error,
        )
        return self._terminal

    async def cancel(self, *, reason: str) -> None:
        """Request Controller cancellation using the durable task identity."""

        if not self._task_id:
            return
        accepted = self._accepted
        await self._with_fresh_client(
            lambda client: client.cancel_task(
                TaskCancel(
                    task_id=self._task_id,
                    trace_id=accepted.trace_id if accepted else self._spec.trace_id,
                    session_id=accepted.session_id if accepted else "",
                    reason=reason,
                )
            )
        )

    async def close(self) -> None:
        """Close transport and best-effort release any still-owned sandbox."""

        if self._client is not None:
            await self._client.close()
            self._client = None
        if self._lease is not None and not self._released:
            try:
                if self._accepted is not None:
                    await self.cancel(reason="managed task session closed before terminal cleanup")
            finally:
                await self._release(failure=True)
        await self._manager.aclose()

    async def _wait_or_recover(self, deadline: float) -> TaskFinished:
        if self._client is not None:
            try:
                return await self._client.wait_for_finished(
                    self._task_id,
                    timeout_seconds=_remaining(deadline),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                if _remaining(deadline) <= 0:
                    raise
        while True:
            statuses = await self._with_fresh_client(
                lambda client: client.task_statuses(
                    [self._task_id],
                    token=self._spec.controller_token.get_secret_value(),
                )
            )
            snapshot = next((item for item in statuses if item.task_id == self._task_id), None)
            if snapshot is not None and snapshot.state in _TERMINAL_STATES:
                return _snapshot_to_finished(snapshot)
            await asyncio.sleep(min(self._spec.recovery_poll_seconds, _remaining(deadline)))

    async def _with_fresh_client[T](self, operation: Callable[[ControllerClientPort], Awaitable[T]]) -> T:
        client = self._clients(self._spec.controller_url)
        await client.connect()
        try:
            return await operation(client)
        finally:
            await client.close()

    async def _release(self, *, failure: bool) -> ManagedArtifactFact:
        if self._lease is None:
            raise RuntimeError("managed task has no sandbox lease to release")
        if self._released:
            existing = self._task_output_dir / "facts" / "cleanup-receipt.json"
            return _fact_for_path(existing, artifact_root=self._spec.output_dir)
        self._release_started_at = _utc_now_iso()
        self._persist_custody()
        failure_reason = SandboxFailureReason.CONTROLLER_TASK_FAILED if failure else None
        released = await self._manager.release(
            self._lease,
            batch_id=self._spec.batch_id,
            task_id=self._task_id,
            strategy=self._spec.sandbox.allocation_strategy,
            teardown_commands=self._spec.teardown_commands,
            log_dir=self._task_output_dir / "sandbox-logs",
            failure_reason=failure_reason,
        )
        if released.state not in {SandboxLeaseState.RELEASED, SandboxLeaseState.FAILED}:
            raise RuntimeError("sandbox manager returned a non-terminal release state")
        self._lease = released
        self._released = True
        self._release_finished_at = _utc_now_iso()
        receipt = self._write_fact(
            "cleanup-receipt",
            {
                "task_id": self._task_id,
                "sandbox_id": released.handle.sandbox_id,
                "provider": released.handle.provider,
                "lease_id": released.lease_id,
                "state": released.state.value,
                "failure_reason": released.failure_reason.value if released.failure_reason else "",
            },
        )
        self._provider_release_observation = self._write_provider_release_observation()
        self._persist_custody()
        return receipt

    def _write_runtime_session_observation(self) -> ManagedArtifactFact:
        """Freeze the provider-returned sandbox identity before Controller execution."""

        trust = self._spec.runtime_trust
        if trust is None:
            return self._write_fact(
                "runtime-session-observation",
                {
                    "task_id": self._task_id,
                    "provider": self._lease.handle.provider if self._lease else "",
                    "sandbox_id": self._lease.handle.sandbox_id if self._lease else "",
                    "observed_at": self._lease_acquired_at,
                    "trust_bound": False,
                },
            )
        if self._lease is None:
            raise RuntimeError("runtime session observation requires an acquired lease")
        return self._write_fact(
            "runtime-session-observation",
            {
                "task_id": self._task_id,
                "provider_ref": trust.provider_ref,
                "provider_kind": trust.provider_kind,
                "provider_version": trust.provider_version,
                "provider_build_digest": trust.provider_build_digest,
                "descriptor_digest": trust.descriptor_digest,
                "conformance_profile_digest": trust.conformance_profile_digest,
                "conformance_report_digest": trust.conformance_report_digest,
                "runtime_spec_digest": trust.runtime_spec_digest,
                "resolved_runtime_ref": trust.resolved_runtime_ref,
                "resolved_runtime_digest": trust.resolved_runtime_digest,
                "sdk_name": trust.sdk_name,
                "sdk_version": trust.sdk_version,
                "sandbox_id": self._lease.handle.sandbox_id,
                "session_id": self._lease.handle.sandbox_id,
                "lease_id": self._lease.lease_id,
                "template": self._lease.handle.template,
                "observed_at": self._lease_acquired_at,
                "trust_bound": True,
            },
        )

    def _write_provider_release_observation(self) -> ManagedArtifactFact:
        """Freeze the provider release result only after termination returns."""

        if self._lease is None:
            raise RuntimeError("provider release observation requires a released lease")
        trust = self._spec.runtime_trust
        return self._write_fact(
            "provider-release-observation",
            {
                "task_id": self._task_id,
                "provider_ref": trust.provider_ref if trust else self._lease.handle.provider,
                "provider_kind": trust.provider_kind if trust else self._lease.handle.provider,
                "provider_version": trust.provider_version if trust else "unverified",
                "provider_build_digest": trust.provider_build_digest if trust else "",
                "sandbox_id": self._lease.handle.sandbox_id,
                "session_id": self._lease.handle.sandbox_id,
                "lease_id": self._lease.lease_id,
                "release_state": self._lease.state.value,
                "observed_at": self._release_finished_at,
                "trust_bound": trust is not None,
            },
        )

    def _runtime_custody_fact(self, cleanup: ManagedArtifactFact) -> ManagedArtifactFact:
        """Persist the public-safe lease timeline needed by external trust authorities."""

        if self._lease is None or not self._released:
            raise RuntimeError("runtime custody requires one released sandbox lease")
        required_times = (self._lease_acquired_at, self._release_started_at, self._release_finished_at)
        if any(not value for value in required_times):
            raise RuntimeError("runtime custody timeline is incomplete")
        if self._runtime_session_observation is None or self._provider_release_observation is None:
            raise RuntimeError("runtime custody observation chain is incomplete")
        return self._write_fact(
            "runtime-custody",
            {
                "task_id": self._task_id,
                "batch_id": self._spec.batch_id,
                "provider": self._lease.handle.provider,
                "template": self._lease.handle.template,
                "sandbox_id": self._lease.handle.sandbox_id,
                "lease_id": self._lease.lease_id,
                "device_id": self._lease.device_id,
                "lease_acquired_at": self._lease_acquired_at,
                "controller_accepted_at": self._controller_accepted_at,
                "release_started_at": self._release_started_at,
                "release_finished_at": self._release_finished_at,
                "release_state": self._lease.state.value,
                "cleanup_receipt_ref": cleanup.ref,
                "cleanup_receipt_sha256": cleanup.sha256,
                "runtime_session_observation_ref": self._runtime_session_observation.ref,
                "runtime_session_observation_sha256": self._runtime_session_observation.sha256,
                "provider_release_observation_ref": self._provider_release_observation.ref,
                "provider_release_observation_sha256": self._provider_release_observation.sha256,
            },
        )

    def _submit_payload(self, device_id: str) -> _ControllerSubmitPayload:
        spec = self._spec.desktop or self._spec.web
        if spec is None:  # pragma: no cover - validated by ManagedTaskSpec.
            raise RuntimeError("managed task has no submit spec")
        payload = dict(spec.submit_payload())
        metadata = dict(cast(dict[str, object], payload["metadata"]))
        metadata.update(
            {
                "evaluation_mode": EXTERNAL_SCRIPT_EVALUATION_MODE,
                "policy_route_ref": self._spec.policy_route_ref,
            }
        )
        return {
            "agent_kind": cast(str, payload["agent_kind"]),
            "request": cast(str, payload["request"]),
            "target_device_id": device_id,
            "required_capabilities": cast(set[Capability], payload["required_capabilities"]),
            "metadata": metadata,
        }

    @property
    def _task_output_dir(self) -> Path:
        return self._spec.output_dir / "managed-tasks" / (self._task_id or "pending")

    def _write_fact(self, name: str, payload: object) -> ManagedArtifactFact:
        path = self._task_output_dir / "facts" / f"{name}.json"
        _atomic_write_json(path, payload)
        return _fact_for_path(path, artifact_root=self._spec.output_dir)

    @property
    def _custody_path(self) -> Path:
        return self._task_output_dir / "custody.json"

    def _persist_custody(self) -> None:
        if self._lease is None or not self._task_id:
            raise RuntimeError("managed task custody requires one acquired lease")
        record = ManagedTaskCustodyRecord(
            task_id=self._task_id,
            spec_sha256=self._spec_sha256(),
            lease=self._lease,
            accepted=self._accepted,
            released=self._released,
            lease_acquired_at=self._lease_acquired_at,
            controller_accepted_at=self._controller_accepted_at,
            release_started_at=self._release_started_at,
            release_finished_at=self._release_finished_at,
            runtime_session_observation=self._runtime_session_observation,
            provider_release_observation=self._provider_release_observation,
        )
        _atomic_write_json(self._custody_path, record.model_dump(mode="json"))

    def _record_acquired_lease(self, lease: SandboxLease) -> None:
        """Persist custody synchronously at the provider-handle acquisition boundary."""

        self._lease = lease
        if not self._lease_acquired_at:
            self._lease_acquired_at = _utc_now_iso()
        self._runtime_session_observation = self._write_runtime_session_observation()
        self._persist_custody()

    def _read_custody(self) -> ManagedTaskCustodyRecord:
        try:
            record = ManagedTaskCustodyRecord.model_validate_json(self._custody_path.read_bytes())
        except FileNotFoundError as exc:
            raise LookupError(f"managed task custody does not exist: {self._task_id}") from exc
        if record.task_id != self._task_id:
            raise ValueError("managed task custody belongs to a different task")
        return record

    def _spec_sha256(self) -> str:
        payload = self._spec.model_dump(mode="json", exclude={"controller_token"})
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()


_TERMINAL_STATES = {TaskState.FINISHED, TaskState.FAILED, TaskState.INTERRUPTED}


def _default_controller_client(server_url: str) -> ControllerClientPort:
    return cast(ControllerClientPort, ControllerSubmitterClient(server_url))


def _snapshot_to_finished(snapshot: TaskStatusSnapshot) -> TaskFinished:
    return TaskFinished(
        task_id=snapshot.task_id,
        trace_id=snapshot.trace_id,
        session_id=snapshot.session_id,
        state=snapshot.state,
        device_id=snapshot.device_id,
        lease_id=snapshot.lease_id,
        result=snapshot.result,
        artifacts=snapshot.artifacts,
        error=snapshot.error or None,
        terminal_decision=snapshot.terminal_decision,
    )


def _remaining(deadline: float) -> float:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TimeoutError("managed task terminal wait timed out")
    return remaining


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _fact_for_path(path: Path, *, artifact_root: Path) -> ManagedArtifactFact:
    artifact = file_artifact_ref(
        artifact_id=path.stem,
        kind=ArtifactKind.JSON,
        path=path,
        mime_type="application/json",
        artifact_root=artifact_root,
    )
    return ManagedArtifactFact(ref=artifact.uri, sha256=artifact.sha256)


__all__ = ["ManagedTaskCustodyRecord", "ManagedTaskFactory", "ManagedTaskSession"]
