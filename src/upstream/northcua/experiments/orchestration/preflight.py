"""Preflight checks for real E2B experiment runs."""

from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlparse

from northcua.experiments.contracts import ExperimentControllerSpec, ExperimentSpec


class ExperimentPreflight:
    """Validate controller reachability and explicit live-run intent."""

    def validate_public_controller_url(self, spec: ExperimentSpec, *, public_url: str) -> None:
        """Reject controller URLs that E2B sandboxes cannot reach.

        Args:
            spec: Experiment spec being run.
            public_url: Effective controller URL after CLI overrides.

        Raises:
            ValueError: If the URL is missing, not websocket based, or points at
                a local/private host while the sandbox provider is not fake.
        """

        self.validate_provider_public_controller_url(
            provider=spec.sandbox.provider,
            public_url=public_url,
            run_label="real E2B experiments",
        )

    def validate_provider_public_controller_url(self, *, provider: str, public_url: str, run_label: str) -> None:
        """Reject public controller URLs that a real provider cannot reach.

        Args:
            provider: Effective sandbox provider id.
            public_url: Controller URL expected to be reachable from sandboxes.
            run_label: Human-readable label for error messages.

        Raises:
            ValueError: If a real provider cannot use the supplied URL.
        """

        if provider == "fake":
            return
        if not public_url:
            raise ValueError(f"{run_label} require --public-controller-url")
        parsed = urlparse(public_url)
        allow_insecure_ws = os.getenv("NORTHCUA_ALLOW_INSECURE_PUBLIC_CONTROLLER_WS", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        if parsed.scheme == "ws" and allow_insecure_ws:
            pass
        elif parsed.scheme != "wss":
            raise ValueError(f"{run_label} require a wss:// public controller URL")
        host = parsed.hostname or ""
        if not host:
            raise ValueError("public controller URL must include a host")
        if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
            raise ValueError("public controller URL must be reachable from E2B, not localhost")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return
        if address.is_private or address.is_loopback or address.is_link_local:
            raise ValueError("public controller URL cannot use a private or loopback IP for real E2B")

    def validate_live_gate(self, spec: ExperimentSpec, *, require_real_e2b: bool) -> None:
        """Require an explicit environment flag before real E2B creation.

        Args:
            spec: Experiment spec being run.
            require_real_e2b: Runtime flag set by live commands.

        Raises:
            RuntimeError: If a real E2B run lacks the opt-in environment flag.
        """

        self.validate_provider_live_gate(
            provider=spec.sandbox.provider,
            require_real_e2b=require_real_e2b,
            env_name="NORTHCUA_ALLOW_LIVE_SANDBOXES",
            run_label="real E2B sandboxes",
        )

    def validate_provider_live_gate(
        self,
        *,
        provider: str,
        require_real_e2b: bool,
        env_name: str,
        run_label: str,
    ) -> None:
        """Require an explicit environment flag before real provider creation.

        Args:
            provider: Effective sandbox provider id.
            require_real_e2b: Runtime flag indicating live creation is possible.
            env_name: Environment variable that must be set to ``1``.
            run_label: Human-readable label for error messages.

        Raises:
            RuntimeError: If a live provider run lacks the opt-in flag.
        """

        if provider == "fake" or not require_real_e2b:
            return
        if os.getenv(env_name) != "1":
            raise RuntimeError(f"set {env_name}=1 before creating {run_label}")

    def validate_controller_auth(self, spec: ExperimentSpec) -> None:
        """Require token auth for public real-provider controllers.

        Args:
            spec: Experiment spec being run.

        Raises:
            ValueError: If real E2B would expose an unauthenticated controller.
        """

        self.validate_provider_controller_auth(
            provider=spec.sandbox.provider,
            controller=spec.controller,
            run_label="real E2B experiments",
        )

    def validate_provider_controller_auth(
        self,
        *,
        provider: str,
        controller: ExperimentControllerSpec,
        run_label: str,
    ) -> None:
        """Require token auth for public real-provider controllers.

        Args:
            provider: Effective sandbox provider id.
            controller: Controller configuration to validate.
            run_label: Human-readable label for error messages.

        Raises:
            ValueError: If a real provider would use an unauthenticated controller.
        """

        if provider == "fake":
            return
        if controller.insecure_dev_no_auth:
            raise ValueError(f"{run_label} cannot use insecure_dev_no_auth")
        if not (controller.token or os.getenv("NORTHCUA_CONTROLLER_TOKEN")):
            raise ValueError(f"{run_label} require controller.token or NORTHCUA_CONTROLLER_TOKEN")
