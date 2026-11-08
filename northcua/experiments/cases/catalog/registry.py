"""In-memory case catalog registry."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from northcua.experiments.cases.contracts import (
    AppProfileId,
    AppProfileSpec,
    TemplateProfileId,
    TemplateProfileSpec,
)


class CaseCatalogError(ValueError):
    """Raised when catalog references are missing or inconsistent."""


class _CaseCatalogDocument(BaseModel):
    """Raw catalog document loaded from YAML before registry indexing.

    Fields:
        templates: Template profiles keyed later by profile_id.
        apps: App profiles keyed later by app_profile.
    """

    templates: list[TemplateProfileSpec] = Field(default_factory=list)
    apps: list[AppProfileSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_document(self) -> "_CaseCatalogDocument":
        template_ids = [item.profile_id for item in self.templates]
        app_ids = [item.app_profile for item in self.apps]
        if len(template_ids) != len(set(template_ids)):
            raise ValueError("case catalog has duplicate template profile ids")
        if len(app_ids) != len(set(app_ids)):
            raise ValueError("case catalog has duplicate app profile ids")
        known_templates = set(template_ids)
        unknown_templates = sorted(
            {app.template_profile.value for app in self.apps if app.template_profile not in known_templates}
        )
        if unknown_templates:
            raise ValueError(f"case catalog apps reference unknown template profiles: {unknown_templates}")
        return self


@dataclass(frozen=True, slots=True)
class CaseCatalog:
    """Typed lookup surface for app and template profiles.

    Attributes:
        templates: Template profiles keyed by stable template profile id.
        apps: App profiles keyed by stable app profile id.
        source_path: Optional YAML path used for diagnostics.
    """

    templates: dict[TemplateProfileId, TemplateProfileSpec]
    apps: dict[AppProfileId, AppProfileSpec]
    source_path: Path | None = None

    @classmethod
    def from_raw(cls, raw: object, *, source_path: Path | None = None) -> "CaseCatalog":
        """Validate raw YAML data and build a registry.

        Args:
            raw: Parsed YAML or JSON catalog payload.
            source_path: Optional path used only for diagnostics.

        Returns:
            A catalog registry with duplicate ids rejected.
        """

        document = _CaseCatalogDocument.model_validate(raw)
        return cls(
            templates={item.profile_id: item for item in document.templates},
            apps={item.app_profile: item for item in document.apps},
            source_path=source_path,
        )

    def template(self, profile_id: TemplateProfileId) -> TemplateProfileSpec:
        """Return a template profile or raise a catalog error.

        Args:
            profile_id: Stable template profile id to resolve.

        Returns:
            Matching template profile.

        Raises:
            CaseCatalogError: When the template profile is unknown.
        """

        try:
            return self.templates[profile_id]
        except KeyError as exc:
            raise CaseCatalogError(f"unknown template profile: {profile_id.value}") from exc

    def app(self, app_profile: AppProfileId) -> AppProfileSpec:
        """Return an app profile or raise a catalog error.

        Args:
            app_profile: Stable app profile id to resolve.

        Returns:
            Matching app profile.

        Raises:
            CaseCatalogError: When the app profile is unknown.
        """

        try:
            return self.apps[app_profile]
        except KeyError as exc:
            raise CaseCatalogError(f"unknown app profile: {app_profile.value}") from exc

    def template_for_app(self, app_profile: AppProfileId) -> TemplateProfileSpec:
        """Return the default template for an app profile.

        Args:
            app_profile: App profile whose default template should be resolved.

        Returns:
            Template profile declared by the app profile.
        """

        return self.template(self.app(app_profile).template_profile)
