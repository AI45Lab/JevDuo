"""Template spec loading and provider build orchestration."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from northcua.sandboxes.contracts import (
    SandboxImageLayer,
    SandboxProviderSpec,
    SandboxTemplateLayer,
    SandboxTemplateSpec,
)
from northcua.sandboxes.provider.registry import SandboxProviderRegistry
from northcua.sandboxes.tool_contracts import SandboxProviderDispatcher

RUNTIME_SOURCE_DESTINATION = "/opt/northcua/source"
RUNTIME_SOURCE_ENV = "NORTHCUA_TEMPLATE_RUNTIME_SOURCE"
RUNTIME_REF_ENV = "NORTHCUA_TEMPLATE_RUNTIME_REF"
RUNTIME_ARCHIVE_URL_ENV = "NORTHCUA_TEMPLATE_RUNTIME_ARCHIVE_URL"
RUNTIME_ARCHIVE_SHA256_ENV = "NORTHCUA_TEMPLATE_RUNTIME_ARCHIVE_SHA256"
RUNTIME_ARCHIVE_STRIP_COMPONENTS_ENV = "NORTHCUA_TEMPLATE_RUNTIME_ARCHIVE_STRIP_COMPONENTS"


def load_template_spec(path: str | Path) -> SandboxTemplateSpec:
    """Load a sandbox template spec from YAML.

    Args:
        path: YAML file path.

    Returns:
        Validated template spec.
    """

    raw = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8")) or {}
    return SandboxTemplateSpec.model_validate(raw)


async def build_template_from_file(path: str | Path, registry: SandboxProviderRegistry) -> str:
    """Build a sandbox template using the provider named in a YAML spec.

    Args:
        path: YAML template spec path.
        registry: Provider registry used to resolve the builder.

    Returns:
        Built provider template alias.
    """

    spec_path = Path(path).expanduser().resolve()
    spec = _apply_runtime_source_overrides(load_template_spec(spec_path))
    spec = _resolve_copy_sources(spec, spec_path=spec_path)
    provider = registry.create(spec.provider)
    dispatcher = SandboxProviderDispatcher(provider)
    await dispatcher.preflight(
        SandboxProviderSpec(
            provider=spec.provider,
            template=spec.alias,
            desktop=spec.desktop,
            resources=spec.resources,
        )
    )
    return await dispatcher.build_template(spec)


def _apply_runtime_source_overrides(spec: SandboxTemplateSpec) -> SandboxTemplateSpec:
    """Apply build-time runtime source overrides without mutating YAML specs.

    Operators can point template builds at a branch/tag from CI while keeping
    the checked-in template catalog stable. Overrides only apply to the
    canonical NorthCUA runtime source destination, not arbitrary git layers.
    """

    archive_url = os.getenv(RUNTIME_ARCHIVE_URL_ENV, "")
    source_override = os.getenv(RUNTIME_SOURCE_ENV, "")
    ref_override = os.getenv(RUNTIME_REF_ENV, "")
    if not archive_url and not source_override and not ref_override:
        return spec

    layers = [
        _override_runtime_source_template_layer(
            layer,
            archive_url=archive_url,
            source_override=source_override,
            ref_override=ref_override,
        )
        for layer in spec.layers
    ]
    image = None
    if spec.image is not None:
        image = spec.image.model_copy(
            update={
                "layers": [
                    _override_runtime_source_image_layer(
                        layer,
                        archive_url=archive_url,
                        source_override=source_override,
                        ref_override=ref_override,
                    )
                    for layer in spec.image.layers
                ]
            }
        )
    return spec.model_copy(update={"layers": layers, "image": image})


def _override_runtime_source_template_layer(
    layer: SandboxTemplateLayer,
    *,
    archive_url: str,
    source_override: str,
    ref_override: str,
) -> SandboxTemplateLayer:
    if layer.kind not in {"git", "archive"} or layer.destination != RUNTIME_SOURCE_DESTINATION:
        return layer
    if archive_url:
        return _runtime_archive_template_layer()
    payload = layer.model_dump()
    if source_override:
        payload["source"] = source_override
    if ref_override:
        payload["ref"] = ref_override
    return SandboxTemplateLayer.model_validate(payload)


def _override_runtime_source_image_layer(
    layer: SandboxImageLayer,
    *,
    archive_url: str,
    source_override: str,
    ref_override: str,
) -> SandboxImageLayer:
    if layer.kind not in {"git", "archive"} or layer.destination != RUNTIME_SOURCE_DESTINATION:
        return layer
    if archive_url:
        return _runtime_archive_image_layer()
    payload = layer.model_dump()
    if source_override:
        payload["source"] = source_override
    if ref_override:
        payload["ref"] = ref_override
    return SandboxImageLayer.model_validate(payload)


def _runtime_archive_template_layer() -> SandboxTemplateLayer:
    return SandboxTemplateLayer.model_validate(_runtime_archive_payload())


def _runtime_archive_image_layer() -> SandboxImageLayer:
    return SandboxImageLayer.model_validate(_runtime_archive_payload())


def _runtime_archive_payload() -> dict[str, object]:
    strip_components = int(os.getenv(RUNTIME_ARCHIVE_STRIP_COMPONENTS_ENV, "1"))
    return {
        "kind": "archive",
        "source_env": RUNTIME_ARCHIVE_URL_ENV,
        "destination": RUNTIME_SOURCE_DESTINATION,
        "sha256": os.getenv(RUNTIME_ARCHIVE_SHA256_ENV, ""),
        "strip_components": strip_components,
    }


def _resolve_copy_sources(spec: SandboxTemplateSpec, *, spec_path: Path) -> SandboxTemplateSpec:
    """Resolve copy-layer sources before provider build submission."""

    repo_root = Path(__file__).resolve().parents[3]
    build_context = _resolve_build_context(spec, repo_root=repo_root)
    resolved_layers = _resolve_template_copy_layers(
        spec.layers,
        spec_path=spec_path,
        repo_root=repo_root,
        build_context=build_context,
        provider=spec.provider,
    )
    image = None
    if spec.image is not None:
        image = spec.image.model_copy(
            update={
                "layers": _resolve_image_copy_layers(
                    spec.image.layers,
                    spec_path=spec_path,
                    repo_root=repo_root,
                    build_context=build_context,
                    provider=spec.provider,
                )
            }
        )
    return spec.model_copy(update={"layers": resolved_layers, "image": image})


def _resolve_build_context(spec: SandboxTemplateSpec, *, repo_root: Path) -> Path:
    """Resolve the context used by provider copy layers."""

    if not spec.build_context_env:
        return repo_root
    raw_context = os.getenv(spec.build_context_env, "")
    if not raw_context:
        raise RuntimeError(f"template build context environment variable is unset: {spec.build_context_env}")
    build_context = Path(raw_context).expanduser().resolve()
    if not build_context.is_dir():
        raise RuntimeError(f"template build context is not a directory: {build_context}")
    return build_context


def _resolve_template_copy_layers(
    layers: list[SandboxTemplateLayer],
    *,
    spec_path: Path,
    repo_root: Path,
    build_context: Path,
    provider: str,
) -> list[SandboxTemplateLayer]:
    resolved_layers: list[SandboxTemplateLayer] = []
    for layer in layers:
        if layer.kind != "copy":
            resolved_layers.append(layer)
            continue
        files = _resolve_copy_files(
            layer.files,
            spec_path=spec_path,
            repo_root=repo_root,
            build_context=build_context,
            provider=provider,
        )
        resolved_layers.append(layer.model_copy(update={"files": files}))
    return resolved_layers


def _resolve_image_copy_layers(
    layers: list[SandboxImageLayer],
    *,
    spec_path: Path,
    repo_root: Path,
    build_context: Path,
    provider: str,
) -> list[SandboxImageLayer]:
    resolved_layers: list[SandboxImageLayer] = []
    for layer in layers:
        if layer.kind != "copy":
            resolved_layers.append(layer)
            continue
        files = _resolve_copy_files(
            layer.files,
            spec_path=spec_path,
            repo_root=repo_root,
            build_context=build_context,
            provider=provider,
        )
        resolved_layers.append(layer.model_copy(update={"files": files}))
    return resolved_layers


def _resolve_copy_files(
    files: dict[str, str],
    *,
    spec_path: Path,
    repo_root: Path,
    build_context: Path,
    provider: str,
) -> dict[str, str]:
    resolved_files: dict[str, str] = {}
    for local, remote in files.items():
        if not remote.startswith("/"):
            raise ValueError(f"template copy destination must be absolute: {remote}")
        resolved = _resolve_copy_source(
            local,
            spec_path=spec_path,
            repo_root=repo_root,
            build_context=build_context,
        )
        if resolved.is_relative_to(build_context):
            resolved_files[resolved.relative_to(build_context).as_posix()] = remote
            continue
        if provider == "e2b":
            raise ValueError(f"E2B template copy sources must live inside the configured build context: {resolved}")
        resolved_files[str(resolved)] = remote
    return resolved_files


def _resolve_copy_source(local: str, *, spec_path: Path, repo_root: Path, build_context: Path) -> Path:
    local_path = Path(local).expanduser()
    if local_path.is_absolute():
        candidates = [local_path]
    elif build_context != repo_root:
        candidates = [build_context / local_path]
    else:
        candidates = [spec_path.parent / local_path, repo_root / local_path]
    resolved = next((candidate.resolve() for candidate in candidates if candidate.exists()), None)
    if resolved is None:
        raise FileNotFoundError(f"template copy source not found: {local}")
    return resolved
