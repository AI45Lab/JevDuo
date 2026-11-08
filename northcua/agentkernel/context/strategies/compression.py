"""Provider-neutral summary compression planning and validation."""

from __future__ import annotations

import math

from northcua.agentkernel.context.domain.models import (
    ContextSummaryDocumentV1,
    SummaryCompressionContract,
)

from .errors import ContextSummaryError


class SummaryCompressionStrategy:
    """Resolve and enforce measurable checkpoint reduction contracts."""

    def __init__(self, *, wrapper_characters: int, minimum_document_characters: int) -> None:
        if wrapper_characters < 0 or minimum_document_characters <= 0:
            raise ValueError("compression character constants must be positive")
        self._wrapper_characters = wrapper_characters
        self._minimum_document_characters = minimum_document_characters

    def resolve(
        self,
        *,
        retained_baseline_characters: int,
        new_source_characters: int,
        minimum_reduction_ratio: float,
        reserved_output_characters: int = 0,
        enforce_minimum: bool = True,
    ) -> SummaryCompressionContract:
        self._validate_ratio(minimum_reduction_ratio)
        if retained_baseline_characters < 0 or new_source_characters < 0:
            raise ValueError("compression source character counts must be non-negative")
        if reserved_output_characters < 0:
            raise ValueError("reserved_output_characters must be non-negative")
        required_reduction = math.ceil(new_source_characters * minimum_reduction_ratio)
        source_characters = retained_baseline_characters + new_source_characters
        maximum_characters = max(0, source_characters - required_reduction)
        maximum_model_characters = max(0, maximum_characters - reserved_output_characters)
        contract = SummaryCompressionContract(
            source_canonical_characters=source_characters,
            retained_baseline_characters=retained_baseline_characters,
            new_source_characters=new_source_characters,
            minimum_reduction_ratio=minimum_reduction_ratio,
            maximum_canonical_characters=maximum_characters,
            global_final_ceiling_characters=maximum_characters,
            reserved_output_characters=reserved_output_characters,
            maximum_model_canonical_characters=maximum_model_characters,
            maximum_json_characters=max(0, maximum_model_characters - self._wrapper_characters),
        )
        if enforce_minimum and maximum_model_characters < self._minimum_document_characters:
            raise ContextSummaryError(
                "summary source is too small to satisfy the configured compression contract "
                f"({maximum_model_characters}<{self._minimum_document_characters} canonical characters)"
            )
        return contract

    def minimum_source_characters(self, minimum_reduction_ratio: float) -> int:
        """Return the smallest new source that can encode a non-empty checkpoint."""

        self._validate_ratio(minimum_reduction_ratio)
        source_characters = math.ceil(self._minimum_document_characters / (1 - minimum_reduction_ratio))
        while (
            source_characters - math.ceil(source_characters * minimum_reduction_ratio)
            < self._minimum_document_characters
        ):
            source_characters += 1
        return source_characters

    @staticmethod
    def _validate_ratio(value: float) -> None:
        if not 0 < value < 1:
            raise ValueError("minimum_reduction_ratio must be greater than 0 and lower than 1")

    @staticmethod
    def validate(
        document: ContextSummaryDocumentV1,
        contract: SummaryCompressionContract,
        *,
        final_document: bool = False,
    ) -> None:
        rendered_characters = len(document.canonical_prompt_text())
        maximum_characters = (
            contract.maximum_canonical_characters if final_document else contract.maximum_model_canonical_characters
        )
        if rendered_characters > maximum_characters:
            raise ContextSummaryError(
                "validated summary exceeds the compression contract "
                f"({rendered_characters}>{maximum_characters} canonical characters)"
            )
