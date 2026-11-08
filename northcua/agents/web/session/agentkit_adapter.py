"""AgentKit session adapter for device-side web-agent browser sessions."""

from __future__ import annotations

import os

from pathlib import Path

from northcua.agentinfra.runtime.artifacts import ArtifactStoreFactory
from northcua.agentkit.session_context import AGENTKIT_SETTINGS_PARAM, sanitize_agentkit_settings_side_channel
from northcua.agentkit.spec import SessionAdapter
from northcua.core.config import ArtifactConfig, WebAgentConfig
from northcua_protocol.payloads import CommandRequest

from ..artifacts import WebArtifactStore
from ..backend import build_playwright_web_backend
from ..observation.ref_alias import WebObservationRefAliases, WebRefAlias
from .models import RegisteredWebAgentSession
from .registry import WebAgentSessionRegistry


class WebAgentDeviceSessionAdapter(SessionAdapter):
    """Ensure device-side web-agent MCP tools have a registered browser backend."""

    def ensure_device_session(self, *, command: object, artifact_root: Path) -> None:
        """Register a web action backend session when the device has not seen it.

        Args:
            command: Controller command carrying session id and optional
                AgentKit settings.
            artifact_root: Device-side root directory for browser artifacts.
        """

        if not isinstance(command, CommandRequest):
            return
        registry = WebAgentSessionRegistry()
        safe_settings = _safe_settings(command)
        try:
            session = registry.get_registered_session(command.session_id)
            _apply_ref_aliases(session, safe_settings)
            return
        except LookupError:
            pass
        settings = WebAgentConfig.model_validate(_web_config_settings(safe_settings))
        settings.browser_executable_path = os.getenv("NORTHCUA_BROWSER_EXECUTABLE") or None
        artifact_store = ArtifactStoreFactory.create_session_store(
            WebArtifactStore,
            root_dir=artifact_root,
            session_id=command.session_id,
            config=ArtifactConfig(),
        )
        registry.register(
            RegisteredWebAgentSession(
                session_id=command.session_id,
                client=build_playwright_web_backend(config=settings, session_id=command.session_id),
                artifact_store=artifact_store,
                settings=settings,
            )
        )
        _apply_ref_aliases(registry.get_registered_session(command.session_id), safe_settings)

    async def close_device_session(self, *, command: object, artifact_root: Path) -> dict[str, object]:
        """Close and unregister a device-side web browser session.

        Args:
            command: Controller command carrying the session id to close.
            artifact_root: Device artifact root, unused because the session
                already owns its artifact store.

        Returns:
            Small protocol-safe status object for the controller command.
        """

        del artifact_root
        if not isinstance(command, CommandRequest):
            return {"status": "ok", "text": "closed web session"}
        registry = WebAgentSessionRegistry()
        try:
            session = registry.get_registered_session(command.session_id)
        except LookupError:
            return {"status": "ok", "text": "closed web session"}
        try:
            (await session.client.close()).require_success()
        finally:
            registry.unregister(command.session_id)
        return {"status": "ok", "text": "closed web session"}


def _safe_settings(command: CommandRequest) -> dict[str, object]:
    raw_settings = command.parameters.get(AGENTKIT_SETTINGS_PARAM)
    safe_settings = sanitize_agentkit_settings_side_channel(raw_settings) if raw_settings is not None else {}
    return dict(safe_settings)


def _web_config_settings(safe_settings: dict[str, object]) -> dict[str, object]:
    allowed = set(WebAgentConfig.model_fields)
    return {key: value for key, value in safe_settings.items() if key in allowed}


def _apply_ref_aliases(session: RegisteredWebAgentSession, safe_settings: dict[str, object]) -> None:
    raw_aliases = safe_settings.get("ref_aliases")
    if not isinstance(raw_aliases, list):
        return
    aliases: list[WebRefAlias] = []
    for raw_alias in raw_aliases:
        if not isinstance(raw_alias, dict):
            continue
        try:
            aliases.append(WebRefAlias.model_validate(raw_alias))
        except Exception:
            continue
    session.apply_external_ref_aliases(
        WebObservationRefAliases(
            observation_id=str(safe_settings.get("ref_alias_observation_id") or ""),
            alias_epoch=_int_value(safe_settings.get("ref_alias_epoch")),
            aliases=aliases,
        )
    )


def _int_value(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(value))
        except ValueError:
            return 0
    return 0
