"""Evaluation inside the actor browser lifetime, shared by Python, CLI and serve."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .tools.live_probe import WebBrowserLiveProbeDispatcher
from northcua_controller_plugins.web_runner_evaluation_helpers import (
    run_web_round_evaluator,
    web_evaluation_enabled,
    web_evaluation_fail_open,
    web_evaluation_live_probe_enabled,
)

if TYPE_CHECKING:
    from ..facade import WebAgentFacade
    from ..runner.main import WebAgentRunner
    from ..domain.results import WebAgentResult

logger = logging.getLogger(__name__)


async def evaluate_active_session(
    app: WebAgentFacade,
    *,
    runner: WebAgentRunner,
    result: WebAgentResult,
    enabled: bool | None,
) -> None:
    if not (web_evaluation_enabled(app) if enabled is None else enabled):
        return
    try:
        dispatcher = None
        if web_evaluation_live_probe_enabled(app):
            dispatcher = WebBrowserLiveProbeDispatcher(
                registered_session=runner.session_registry.get_registered_session(result.session_id),
            )
        outcome = await run_web_round_evaluator(
            app,
            remote=False,
            task_id=result.session_id,
            round_num=result.round_num,
            client_tool_dispatcher=dispatcher,
        )
        result.evaluation_report = outcome.report
        result.evaluation_report_path = outcome.report_path
    except Exception as exc:
        result.evaluation_error = str(exc)
        if not web_evaluation_fail_open(app):
            raise
        logger.warning("Web evaluator failed open", exc_info=True)
