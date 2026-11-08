"""Registry for allowlisted case init recipe renderers."""

from __future__ import annotations

from dataclasses import dataclass

from northcua.experiments.cases.contracts import InitRecipeSpec, RecipeKind
from northcua.experiments.cases.recipes.base import RecipeRenderer
from northcua.experiments.cases.recipes.context import RecipeRenderContext
from northcua.experiments.cases.recipes.renderers import (
    AssertBinaryRecipe,
    AssertFileExistsRecipe,
    LaunchCommandRecipe,
    OpenBlankAppRecipe,
    OpenDirectoryRecipe,
    OpenFileRecipe,
    OpenUrlRecipe,
    RunSetupScriptRecipe,
    WaitWindowRecipe,
)
from northcua.sandboxes.contracts import SandboxCommandSpec


@dataclass(frozen=True, slots=True)
class RecipeRegistry:
    """Typed lookup surface for recipe renderers.

    Attributes:
        renderers: Renderer instances keyed by allowlisted recipe kind.
    """

    renderers: dict[RecipeKind, RecipeRenderer]

    def render(self, recipe: InitRecipeSpec, context: RecipeRenderContext) -> list[SandboxCommandSpec]:
        """Render a recipe or raise a clear error.

        Args:
            recipe: Typed init recipe to render.
            context: Case/catalog context used by the renderer.

        Returns:
            Provider-neutral sandbox commands for the recipe.

        Raises:
            ValueError: When the recipe kind has no registered renderer.
        """

        try:
            renderer = self.renderers[recipe.recipe]
        except KeyError as exc:
            raise ValueError(f"unsupported init recipe: {recipe.recipe.value}") from exc
        return renderer.render(recipe, context)


def default_recipe_registry() -> RecipeRegistry:
    """Return the built-in recipe registry.

    Returns:
        Registry containing all first-party case init recipes.
    """

    renderers: list[RecipeRenderer] = [
        OpenFileRecipe(),
        OpenUrlRecipe(),
        OpenBlankAppRecipe(),
        OpenDirectoryRecipe(),
        LaunchCommandRecipe(),
        RunSetupScriptRecipe(),
        WaitWindowRecipe(),
        AssertBinaryRecipe(),
        AssertFileExistsRecipe(),
    ]
    return RecipeRegistry(renderers={renderer.kind: renderer for renderer in renderers})
