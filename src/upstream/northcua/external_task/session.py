"""Execute one Controller task against an externally owned runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

from pydantic import SecretStr

from northcua.controller.storage.file_artifacts import file_artifact_ref
from northcua.external_task.contracts import (
    ExternalTaskFact,
    ExternalTaskIntent,
    ExternalTaskObservation,
    ExternalTaskObservationState,
    ExternalTaskRecoverySpec,
    ExternalTaskSpec,
    ExternalTaskTerminal,
)
from northcua.submitter.client import ControllerSubmitterClient
from northcua.submitter.ports import ControllerClientPort
from northcua.submitter.single_task import (
    acceptance_from_exact_snapshot,
    external_script_submit_payload,
    snapshot_to_finished,
    task_request_for_payload,
)
from northcua_protocol.artifacts import ArtifactKind
from northcua_protocol.capabilities import Capability
from northcua_protocol.payloads import (
    TaskAccepted,
    TaskCancel,
    TaskFinished,
    TaskRequest,
    TaskStatusSnapshot,
    task_request_fingerprint_sha256,
)
from northcua_protocol.state import TaskState

ControllerClientFactory = Callable[[str], ControllerClientPort]
_TERMINAL_STATES = {TaskState.FINISHED, TaskState.FAILED, TaskState.INTERRUPTED}


class ExternalTaskFactory:
    """Create inert external task sessions without touching a runtime."""

    def __init__(self, *, controller_client_factory: ControllerClientFactory | None = None) -> None:
        self._clients = controller_client_factory or _default_controller_client

    async def open(self, spec: ExternalTaskSpec) -> "ExternalTaskSession":
        """Return a side-effect-free session."""

        return ExternalTaskSession(spec=spec, controller_client_factory=self._clients)

    async def recover(self, spec: ExternalTaskSpec, *, task_id: str) -> "ExternalTaskSession":
        """Restore durable submit/acceptance identity without runtime ownership."""

        session = await self.open(spec)
        session.recover(task_id=task_id)
        return session

    async def recover_from_intent(
        self,
        *,
        output_dir: Path,
        task_id: str,
        controller_token: SecretStr,
    ) -> "ExternalTaskSession":
        """Restore a session from secret-free durable intent without submitting it."""

        intent_path = output_dir.expanduser().resolve() / "external-tasks" / task_id / "intent.json"
        intent = ExternalTaskIntent.model_validate_json(intent_path.read_bytes())
        if intent.task_id != task_id or intent.recovery_spec.output_dir.expanduser().resolve() != output_dir.resolve():
            raise ValueError("external task recovery intent differs from the requested task scope")
        return await self.recover(intent.recovery_spec.restore(controller_token), task_id=task_id)


class ExternalTaskSession:
    """Submit, recover, wait, collect and cancel one external-runtime task."""

    def __init__(self, *, spec: ExternalTaskSpec, controller_client_factory: ControllerClientFactory) -> None:
        self._spec = spec
        self._clients = controller_client_factory
        self._client: ControllerClientPort | None = None
        self._task_id = ""
        self._request: TaskRequest | None = None
        self._accepted: TaskAccepted | None = None
        self._finished: TaskFinished | None = None
        self._terminal: ExternalTaskTerminal | None = None

    async def submit(self, *, task_id: str) -> TaskAccepted:
        """Persist intent, then idempotently submit the exact caller task id."""

        if self._accepted is not None:
            if self._accepted.task_id != task_id:
                raise ValueError("external task session cannot change task_id")
            return self._accepted
        if self._task_id and self._task_id != task_id:
            raise ValueError("external task session cannot change task_id")
        self._task_id = task_id
        payload = self._submit_payload()
        self._request = task_request_for_payload(
            payload,
            task_id=task_id,
            trace_id=self._spec.trace_id,
            token=self._spec.controller_token.get_secret_value(),
        )
        self._persist_intent(self._request)
        self._client = self._clients(self._spec.controller_url)
        await self._client.connect()
        try:
            accepted = await self._client.submit_task(
                agent_kind=cast(str, payload["agent_kind"]),
                request=cast(str, payload["request"]),
                token=self._spec.controller_token.get_secret_value(),
                target_device_id=cast(str, payload["target_device_id"]),
                required_capabilities=cast(set[Capability], payload["required_capabilities"]),
                metadata=cast(dict[str, object], payload["metadata"]),
                task_id=task_id,
                trace_id=self._spec.trace_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            accepted = await self._recover_acceptance(exc)
        if (
            accepted.task_id != task_id
            or accepted.trace_id != self._spec.trace_id
            or accepted.device_id not in {"", self._spec.device.device_id}
            or not accepted.session_id
        ):
            raise RuntimeError("controller acceptance differs from external task identity")
        self._accepted = accepted
        self._persist_intent(self._request, accepted=accepted)
        return accepted

    def recover(self, *, task_id: str) -> None:
        """Load a durable intent and reject spec or request drift."""

        if self._task_id and self._task_id != task_id:
            raise ValueError("external task session cannot change task_id")
        self._task_id = task_id
        intent = ExternalTaskIntent.model_validate_json(self._intent_path.read_text(encoding="utf-8"))
        request = task_request_for_payload(
            self._submit_payload(),
            task_id=task_id,
            trace_id=self._spec.trace_id,
            token=self._spec.controller_token.get_secret_value(),
        )
        if (
            intent.recovery_spec != ExternalTaskRecoverySpec.from_task_spec(self._spec)
            or intent.spec_sha256 != self._spec_sha256()
            or intent.request_fingerprint_sha256 != task_request_fingerprint_sha256(request)
        ):
            raise ValueError("external task recovery specification differs from durable intent")
        self._request = request
        self._accepted = intent.accepted
        self._terminal = self._read_terminal_if_present()

    async def observe(self) -> ExternalTaskObservation:
        """Observe a durable task intent without submitting or cancelling it."""

        if not self._task_id or self._request is None:
            raise RuntimeError("external task must be recovered before observation")
        if self._terminal is not None:
            return ExternalTaskObservation(
                task_id=self._task_id,
                state=ExternalTaskObservationState.TERMINAL,
                controller_state=self._terminal.state,
                terminal=self._terminal,
            )
        try:
            statuses = await self._with_fresh_client(
                lambda client: client.task_statuses(
                    [self._task_id],
                    token=self._spec.controller_token.get_secret_value(),
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return ExternalTaskObservation(task_id=self._task_id, state=ExternalTaskObservationState.UNKNOWN)
        snapshot = next((item for item in statuses if item.task_id == self._task_id), None)
        if snapshot is None:
            return ExternalTaskObservation(task_id=self._task_id, state=ExternalTaskObservationState.ABSENT)
        self._record_recovered_acceptance(snapshot)
        if snapshot.state in _TERMINAL_STATES:
            terminal = self._collect_finished(snapshot_to_finished(snapshot))
            return ExternalTaskObservation(
                task_id=self._task_id,
                state=ExternalTaskObservationState.TERMINAL,
                controller_state=snapshot.state.value,
                terminal=terminal,
            )
        return ExternalTaskObservation(
            task_id=self._task_id,
            state=ExternalTaskObservationState.ACTIVE,
            controller_state=snapshot.state.value,
        )

    async def wait_terminal(self, *, timeout_seconds: float) -> ExternalTaskTerminal:
        """Wait for Controller terminal state and durably freeze it before return."""

        if self._terminal is not None:
            return self._terminal
        if not self._task_id or self._request is None:
            raise RuntimeError("external task must be recovered before waiting")
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        if self._client is not None and self._accepted is not None:
            try:
                self._finished = await self._client.wait_for_finished(
                    self._task_id,
                    timeout_seconds=_remaining(deadline),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                self._finished = None
        while self._finished is None:
            if _remaining(deadline) <= 0:
                raise TimeoutError(f"external task {self._task_id!r} did not reach terminal state")
            statuses = await self._with_fresh_client(
                lambda client: client.task_statuses(
                    [self._task_id],
                    token=self._spec.controller_token.get_secret_value(),
                )
            )
            snapshot = next((item for item in statuses if item.task_id == self._task_id), None)
            if snapshot is not None:
                self._record_recovered_acceptance(snapshot)
                if snapshot.state in _TERMINAL_STATES:
                    self._finished = snapshot_to_finished(snapshot)
                    break
            await asyncio.sleep(min(self._spec.recovery_poll_seconds, _remaining(deadline)))
        return self._collect_finished(self._finished)

    async def collect(self) -> ExternalTaskTerminal:
        """Idempotently collect a terminal snapshot after restart or transport loss."""

        if self._terminal is not None:
            return self._terminal
        if self._finished is not None:
            return self._collect_finished(self._finished)
        if not self._task_id or self._request is None:
            raise RuntimeError("external task has no durable request to collect")
        statuses = await self._with_fresh_client(
            lambda client: client.task_statuses(
                [self._task_id],
                token=self._spec.controller_token.get_secret_value(),
            )
        )
        snapshot = next((item for item in statuses if item.task_id == self._task_id), None)
        if snapshot is None or snapshot.state not in _TERMINAL_STATES:
            raise RuntimeError("external task is not terminal")
        acceptance_from_exact_snapshot(snapshot, self._request)
        return self._collect_finished(snapshot_to_finished(snapshot))

    async def cancel(self, *, reason: str) -> None:
        """Explicitly request Controller cancellation; never release the runtime."""

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

    async def close_transport(self) -> None:
        """Close only the websocket transport; do not cancel or release anything."""

        if self._client is not None:
            await self._client.close()
            self._client = None

    async def close(self) -> None:
        """Compatibility spelling for transport-only close."""

        await self.close_transport()

    async def _recover_acceptance(self, exc: Exception) -> TaskAccepted:
        if self._request is None:
            raise exc
        try:
            statuses = await self._with_fresh_client(
                lambda client: client.task_statuses(
                    [self._task_id],
                    token=self._spec.controller_token.get_secret_value(),
                )
            )
        except Exception:
            raise exc
        snapshot = next((item for item in statuses if item.task_id == self._task_id), None)
        if snapshot is None:
            raise exc
        try:
            return acceptance_from_exact_snapshot(snapshot, self._request)
        except Exception as mismatch:
            raise RuntimeError("controller duplicate task identity could not be reconciled") from mismatch

    async def _with_fresh_client[T](self, operation: Callable[[ControllerClientPort], Awaitable[T]]) -> T:
        client = self._clients(self._spec.controller_url)
        await client.connect()
        try:
            return await operation(client)
        finally:
            await client.close()

    def _record_recovered_acceptance(self, snapshot: TaskStatusSnapshot) -> None:
        """Durably repair the accept-response crash window from an exact Controller snapshot."""

        if self._request is None:
            raise RuntimeError("external task request identity is unavailable")
        accepted = acceptance_from_exact_snapshot(snapshot, self._request)
        if self._accepted is not None:
            existing = self._accepted
            if (existing.task_id, existing.trace_id, existing.session_id) != (
                accepted.task_id,
                accepted.trace_id,
                accepted.session_id,
            ) or (existing.device_id and accepted.device_id and existing.device_id != accepted.device_id):
                raise RuntimeError("controller acceptance conflicts with durable external task intent")
            return
        self._accepted = accepted
        self._persist_intent(self._request, accepted=accepted)

    def _submit_payload(self) -> dict[str, object]:
        submit = self._spec.desktop or self._spec.web
        if submit is None:  # pragma: no cover - validated by ExternalTaskSpec.
            raise RuntimeError("external task has no submit spec")
        return external_script_submit_payload(
            submit,
            device_id=self._spec.device.device_id,
            policy_route_ref=self._spec.policy_route_ref,
            metadata={
                "runtime_session_ref": self._spec.device.runtime_session_ref,
                "bootstrap_operation_id": self._spec.device.bootstrap_operation_id,
                "bootstrap_plan_sha256": self._spec.device.bootstrap_plan_sha256,
                "source_projection_sha256": self._spec.source_projection_sha256,
            },
        )

    def _persist_intent(self, request: TaskRequest, *, accepted: TaskAccepted | None = None) -> None:
        current = self._read_intent_if_present()
        intent = ExternalTaskIntent(
            task_id=self._task_id,
            recovery_spec=ExternalTaskRecoverySpec.from_task_spec(self._spec),
            spec_sha256=self._spec_sha256(),
            request_fingerprint_sha256=task_request_fingerprint_sha256(request),
            accepted=accepted or (current.accepted if current else None),
        )
        if current is not None and (
            current.task_id,
            current.recovery_spec,
            current.spec_sha256,
            current.request_fingerprint_sha256,
        ) != (
            intent.task_id,
            intent.recovery_spec,
            intent.spec_sha256,
            intent.request_fingerprint_sha256,
        ):
            raise ValueError("external task intent conflicts with the durable task identity")
        _atomic_write_json(self._intent_path, intent.model_dump(mode="json"))

    def _collect_finished(self, finished: TaskFinished) -> ExternalTaskTerminal:
        expected = (self._task_id, self._spec.trace_id, self._spec.device.device_id)
        actual = (finished.task_id, finished.trace_id, finished.device_id)
        if actual != expected or not finished.session_id:
            raise RuntimeError("controller terminal differs from external task identity")
        if self._request is None:
            raise RuntimeError("external task request identity is unavailable")
        intent_fact = self._fact_for_path("submit-intent", self._intent_path)
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
        self._finished = finished
        self._terminal = ExternalTaskTerminal(
            task_id=finished.task_id,
            trace_id=finished.trace_id,
            session_id=finished.session_id,
            state=finished.state.value,
            spec_sha256=self._spec_sha256(),
            request_fingerprint_sha256=task_request_fingerprint_sha256(self._request),
            intent_fact=intent_fact,
            controller_terminal_fact=terminal_fact,
            execution_fact=execution_fact,
            artifact_refs=tuple(artifact.uri for artifact in finished.artifacts),
            public_error=finished.error,
            terminal_decision=dict(finished.terminal_decision),
        )
        _atomic_write_json(self._terminal_index_path, self._terminal.model_dump(mode="json"))
        return self._terminal

    def _write_fact(self, name: str, payload: object) -> ExternalTaskFact:
        path = self._task_output_dir / "facts" / f"{name}.json"
        _atomic_write_json(path, payload)
        return self._fact_for_path(name, path)

    def _fact_for_path(self, name: str, path: Path) -> ExternalTaskFact:
        artifact = file_artifact_ref(
            artifact_id=name,
            kind=ArtifactKind.JSON,
            path=path,
            mime_type="application/json",
            artifact_root=self._spec.output_dir,
        )
        return ExternalTaskFact(ref=artifact.uri, sha256=artifact.sha256)

    def _read_intent_if_present(self) -> ExternalTaskIntent | None:
        if not self._intent_path.exists():
            return None
        return ExternalTaskIntent.model_validate_json(self._intent_path.read_text(encoding="utf-8"))

    def _read_terminal_if_present(self) -> ExternalTaskTerminal | None:
        if not self._terminal_index_path.exists():
            return None
        terminal = ExternalTaskTerminal.model_validate_json(self._terminal_index_path.read_bytes())
        if self._request is None:
            raise ValueError("external task request identity is unavailable")
        expected_identity = (
            self._task_id,
            self._spec.trace_id,
            self._spec_sha256(),
            task_request_fingerprint_sha256(self._request),
        )
        actual_identity = (
            terminal.task_id,
            terminal.trace_id,
            terminal.spec_sha256,
            terminal.request_fingerprint_sha256,
        )
        if actual_identity != expected_identity:
            raise ValueError("external task terminal differs from durable intent")
        expected_facts = (
            self._fact_for_path("submit-intent", self._intent_path),
            self._fact_for_path("controller-terminal", self._task_output_dir / "facts" / "controller-terminal.json"),
            self._fact_for_path("execution-fact", self._task_output_dir / "facts" / "execution-fact.json"),
        )
        if (
            terminal.intent_fact,
            terminal.controller_terminal_fact,
            terminal.execution_fact,
        ) != expected_facts:
            raise ValueError("external task terminal fact integrity mismatch")
        controller = TaskFinished.model_validate_json(
            (self._task_output_dir / "facts" / "controller-terminal.json").read_bytes()
        )
        execution_raw = json.loads((self._task_output_dir / "facts" / "execution-fact.json").read_bytes())
        if not isinstance(execution_raw, dict):
            raise ValueError("external task execution fact is invalid")
        summary = (terminal.task_id, terminal.trace_id, terminal.session_id, terminal.state)
        if summary != (controller.task_id, controller.trace_id, controller.session_id, controller.state.value):
            raise ValueError("external task terminal conflicts with controller fact")
        if summary != (
            execution_raw.get("task_id"),
            execution_raw.get("trace_id"),
            execution_raw.get("session_id"),
            execution_raw.get("state"),
        ):
            raise ValueError("external task terminal conflicts with execution fact")
        expected_artifact_refs = tuple(artifact.uri for artifact in controller.artifacts)
        if terminal.artifact_refs != expected_artifact_refs or terminal.public_error != controller.error:
            raise ValueError("external task terminal payload conflicts with controller fact")
        controller_decision = dict(controller.terminal_decision)
        if (
            terminal.terminal_decision != controller_decision
            or execution_raw.get("terminal_decision") != controller_decision
        ):
            raise ValueError("external task terminal decision conflicts with canonical facts")
        if terminal.trace_refs:
            raise ValueError("external task terminal contains unbound trace references")
        return terminal

    def _spec_sha256(self) -> str:
        public = ExternalTaskRecoverySpec.from_task_spec(self._spec).model_dump(mode="json")
        for surface in ("desktop", "web"):
            submit = public.get(surface)
            if isinstance(submit, dict) and isinstance(submit.get("required_capabilities"), list):
                submit["required_capabilities"] = sorted(submit["required_capabilities"])
        encoded = json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def _task_output_dir(self) -> Path:
        return self._spec.output_dir / "external-tasks" / (self._task_id or "pending")

    @property
    def _intent_path(self) -> Path:
        return self._task_output_dir / "intent.json"

    @property
    def _terminal_index_path(self) -> Path:
        return self._task_output_dir / "terminal.json"


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
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


def _remaining(deadline: float) -> float:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        return 0.0
    return remaining


def _default_controller_client(url: str) -> ControllerClientPort:
    return ControllerSubmitterClient(url)


__all__ = ["ExternalTaskFactory", "ExternalTaskSession"]
