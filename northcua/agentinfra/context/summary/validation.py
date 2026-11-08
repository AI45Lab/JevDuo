"""Typed summary parsing, provenance expansion and safety validation."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping

from northcua.agentinfra.context.security import contains_sensitive_text
from northcua.agentkernel.context.domain.models import (
    ContextSummaryDocumentV1,
    SummaryItem,
)
from northcua.agentkernel.context.strategies import ContextSummaryError

_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_FORBIDDEN_OUTPUT = re.compile(
    r"data:image/|encrypted_content|reasoning_signature|thought_signature|\bBearer\s+",
    re.IGNORECASE,
)
_PROFILE_VOLATILE_OUTPUT = re.compile(
    r"(?i)(?:\\?[\"'])?\b(?:call_id|coordinates?|element_id|node_id|ref|refs|x|y)\b" r"(?:\\?[\"'])?\s*[:=]"
)
_RAW_SOURCE_PREFIX = "segment-raw-"
logger = logging.getLogger(__name__)


def parse_summary_document(content: str) -> ContextSummaryDocumentV1:
    stripped = _JSON_FENCE.sub("", content.strip()).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise ContextSummaryError("summary response does not contain a JSON object")
    candidate = stripped[start : end + 1]
    if _FORBIDDEN_OUTPUT.search(candidate) or contains_sensitive_text(candidate):
        raise ContextSummaryError("summary response contains volatile or sensitive state")
    try:
        document = ContextSummaryDocumentV1.model_validate_json(candidate)
    except ValueError as exc:
        raise ContextSummaryError(f"summary schema validation failed: {exc}") from exc
    if not document.goal.strip() and not any(
        (
            document.constraints,
            document.verified_facts,
            document.decisions,
            document.completed,
            document.failures,
            document.current_state,
            document.open_items,
            document.artifacts,
        )
    ):
        raise ContextSummaryError("summary document is empty")
    return document


def validate_and_expand_source_references(
    document: ContextSummaryDocumentV1,
    *,
    previous_summary: ContextSummaryDocumentV1 | None,
    allowed_source_ids: frozenset[str],
    source_aliases: Mapping[str, tuple[str, ...]] | None,
) -> ContextSummaryDocumentV1:
    if not source_aliases:
        return _validate_or_drop_raw_source_references(document, previous_summary, allowed_source_ids)
    alias_ids = frozenset(source_aliases)
    document = _validate_or_drop_raw_source_references(document, previous_summary, allowed_source_ids | alias_ids)
    expanded = _expand_document_source_aliases(document, source_aliases)
    return _validate_or_drop_raw_source_references(expanded, previous_summary, allowed_source_ids)


def validate_output_safety(document: ContextSummaryDocumentV1, *, profile: str) -> None:
    texts = [document.goal]
    for field_name in (
        "constraints",
        "verified_facts",
        "decisions",
        "completed",
        "failures",
        "current_state",
        "open_items",
        "artifacts",
    ):
        texts.extend(item.text for item in getattr(document, field_name))
    for text in texts:
        if _FORBIDDEN_OUTPUT.search(text) or contains_sensitive_text(text):
            raise ContextSummaryError("summary document contains sensitive state")
        if profile in {"web", "desktop"} and _PROFILE_VOLATILE_OUTPUT.search(text):
            raise ContextSummaryError("summary document contains volatile executable state")


def document_source_ids(document: ContextSummaryDocumentV1) -> set[str]:
    ids: set[str] = set()
    for field_name in (
        "constraints",
        "verified_facts",
        "decisions",
        "completed",
        "failures",
        "current_state",
        "open_items",
        "artifacts",
    ):
        for item in getattr(document, field_name):
            ids.update(item.source_segment_ids)
    return ids


def _validate_source_references(
    document: ContextSummaryDocumentV1,
    previous_summary: ContextSummaryDocumentV1 | None,
    allowed_source_ids: frozenset[str],
) -> None:
    allowed = set(allowed_source_ids)
    if previous_summary is not None:
        allowed.update(document_source_ids(previous_summary))
    unknown = document_source_ids(document) - allowed
    if unknown:
        raise ContextSummaryError(f"summary contains unknown source segment ids: {sorted(unknown)[:5]}")


def _validate_or_drop_raw_source_references(
    document: ContextSummaryDocumentV1,
    previous_summary: ContextSummaryDocumentV1 | None,
    allowed_source_ids: frozenset[str],
) -> ContextSummaryDocumentV1:
    allowed = set(allowed_source_ids)
    if previous_summary is not None:
        allowed.update(document_source_ids(previous_summary))
    unknown = document_source_ids(document) - allowed
    if not unknown:
        return document
    non_raw_unknown = [source_id for source_id in unknown if not source_id.startswith(_RAW_SOURCE_PREFIX)]
    if non_raw_unknown:
        raise ContextSummaryError(f"summary contains unknown source segment ids: {sorted(non_raw_unknown)[:5]}")
    sanitized = _drop_source_ids(document, frozenset(unknown))
    logger.warning(
        "summary dropped unknown raw source segment ids",
        extra={"dropped_source_count": len(unknown), "dropped_source_prefix": _RAW_SOURCE_PREFIX},
    )
    return sanitized


def _drop_source_ids(document: ContextSummaryDocumentV1, source_ids: frozenset[str]) -> ContextSummaryDocumentV1:
    updates: dict[str, tuple[SummaryItem, ...]] = {}
    for field_name in (
        "constraints",
        "verified_facts",
        "decisions",
        "completed",
        "failures",
        "current_state",
        "open_items",
        "artifacts",
    ):
        items: list[SummaryItem] = []
        for item in getattr(document, field_name):
            retained = tuple(source_id for source_id in item.source_segment_ids if source_id not in source_ids)
            items.append(item.model_copy(update={"source_segment_ids": retained}))
        updates[field_name] = tuple(items)
    return document.model_copy(update=updates)


def _expand_document_source_aliases(
    document: ContextSummaryDocumentV1,
    aliases: Mapping[str, tuple[str, ...]],
) -> ContextSummaryDocumentV1:
    updates: dict[str, tuple[SummaryItem, ...]] = {}
    for field_name in (
        "constraints",
        "verified_facts",
        "decisions",
        "completed",
        "failures",
        "current_state",
        "open_items",
        "artifacts",
    ):
        items: list[SummaryItem] = []
        for item in getattr(document, field_name):
            expanded_ids: list[str] = []
            for source_id in item.source_segment_ids:
                expanded_ids.extend(aliases.get(source_id, (source_id,)))
            items.append(item.model_copy(update={"source_segment_ids": tuple(dict.fromkeys(expanded_ids))}))
        updates[field_name] = tuple(items)
    return document.model_copy(update=updates)
