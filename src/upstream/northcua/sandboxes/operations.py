"""Application operations backing sandbox CLI commands."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from northcua.experiments.cases.catalog import CaseCatalogLoader
from northcua.experiments.orchestration.preflight import ExperimentPreflight
from northcua.sandboxes.contracts import (
    SandboxCapacityProbeRequest,
    SandboxCapacityProbeResult,
    SandboxCommandResult,
    SandboxProviderSpec,
)
from northcua.sandboxes.provider.registry import SandboxProviderRegistry, default_sandbox_provider_registry
from northcua.sandboxes.template.doctor import SandboxTemplateDoctor, SandboxTemplateDoctorReport
from northcua.sandboxes.template.loader import build_template_from_file
from northcua.sandboxes.tool_contracts import SandboxProviderDispatcher


class SandboxProviderInfo(BaseModel):
    """Provider features presented by ``northcua sandbox list``.

    Attributes:
        provider: Registered provider id.
        features: Sorted feature names advertised by the provider.
    """

    provider: str
    features: list[str]


class SandboxOperations:
    """Product-facing sandbox operations independent from argparse."""

    def __init__(self, registry: SandboxProviderRegistry | None = None) -> None:
        self._registry = registry or default_sandbox_provider_registry()

    def list_providers(self) -> list[SandboxProviderInfo]:
        """Return registered providers and their advertised capabilities.

        Returns:
            Provider ids and feature names suitable for JSON CLI output.
        """

        return [
            SandboxProviderInfo(
                provider=provider_id,
                features=sorted(
                    feature.value for feature in self._registry.create(provider_id).capabilities().features
                ),
            )
            for provider_id in self._registry.provider_ids()
        ]

    async def build_template(self, spec_path: str | Path) -> str:
        """Build a provider template from a typed YAML spec.

        Args:
            spec_path: YAML template spec path.

        Returns:
            Provider template alias or id returned by the backend.
        """

        return await build_template_from_file(spec_path, self._registry)

    async def probe(self, *, provider_id: str, template: str, desktop: bool) -> SandboxCommandResult:
        """Run a provider smoke probe.

        Args:
            provider_id: Registered provider id to probe.
            template: Provider template alias to create or connect.
            desktop: Whether to use desktop-capable provider APIs.

        Returns:
            Probe command result from the provider.
        """

        provider = self._registry.create(provider_id)
        return await SandboxProviderDispatcher(provider).probe(
            SandboxProviderSpec(
                provider=provider_id,
                template=template,
                desktop=desktop,
            )
        )

    async def doctor(
        self,
        *,
        provider_id: str,
        template: str,
        desktop: bool,
        checked_binaries: list[str] | None = None,
        python_executable: str = "python3.12",
        catalog_path: str | Path = "",
        template_profile: str = "",
    ) -> SandboxTemplateDoctorReport:
        """Run a live template doctor probe in a short-lived sandbox.

        Args:
            provider_id: Registered provider id to create.
            template: Template alias to probe.
            desktop: Whether the provider should expose desktop APIs.
            checked_binaries: Optional explicit binaries to verify.
            python_executable: Python executable used for import probes.
            catalog_path: Optional catalog path used to derive binaries.
            template_profile: Optional catalog template profile id filter.

        Returns:
            Structured template doctor report.
        """

        ExperimentPreflight().validate_provider_live_gate(
            provider=provider_id,
            require_real_e2b=True,
            env_name="NORTHCUA_RUN_E2B_BATCH",
            run_label="sandbox template doctor sandboxes",
        )
        provider = self._registry.create(provider_id)
        dispatcher = SandboxProviderDispatcher(provider)
        spec = SandboxProviderSpec(provider=provider_id, template=template, desktop=desktop)
        await dispatcher.preflight(spec)
        if checked_binaries is None and catalog_path:
            checked_binaries = _doctor_binaries_from_catalog(
                catalog_path=Path(catalog_path),
                template=template,
                template_profile=template_profile,
            )
        return await SandboxTemplateDoctor(checked_binaries=checked_binaries, python_executable=python_executable).run(
            provider=provider,
            spec=spec,
        )

    async def capacity_probe(self, request: SandboxCapacityProbeRequest) -> SandboxCapacityProbeResult:
        """Run a passive provider capacity probe.

        Args:
            request: Probe request with provider, template, and configured
                capacity knobs.

        Returns:
            Passive capacity summary without creating sandbox resources.
        """

        provider = self._registry.create(request.provider)
        return await SandboxProviderDispatcher(provider).capacity_probe(request)


def _doctor_binaries_from_catalog(*, catalog_path: Path, template: str, template_profile: str) -> list[str]:
    catalog = CaseCatalogLoader().load(catalog_path)
    selected = None
    for profile in catalog.templates.values():
        if (template_profile and profile.profile_id.value == template_profile) or (
            not template_profile and profile.alias == template
        ):
            selected = profile
            break
    if selected is None:
        raise ValueError(
            f"template profile not found in catalog for template={template!r} profile={template_profile!r}"
        )
    return selected.required_binaries
