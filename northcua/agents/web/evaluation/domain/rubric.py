"""Default Web evaluation criteria."""

from __future__ import annotations

from northcua.agentkit.evaluator.protocol import EvaluationCriterion


def default_web_evaluation_criteria() -> list[EvaluationCriterion]:
    """Return the baseline Web round rubric used when no generated rubric exists."""

    return [
        EvaluationCriterion(
            id="task_completion",
            name="Task Completion",
            description="Verify the requested end state using terminal URL/title, DOM, and screenshot evidence.",
            weight=0.22,
        ),
        EvaluationCriterion(
            id="navigation_correctness",
            name="Navigation Correctness",
            description="Check whether the agent navigated to the intended site, tab, route, and final URL.",
            weight=0.13,
        ),
        EvaluationCriterion(
            id="dom_page_state_correctness",
            name="DOM/Page State Correctness",
            description="Inspect DOM snapshots for visible text, form state, selected options, checked state, and redirects.",
            weight=0.15,
        ),
        EvaluationCriterion(
            id="visual_som_grounding",
            name="Visual/SoM Grounding",
            description="Use screenshots and SoM annotations to verify visible state and action targets.",
            weight=0.12,
        ),
        EvaluationCriterion(
            id="tool_action_correctness",
            name="Tool/Action Correctness",
            description="Check whether web_* actions used refs, points, tabs, waits, and text input correctly.",
            weight=0.13,
        ),
        EvaluationCriterion(
            id="final_verification_quality",
            name="Final Verification Quality",
            description=(
                "Verify assistant final-answer claims against terminal and earlier page evidence; "
                "do not require answer text to be rendered in the browser DOM."
            ),
            weight=0.1,
        ),
        EvaluationCriterion(
            id="safety_side_effects",
            name="Safety/Side Effects",
            description="Identify unsolicited navigation, submission, destructive actions, or privacy-sensitive side effects.",
            weight=0.08,
        ),
        EvaluationCriterion(
            id="efficiency_recovery",
            name="Efficiency/Recovery",
            description="Assess whether the agent avoided repeated ineffective actions and recovered from stale refs or page blockers.",
            weight=0.07,
        ),
    ]
