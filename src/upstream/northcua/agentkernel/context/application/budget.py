"""Deterministic context budget calculation."""

from __future__ import annotations

import math
from decimal import ROUND_FLOOR, Decimal

from northcua.agentkernel.context.domain.config import ContextBudgetConfig
from northcua.agentkernel.context.domain.models import ContextBudget


class ContextBudgetPlanner:
    """Resolve soft, target and hard input budgets from model capacity."""

    def resolve(
        self,
        *,
        context_limit: int,
        output_reserve: int,
        config: ContextBudgetConfig,
    ) -> ContextBudget:
        if context_limit <= 0:
            raise ValueError("context_limit must be positive")
        if output_reserve < 0 or output_reserve >= context_limit:
            raise ValueError("output_reserve must be non-negative and below context_limit")
        base_input_capacity = context_limit - output_reserve
        safety_margin = max(
            config.safety_margin_min_tokens,
            math.ceil(base_input_capacity * config.safety_margin_ratio),
        )
        hard_input_budget = base_input_capacity - safety_margin
        if hard_input_budget <= 0:
            raise ValueError("context budget is exhausted by output reserve and safety margin")
        soft_trigger_tokens = max(
            1,
            int(
                (Decimal(hard_input_budget) * Decimal(str(config.soft_trigger_ratio))).to_integral_value(
                    rounding=ROUND_FLOOR
                )
            ),
        )
        target_tokens = max(
            1,
            int(
                (Decimal(hard_input_budget) * Decimal(str(config.target_ratio))).to_integral_value(rounding=ROUND_FLOOR)
            ),
        )
        if target_tokens >= soft_trigger_tokens:
            raise ValueError("resolved target budget must be below soft trigger")
        return ContextBudget(
            context_limit=context_limit,
            output_reserve=output_reserve,
            safety_margin=safety_margin,
            hard_input_budget=hard_input_budget,
            soft_trigger_tokens=soft_trigger_tokens,
            target_tokens=target_tokens,
        )
