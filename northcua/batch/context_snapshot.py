"""Immutable controller/context configuration snapshots for batch workers."""

from __future__ import annotations

try:
    import fcntl
except ImportError:  # Windows can use local agents; batch controllers require Linux.
    fcntl = None
import hashlib
import json
import os
import shutil
from enum import StrEnum
from pathlib import Path
from typing import cast
from uuid import uuid4

import yaml

from northcua.agentkernel.context.domain.enums import ContextManagementMode
from northcua.batch.contracts import BatchManifest
from northcua.core.config import NorthCUAAppConfig
from northcua_protocol.capabilities import JsonObject

_APP_CONFIG_PATH_KEYS = frozenset({"agent_config_path", "web_agent_config_path"})
_AGENT_CONFIG_PATH_KEYS = frozenset({"config_path", "yaml_path", "skills"})


class _SystemPromptType(StrEnum):
    STRING = "string"
    FILE = "file"
    JINJA = "jinja"

    @property
    def is_path_backed(self) -> bool:
        return self in {self.FILE, self.JINJA}


def freeze_batch_context_configs(manifest: BatchManifest, *, batch_output_dir: Path) -> BatchManifest:
    """Copy app and referenced agent configs and retarget every task to them.

    The returned manifest is the durable runtime snapshot. A resume that loads
    this manifest sees paths inside the batch directory and therefore never
    re-reads mutable operator YAML.
    """

    frozen = manifest.model_copy(deep=True)
    snapshot_root = (batch_output_dir / "config_snapshot").resolve()
    cache: dict[Path, Path] = {}
    records: list[JsonObject] = []
    for task in frozen.tasks:
        raw_path = str(
            task.metadata.get("controller_app_config_path") or task.metadata.get("app_config_path") or ""
        ).strip()
        if not raw_path:
            continue
        source = Path(raw_path).expanduser().resolve()
        if not source.is_file():
            # Preserve legacy dry-run/validation semantics for manifests whose
            # runtime config is intentionally supplied only on the controller.
            continue
        if source.is_relative_to(snapshot_root):
            _validate_persisted_batch_snapshot(task.metadata, source=source, snapshot_root=snapshot_root)
            continue
        if not context_management_enabled(source, agent_kind=task.agent_kind):
            continue
        snapshot_root.mkdir(parents=True, exist_ok=True)
        target = cache.get(source)
        if target is None:
            target, record = _snapshot_app_config(source, snapshot_root=snapshot_root)
            cache[source] = target
            records.append(record)
        task.metadata = {
            **task.metadata,
            "controller_app_config_path": str(target),
            "context_config_snapshot_sha256": _sha256_file(target),
            "context_config_snapshot_graph_sha256": _config_graph_digest(
                target, allowed_path_keys=_APP_CONFIG_PATH_KEYS
            ),
        }
    index_path = snapshot_root / "index.json"
    if records:
        _atomic_write_text(index_path, json.dumps({"version": 1, "configs": records}, indent=2))
    return frozen


def context_management_enabled(source: Path, *, agent_kind: str | None = None) -> bool:
    """Return whether an app config opts any context-management profile in."""

    config = NorthCUAAppConfig.from_yaml(source)
    management = config.context_management
    if agent_kind is not None:
        return management.resolve(agent_kind).mode != ContextManagementMode.OFF
    if management.mode != ContextManagementMode.OFF:
        return True
    return any(
        override.mode is not None and override.mode != ContextManagementMode.OFF
        for override in management.agent_overrides.values()
    )


def _validate_persisted_batch_snapshot(
    metadata: JsonObject,
    *,
    source: Path,
    snapshot_root: Path,
) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"persisted batch config snapshot is missing: {source}")
    expected_file = str(metadata.get("context_config_snapshot_sha256") or "")
    expected_graph = str(metadata.get("context_config_snapshot_graph_sha256") or "")
    actual_file = _sha256_file(source)
    actual_graph = _config_graph_digest(source, allowed_path_keys=_APP_CONFIG_PATH_KEYS)
    if not expected_file or not expected_graph or actual_file != expected_file or actual_graph != expected_graph:
        raise ValueError(f"persisted batch config snapshot failed integrity validation: {source}")
    if not source.is_relative_to(snapshot_root):
        raise ValueError(f"persisted batch config snapshot escaped its batch root: {source}")


def snapshot_controller_app_config(source: Path, *, snapshot_root: Path) -> Path:
    """Freeze a controller-trusted app config across process restarts."""

    if fcntl is None:
        raise RuntimeError("Batch/controller execution requires Linux; local agents support Windows.")
    resolved = source.resolve()
    _validate_controller_config_dependency_graph(resolved)
    if not resolved.is_file():
        # Keep legacy startup validation and error reporting for deployments
        # that mount the config after plugin registration.
        return source
    snapshot_root = snapshot_root.resolve()
    snapshot_root.mkdir(parents=True, exist_ok=True)
    lock_path = snapshot_root / "snapshot.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return _snapshot_controller_app_config_locked(resolved, snapshot_root=snapshot_root)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _validate_controller_config_dependency_graph(source: Path) -> None:
    """Reject unsafe paths throughout a controller configuration graph."""

    seen: set[Path] = set()

    def visit(path: Path, *, allowed_path_keys: frozenset[str]) -> None:
        resolved = _resolve_config_dependency_path(path)
        if resolved in seen:
            return
        seen.add(resolved)
        if resolved.is_dir():
            candidates = tuple(sorted(resolved.rglob("*")))
            symlinks = tuple(candidate for candidate in candidates if candidate.is_symlink())
            if symlinks:
                rendered = ", ".join(str(candidate) for candidate in symlinks)
                raise ValueError(f"controller config dependency directories may not contain symlinks: {rendered}")
            for child in (candidate for candidate in candidates if candidate.is_file()):
                visit(child, allowed_path_keys=_AGENT_CONFIG_PATH_KEYS)
            return
        if not resolved.is_file() or resolved.suffix.lower() not in {".yaml", ".yml"}:
            return
        parsed = yaml.safe_load(resolved.read_text(encoding="utf-8"))
        for dependency in _referenced_paths(
            parsed,
            source_dir=resolved.parent,
            allowed_path_keys=allowed_path_keys,
        ):
            visit(dependency, allowed_path_keys=_AGENT_CONFIG_PATH_KEYS)

    visit(source, allowed_path_keys=_APP_CONFIG_PATH_KEYS)


def validate_published_controller_app_config_snapshot(source: Path) -> bool:
    """Return whether ``source`` is an intact published controller snapshot.

    A controller runner may receive a path already frozen by a higher-level
    experiment. Re-snapshotting that graph changes its semantic path identity
    and weakens parent/child attestation. A matching write-once ``active.json``
    proves the graph was published by :func:`snapshot_controller_app_config`.

    Raises:
        ValueError: If snapshot metadata exists but fails integrity checks.
    """

    resolved = source.expanduser().resolve()
    active_path = resolved.parent.parent / "active.json"
    if not active_path.is_file():
        return False
    active = json.loads(active_path.read_text(encoding="utf-8"))
    if not isinstance(active, dict):
        raise ValueError(f"invalid controller config snapshot pointer: {active_path}")
    target = Path(str(active.get("snapshot_path") or "")).resolve()
    expected_file = str(active.get("snapshot_sha256") or "")
    expected_graph = str(active.get("snapshot_graph_sha256") or "")
    if (
        target != resolved
        or not resolved.is_file()
        or not expected_file
        or not expected_graph
        or _sha256_file(resolved) != expected_file
        or _config_graph_digest(resolved, allowed_path_keys=_APP_CONFIG_PATH_KEYS) != expected_graph
    ):
        raise ValueError(f"controller config snapshot pointer failed integrity validation: {active_path}")
    return True


def _snapshot_controller_app_config_locked(resolved: Path, *, snapshot_root: Path) -> Path:
    active_path = snapshot_root / "active.json"
    refresh = os.getenv("NORTHCUA_REFRESH_CONTROLLER_CONFIG_SNAPSHOT", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if active_path.is_file():
        active = json.loads(active_path.read_text(encoding="utf-8"))
        if not isinstance(active, dict):
            raise ValueError(f"invalid controller config snapshot pointer: {active_path}")
        target = Path(str(active.get("snapshot_path") or "")).resolve()
        digest = str(active.get("snapshot_sha256") or "")
        graph_digest = str(active.get("snapshot_graph_sha256") or "")
        if (
            not target.is_relative_to(snapshot_root)
            or not target.is_file()
            or _sha256_file(target) != digest
            or not graph_digest
            or _config_graph_digest(target, allowed_path_keys=_APP_CONFIG_PATH_KEYS) != graph_digest
        ):
            raise ValueError(f"controller config snapshot pointer failed integrity validation: {active_path}")
        recorded_source = str(active.get("source_path") or "")
        recorded_source_digest = str(active.get("source_graph_sha256") or "")
        current_source_digest = _config_graph_digest(resolved, allowed_path_keys=_APP_CONFIG_PATH_KEYS)
        if recorded_source != str(resolved):
            raise ValueError(
                "controller config snapshot belongs to a different app config path; "
                "use a new artifact root or explicitly rotate the snapshot"
            )
        if recorded_source_digest == current_source_digest:
            return target
        if not refresh:
            raise ValueError(
                "controller app config changed after its immutable snapshot was created; set "
                "NORTHCUA_REFRESH_CONTROLLER_CONFIG_SNAPSHOT=1 for an explicit rollout/rollback"
            )
    target, _record = _snapshot_app_config(resolved, snapshot_root=snapshot_root)
    _atomic_write_text(
        active_path,
        json.dumps(
            {
                "source_path": str(resolved),
                "source_sha256": _sha256_file(resolved),
                "source_graph_sha256": _config_graph_digest(resolved, allowed_path_keys=_APP_CONFIG_PATH_KEYS),
                "snapshot_path": str(target),
                "snapshot_sha256": _sha256_file(target),
                "snapshot_graph_sha256": _config_graph_digest(target, allowed_path_keys=_APP_CONFIG_PATH_KEYS),
            },
            indent=2,
        ),
    )
    return target


def _snapshot_app_config(source: Path, *, snapshot_root: Path) -> tuple[Path, JsonObject]:
    if not source.exists():
        raise FileNotFoundError(f"controller app config does not exist: {source}")
    raw_text = source.read_text(encoding="utf-8")
    parsed = yaml.safe_load(raw_text)
    if not isinstance(parsed, dict):
        raise ValueError(f"controller app config must contain a mapping: {source}")
    source_digest = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    digest = _config_graph_digest(source, allowed_path_keys=_APP_CONFIG_PATH_KEYS)
    config_dir = snapshot_root / digest[:16]
    config_dir.mkdir(parents=True, exist_ok=True)
    target = config_dir / "app.yaml"
    record_path = config_dir / "record.json"
    if target.is_file() and record_path.is_file():
        existing_record = json.loads(record_path.read_text(encoding="utf-8"))
        if not isinstance(existing_record, dict):
            raise ValueError(f"invalid batch config snapshot record: {record_path}")
        return target, cast(JsonObject, existing_record)
    copied: list[JsonObject] = []
    rewritten = _rewrite_referenced_configs(
        cast(JsonObject, parsed),
        source_dir=source.parent,
        target_dir=config_dir,
        copied=copied,
        allowed_path_keys=_APP_CONFIG_PATH_KEYS,
    )
    _atomic_write_text(target, yaml.safe_dump(rewritten, allow_unicode=True, sort_keys=False))
    record: JsonObject = {
        "source_sha256": source_digest,
        "source_graph_sha256": digest,
        "snapshot_path": str(target),
        "snapshot_sha256": _sha256_file(target),
        "referenced_configs": copied,
    }
    _atomic_write_text(record_path, json.dumps(record, indent=2))
    return target, record


def _rewrite_referenced_configs(
    value: object,
    *,
    source_dir: Path,
    target_dir: Path,
    copied: list[JsonObject],
    allowed_path_keys: frozenset[str],
    key: str = "",
) -> object:
    if isinstance(value, dict):
        prompt_type = _system_prompt_type(value) if allowed_path_keys is _AGENT_CONFIG_PATH_KEYS else None
        rewritten: dict[str, object] = {}
        for child_key, child in value.items():
            child_key_text = str(child_key)
            if child_key_text == "system_prompt" and prompt_type is not None:
                rewritten[child_key_text] = _rewrite_system_prompt(
                    child,
                    prompt_type=prompt_type,
                    source_dir=source_dir,
                    target_dir=target_dir,
                    copied=copied,
                )
                continue
            rewritten[child_key_text] = _rewrite_referenced_configs(
                child,
                source_dir=source_dir,
                target_dir=target_dir,
                copied=copied,
                allowed_path_keys=allowed_path_keys,
                key=child_key_text,
            )
        return rewritten
    if isinstance(value, list):
        return [
            _rewrite_referenced_configs(
                child,
                source_dir=source_dir,
                target_dir=target_dir,
                copied=copied,
                allowed_path_keys=allowed_path_keys,
                key=key,
            )
            for child in value
        ]
    if not isinstance(value, str) or not _is_config_path_key(key, allowed_path_keys):
        return value
    source = Path(value).expanduser()
    if not source.is_absolute():
        source = source_dir / source
    source = _resolve_config_dependency_path(source)
    if not source.exists():
        # Preserve optional/unresolved paths so normal config validation reports
        # the same domain-specific error at startup.
        return value
    return _rewrite_dependency_path(source, target_dir=target_dir, copied=copied)


def _system_prompt_type(config: dict[object, object]) -> _SystemPromptType:
    raw = config.get("system_prompt_type", _SystemPromptType.STRING.value)
    try:
        return _SystemPromptType(str(raw))
    except ValueError:
        # Preserve invalid configs for NexAU's domain-specific validator. An
        # unknown type must never be guessed to be a filesystem dependency.
        return _SystemPromptType.STRING


def _rewrite_system_prompt(
    value: object,
    *,
    prompt_type: _SystemPromptType,
    source_dir: Path,
    target_dir: Path,
    copied: list[JsonObject],
) -> object:
    """Snapshot only NexAU path-backed system prompt representations."""

    if not prompt_type.is_path_backed:
        return value
    if isinstance(value, str):
        return _rewrite_optional_dependency(
            value,
            source_dir=source_dir,
            target_dir=target_dir,
            copied=copied,
        )
    if not isinstance(value, list):
        return value
    rewritten: list[object] = []
    for item in value:
        if isinstance(item, str):
            rewritten.append(
                _rewrite_optional_dependency(
                    item,
                    source_dir=source_dir,
                    target_dir=target_dir,
                    copied=copied,
                )
            )
            continue
        if isinstance(item, dict) and isinstance(item.get("content"), str):
            block = dict(item)
            block["content"] = _rewrite_optional_dependency(
                cast(str, item["content"]),
                source_dir=source_dir,
                target_dir=target_dir,
                copied=copied,
            )
            rewritten.append(block)
            continue
        rewritten.append(item)
    return rewritten


def _rewrite_optional_dependency(
    value: str,
    *,
    source_dir: Path,
    target_dir: Path,
    copied: list[JsonObject],
) -> str:
    source = Path(value).expanduser()
    if not source.is_absolute():
        source = source_dir / source
    source = _resolve_config_dependency_path(source)
    if not source.exists():
        # Preserve unresolved paths so normal config validation reports the
        # same domain-specific error at startup.
        return value
    return _rewrite_dependency_path(source, target_dir=target_dir, copied=copied)


def _rewrite_dependency_path(
    source: Path,
    *,
    target_dir: Path,
    copied: list[JsonObject],
) -> str:
    digest = _sha256_path(source)
    target = _snapshot_dependency(source, target_dir=target_dir, copied=copied)
    copied.append({"sha256": digest, "snapshot_path": str(target)})
    return str(target)


def _is_config_path_key(key: str, allowed_path_keys: frozenset[str]) -> bool:
    return key in allowed_path_keys or (
        allowed_path_keys is _APP_CONFIG_PATH_KEYS and key.endswith("_agent_config_path")
    )


def _snapshot_dependency(source: Path, *, target_dir: Path, copied: list[JsonObject]) -> Path:
    if not source.is_file() and not source.is_dir():
        raise ValueError(f"config dependency must be a regular file or directory: {source}")
    digest = _sha256_path(source)
    target = target_dir / f"{digest[:16]}-{source.name}"
    if target.exists():
        return target
    if source.is_dir():
        shutil.copytree(source, target)
        return target
    if source.suffix.lower() not in {".yaml", ".yml"}:
        target.write_bytes(source.read_bytes())
        os.chmod(target, 0o600)
        return target
    parsed = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        target.write_bytes(source.read_bytes())
        os.chmod(target, 0o600)
        return target
    rewritten = _rewrite_referenced_configs(
        cast(JsonObject, parsed),
        source_dir=source.parent,
        target_dir=target_dir,
        copied=copied,
        allowed_path_keys=_AGENT_CONFIG_PATH_KEYS,
    )
    _atomic_write_text(target, yaml.safe_dump(rewritten, allow_unicode=True, sort_keys=False))
    return target


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        payload = text.encode("utf-8")
        offset = 0
        while offset < len(payload):
            offset += os.write(fd, payload[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _sha256_path(path: Path) -> str:
    if path.is_file():
        return _sha256_file(path)
    hasher = hashlib.sha256()
    for child in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        hasher.update(str(child.relative_to(path)).encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(child.read_bytes())
        hasher.update(b"\n")
    return hasher.hexdigest()


def _config_graph_digest(
    source: Path,
    *,
    allowed_path_keys: frozenset[str],
    seen: set[Path] | None = None,
) -> str:
    seen = seen or set()
    source = source.resolve()
    if source in seen:
        return hashlib.sha256(f"cycle:{source}".encode("utf-8")).hexdigest()
    seen.add(source)
    hasher = hashlib.sha256()
    hasher.update(source.read_bytes())
    if source.suffix.lower() not in {".yaml", ".yml"}:
        return hasher.hexdigest()
    parsed = yaml.safe_load(source.read_text(encoding="utf-8"))
    for dependency in _referenced_paths(
        parsed,
        source_dir=source.parent,
        allowed_path_keys=allowed_path_keys,
    ):
        hasher.update(str(dependency).encode("utf-8"))
        hasher.update(b"\0")
        if dependency.is_dir():
            hasher.update(_sha256_path(dependency).encode("ascii"))
        elif dependency.suffix.lower() in {".yaml", ".yml"}:
            hasher.update(
                _config_graph_digest(
                    dependency,
                    allowed_path_keys=_AGENT_CONFIG_PATH_KEYS,
                    seen=seen,
                ).encode("ascii")
            )
        else:
            hasher.update(dependency.read_bytes())
        hasher.update(b"\n")
    return hasher.hexdigest()


def _referenced_paths(
    value: object,
    *,
    source_dir: Path,
    allowed_path_keys: frozenset[str],
    key: str = "",
) -> tuple[Path, ...]:
    paths: list[Path] = []
    if isinstance(value, dict):
        prompt_type = _system_prompt_type(value) if allowed_path_keys is _AGENT_CONFIG_PATH_KEYS else None
        for child_key, child in value.items():
            if str(child_key) == "system_prompt" and prompt_type is not None:
                paths.extend(
                    _system_prompt_referenced_paths(
                        child,
                        prompt_type=prompt_type,
                        source_dir=source_dir,
                    )
                )
                continue
            paths.extend(
                _referenced_paths(
                    child,
                    source_dir=source_dir,
                    allowed_path_keys=allowed_path_keys,
                    key=str(child_key),
                )
            )
    elif isinstance(value, list):
        for child in value:
            paths.extend(
                _referenced_paths(
                    child,
                    source_dir=source_dir,
                    allowed_path_keys=allowed_path_keys,
                    key=key,
                )
            )
    elif isinstance(value, str) and _is_config_path_key(key, allowed_path_keys):
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = source_dir / candidate
        candidate = _resolve_config_dependency_path(candidate)
        if candidate.is_file() or candidate.is_dir():
            paths.append(candidate)
    return tuple(paths)


def _system_prompt_referenced_paths(
    value: object,
    *,
    prompt_type: _SystemPromptType,
    source_dir: Path,
) -> tuple[Path, ...]:
    if not prompt_type.is_path_backed:
        return ()
    raw_paths: list[str] = []
    if isinstance(value, str):
        raw_paths.append(value)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                raw_paths.append(item)
            elif isinstance(item, dict) and isinstance(item.get("content"), str):
                raw_paths.append(cast(str, item["content"]))
    resolved: list[Path] = []
    for raw_path in raw_paths:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = source_dir / candidate
        candidate = _resolve_config_dependency_path(candidate)
        if candidate.is_file() or candidate.is_dir():
            resolved.append(candidate)
    return tuple(resolved)


def _resolve_config_dependency_path(path: Path) -> Path:
    """Resolve a config dependency only after rejecting symlink components."""

    absolute = Path(os.path.abspath(path.expanduser()))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"controller config dependencies may not contain symlinks: {current}")
    return absolute.resolve()
