"""Composable infrastructure helpers for internal context summarization."""

from .chunking import chunk_segments, minimum_source_characters, rebalance_short_chunks
from .markers import BoundedOutputReserve, bounded_output_reserve, with_bounded_source_markers
from .prompt import (
    SummaryPromptRenderer,
    SummaryRepairTemplateContext,
    SummarySystemTemplateContext,
    SummaryTemplateSet,
    SummaryUserTemplateContext,
    summary_messages,
    summary_prompt_fingerprint,
    summary_repair_message,
)
from .validation import (
    document_source_ids,
    parse_summary_document,
    validate_and_expand_source_references,
    validate_output_safety,
)

__all__ = [
    "BoundedOutputReserve",
    "SummaryPromptRenderer",
    "SummaryRepairTemplateContext",
    "SummarySystemTemplateContext",
    "SummaryTemplateSet",
    "SummaryUserTemplateContext",
    "bounded_output_reserve",
    "chunk_segments",
    "document_source_ids",
    "minimum_source_characters",
    "parse_summary_document",
    "rebalance_short_chunks",
    "summary_messages",
    "summary_prompt_fingerprint",
    "summary_repair_message",
    "validate_and_expand_source_references",
    "validate_output_safety",
    "with_bounded_source_markers",
]
