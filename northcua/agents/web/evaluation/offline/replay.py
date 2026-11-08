"""Offline Web evaluation bundle loading from persisted run artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from northcua.agentkit.evaluator.evidence import EvidenceSelector
from northcua.agentkit.evaluator.protocol import EvaluationEvidenceManifest, RoundEvaluationBundle
from northcua.agentkit.evaluator.spec import EvidencePolicy
from northcua.core.json_types import JsonObject

from ...domain.results import WebAgentResult
from ..adapters import WebRoundEvaluationBundleAdapter
from ..domain import default_web_evaluation_criteria


@dataclass(slots=True)
class WebOfflineEvaluationBundle:
    """Artifact-only evaluator input loaded from a Web agent artifact directory."""

    artifact_root: Path
    result: WebAgentResult
    bundle: RoundEvaluationBundle
    evidence_manifest: EvaluationEvidenceManifest
    step_trace_by_session_step: dict[int, JsonObject]


def load_web_offline_evaluation_bundle(
    artifact_root: Path,
    *,
    round_num: int = 1,
    evidence_policy: EvidencePolicy | None = None,
) -> WebOfflineEvaluationBundle:
    """Load a persisted Web run and build an evaluator-ready bundle."""

    result_path = artifact_root / "rounds" / f"round_{round_num:04d}" / "result.json"
    if not result_path.is_file():
        result_path = artifact_root / "result.json"
    trace_path = artifact_root / "web_agent_trace.jsonl"
    result = WebAgentResult.model_validate_json(result_path.read_text(encoding="utf-8"))
    if not any(step.round_num == round_num for step in result.steps):
        raise ValueError(f"No persisted steps for Web round {round_num}")
    bundle = WebRoundEvaluationBundleAdapter(result=result, round_num=round_num).build_bundle()
    manifest = EvidenceSelector(evidence_policy or EvidencePolicy()).select(
        bundle=bundle,
        criteria=default_web_evaluation_criteria(),
    )
    return WebOfflineEvaluationBundle(
        artifact_root=artifact_root,
        result=result,
        bundle=bundle,
        evidence_manifest=manifest,
        step_trace_by_session_step=_load_step_trace(trace_path),
    )


def _load_step_trace(trace_path: Path) -> dict[int, JsonObject]:
    if not trace_path.exists():
        return {}
    traces: dict[int, JsonObject] = {}
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            continue
        session_step = payload.get("session_step")
        if isinstance(session_step, int):
            traces[session_step] = payload
    return traces
