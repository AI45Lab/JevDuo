"""Read-only tools for inspecting registered folder-based skill assets."""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from nexau.archs.main_sub.skill import Skill
from nexau.archs.tool.formatters import ToolFormatterContext
from nexau.archs.tool.tool import Tool
from pydantic import BaseModel, Field

from northcua.agentkernel.tools import contract_to_nexau_tool
from northcua.agentkit.skills import ResolvedAgentSkillSettings
from northcua.core.json_types import JsonObject
from northcua_runtime_shared.tool_contracts import ToolContractKind, ToolPlacement, define_tool

logger = logging.getLogger(__name__)
_MAX_SKILL_ASSET_HASH_BYTES = 16 * 1024 * 1024


class ListSkillAssetsParams(BaseModel):
    """Arguments for listing files inside a registered skill folder.

    Fields:
        skill_name: Registered NexAU skill name.
        path: Skill-relative directory to list.
        max_entries: Maximum recursive entries returned to the model.
    """

    skill_name: str = Field(description="Registered skill name.")
    path: str = Field(default="", description="Skill-relative directory path.")
    max_entries: int = Field(default=100, gt=0, le=1000)


class ReadSkillAssetParams(BaseModel):
    """Arguments for reading one file inside a registered skill folder.

    Fields:
        skill_name: Registered NexAU skill name.
        relative_path: Skill-relative file path.
        max_bytes: Optional per-call byte cap. ``0`` uses agent policy.
    """

    skill_name: str = Field(description="Registered skill name.")
    relative_path: str = Field(description="Skill-relative file path.")
    max_bytes: int = Field(default=0, ge=0, le=1024 * 1024)


@dataclass(frozen=True, slots=True)
class SkillAssetReadResult:
    """Result of resolving a skill asset path.

    Attributes:
        path: Absolute resolved file path after containment checks.
        relative_path: Path relative to the skill root for trace output.
    """

    path: Path
    relative_path: str


class AgentKitSkillAssetToolFactory:
    """Build read-only tools for folder-based skill assets."""

    def __init__(
        self,
        *,
        skills: Iterable[Skill],
        settings: ResolvedAgentSkillSettings,
        usage_recorder: Callable[[JsonObject], None] | None = None,
    ) -> None:
        """Create a factory for a resolved skill set.

        Args:
            skills: NexAU skill objects already loaded by ``AgentConfig``.
            settings: AgentKit skill asset-read policy.
            usage_recorder: Optional callback used by agents to persist
                skill-access metadata into their own trace/result.
        """

        self._settings = settings
        self._usage_recorder = usage_recorder
        self._skills = {skill.name: Path(skill.folder).resolve() for skill in skills}

    def build_tools(self) -> tuple[Tool, ...]:
        """Return the optional skill asset tools.

        Returns:
            Empty tuple when disabled or no skills are registered; otherwise
            list/read tools that run through the standard NexAU tool path.
        """

        if not self._settings.asset_tools_enabled or not self._skills:
            return ()
        return (
            _contract_tool(
                name="list_skill_assets",
                description=(
                    "List files under a registered skill folder. Use this after LoadSkill when a skill refers "
                    "to examples, scripts, schemas, or templates that should be inspected without guessing."
                ),
                params=ListSkillAssetsParams,
                implementation=self.list_skill_assets,
            ),
            _contract_tool(
                name="read_skill_asset",
                description=(
                    "Read one text asset from a registered skill folder. Paths are skill-relative; absolute paths, "
                    "parent-directory traversal, and symlink escapes are rejected. Binary assets return a checksum "
                    "summary when enabled."
                ),
                params=ReadSkillAssetParams,
                implementation=self.read_skill_asset,
            ),
        )

    def list_skill_assets(self, **payload: object) -> JsonObject:
        """List skill-local assets with path safety checks.

        Args:
            payload: Tool arguments matching ``ListSkillAssetsParams``.

        Returns:
            Model-facing success or failure payload.

        Raises:
            No exception escapes to the model; failures are converted to tool
            payloads after logging.
        """

        try:
            params = ListSkillAssetsParams.model_validate(payload)
            root, target = self._resolve_skill_path(skill_name=params.skill_name, relative_path=params.path)
            if not target.is_dir():
                raise NotADirectoryError(params.path or ".")
            entries: list[JsonObject] = []
            for item in _iter_skill_assets(target, limit=params.max_entries):
                relative = str(item.relative_to(root))
                is_symlink = item.is_symlink()
                is_directory = item.is_dir() and not is_symlink
                entries.append(
                    {
                        "path": relative,
                        "type": "symlink" if is_symlink else ("directory" if is_directory else "file"),
                        "size": item.stat().st_size if item.is_file() and not is_symlink else 0,
                        "extension": item.suffix.lower(),
                    }
                )
            self._record_usage("list_skill_assets", params.skill_name, params.path, {"entry_count": len(entries)})
            return {"status": "success", "summary": "skill assets listed.", "result": {"entries": entries}}
        except Exception as exc:  # noqa: BLE001 - model-facing tool feedback.
            logger.warning("skill asset listing failed", exc_info=True)
            return {"status": "failure", "summary": f"list_skill_assets failed: {type(exc).__name__}: {exc}"}

    def read_skill_asset(self, **payload: object) -> JsonObject:
        """Read one skill-local asset or return a binary summary.

        Args:
            payload: Tool arguments matching ``ReadSkillAssetParams``.

        Returns:
            Model-facing success or failure payload.

        Raises:
            No exception escapes to the model; failures are converted to tool
            payloads after logging.
        """

        try:
            params = ReadSkillAssetParams.model_validate(payload)
            root, target = self._resolve_skill_path(skill_name=params.skill_name, relative_path=params.relative_path)
            if not target.is_file():
                raise FileNotFoundError(params.relative_path)
            size = target.stat().st_size
            max_bytes = (
                min(params.max_bytes, self._settings.asset_read_max_bytes)
                if params.max_bytes
                else self._settings.asset_read_max_bytes
            )
            suffix = target.suffix.lower()
            allowed_as_text = suffix in self._settings.allowed_file_extensions
            if not allowed_as_text:
                return self._binary_summary(
                    skill_name=params.skill_name,
                    relative_path=str(target.relative_to(root)),
                    target=target,
                    reason=f"extension {suffix or '<none>'} is not text-readable by policy",
                )
            raw = _read_prefix(target, max_bytes=max_bytes)
            try:
                text = raw[:max_bytes].decode("utf-8")
            except UnicodeDecodeError:
                return self._binary_summary(
                    skill_name=params.skill_name,
                    relative_path=str(target.relative_to(root)),
                    target=target,
                    reason="asset is not valid UTF-8",
                )
            truncated = size > max_bytes
            result: JsonObject = {
                "skill_name": params.skill_name,
                "relative_path": str(target.relative_to(root)),
                "size": size,
                "sha256": _bounded_sha256(target, size=size),
                "content": text,
                "truncated": truncated,
            }
            self._record_usage("read_skill_asset", params.skill_name, params.relative_path, {"bytes_read": len(text)})
            return {"status": "success", "summary": "skill asset read.", "result": result}
        except Exception as exc:  # noqa: BLE001 - model-facing tool feedback.
            logger.warning("skill asset read failed", exc_info=True)
            return {"status": "failure", "summary": f"read_skill_asset failed: {type(exc).__name__}: {exc}"}

    def _resolve_skill_path(self, *, skill_name: str, relative_path: str) -> tuple[Path, Path]:
        if skill_name not in self._skills:
            available = ", ".join(sorted(self._skills))
            raise ValueError(f"unknown skill {skill_name!r}; available skills: {available}")
        root = self._skills[skill_name]
        if Path(relative_path).is_absolute() or "\x00" in relative_path:
            raise ValueError("skill asset path must be relative")
        target = (root / relative_path).resolve()
        if target != root and root not in target.parents:
            raise ValueError("skill asset path escapes the skill folder")
        return root, target

    def _binary_summary(self, *, skill_name: str, relative_path: str, target: Path, reason: str) -> JsonObject:
        if not self._settings.allow_binary_summary:
            raise ValueError(reason)
        result: JsonObject = {
            "skill_name": skill_name,
            "relative_path": relative_path,
            "size": target.stat().st_size,
            "sha256": _bounded_sha256(target, size=target.stat().st_size),
            "binary_summary": True,
            "reason": reason,
        }
        self._record_usage("read_skill_asset", skill_name, relative_path, {"binary_summary": True})
        return {"status": "success", "summary": "skill asset summarized as binary metadata.", "result": result}

    def _record_usage(self, tool_name: str, skill_name: str, path: str, extra: JsonObject) -> None:
        if self._usage_recorder is None:
            return
        self._usage_recorder({"tool": tool_name, "skill_name": skill_name, "path": path, **extra})


def _format_tool_output(context: ToolFormatterContext) -> str:
    output = context.tool_output
    if isinstance(output, dict):
        return str(output.get("summary") or output.get("status") or output)
    return str(output)


def _contract_tool(
    *,
    name: str,
    description: str,
    params: type[BaseModel],
    implementation: Callable[..., object],
) -> Tool:
    contract = define_tool(
        tool_id=name,
        namespace="agentkit.skills",
        kind=ToolContractKind.SKILL_ASSET,
        placement=ToolPlacement.LOCAL_HANDLER,
        description=description,
        params_model=params,
        side_effect=False,
        readonly=True,
        timeout_seconds=30.0,
    )
    return contract_to_nexau_tool(
        contract,
        implementation=implementation,
        formatter=_format_tool_output,
        execution="sync",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bounded_sha256(path: Path, *, size: int) -> str:
    if size > _MAX_SKILL_ASSET_HASH_BYTES:
        return ""
    return _sha256(path)


def _iter_skill_assets(root: Path, *, limit: int) -> Iterable[Path]:
    remaining = limit
    for current_root, dir_names, file_names in os.walk(root):
        if remaining <= 0:
            break
        dir_names.sort()
        file_names.sort()
        current_path = Path(current_root)
        for dir_name in dir_names:
            if remaining <= 0:
                break
            yield current_path / dir_name
            remaining -= 1
        for file_name in file_names:
            if remaining <= 0:
                break
            yield current_path / file_name
            remaining -= 1


def _read_prefix(path: Path, *, max_bytes: int) -> bytes:
    with path.open("rb") as handle:
        return handle.read(max_bytes)


__all__ = ["AgentKitSkillAssetToolFactory", "ListSkillAssetsParams", "ReadSkillAssetParams"]
