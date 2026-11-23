"""Run one planned goal through bounded, evidence-based execution attempts."""

from __future__ import annotations

import asyncio
import time

from northcua.agentinfra.providers.token_usage import ModelUsageLedger
from northcua.agentkit.orchestration.contracts import (
    RouteDecision,
    RoutingState,
    SlowDecision,
    SubgoalBudget,
)
from northcua.agentkit.orchestration.decisions import DecisionClient
from northcua.agentkit.orchestration.routing import (
    bind_contract,
    choose,
    compact_evidence,
    decision_request,
    recent_method_failures,
    unresolved,
)
from northcua.agentkit.orchestration.runtime import BudgetExceeded
from northcua.core.terminal import AgentTerminalDecision, AgentTerminalReason

from .orchestration import DispatchResult


async def run_subgoal(ports, context, decision):
    config = ports.budget.config.fast_router
    intent = decision.contract.model_copy(deep=True)
    fixed = ports.budget.config.subagent_steps_per_cycle
    minimum = ports.budget.config.min_subagent_steps
    intent.budget.max_model_calls = fixed or max(intent.budget.max_model_calls, minimum)
    intent.budget.max_tool_calls = fixed or max(intent.budget.max_tool_calls, minimum)
    decision.contract = intent
    routing = RoutingState(
        cycle_id=ports.cycle_id,
        decision=decision,
        started_at=time.time(),
        usage_start=ports.state.usage.model_copy(),
    )
    ports.state.routing = routing
    ports.journal.write("goal_started", ports.state)
    deadline = time.monotonic() + intent.budget.max_seconds
    result = None

    def remaining():
        used, start = ports.state.usage, routing.usage_start
        return SubgoalBudget(
            max_model_calls=max(
                0,
                min(
                    intent.budget.max_model_calls - (used.model_calls - start.model_calls),
                    ports.budget.config.max_model_calls - used.model_calls - 1,
                ),
            ),
            max_tool_calls=max(
                0,
                min(
                    intent.budget.max_tool_calls - (used.tool_calls - start.tool_calls),
                    ports.budget.config.max_tool_calls - used.tool_calls,
                ),
            ),
            max_seconds=max(
                0.001,
                min(
                    deadline - time.monotonic(),
                    ports.budget.seconds_left() - config.verification_reserve_seconds,
                ),
            ),
        )

    def record(route):
        routing.decisions.append(route)
        ports.journal.write("route_decided", ports.state, decision=route.model_dump(mode="json"))

    def stop(reason):
        record(
            RouteDecision(
                observation_id=ports.observation_ref()["observation_id"],
                action="return_to_slow",
                reason=reason,
            )
        )

    async def dispatch(contract):
        execution = SlowDecision.model_validate({**decision.model_dump(), "contract": contract.model_dump()})
        return await ports.dispatch_tools(context, execution, routed=True)

    async def finish(result):
        ports.journal.write("goal_finished", ports.state)
        return await ports.finish_dispatch(context, decision, result)

    if intent.user_question is not None:
        spec = ports.clarification_spec()
        if spec is None:
            raise ValueError("user_clarification_unavailable")
        budget = remaining()
        if budget.max_seconds <= 0.001 or budget.max_tool_calls < 1:
            stop("goal_budget_exhausted")
            return await finish(None)
        contract = bind_contract(
            intent,
            spec,
            config,
            1,
            SubgoalBudget(
                max_model_calls=0,
                max_tool_calls=1,
                max_seconds=budget.max_seconds,
            ),
        )
        record(
            RouteDecision(
                observation_id=ports.observation_ref()["observation_id"],
                action="dispatch",
                agent_id=spec.agent_id,
                reason="user_clarification",
            )
        )
        dispatched = await dispatch(contract)
        return dispatched if dispatched.result is None else await finish(dispatched.result)

    while len(routing.attempts) < config.max_attempts:
        budget = remaining()
        if budget.max_seconds <= 0.001 or budget.max_model_calls < 2 or budget.max_tool_calls < 1:
            stop("goal_budget_exhausted")
            break
        if unresolved(result):
            stop("unresolved_action_requires_verification")
            break
        if result is not None and result.outcome == "achieved":
            stop("subgoal_completion_claim")
            break
        executors = {}
        candidates = []
        for spec in ports.registry.specs():
            if spec.agent_id == "user":
                continue
            try:
                contract = bind_contract(intent, spec, config, len(routing.attempts) + 1, budget)
                agent = ports.registry.resolve(contract)
            except ValueError:
                continue
            executors[spec.agent_id] = (contract, agent)
            candidates.append(spec)
        if not candidates:
            stop("no_subagent_available")
            return DispatchResult(
                AgentTerminalDecision.error(
                    reason=AgentTerminalReason.CONFIGURATION_ERROR,
                    final_answer="no_subagent_available",
                    retryable=False,
                )
            )
        observation = ports.observation_ref()
        native = {}
        for spec in candidates:
            _, agent = executors[spec.agent_id]
            probe = getattr(agent, "routing_observation", None)
            if probe is None or remaining().max_tool_calls <= 1:
                continue
            probe_started = time.monotonic()

            async def read(arguments, *, agent=agent, agent_id=spec.agent_id):
                if not arguments.get("argv") or arguments["argv"][0] != "snapshot":
                    raise ValueError("routing_observation_requires_read_only_snapshot")
                if remaining().max_tool_calls <= 1:
                    raise BudgetExceeded("goal_tool_calls_limit")
                ports.budget.charge("tool_calls")
                ports.journal.write("routing_observation_started", ports.state, agent_id=agent_id)
                return await agent.environment.execute(arguments)

            try:
                async with asyncio.timeout(min(8, remaining().max_seconds)):
                    native[spec.agent_id] = await probe(read)
            except (RuntimeError, ValueError, KeyError, TypeError, TimeoutError) as error:
                native[spec.agent_id] = {"status": "unavailable", "error": type(error).__name__}
            ports.journal.write(
                "routing_observation_finished",
                ports.state,
                agent_id=spec.agent_id,
                observation_id=observation["observation_id"],
                evidence=native[spec.agent_id],
                elapsed_seconds=time.monotonic() - probe_started,
            )
        observation["native"] = native
        budget = remaining()
        request = decision_request(
            context.query,
            intent,
            observation,
            candidates,
            routing.attempts,
            budget,
            context={
                "verification": decision.verification,
                "notes": ports.memory.active_entries(context.cursor.round_num),
                "previous_result": compact_evidence(ports.state.previous_result)
                if not routing.attempts
                else None,
                "recent_method_failures": recent_method_failures(
                    ports.cycle_history(context.cursor.round_num)
                ),
            },
        )
        usage_ledger = ModelUsageLedger(
            ports.store.root / "model_usage.jsonl",
            {
                "session_id": context.cursor.session_id,
                "session_step": context.cursor.session_step,
                "round_num": context.cursor.round_num,
                "cycle_id": ports.cycle_id,
                "subgoal_id": intent.subgoal_id,
                "observation_id": observation["observation_id"],
                "agent_id": "fast_router",
                "purpose": "routing",
                "response_ref": str(ports.journal.path),
            },
        )

        async def call_model(model, call, *, observation=observation, request=request):
            if remaining().max_model_calls == 0:
                raise BudgetExceeded("goal_model_calls_limit")
            ports.budget.charge("model_calls", reserve=1)
            ports.journal.write(
                "router_model_started",
                ports.state,
                model=model,
                observation=observation,
                request=request,
            )
            started = time.monotonic()
            try:
                async with asyncio.timeout(min(config.timeout_seconds, remaining().max_seconds)):
                    response = await call()
            except BaseException as error:
                ports.journal.write(
                    "router_model_failed",
                    ports.state,
                    model=model,
                    error_type=type(error).__name__,
                    status_code=getattr(getattr(error, "response", None), "status_code", None),
                    elapsed_seconds=time.monotonic() - started,
                )
                raise
            ports.journal.write(
                "router_model_finished",
                ports.state,
                model=model,
                response=response,
                elapsed_seconds=time.monotonic() - started,
            )
            return response

        try:
            if ports.router_client is None:
                ports.router_client = DecisionClient.from_env(
                    model=config.model, timeout=config.timeout_seconds
                )
            async with asyncio.timeout(budget.max_seconds):
                response = await ports.router_client.ask(request, call_model, model_usage=usage_ledger)
            route = choose(response, candidates, routing.attempts, observation["observation_id"], config)
        except BudgetExceeded:
            stop("goal_budget_exhausted")
            break
        except (RuntimeError, ValueError, KeyError, TypeError, TimeoutError) as error:
            stop(f"router_unavailable:{type(error).__name__}")
            break
        record(route)
        if route.action == "return_to_slow":
            break
        budget = remaining()
        if budget.max_model_calls < 2 or budget.max_tool_calls < 1 or budget.max_seconds <= 0.001:
            stop("goal_budget_exhausted")
            break
        contract, _ = executors[route.agent_id]
        contract.budget = SubgoalBudget(
            max_model_calls=min(config.attempt_model_calls, budget.max_model_calls - 1),
            max_tool_calls=min(config.attempt_tool_calls, budget.max_tool_calls),
            max_seconds=budget.max_seconds,
        )
        dispatched = await dispatch(contract)
        if dispatched.result is None:
            return dispatched
        result = dispatched.result
        if remaining().max_seconds <= 0.001:
            stop("goal_budget_exhausted")
            break
        try:
            async with asyncio.timeout(remaining().max_seconds):
                ports.observation = await ports.capture(context)
        except TimeoutError:
            stop("fresh_observation_required")
            break
    else:
        stop("goal_attempt_limit")
    return await finish(result)
