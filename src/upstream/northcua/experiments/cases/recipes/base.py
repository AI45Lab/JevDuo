"""Base classes for typed case initialization recipes."""

from __future__ import annotations

from abc import ABC, abstractmethod

from northcua.experiments.cases.contracts import InitRecipeSpec, RecipeKind
from northcua.experiments.cases.recipes.context import RecipeRenderContext
from northcua.sandboxes.contracts import SandboxCommandSpec


class RecipeRenderer(ABC):
    """Render a typed recipe into provider-neutral sandbox commands."""

    kind: RecipeKind

    @abstractmethod
    def render(self, recipe: InitRecipeSpec, context: RecipeRenderContext) -> list[SandboxCommandSpec]:
        """Render one recipe for a concrete case.

        Args:
            recipe: Typed init recipe with kind-specific args.
            context: Catalog and case context for rendering commands.

        Returns:
            Provider-neutral sandbox commands to run before the agent starts.
        """
