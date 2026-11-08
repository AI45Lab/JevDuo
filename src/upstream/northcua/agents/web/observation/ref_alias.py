"""Model-visible numeric ref aliases for web observations.

The browser substrate owns source refs such as ``@e39``. The model-facing
surface uses compact numeric aliases such as ``39`` so Set-of-Mark labels stay
readable. This module keeps that translation typed and observation-scoped.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..domain.observations import WebElementRef, WebScreenshotAnnotation, WebScreenshotMark, WebSnapshotObservation
from .actionable_refs import WebModelActionableRefPolicy


class WebRefAlias(BaseModel):
    """One model-visible ref alias backed by a browser source ref."""

    ref: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)


class WebObservationRefAliases(BaseModel):
    """Alias mapping for one prompt/tool-result observation."""

    observation_id: str = ""
    alias_epoch: int = Field(default=0, ge=0)
    aliases: list[WebRefAlias] = Field(default_factory=list)

    @property
    def alias_to_source(self) -> dict[str, str]:
        """Return model-visible ref -> browser source ref mapping."""

        return {alias.ref: alias.source_ref for alias in self.aliases}

    @property
    def source_to_alias(self) -> dict[str, str]:
        """Return browser source ref -> model-visible ref mapping."""

        return {alias.source_ref: alias.ref for alias in self.aliases}


class WebProjectedRef(BaseModel):
    """One model-visible ref paired with the source element it aliases."""

    element: WebElementRef
    alias: WebRefAlias


class WebRefAliasService:
    """Build and apply observation-scoped numeric ref aliases."""

    def __init__(self, *, actionable_policy: WebModelActionableRefPolicy | None = None) -> None:
        self.actionable_policy = actionable_policy or WebModelActionableRefPolicy()

    def aliases_for_observation(
        self,
        snapshot: WebSnapshotObservation,
        *,
        annotation: WebScreenshotAnnotation | None = None,
        observation_id: str = "",
    ) -> WebObservationRefAliases:
        """Return aliases in the same order the model sees refs."""

        if annotation is not None:
            aliases = self._aliases_for_annotation(annotation)
            return WebObservationRefAliases(observation_id=observation_id, aliases=aliases)
        source_refs = [projected.alias.source_ref for projected in self.projected_refs_for_observation(snapshot)]
        aliases = [
            WebRefAlias(ref=str(index), source_ref=source_ref)
            for index, source_ref in enumerate(source_refs, start=1)
            if source_ref
        ]
        return WebObservationRefAliases(observation_id=observation_id, aliases=aliases)

    def projected_refs_for_observation(self, snapshot: WebSnapshotObservation) -> list[WebProjectedRef]:
        """Return actionable elements paired with dense model-visible aliases."""

        projected: list[WebProjectedRef] = []
        for index, element in enumerate(self.actionable_policy.actionable_elements(snapshot), start=1):
            source_ref = element.model_ref
            if not source_ref:
                continue
            projected.append(
                WebProjectedRef(
                    element=element,
                    alias=WebRefAlias(ref=str(index), source_ref=source_ref),
                )
            )
        return projected

    def project_ref(self, source_ref: str, aliases: WebObservationRefAliases) -> str:
        """Return the model-visible alias for ``source_ref`` when known."""

        if not is_source_ref_like(source_ref):
            return source_ref
        canonical_source = canonical_source_ref(source_ref)
        return aliases.source_to_alias.get(canonical_source, canonical_source)

    def project_annotation(
        self,
        annotation: WebScreenshotAnnotation,
        aliases: WebObservationRefAliases,
    ) -> WebScreenshotAnnotation:
        """Return a copy whose marks use model-visible numeric refs."""

        projected_marks: list[WebScreenshotMark] = []
        for mark in annotation.marks:
            source_ref = _source_ref_for_mark(mark)
            alias = aliases.source_to_alias.get(source_ref, mark.ref)
            projected_marks.append(
                mark.model_copy(
                    update={
                        "ref": alias,
                        "label": alias,
                        "source_ref": source_ref,
                    }
                )
            )
        return annotation.model_copy(update={"marks": projected_marks})

    def _source_refs_for_observation(
        self,
        snapshot: WebSnapshotObservation,
        *,
        annotation: WebScreenshotAnnotation | None,
    ) -> list[str]:
        if annotation is None:
            return [element.model_ref for element in snapshot.elements]
        refs: list[str] = []
        for mark in annotation.marks:
            source_ref = _source_ref_for_mark(mark)
            if source_ref:
                refs.append(source_ref)
        return refs

    def _aliases_for_annotation(self, annotation: WebScreenshotAnnotation | None) -> list[WebRefAlias]:
        if annotation is None:
            return []
        aliases: list[WebRefAlias] = []
        for mark in annotation.marks:
            ref = str(mark.ref or "").strip()
            source_ref = _source_ref_for_mark(mark)
            if ref and source_ref:
                aliases.append(WebRefAlias(ref=ref, source_ref=source_ref))
        return aliases


def _source_ref_for_mark(mark: WebScreenshotMark) -> str:
    """Return a source ref without guessing numeric SoM labels."""

    if mark.source_ref:
        return canonical_source_ref(mark.source_ref)
    if is_source_ref_like(mark.ref):
        return canonical_source_ref(mark.ref)
    return ""


def canonical_source_ref(ref: object) -> str:
    """Return canonical internal ``@``-prefixed ref spelling."""

    value = str(ref or "").strip()
    if not value:
        return ""
    if value.startswith("@"):
        return value
    if value.startswith("e") and value[1:].isdigit():
        return f"@{value}"
    if value.isdigit():
        return f"@e{value}"
    return f"@{value}"


def is_source_ref_like(ref: object) -> bool:
    """Return whether ``ref`` looks like a browser-owned source ref.

    Model-visible aliases are plain numbers. Backend refs are explicit source
    refs such as ``@e39`` or ``e39``; only those should be projected in arbitrary
    tool-result payloads so URLs and human text remain untouched.
    """

    value = str(ref or "").strip()
    if not value:
        return False
    if value.startswith("@") and len(value) > 1:
        return True
    return value.startswith("e") and value[1:].isdigit()
