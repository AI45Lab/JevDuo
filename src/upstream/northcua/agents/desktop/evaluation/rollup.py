"""Aggregate round evaluation records into a session-level summary."""

from __future__ import annotations

from statistics import fmean

from northcua.core.enums import EvaluationVerdict

from .protocol import RoundEvaluationRecord, SessionEvaluationRollup


class SessionEvaluationRollupService:
    """Aggregate round-level records into a compact session summary."""

    def build(self, records: list[RoundEvaluationRecord]) -> SessionEvaluationRollup:
        """Build the session rollup for persisted round records.

        Args:
            records: Round evaluation records in persisted order.

        Returns:
            Session-level aggregate counters and completion summary.
        """

        if not records:
            return SessionEvaluationRollup(session_id="", summary="No round evaluations were recorded.")
        success_count = sum(1 for record in records if record.overall_verdict == EvaluationVerdict.SUCCESS)
        partial_count = sum(1 for record in records if record.overall_verdict == EvaluationVerdict.PARTIAL)
        failure_count = sum(1 for record in records if record.overall_verdict == EvaluationVerdict.FAILURE)
        uncertain_count = sum(1 for record in records if record.overall_verdict == EvaluationVerdict.UNCERTAIN)
        average_completion = fmean(record.completion_rating for record in records)
        return SessionEvaluationRollup(
            session_id=records[0].session_id,
            round_count=len(records),
            success_count=success_count,
            partial_count=partial_count,
            failure_count=failure_count,
            uncertain_count=uncertain_count,
            average_completion_rating=round(average_completion, 4),
            summary=(
                f"Session completed {len(records)} evaluated rounds: "
                f"{success_count} success, {partial_count} partial, "
                f"{failure_count} failure, {uncertain_count} uncertain."
            ),
            rounds=records,
        )
