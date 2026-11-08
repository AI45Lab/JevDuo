"""Public script evaluator API usable without the batch scheduler.

The implementation is the existing, battle-tested post-run command-json
runner.  These aliases give single-task callers a scheduler-neutral import
path while batch keeps its backward-compatible imports.
"""

from northcua.batch.post_run.contracts import PostRunArtifactSpec as ScriptArtifactSpec
from northcua.batch.post_run.contracts import PostRunHookKind as ScriptEvaluatorKind
from northcua.batch.post_run.contracts import PostRunHookResult as ScriptEvaluatorResult
from northcua.batch.post_run.contracts import PostRunHookSpec as ScriptEvaluatorSpec
from northcua.batch.post_run.contracts import PostRunHookStatus as ScriptEvaluatorStatus
from northcua.batch.post_run.runner import PostRunHookRunner as ScriptEvaluatorRunner
from northcua.evaluation.script.runtime import SandboxLeaseScriptEvaluatorRuntime, ScriptEvaluatorRuntimePort

__all__ = [
    "ScriptArtifactSpec",
    "SandboxLeaseScriptEvaluatorRuntime",
    "ScriptEvaluatorKind",
    "ScriptEvaluatorResult",
    "ScriptEvaluatorRuntimePort",
    "ScriptEvaluatorRunner",
    "ScriptEvaluatorSpec",
    "ScriptEvaluatorStatus",
]
