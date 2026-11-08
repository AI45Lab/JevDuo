"""NexAU/UMP adapters behind NorthCUA context-management ports."""

from .errors import is_context_overflow_error
from .lazy_summary import LazySummaryClient
from .provider_guard import ProviderContextBudgetGuard, install_provider_context_budget_guard
from .provider_runtime import (
    SummaryProviderRuntime,
    configured_summary_context_limit,
    resolve_summary_llm_config,
    resolve_summary_provider_runtime,
)
from .summary import (
    NexAUSummaryClient,
    summary_prompt_fingerprint,
    summary_reasoning_fingerprint_mode,
    summary_source_capacity_bytes_from_hard_budget,
)
from .ump import NexAUTokenEstimator, UMPContextProjector, UMPSummaryMessageFactory

__all__ = [
    "NexAUSummaryClient",
    "NexAUTokenEstimator",
    "SummaryProviderRuntime",
    "LazySummaryClient",
    "ProviderContextBudgetGuard",
    "install_provider_context_budget_guard",
    "UMPContextProjector",
    "UMPSummaryMessageFactory",
    "is_context_overflow_error",
    "resolve_summary_provider_runtime",
    "resolve_summary_llm_config",
    "summary_prompt_fingerprint",
    "summary_reasoning_fingerprint_mode",
    "summary_source_capacity_bytes_from_hard_budget",
    "configured_summary_context_limit",
]
