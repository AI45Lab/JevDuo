"""Observation normalization and collection for the web agent."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .builder import WebObservationBuilder
    from .collector import WebObservationCollector
    from .normalizer import WebScreenshotNormalizer, WebSnapshotNormalizer
    from .projector import WebObservationProjector
    from .ref_alias import WebObservationRefAliases, WebRefAlias, WebRefAliasService
    from .settle import WebPostActionSettleService, WebSettleResult


def __getattr__(name: str) -> object:
    """Lazily expose observation helpers and keep imports cheap."""

    if name == "WebObservationBuilder":
        from .builder import WebObservationBuilder

        return WebObservationBuilder
    if name == "WebObservationCollector":
        from .collector import WebObservationCollector

        return WebObservationCollector
    if name == "WebObservationProjector":
        from .projector import WebObservationProjector

        return WebObservationProjector
    if name in {"WebPostActionSettleService", "WebSettleResult"}:
        from .settle import WebPostActionSettleService, WebSettleResult

        return {"WebPostActionSettleService": WebPostActionSettleService, "WebSettleResult": WebSettleResult}[name]
    if name in {"WebScreenshotNormalizer", "WebSnapshotNormalizer"}:
        from .normalizer import WebScreenshotNormalizer, WebSnapshotNormalizer

        return {"WebScreenshotNormalizer": WebScreenshotNormalizer, "WebSnapshotNormalizer": WebSnapshotNormalizer}[
            name
        ]
    if name in {"WebObservationRefAliases", "WebRefAlias", "WebRefAliasService", "canonical_source_ref"}:
        from .ref_alias import WebObservationRefAliases, WebRefAlias, WebRefAliasService, canonical_source_ref

        return {
            "WebObservationRefAliases": WebObservationRefAliases,
            "WebRefAlias": WebRefAlias,
            "WebRefAliasService": WebRefAliasService,
            "canonical_source_ref": canonical_source_ref,
        }[name]
    raise AttributeError(name)


__all__ = [
    "WebObservationBuilder",
    "WebObservationCollector",
    "WebObservationProjector",
    "WebObservationRefAliases",
    "WebPostActionSettleService",
    "WebRefAlias",
    "WebRefAliasService",
    "WebScreenshotNormalizer",
    "WebSettleResult",
    "WebSnapshotNormalizer",
    "canonical_source_ref",
]
