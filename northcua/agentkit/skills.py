"""Skill configuration bridge between AgentKit and NexAU."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from northcua.core.json_types import JsonObject

from .spec import AgentSkillSpec

if TYPE_CHECKING:
    from northcua.core.config import AgentKitAgentConfig


class _SkillConfigLike(Protocol):
    enabled: bool
    paths: list[str]
    asset_tools_enabled: bool
    asset_read_max_bytes: int
    allowed_file_extensions: list[str]
    allow_binary_summary: bool
    allow_missing: bool


@dataclass(frozen=True, slots=True)
class ResolvedAgentSkillSettings:
    """Fully resolved skill settings for one agent.

    Attributes:
        skill_paths: Existing skill directories after spec/config merge.
        asset_tools_enabled: Whether read-only skill asset tools should be
            exposed by the agent runtime.
        asset_read_max_bytes: Maximum bytes read from one skill asset.
        allowed_file_extensions: Lowercase extensions readable as text.
        allow_binary_summary: Whether binary assets return metadata summaries.
        allow_missing: Whether missing configured paths were allowed.
    """

    skill_paths: tuple[Path, ...]
    asset_tools_enabled: bool
    asset_read_max_bytes: int
    allowed_file_extensions: frozenset[str]
    allow_binary_summary: bool
    allow_missing: bool


class AgentKitSkillResolver:
    """Resolve AgentKit skill defaults into NexAU-compatible config.

    The resolver intentionally returns ordinary folder paths because NexAU
    already owns progressive disclosure and the ``LoadSkill`` tool. AgentKit
    only decides which folders belong to a given agent and whether optional
    asset-read tools should be exposed.
    """

    @classmethod
    def apply_to_raw_agent_config(
        cls,
        raw_config: JsonObject,
        *,
        spec: object | None,
        app_agent_config: "AgentKitAgentConfig | None",
        app_config_path: Path,
        agent_config_path: Path,
    ) -> ResolvedAgentSkillSettings:
        """Merge spec/app/NexAU skill paths and update ``raw_config`` in place.

        Args:
            raw_config: Raw NexAU agent config mapping.
            spec: Optional ``AgentSpec`` with ``skills`` defaults.
            app_agent_config: Optional generic app config for this agent kind.
            app_config_path: Path to ``app.yaml`` used to resolve app overrides.
            agent_config_path: Path to the NexAU agent config used to resolve
                spec defaults and existing raw ``skills`` values.

        Returns:
            Resolved settings used later by optional asset tool factories.

        Raises:
            FileNotFoundError: If a configured skill directory is missing and
                missing paths are not explicitly allowed.
            ValueError: If the raw NexAU ``skills`` field is malformed.
        """

        spec_skills = getattr(spec, "skills", None)
        if not isinstance(spec_skills, AgentSkillSpec):
            spec_skills = AgentSkillSpec()
        app_skills = app_agent_config.skills if app_agent_config is not None else None
        merged = cls.resolve_settings(
            spec_skills=spec_skills,
            app_skills=app_skills,
            app_config_path=app_config_path,
            agent_config_path=agent_config_path,
            raw_skill_paths=cls._raw_skill_paths(raw_config),
        )
        if merged.skill_paths:
            raw_config["skills"] = [str(path) for path in merged.skill_paths]
        elif "skills" in raw_config:
            raw_config["skills"] = []
        return merged

    @classmethod
    def resolve_settings(
        cls,
        *,
        spec_skills: AgentSkillSpec,
        app_skills: _SkillConfigLike | None,
        app_config_path: Path,
        agent_config_path: Path,
        raw_skill_paths: tuple[str, ...] = (),
    ) -> ResolvedAgentSkillSettings:
        """Resolve skill paths and asset-tool settings without mutating config.

        Args:
            spec_skills: AgentSpec-level defaults.
            app_skills: Optional app-config overrides.
            app_config_path: Path to the app config for resolving app paths.
            agent_config_path: Path to the NexAU agent config for resolving
                spec and raw agent paths.
            raw_skill_paths: Existing NexAU ``skills`` entries.

        Returns:
            Resolved paths and asset-read policy.
        """

        config_enabled = app_skills.enabled if app_skills is not None else True
        allow_missing = bool(
            spec_skills.allow_missing or (app_skills.allow_missing if app_skills is not None else False)
        )
        path_candidates: list[tuple[str, Path]] = [
            *((path, agent_config_path.parent) for path in raw_skill_paths),
        ]
        if spec_skills.enabled and config_enabled:
            path_candidates.extend((path, agent_config_path.parent) for path in spec_skills.paths)
            if app_skills is not None:
                path_candidates.extend((path, app_config_path.parent) for path in app_skills.paths)
        resolved_paths = cls._resolve_unique_paths(path_candidates, allow_missing=allow_missing)
        configured_extensions = tuple(app_skills.allowed_file_extensions if app_skills is not None else ())
        allowed_extensions = _normalize_extensions(configured_extensions, spec_skills.allowed_file_extensions)
        return ResolvedAgentSkillSettings(
            skill_paths=resolved_paths,
            asset_tools_enabled=bool(
                config_enabled
                and (
                    spec_skills.asset_tools_enabled
                    or (app_skills.asset_tools_enabled if app_skills is not None else False)
                )
            ),
            asset_read_max_bytes=(
                int(app_skills.asset_read_max_bytes)
                if app_skills is not None and app_skills.asset_read_max_bytes
                else int(spec_skills.asset_read_max_bytes)
            ),
            allowed_file_extensions=allowed_extensions,
            allow_binary_summary=bool(
                spec_skills.allow_binary_summary
                if app_skills is None
                else app_skills.allow_binary_summary or spec_skills.allow_binary_summary
            ),
            allow_missing=allow_missing,
        )

    @staticmethod
    def _raw_skill_paths(raw_config: JsonObject) -> tuple[str, ...]:
        raw_skills = raw_config.get("skills")
        if raw_skills is None:
            return ()
        if not isinstance(raw_skills, list):
            raise ValueError("NexAU agent config field 'skills' must be a list of folder paths")
        paths: list[str] = []
        for item in raw_skills:
            if not isinstance(item, str):
                raise ValueError("NexAU agent config field 'skills' must contain only strings")
            paths.append(item)
        return tuple(paths)

    @staticmethod
    def _resolve_unique_paths(
        path_candidates: list[tuple[str, Path]],
        *,
        allow_missing: bool,
    ) -> tuple[Path, ...]:
        seen: set[Path] = set()
        resolved: list[Path] = []
        for raw_path, base_dir in path_candidates:
            expanded = Path(raw_path).expanduser()
            if not expanded.is_absolute():
                expanded = base_dir / expanded
            path = expanded.resolve()
            if not path.exists():
                if allow_missing:
                    continue
                raise FileNotFoundError(f"AgentKit skill path does not exist: {path}")
            if path not in seen:
                seen.add(path)
                resolved.append(path)
        return tuple(resolved)


def _normalize_extensions(configured: tuple[str, ...], defaults: frozenset[str]) -> frozenset[str]:
    source = configured or tuple(defaults)
    normalized: set[str] = set()
    for extension in source:
        value = extension.strip().lower()
        if value and not value.startswith("."):
            value = f".{value}"
        normalized.add(value)
    return frozenset(normalized)


__all__ = ["AgentKitSkillResolver", "ResolvedAgentSkillSettings"]
