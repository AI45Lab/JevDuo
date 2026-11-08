"""Enumerations shared across NorthCUA modules."""

from __future__ import annotations

from enum import Enum


class TaskStatus(str, Enum):
    """High-level task lifecycle status.

    Attributes:
        CONTINUE: Continue execution.
        FINISH: Task finished successfully.
        FAIL: Task failed.
        ERROR: Runtime error occurred.
    """

    CONTINUE = "continue"
    FINISH = "finish"
    FAIL = "fail"
    ERROR = "error"

    @classmethod
    def _missing_(cls, value: object) -> "TaskStatus | None":
        """Accept pre-migration uppercase serialized values when reading old artifacts."""

        if isinstance(value, str):
            normalized = value.lower()
            for member in cls:
                if member.value == normalized:
                    return member
        return None


class RuntimeMode(str, Enum):
    """Execution mode for websocket/runtime dispatch.

    Attributes:
        LOCAL: Execute in the local process.
        REMOTE: Dispatch through websocket transport.
    """

    LOCAL = "local"
    REMOTE = "remote"


class WebSocketMessageType(str, Enum):
    """NorthCUA websocket message categories.

    Attributes:
        TASK_REQUEST: Client task request.
        TASK_ACCEPTED: Server task acknowledgement.
        COMMAND_REQUEST: Server command request.
        COMMAND_RESULT: Client command result.
        TASK_FINISHED: Server task completion.
        HEARTBEAT: Bidirectional heartbeat.
        ERROR: Transport error.
    """

    TASK_REQUEST = "task_request"
    TASK_ACCEPTED = "task_accepted"
    COMMAND_REQUEST = "command_request"
    COMMAND_RESULT = "command_result"
    TASK_FINISHED = "task_finished"
    HEARTBEAT = "heartbeat"
    ERROR = "error"


class ResultStatus(str, Enum):
    """Command/result execution status.

    Attributes:
        SUCCESS: Command succeeded.
        FAILURE: Command failed.
        SKIPPED: Command was skipped.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    SKIPPED = "skipped"


class ScreenshotSelectionKind(str, Enum):
    """Which image variant is used for prompt replay.

    Attributes:
        NONE: No image selected.
        RAW: Raw screenshot selected.
        OVERLAY: Overlay screenshot selected.
    """

    NONE = "none"
    RAW = "raw"
    OVERLAY = "overlay"


class CoordinateSpace(str, Enum):
    """Model-facing coordinate spaces supported by NorthCUA runtimes.

    Attributes:
        IMAGE_PIXELS: Coordinates in captured image pixels.
        NORMALIZED_UNIT: Coordinates normalized to 0..1.
        NORMALIZED_1000: Coordinates normalized to 0..1000.
        ABSOLUTE_PIXELS: Coordinates in absolute screen pixels.
    """

    IMAGE_PIXELS = "image_pixels"
    NORMALIZED_UNIT = "normalized_unit"
    NORMALIZED_1000 = "normalized_1000"
    ABSOLUTE_PIXELS = "absolute_pixels"


class ObservationArtifactKind(str, Enum):
    """Kinds of observation artifacts retained by one round.

    Attributes:
        IMAGE: Image artifact.
        TEXT: Text artifact.
        STRUCTURED: Structured data artifact.
        ACCESSIBILITY_TREE: Accessibility tree artifact.
        ACCESSIBILITY_DIFF: Accessibility diff artifact.
    """

    IMAGE = "image"
    TEXT = "text"
    STRUCTURED = "structured"
    ACCESSIBILITY_TREE = "accessibility_tree"
    ACCESSIBILITY_DIFF = "accessibility_diff"


class AccessibilitySourceKind(str, Enum):
    """Native accessibility subsystem used for a capture.

    Attributes:
        UIA: Windows UI Automation source.
        AX: macOS Accessibility source.
        ATSPI: Linux AT-SPI source.
    """

    UIA = "uia"
    AX = "ax"
    ATSPI = "atspi"


class AccessibilityScope(str, Enum):
    """Logical accessibility capture scopes.

    Attributes:
        DESKTOP: Capture the full desktop scope.
        ACTIVE_WINDOW: Capture the active window scope.
    """

    DESKTOP = "desktop"
    ACTIVE_WINDOW = "active_window"


class AccessibilityRawFormat(str, Enum):
    """Optional native/raw accessibility payload formats.

    Attributes:
        NONE: No raw payload.
        JSON: JSON raw payload.
        XML: XML raw payload.
    """

    NONE = "none"
    JSON = "json"
    XML = "xml"


class AccessibilityCaptureStatus(str, Enum):
    """Normalized lifecycle status for one accessibility capture.

    Attributes:
        SUCCESS: Capture succeeded.
        UNSUPPORTED: Capture unsupported.
        PERMISSION_DENIED: Permission denied.
        TIMEOUT: Capture timed out.
        ERROR: Capture errored.
        DISABLED: Capture disabled by config.
    """

    SUCCESS = "success"
    UNSUPPORTED = "unsupported"
    PERMISSION_DENIED = "permission_denied"
    TIMEOUT = "timeout"
    ERROR = "error"
    DISABLED = "disabled"


class AccessibilityDiffMode(str, Enum):
    """Supported accessibility diff strategies.

    Attributes:
        NONE: Do not compute a diff.
        BASIC: Compute a basic structural diff.
    """

    NONE = "none"
    BASIC = "basic"


class EvaluationSchedule(str, Enum):
    """When the evaluator should run relative to round/session lifecycle.

    Attributes:
        DISABLED: Evaluation disabled.
        MANUAL: Evaluation runs manually.
        AFTER_ROUND_SYNC: Evaluate synchronously after each round.
        AFTER_ROUND_ASYNC: Evaluate asynchronously after each round.
        SESSION_ROLLUP_ONLY: Only produce session rollup.
    """

    DISABLED = "disabled"
    MANUAL = "manual"
    AFTER_ROUND_SYNC = "after_round_sync"
    AFTER_ROUND_ASYNC = "after_round_async"
    SESSION_ROLLUP_ONLY = "session_rollup_only"


class EvaluationMode(str, Enum):
    """Evidence mode used by the evaluator pipeline.

    Attributes:
        TRACE_ONLY: Use trace evidence only.
        ARTIFACT_AWARE: Include persisted artifacts.
        TOOL_ASSISTED: Allow tool-assisted evidence.
        LIVE_PROBE: Use live probe evidence.
    """

    TRACE_ONLY = "trace_only"
    ARTIFACT_AWARE = "artifact_aware"
    TOOL_ASSISTED = "tool_assisted"
    LIVE_PROBE = "live_probe"


class EvaluationVerdict(str, Enum):
    """Top-level round verdict.

    Attributes:
        SUCCESS: Round succeeded.
        PARTIAL: Round partially succeeded.
        FAILURE: Round failed.
        UNCERTAIN: Verdict is uncertain.
    """

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILURE = "failure"
    UNCERTAIN = "uncertain"


class EvaluationOutcome(str, Enum):
    """Outcome-oriented label separate from process quality.

    Attributes:
        SUCCESS: Desired outcome achieved.
        FAILURE: Desired outcome not achieved.
        UNCERTAIN: Outcome cannot be determined.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    UNCERTAIN = "uncertain"


class EvaluationControllability(str, Enum):
    """Whether observed failures were controllable by the agent.

    Attributes:
        CONTROLLABLE: Failure was controllable.
        UNCONTROLLABLE: Failure was not controllable.
        MIXED: Mixed controllability.
        UNKNOWN: Controllability unknown.
    """

    CONTROLLABLE = "controllable"
    UNCONTROLLABLE = "uncontrollable"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class EvaluationSelectorStrategy(str, Enum):
    """Strategy for selecting evidence passed to the judge.

    Attributes:
        ALL: Select all evidence.
        HEAD_TAIL: Select head/tail evidence.
        ALL_OR_TOPK: Select all or top-k evidence.
        RELEVANCE_TOPK: Select top-k relevant evidence.
    """

    ALL = "all"
    HEAD_TAIL = "head_tail"
    ALL_OR_TOPK = "all_or_topk"
    RELEVANCE_TOPK = "relevance_topk"


class EvaluationToolMode(str, Enum):
    """Which tool classes the evaluator may invoke.

    Attributes:
        NONE: No evaluator tools.
        DETERMINISTIC_ONLY: Deterministic checks only.
        ALLOW_LLM_SELECTED_TOOLS: Allow LLM-selected tools.
    """

    NONE = "none"
    DETERMINISTIC_ONLY = "deterministic_only"
    ALLOW_LLM_SELECTED_TOOLS = "allow_llm_selected_tools"


class EvaluationToolPlacement(str, Enum):
    """Where an evaluator tool is allowed to execute.

    Attributes:
        SERVER_ARTIFACT: Server/controller-side artifact inspection.
        CLIENT_PROBE: Client/device-side read-only live probe.
        CLIENT_MUTATING: Client/device-side state-changing action.
        FINALIZER: Server-side final report submission.
    """

    SERVER_ARTIFACT = "server_artifact"
    CLIENT_PROBE = "client_probe"
    CLIENT_MUTATING = "client_mutating"
    FINALIZER = "finalizer"


class DiagnosisCategory(str, Enum):
    """CUA-specific primary failure taxonomy.

    Attributes:
        TASK_SPEC_OR_FEASIBILITY_ERROR: Task specification, feasibility,
            ambiguity, or safety-intent handling error.
        PERCEPTION_OR_STATE_RECOGNITION_ERROR: Screenshot, OCR, active-window,
            UI state, popup, loading, disabled, or selection-state recognition
            error.
        GROUNDING_LOCALIZATION_ERROR: Semantic target was understood, but
            click, drag, selection, or coordinate grounding landed incorrectly.
        PLAN_DECOMPOSITION_ERROR: Wrong plan, subtask decomposition, ordering,
            or application workflow choice.
        VERIFICATION_TERMINATION_RECOVERY_ERROR: Missing verification, premature
            stop, unnecessary post-completion action, loop, or failed recovery.
        ENVIRONMENT_OR_EXECUTION_ERROR: Environment, tool execution, network,
            app, permission, dependency, or external-system failure.
        OTHER_AGENT_ERROR: Evidence-grounded agent error that does not fit the
            other categories.
    """

    TASK_SPEC_OR_FEASIBILITY_ERROR = "task_spec_or_feasibility_error"
    PERCEPTION_OR_STATE_RECOGNITION_ERROR = "perception_or_state_recognition_error"
    GROUNDING_LOCALIZATION_ERROR = "grounding_localization_error"
    PLAN_DECOMPOSITION_ERROR = "plan_decomposition_error"
    VERIFICATION_TERMINATION_RECOVERY_ERROR = "verification_termination_recovery_error"
    ENVIRONMENT_OR_EXECUTION_ERROR = "environment_or_execution_error"
    OTHER_AGENT_ERROR = "other_agent_error"


class EvaluationEvidenceKind(str, Enum):
    """Kinds of evidence retained in evaluator manifests.

    Attributes:
        IMAGE: Image evidence.
        A11Y: Accessibility evidence.
        DOM: Web DOM/page snapshot evidence.
        RUNTIME: Runtime event evidence.
        TRACE: LLM trace evidence.
        TOOL_RESULT: Tool result evidence.
    """

    IMAGE = "image"
    A11Y = "a11y"
    DOM = "dom"
    RUNTIME = "runtime"
    TRACE = "trace"
    TOOL_RESULT = "tool_result"


class EvaluationSeverity(str, Enum):
    """Severity level for diagnoses and side effects.

    Attributes:
        MINOR: Minor severity.
        MAJOR: Major severity.
        CRITICAL: Critical severity.
    """

    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"


class EvaluationSafetyRiskLevel(str, Enum):
    """Safety risk level assigned by evaluator reports.

    Attributes:
        NONE: No safety concern was observed.
        LOW: Low-impact concern or benign sensitive context.
        MEDIUM: Meaningful risk that did not cause serious harm.
        HIGH: Serious risk or sensitive/destructive action.
        CRITICAL: Severe unsafe action or irreversible harmful effect.
    """

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class EvaluationSafetyCategory(str, Enum):
    """Safety finding category used in evaluator reports.

    Attributes:
        SENSITIVE_OPERATION: Account, credential, personal data, payment, or
            similarly sensitive operation.
        DESTRUCTIVE_ACTION: Delete, overwrite, install, permission, system, or
            other potentially destructive action.
        UNSOLICITED_SIDE_EFFECT: Unrequested change outside the task scope.
        POLICY_OR_PERMISSION: Blocked permission, policy, or access boundary.
        EXTERNAL_COMMUNICATION: Network, messaging, publishing, or external
            communication risk.
        OTHER: Safety concern not covered by another category.
    """

    SENSITIVE_OPERATION = "sensitive_operation"
    DESTRUCTIVE_ACTION = "destructive_action"
    UNSOLICITED_SIDE_EFFECT = "unsolicited_side_effect"
    POLICY_OR_PERMISSION = "policy_or_permission"
    EXTERNAL_COMMUNICATION = "external_communication"
    OTHER = "other"


class EvaluationToolCheckStatus(str, Enum):
    """Status for evaluator-owned deterministic tool checks.

    Attributes:
        SUCCESS: Check succeeded.
        FAILURE: Check failed.
        SKIPPED: Check skipped.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    SKIPPED = "skipped"


class MCPServerMode(str, Enum):
    """Supported MCP transport modes for NorthCUA registrations.

    Attributes:
        STDIO: Stdio MCP transport.
        HTTP: HTTP MCP transport.
    """

    STDIO = "stdio"
    HTTP = "http"


class TargetKind(str, Enum):
    """Kinds of normalized desktop targets.

    Attributes:
        WINDOW: Window target.
        CONTROL: Control target.
    """

    WINDOW = "window"
    CONTROL = "control"


class PlaywrightBrowserEngine(str, Enum):
    """Supported Playwright browser engines.

    Attributes:
        CHROMIUM: Chromium browser engine.
        FIREFOX: Firefox browser engine.
        WEBKIT: WebKit browser engine.
    """

    CHROMIUM = "chromium"
    FIREFOX = "firefox"
    WEBKIT = "webkit"
