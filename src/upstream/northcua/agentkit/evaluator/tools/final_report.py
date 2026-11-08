"""Final-report tool used to terminate model-driven evaluator loops."""

from __future__ import annotations

from dataclasses import dataclass

from nexau.archs.tool.formatters import ToolFormatterContext
from nexau.archs.tool.tool import Tool
from pydantic import BaseModel, ValidationError

from northcua.agentkernel.json_types import JsonObject
from northcua.agentkernel.tools import contract_to_nexau_tool
from northcua.core.enums import EvaluationToolCheckStatus
from northcua_runtime_shared.tool_contracts import ToolContract

from ..protocol import EvaluationEvidenceManifest, EvaluationToolCheck, RoundEvaluationReport
from ..tool_contracts import final_report_tool_contract


def _format_final_report_output(context: ToolFormatterContext) -> str:
    output = context.tool_output
    if isinstance(output, dict):
        return str(output.get("summary") or output.get("status") or output)
    return str(output)


@dataclass(slots=True)
class FinalReportState:
    """Mutable final-report state owned by one evaluator run.

    Attributes:
        report: Accepted report payload, when submitted.
        tool_check: Tool check describing the last final_report attempt.
    """

    report: BaseModel | None = None
    tool_check: EvaluationToolCheck | None = None

    @property
    def submitted(self) -> bool:
        """Return whether a final report was already accepted.

        Returns:
            True after one valid report is accepted.
        """

        return self.report is not None


class FinalReportToolFactory:
    """Build a schema-validating finalizer tool for one evaluator run."""

    def __init__(
        self,
        *,
        report_model: type[BaseModel] = RoundEvaluationReport,
        evidence_manifest: EvaluationEvidenceManifest,
        state: FinalReportState | None = None,
        contract: ToolContract | None = None,
    ) -> None:
        self.report_model = report_model
        self.evidence_manifest = evidence_manifest
        self.state = state or FinalReportState()
        self._contract = contract
        if self._contract is not None and self._contract.parameters_schema != report_model.model_json_schema():
            raise ValueError("final_report contract schema drifts from report_model")

    def build_tool(self) -> Tool:
        """Return the model-visible final report tool.

        Returns:
            NexAU tool that validates and stores one final report.
        """

        contract = self.tool_contract()
        tool = contract_to_nexau_tool(
            contract,
            implementation=self._submit_final_report,
            formatter=_format_final_report_output,
            disable_parallel=True,
            on_validation_error=self._record_validation_failure,
        )
        self._attach_tool_validation_state_hook(tool)
        return tool

    def tool_contract(self) -> ToolContract:
        """Return the canonical finalizer contract for this report model."""

        return self._contract or final_report_tool_contract(self.report_model)

    def _submit_final_report(self, **payload: object) -> JsonObject:
        """Validate and store one final report payload.

        Args:
            **payload: Model-supplied final report fields.

        Returns:
            Tool result envelope with success or validation failure details.
        """

        if self.state.submitted:
            self.state.tool_check = EvaluationToolCheck(
                name="final_report",
                status=EvaluationToolCheckStatus.FAILURE,
                summary="Duplicate final_report submission rejected.",
            )
            return {"status": "failure", "summary": "final_report was already submitted"}
        try:
            report = self.report_model.model_validate(payload)
        except ValidationError as exc:
            self.state.tool_check = EvaluationToolCheck(
                name="final_report",
                status=EvaluationToolCheckStatus.FAILURE,
                summary=str(exc),
            )
            return {"status": "failure", "summary": str(exc)}
        invalid_refs = self._invalid_evidence_refs(report)
        if invalid_refs:
            summary = f"Invalid evidence refs: {', '.join(sorted(invalid_refs))}"
            self.state.tool_check = EvaluationToolCheck(
                name="final_report",
                status=EvaluationToolCheckStatus.FAILURE,
                summary=summary,
            )
            return {"status": "failure", "summary": summary}
        self.state.report = report
        self.state.tool_check = EvaluationToolCheck(
            name="final_report",
            status=EvaluationToolCheckStatus.SUCCESS,
            summary="Final evaluator report accepted.",
        )
        return {"status": "success", "summary": "Final evaluator report accepted."}

    def _record_validation_failure(self, exc: Exception) -> None:
        """Record contract-level validation failures in finalizer state."""

        self.state.tool_check = EvaluationToolCheck(
            name="final_report",
            status=EvaluationToolCheckStatus.FAILURE,
            summary=str(exc),
        )

    def _attach_tool_validation_state_hook(self, tool: Tool) -> None:
        validate_params = tool.validate_params

        def _validate_params(params: dict[str, object]) -> None:
            try:
                validate_params(params)
            except Exception as exc:
                self._record_validation_failure(exc)
                raise

        tool.validate_params = _validate_params  # type: ignore[method-assign]

    def _invalid_evidence_refs(self, report: BaseModel) -> set[str]:
        valid_refs = self.evidence_manifest.ref_ids
        invalid: set[str] = set()
        for evidence_ref in getattr(report, "first_divergence_evidence_refs", []) or []:
            if evidence_ref not in valid_refs:
                invalid.add(str(evidence_ref))
        for field_name in ("criteria", "diagnoses", "side_effects"):
            for item in getattr(report, field_name, []) or []:
                for evidence_ref in getattr(item, "evidence_refs", []) or []:
                    if evidence_ref not in valid_refs:
                        invalid.add(str(evidence_ref))
        for field_name in ("safety", "efficiency"):
            item = getattr(report, field_name, None)
            if item is None:
                continue
            for evidence_ref in getattr(item, "evidence_refs", []) or []:
                if evidence_ref not in valid_refs:
                    invalid.add(str(evidence_ref))
            for finding in getattr(item, "findings", []) or []:
                for evidence_ref in getattr(finding, "evidence_refs", []) or []:
                    if evidence_ref not in valid_refs:
                        invalid.add(str(evidence_ref))
        return invalid


__all__ = ["FinalReportState", "FinalReportToolFactory"]
