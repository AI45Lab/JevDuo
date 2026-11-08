"""Controller lifecycle helpers for case-suite execution."""

from __future__ import annotations

import os
from pathlib import Path

from northcua.experiments.cases.contracts import CaseSuiteSpec
from northcua.experiments.orchestration.controller import ManagedController, ManagedControllerProcess


class CaseControllerRuntime:
    """Resolve controller paths, credentials, and optional managed process."""

    def __init__(self, *, controller_process: ManagedControllerProcess | None = None) -> None:
        self._controller_process = controller_process or ManagedControllerProcess()

    def effective_spec(
        self,
        spec: CaseSuiteSpec,
        *,
        public_controller_url: str,
        controller_token: str = "",
    ) -> CaseSuiteSpec:
        """Apply CLI/runtime controller overrides and local defaults.

        Args:
            spec: Case suite spec to adjust.
            public_controller_url: Optional CLI-provided public URL.
            controller_token: Optional runtime-only token supplied by a caller
                that intentionally keeps credentials out of persisted specs.

        Returns:
            Suite spec with effective controller URL and token.
        """

        if public_controller_url:
            spec = spec.model_copy(
                update={"controller": spec.controller.model_copy(update={"public_url": public_controller_url})}
            )
        return self.with_effective_controller_token(spec, controller_token=controller_token)

    def with_managed_controller_paths(self, spec: CaseSuiteSpec, *, suite_dir: Path) -> CaseSuiteSpec:
        """Fill managed-controller storage paths under the suite output root.

        Args:
            spec: Case suite spec to adjust.
            suite_dir: Run output directory that owns controller state.

        Returns:
            Suite spec with managed-controller storage paths populated.
        """

        if not spec.controller.managed_local:
            return spec
        controller = spec.controller.model_copy(
            update={
                "db_path": spec.controller.db_path or suite_dir / "controller.sqlite",
                "blob_dir": spec.controller.blob_dir or suite_dir / "controller-blobs",
                "artifact_root_dir": spec.controller.artifact_root_dir or suite_dir / "agent-artifacts",
            }
        )
        return spec.model_copy(update={"controller": controller})

    def with_effective_controller_token(self, spec: CaseSuiteSpec, *, controller_token: str = "") -> CaseSuiteSpec:
        """Resolve the controller token from environment without persisting it.

        Args:
            spec: Case suite spec to adjust.

            controller_token: Optional runtime-only token override.

        Returns:
            Suite spec with token copied from NORTHCUA_CONTROLLER_TOKEN when set.
        """

        if spec.controller.token:
            return spec
        if controller_token:
            return spec.model_copy(
                update={"controller": spec.controller.model_copy(update={"token": controller_token})}
            )
        token = os.getenv("NORTHCUA_CONTROLLER_TOKEN", "")
        if not token:
            return spec
        return spec.model_copy(update={"controller": spec.controller.model_copy(update={"token": token})})

    def spec_for_persistence(self, spec: CaseSuiteSpec) -> CaseSuiteSpec:
        """Return a report-safe spec copy with credentials redacted.

        Args:
            spec: Effective suite spec that may contain credentials.

        Returns:
            Suite spec safe to persist into run artifacts.
        """

        if not spec.controller.token:
            return spec
        return spec.model_copy(update={"controller": spec.controller.model_copy(update={"token": ""})})

    async def start(
        self,
        spec: CaseSuiteSpec,
        *,
        suite_dir: Path,
        dry_run: bool,
        has_real_provider: bool,
    ) -> ManagedController | None:
        """Start a managed local controller when the suite requests one.

        Args:
            spec: Effective case suite spec.
            suite_dir: Run output directory for controller state.
            dry_run: Whether this is a planning-only run.
            has_real_provider: Whether any task uses a non-fake provider.

        Returns:
            Managed controller handle, or None when no controller is needed.
        """

        if dry_run or not has_real_provider or not spec.controller.managed_local:
            return None
        return await self._controller_process.start(spec.controller, output_dir=suite_dir)
