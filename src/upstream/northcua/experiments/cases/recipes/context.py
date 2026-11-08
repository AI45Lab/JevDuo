"""Context passed to case init recipe renderers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from northcua.experiments.cases.contracts import AppProfileId, AppProfileSpec, CaseSpec, TemplateProfileSpec


@dataclass(frozen=True, slots=True)
class RecipeRenderContext:
    """Stable recipe inputs derived from the case catalog.

    Attributes:
        case: Case spec with resolved app and template intent.
        app: Primary app profile for the case.
        app_profiles: All catalog app profiles for explicit recipe overrides.
        template: Template profile selected for the case.
        remote_workdir: Per-case remote working directory for logs and pid files.
    """

    case: CaseSpec
    app: AppProfileSpec
    app_profiles: Mapping[AppProfileId, AppProfileSpec]
    template: TemplateProfileSpec
    remote_workdir: str

    def app_for(self, app_profile: AppProfileId | None) -> AppProfileSpec:
        """Resolve an explicit recipe app or return the case app.

        Args:
            app_profile: Optional app override from recipe args.

        Returns:
            Resolved app profile used by the recipe renderer.
        """

        if app_profile is None:
            return self.app
        return self.app_profiles[app_profile]
