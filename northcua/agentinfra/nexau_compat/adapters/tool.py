"""Legacy-compatible NexAU tool construction and behavior.

The public NexAU revision used by NorthCUA intentionally changed two defaults:
tool output formatting moved from XML to Markdown, and JSON Schema validation
started rejecting parameters not declared by the schema.  NorthCUA must keep
the private-pin contract until those changes are rolled out deliberately.

This module specializes the public :class:`nexau.archs.tool.Tool` instead of
wrapping it.  Consumers therefore retain NexAU type identity (``isinstance``),
while the compatibility behavior stays explicit and locally testable.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MethodType
from typing import Any, cast

import jsonschema
from nexau.archs.main_sub.utils import import_from_string
from nexau.archs.tool.formatters import ToolFormatter, ToolFormatterContext, resolve_tool_formatter
from nexau.archs.tool.tool import Tool

_LEGACY_FORMATTER_NAME = "xml"
_NEXAU_TOOL_LOGGER_NAME = "nexau.archs.tool.tool"
_LEGACY_FORMATTER_MARKER = "__northcua_legacy_formatter_installed__"
_LEGACY_VALIDATION_MARKER = "__northcua_legacy_validation_installed__"

logger = logging.getLogger(_NEXAU_TOOL_LOGGER_NAME)


@dataclass(frozen=True, slots=True)
class NexAUToolCapabilities:
    """Version-dependent constructor fields detected at one boundary."""

    permissions: bool
    source_id: bool
    yaml_permissions: bool
    yaml_source_id: bool

    @classmethod
    def detect(cls) -> "NexAUToolCapabilities":
        constructor = inspect.signature(Tool.__init__).parameters
        yaml_factory = inspect.signature(Tool.from_yaml).parameters
        return cls(
            permissions="permissions" in constructor,
            source_id="source_id" in constructor,
            yaml_permissions="permissions" in yaml_factory,
            yaml_source_id="source_id" in yaml_factory,
        )


class LegacyCompatibleTool(Tool):
    """A public NexAU ``Tool`` with NorthCUA's stable formatting contract.

    The class is a real NexAU ``Tool`` subtype, not a proxy.  It changes only
    three behavior points at the public NexAU boundary: the implicit formatter,
    the formatter failure fallback, and validation of undeclared parameters.
    """

    def resolve_formatter(self) -> ToolFormatter:
        """Resolve an omitted formatter to the legacy XML implementation."""

        if self._resolved_formatter is None:
            self._resolved_formatter = _resolve_legacy_formatter(self.formatter)
        return self._resolved_formatter

    def format_output_for_llm(
        self,
        *,
        tool_input: dict[str, Any],
        tool_output: object,
        tool_call_id: str | None,
        is_error: bool,
    ) -> object:
        """Format output with the legacy XML fallback chain.

        A configured custom formatter is still honored.  If it fails, NorthCUA
        retries with XML and finally returns the raw value.
        """

        context = ToolFormatterContext(
            tool_name=self.name,
            tool_input=cast(dict[str, object], dict(tool_input)),
            tool_output=tool_output,
            tool_call_id=tool_call_id,
            is_error=is_error,
        )
        formatter = self.resolve_formatter()
        return _format_with_xml_fallback(context, formatter)

    def validate_params(self, params: dict[str, Any]) -> None:
        """Validate only schema-declared parameters, while preserving injected runtime arguments.

        Hook- or framework-injected arguments remain available to the tool
        implementation but do not make a schema with
        ``additionalProperties: false`` fail validation.
        """

        _validate_legacy_params(self, params)

    def __repr__(self) -> str:
        """Keep the stable representation for omitted formatters."""

        implementation = self.implementation
        implementation_name = (
            getattr(implementation, "__name__", repr(implementation)) if implementation is not None else "None"
        )
        formatter_name = (
            self.formatter
            if isinstance(self.formatter, str)
            else getattr(self.formatter, "__name__", _LEGACY_FORMATTER_NAME)
        )
        return f"Tool(name='{self.name}', implementation={implementation_name}, formatter={formatter_name})"


class NexAUToolAdapter:
    """Factory facade for tools that require the private-pin behavior."""

    tool_type: type[LegacyCompatibleTool] = LegacyCompatibleTool

    @classmethod
    def enforce_legacy_formatter(cls, tool: Tool) -> Tool:
        """Apply all private-pin behavior to a NexAU-created tool.

        MCP, LoadSkill, ToolSearch, and team tools are constructed inside
        NexAU and therefore cannot use :meth:`create`. Assigning instance-level
        formatter and validation policies keeps their real concrete type while
        avoiding both public migrations: Markdown output and rejection of
        undeclared framework-injected parameters.
        """

        if isinstance(tool, LegacyCompatibleTool):
            return tool
        if not getattr(tool, _LEGACY_FORMATTER_MARKER, False):
            tool.formatter = (
                _format_with_legacy_xml_fallback
                if tool.formatter is None
                else _legacy_custom_formatter_with_xml_fallback(_resolve_legacy_formatter(tool.formatter))
            )
            tool._resolved_formatter = None
            setattr(tool, _LEGACY_FORMATTER_MARKER, True)
        if not getattr(tool, _LEGACY_VALIDATION_MARKER, False):
            setattr(tool, "validate_params", MethodType(_validate_nexau_owned_tool_params, tool))
            setattr(tool, _LEGACY_VALIDATION_MARKER, True)
        return tool

    @classmethod
    def create(
        cls,
        *,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        implementation: Callable[..., Any] | str | None,
        skill_description: str | None = None,
        as_skill: bool = False,
        disable_parallel: bool = False,
        lazy: bool = False,
        defer_loading: bool = False,
        search_hint: str | None = None,
        template_override: str | None = None,
        formatter: str | ToolFormatter | None = None,
        extra_kwargs: dict[str, Any] | None = None,
        source_name: str | None = None,
        permissions: dict[str, list[str]] | None = None,
        source_id: str | None = None,
    ) -> Tool:
        """Create a legacy-compatible object that remains a NexAU ``Tool``."""

        kwargs: dict[str, Any] = {
            "name": name,
            "description": description,
            "input_schema": input_schema,
            "implementation": implementation,
            "skill_description": skill_description,
            "as_skill": as_skill,
            "disable_parallel": disable_parallel,
            "lazy": lazy,
            "defer_loading": defer_loading,
            "search_hint": search_hint,
            "template_override": template_override,
            "formatter": formatter,
            "extra_kwargs": extra_kwargs,
            "source_name": source_name,
        }
        capabilities = NexAUToolCapabilities.detect()
        if capabilities.permissions:
            kwargs["permissions"] = permissions
        if capabilities.source_id:
            kwargs["source_id"] = source_id
        return cls.tool_type(**kwargs)

    @classmethod
    def from_yaml(
        cls,
        yaml_path: str,
        binding: Callable[..., Any] | str | None = None,
        *,
        as_skill: bool = False,
        extra_kwargs: dict[str, Any] | None = None,
        lazy: bool | None = None,
        name: str | None = None,
        description: str | None = None,
        description_suffix: str = "",
        defer_loading: bool | None = None,
        permissions: dict[str, list[str]] | None = None,
        source_id: str | None = None,
    ) -> Tool:
        """Load a YAML tool without losing NexAU type identity."""

        kwargs: dict[str, Any] = {
            "as_skill": as_skill,
            "extra_kwargs": extra_kwargs,
            "lazy": lazy,
            "name": name,
            "description": description,
            "description_suffix": description_suffix,
            "defer_loading": defer_loading,
        }
        capabilities = NexAUToolCapabilities.detect()
        if capabilities.yaml_permissions:
            kwargs["permissions"] = permissions
        if capabilities.yaml_source_id:
            kwargs["source_id"] = source_id
        return cls.tool_type.from_yaml(
            yaml_path,
            binding,
            **kwargs,
        )


def _resolve_legacy_formatter(formatter: str | ToolFormatter | None) -> ToolFormatter:
    """Resolve the XML default or an explicitly imported formatter.

    In particular, ``markdown`` is not treated as a built-in alias.  Other strings
    resolve as imports, so a misspelled custom formatter is an explicit
    configuration error.
    """

    if formatter is None or formatter == _LEGACY_FORMATTER_NAME:
        return resolve_tool_formatter(_LEGACY_FORMATTER_NAME)
    if callable(formatter):
        return formatter
    imported = import_from_string(formatter)
    if not callable(imported):
        raise ValueError(f"Tool formatter '{formatter}' did not resolve to a callable")
    return cast(ToolFormatter, imported)


def _validate_legacy_params(tool: Tool, params: dict[str, Any]) -> None:
    """Validate only schema-declared inputs, while retaining injected execution context."""

    raw_properties = tool.input_schema.get("properties", {})
    properties: Mapping[str, object]
    if isinstance(raw_properties, Mapping):
        properties = cast(Mapping[str, object], raw_properties)
    else:  # Defensive only; NexAU validates the schema during construction.
        properties = {}
    schema_params = {key: value for key, value in params.items() if key in properties}
    try:
        jsonschema.validate(schema_params, tool.input_schema)
    except jsonschema.ValidationError as exc:
        raise ValueError(
            f"Invalid parameters for tool '{tool.name}': {exc.message}. params={schema_params}",
        ) from exc


def _validate_nexau_owned_tool_params(tool: Tool, params: dict[str, Any]) -> None:
    """Bound-method target used for builtin, MCP, skill, and team tools."""

    _validate_legacy_params(tool, params)


def _format_with_legacy_xml_fallback(context: ToolFormatterContext) -> object:
    """Preserve private XML → XML → raw behavior on NexAU-owned Tool types."""

    return _format_with_xml_fallback(context, resolve_tool_formatter(_LEGACY_FORMATTER_NAME))


def _format_with_xml_fallback(context: ToolFormatterContext, formatter: ToolFormatter) -> object:
    """Run one formatter through the private XML then raw fallback chain."""

    try:
        return formatter(context)
    except Exception:
        logger.exception(
            "Tool '%s' formatter failed; falling back to builtin XML formatter",
            context.tool_name,
        )
    try:
        return resolve_tool_formatter(_LEGACY_FORMATTER_NAME)(context)
    except Exception:
        logger.exception(
            "Tool '%s' builtin XML formatter failed; falling back to raw tool output",
            context.tool_name,
        )
        return context.tool_output


def _legacy_custom_formatter_with_xml_fallback(formatter: ToolFormatter) -> ToolFormatter:
    """Keep a NexAU-owned custom formatter while replacing Markdown fallback."""

    def format_output(context: ToolFormatterContext) -> object:
        return _format_with_xml_fallback(context, formatter)

    return format_output


__all__ = ["LegacyCompatibleTool", "NexAUToolAdapter", "NexAUToolCapabilities"]
