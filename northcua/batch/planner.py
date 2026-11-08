"""Translate batch tasks into controller submissions."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import cast

from northcua.batch.contracts import BatchManifest, BatchTaskSpec
from northcua.batch.request_adapters import BatchRequestAdapterRegistry, default_batch_request_adapter_registry
from northcua.submitter.specs import SubmitTaskPayload
from northcua_protocol.artifacts import ArtifactRef
from northcua_protocol.capabilities import Capability, JsonObject


@dataclass(frozen=True)
class PlannedTaskSubmission:
    """Controller submission payload derived from a batch task.

    Attributes:
        agent_kind: AgentKit agent kind sent to the controller.
        request: Canonical task text submitted to the agent runner.
        target_device_id: Strict sandbox device id selected for this task.
        required_capabilities: Capabilities required by controller scheduling.
        input_artifacts: Materialized artifacts attached to the task.
        metadata: JSON metadata used for tracing, resume, and reporting.
        controller_task_id: Deterministic controller task id for this attempt.
        trace_id: Deterministic trace id for this attempt.
    """

    agent_kind: str
    request: str
    target_device_id: str
    required_capabilities: set[Capability]
    input_artifacts: list[ArtifactRef]
    metadata: JsonObject
    controller_task_id: str
    trace_id: str


class BatchTaskPlanner:
    """Map manifest tasks to controller submitter payloads."""

    def __init__(self, *, request_adapters: BatchRequestAdapterRegistry | None = None) -> None:
        """Initialize the planner.

        Args:
            request_adapters: Optional adapter registry. Tests and future
                providers can inject custom agent projections without changing
                planner code.
        """

        self._request_adapters = request_adapters or default_batch_request_adapter_registry()

    def plan(
        self,
        manifest: BatchManifest,
        task: BatchTaskSpec,
        *,
        device_id: str,
        sandbox_id: str,
        attempt: int = 1,
    ) -> PlannedTaskSubmission:
        """Build one controller submission from a task and sandbox binding.

        Args:
            manifest: Batch manifest that owns the task.
            task: Task to translate.
            device_id: Strict target device id for the sandbox worker.
            sandbox_id: Provider sandbox id used for trace metadata.
            attempt: One-based task attempt number.

        Returns:
            Controller submission payload.
        """

        payload = self._task_payload(task)
        controller_task_id, trace_id = self._deterministic_ids(
            batch_id=manifest.batch_id,
            task_id=task.task_id,
            attempt=attempt,
        )
        required = set(payload["required_capabilities"])
        required.update(task.required_capabilities)
        metadata = dict(payload["metadata"])
        metadata.update(task.metadata)
        metadata.update(
            {
                "batch_id": manifest.batch_id,
                "batch_task_id": task.task_id,
                "sandbox_id": sandbox_id,
                "submitted_by": "northcua-batch",
                "batch_attempt": attempt,
            }
        )
        return PlannedTaskSubmission(
            agent_kind=str(payload["agent_kind"]),
            request=str(payload["request"]),
            target_device_id=device_id,
            required_capabilities=required,
            input_artifacts=list(payload.get("input_artifacts", [])) + task.input_artifacts,
            metadata=metadata,
            controller_task_id=controller_task_id,
            trace_id=trace_id,
        )

    @staticmethod
    def _deterministic_ids(*, batch_id: str, task_id: str, attempt: int) -> tuple[str, str]:
        salt = os.getenv("NORTHCUA_BATCH_ATTEMPT_ID_SALT", "").strip()
        seed_parts = [batch_id, task_id, str(attempt)]
        if salt:
            seed_parts.append(salt)
        seed = ":".join(seed_parts).encode("utf-8")
        digest = hashlib.sha256(seed).hexdigest()
        return f"batch-{digest[:32]}", f"trace-{digest[32:64]}"

    def _task_payload(self, task: BatchTaskSpec) -> SubmitTaskPayload:
        payload = self._request_adapters.project(task)
        if payload is not None:
            return payload
        metadata = dict(task.metadata)
        if task.max_steps:
            metadata.setdefault("max_steps", task.max_steps)
        if task.max_session_steps:
            metadata.setdefault("max_session_steps", task.max_session_steps)
        return cast(
            SubmitTaskPayload,
            {
                "agent_kind": task.agent_kind,
                "request": task.request,
                "target_device_id": "",
                "required_capabilities": set(task.required_capabilities),
                "input_artifacts": [],
                "metadata": metadata,
            },
        )
