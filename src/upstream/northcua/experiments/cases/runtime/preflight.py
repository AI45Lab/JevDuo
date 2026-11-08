"""Preflight checks for reusable E2B case-suite runs."""

from __future__ import annotations

from northcua.batch.contracts import BatchManifest
from northcua.experiments.cases.contracts import CaseSuiteSpec
from northcua.experiments.orchestration.preflight import ExperimentPreflight

_LIVE_E2B_ENV = "NORTHCUA_RUN_E2B_BATCH"


class CaseRunPreflight:
    """Validate controller and live-provider settings before side effects."""

    def __init__(self, *, experiment_preflight: ExperimentPreflight | None = None) -> None:
        self._preflight = experiment_preflight or ExperimentPreflight()

    def validate(self, spec: CaseSuiteSpec, *, manifest: BatchManifest, public_url: str, dry_run: bool) -> None:
        """Run all preflight checks for a case suite.

        Args:
            spec: Case suite spec with controller settings.
            manifest: Materialized batch manifest to inspect for providers.
            public_url: Public controller URL expected by real sandboxes.
            dry_run: Whether provider checks should be skipped.
        """

        if dry_run:
            return
        require_real_e2b = not dry_run
        for provider in sorted(self.effective_providers(manifest)):
            self._preflight.validate_provider_public_controller_url(
                provider=provider,
                public_url=public_url,
                run_label="real case runs",
            )
            self._preflight.validate_provider_controller_auth(
                provider=provider,
                controller=spec.controller,
                run_label="real case runs",
            )
            self._preflight.validate_provider_live_gate(
                provider=provider,
                require_real_e2b=require_real_e2b,
                env_name=_LIVE_E2B_ENV,
                run_label="real case sandboxes",
            )

    @staticmethod
    def effective_providers(manifest: BatchManifest) -> set[str]:
        """Return providers that will actually be used by manifest tasks.

        Args:
            manifest: Batch manifest with defaults and per-task overrides.

        Returns:
            Provider ids after applying per-task sandbox overrides.
        """

        return {(task.sandbox or manifest.defaults.sandbox).provider for task in manifest.tasks}

    @classmethod
    def has_real_provider(cls, manifest: BatchManifest) -> bool:
        """Return whether any effective task provider is non-fake.

        Args:
            manifest: Batch manifest with defaults and per-task overrides.

        Returns:
            True when any task uses a provider other than fake.
        """

        return any(provider != "fake" for provider in cls.effective_providers(manifest))
