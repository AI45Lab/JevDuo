"""Small import helpers for declarative specs."""

from __future__ import annotations

from importlib import import_module
from typing import TypeVar, cast

T = TypeVar("T")


def resolve_dotted(path_or_object: str | T) -> T:
    """Resolve ``module:attr`` strings while passing through objects.

    Args:
        path_or_object: Dotted import path or already-resolved object.

    Returns:
        Resolved object, preserving non-string inputs unchanged.

    Raises:
        ValueError: If a string is not in ``module:attribute`` form.
        ImportError: If the module cannot be imported.
        AttributeError: If the module does not expose the requested attribute.
    """

    if not isinstance(path_or_object, str):
        return path_or_object
    module_name, _, attr_name = path_or_object.partition(":")
    if not module_name or not attr_name:
        raise ValueError(f"Expected dotted path in 'module:attr' form, got {path_or_object!r}")
    module = import_module(module_name)
    return cast(T, getattr(module, attr_name))
