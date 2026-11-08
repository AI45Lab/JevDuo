"""Controller-side remote web action backend for the web agent."""

from __future__ import annotations

from collections.abc import Mapping

from northcua.agentkit.controller_dispatch import command_result_record, normalize_remote_tool_payload
from northcua.agentkit.session_context import AGENTKIT_SETTINGS_PARAM, sanitize_agentkit_settings_side_channel
from northcua.agents.web.backend import WebActionCommandResult
from northcua.agents.web.domain.actions import WebMouseButton, WebScrollDirection, WebWaitParams, canonical_web_ref
from northcua.agents.web.domain.observations import WebScreenshotObservation, WebSnapshotObservation
from northcua.agents.web.observation.normalizer import WebScreenshotNormalizer, WebSnapshotNormalizer
from northcua.controller.execution.remote_dispatcher import RemoteCommandDispatcher
from northcua.core.enums import ResultStatus
from northcua.core.json_types import JsonObject


def _mouse_button_value(button: WebMouseButton | str) -> str:
    return button.value if isinstance(button, WebMouseButton) else str(button)


def _remote_web_ref(ref: str) -> str:
    canonical = canonical_web_ref(ref)
    if canonical.startswith("@") or canonical.isdigit():
        return canonical
    return f"@{canonical}"


class RemoteWebActionBackend:
    """Backend-neutral web action backend backed by controller remote commands."""

    def __init__(
        self,
        dispatcher: RemoteCommandDispatcher,
        *,
        timeout_seconds: float = 30.0,
        web_settings: Mapping[str, object] | None = None,
    ) -> None:
        self.dispatcher = dispatcher
        self.timeout_seconds = timeout_seconds
        self.web_settings = sanitize_agentkit_settings_side_channel(dict(web_settings or {}))
        self.snapshot_normalizer = WebSnapshotNormalizer()
        self.screenshot_normalizer = WebScreenshotNormalizer()

    async def open(self, url: str) -> WebActionCommandResult:
        """Open a URL on the remote browser session.

        Args:
            url: URL already validated by the controller-side web runner.

        Returns:
            backend-neutral command result from the remote device.
        """

        return await self._dispatch("web.web_agent_backend_open", {"url": url})

    async def snapshot(
        self,
        *,
        interactive: bool | None = None,
        compact: bool | None = None,
        depth: int | None = None,
    ) -> WebSnapshotObservation:
        """Collect and normalize a remote DOM snapshot.

        Args:
            interactive: Protocol-level snapshot hint; remote Web snapshots
                currently use the device-side default.
            compact: Protocol-level compaction hint; remote Web snapshots
                currently use the device-side default.
            depth: Protocol-level depth hint; remote Web snapshots currently use
                the device-side default.

        Returns:
            Typed snapshot observation normalized from remote tool output.
        """

        del interactive, compact, depth
        # Use the runtime-only probe, not the model-facing action tool. The
        # model-facing web_snapshot response intentionally contains projected
        # numeric refs; feeding that back into the controller-side backend would
        # turn source refs into @1/@2 and break subsequent ref actions.
        return self.snapshot_normalizer.normalize(await self._dispatch_payload("web.web_agent_snapshot", {}))

    async def click_ref(self, ref: str) -> WebActionCommandResult:
        """Click a remote DOM ref.

        Args:
            ref: DOM/snapshot reference selected by the model.

        Returns:
            backend-neutral command result from the remote device.
        """

        return await self._dispatch("web.web_agent_backend_click_ref", {"ref": _remote_web_ref(ref)})

    async def hover_ref(self, ref: str) -> WebActionCommandResult:
        """Hover a remote DOM ref."""

        return await self._dispatch("web.web_agent_backend_hover_ref", {"ref": _remote_web_ref(ref)})

    async def fill_ref(self, ref: str, text: str) -> WebActionCommandResult:
        """Fill a remote DOM ref.

        Args:
            ref: DOM/snapshot reference for a fillable element.
            text: Text to set in the element.

        Returns:
            backend-neutral command result from the remote device.
        """

        return await self._dispatch("web.web_agent_backend_fill_ref", {"ref": _remote_web_ref(ref), "text": text})

    async def select_option_ref(self, ref: str, values: list[str]) -> WebActionCommandResult:
        """Select one or more option values on a remote DOM ref."""

        return await self._dispatch(
            "web.web_agent_backend_select_option_ref", {"ref": _remote_web_ref(ref), "values": values}
        )

    async def check_ref(self, ref: str) -> WebActionCommandResult:
        """Check a remote checkbox or radio DOM ref."""

        return await self._dispatch("web.web_agent_backend_check_ref", {"ref": _remote_web_ref(ref)})

    async def uncheck_ref(self, ref: str) -> WebActionCommandResult:
        """Uncheck a remote checkbox DOM ref."""

        return await self._dispatch("web.web_agent_backend_uncheck_ref", {"ref": _remote_web_ref(ref)})

    async def drag_ref(self, from_ref: str, to_ref: str) -> WebActionCommandResult:
        """Drag one remote DOM ref onto another."""

        return await self._dispatch(
            "web.web_agent_backend_drag_ref",
            {"from_ref": _remote_web_ref(from_ref), "to_ref": _remote_web_ref(to_ref)},
        )

    async def click_point(self, x: float, y: float, button: WebMouseButton | str) -> WebActionCommandResult:
        """Click viewport CSS coordinates in the remote browser."""

        return await self._dispatch(
            "web.web_agent_backend_click_point", {"x": x, "y": y, "button": _mouse_button_value(button)}
        )

    async def double_click_point(self, x: float, y: float, button: WebMouseButton | str) -> WebActionCommandResult:
        """Double-click viewport CSS coordinates in the remote browser."""

        return await self._dispatch(
            "web.web_agent_backend_double_click_point",
            {"x": x, "y": y, "button": _mouse_button_value(button)},
        )

    async def drag_point(self, from_x: float, from_y: float, to_x: float, to_y: float) -> WebActionCommandResult:
        """Drag between viewport CSS coordinates in the remote browser."""

        return await self._dispatch(
            "web.web_agent_backend_drag_point",
            {"from_x": from_x, "from_y": from_y, "to_x": to_x, "to_y": to_y},
        )

    async def type_text(self, text: str) -> WebActionCommandResult:
        """Type text into the active remote page.

        Args:
            text: Literal text to type into the focused browser element.

        Returns:
            backend-neutral command result from the remote device.
        """

        return await self._dispatch("web.web_agent_backend_type_text", {"text": text})

    async def press(self, key: str) -> WebActionCommandResult:
        """Press a key in the active remote page.

        Args:
            key: Browser key name to press.

        Returns:
            backend-neutral command result from the remote device.
        """

        return await self._dispatch("web.web_agent_backend_press", {"key": key})

    async def scroll(self, direction: WebScrollDirection, amount: int) -> WebActionCommandResult:
        """Scroll the active remote page.

        Args:
            direction: Typed scroll direction from the web action schema.
            amount: Scroll amount passed to the device-side browser tool.

        Returns:
            backend-neutral command result from the remote device.
        """

        return await self._dispatch("web.web_agent_backend_scroll", {"direction": direction.value, "amount": amount})

    async def get_text(self, ref: str) -> WebActionCommandResult:
        """Read text from a remote DOM ref.

        Args:
            ref: DOM/snapshot reference whose text should be read.

        Returns:
            backend-neutral command result from the remote device.
        """

        return await self._dispatch("web.web_agent_backend_get_text", {"ref": _remote_web_ref(ref)})

    async def get_box(self, ref: str) -> WebActionCommandResult:
        """Read a remote DOM ref bounding box in viewport CSS coordinates."""

        return await self._dispatch("web.web_agent_get_box", {"ref": _remote_web_ref(ref)})

    async def get_boxes(self, refs: list[str]) -> WebActionCommandResult:
        """Read many remote DOM ref bounding boxes in viewport CSS coordinates."""

        normalized_refs = list(dict.fromkeys(_remote_web_ref(ref) for ref in refs))
        return await self._dispatch("web.web_agent_get_boxes", {"refs": normalized_refs})

    async def get_ref_states(self, refs: list[str]) -> WebActionCommandResult:
        """Read visible/enabled state for many remote DOM refs."""

        normalized_refs = list(dict.fromkeys(_remote_web_ref(ref) for ref in refs))
        return await self._dispatch("web.web_agent_get_ref_states", {"refs": normalized_refs})

    async def get_url(self) -> WebActionCommandResult:
        """Return the current remote page URL.

        Returns:
            backend-neutral command result containing the current URL.
        """

        return await self._dispatch("web.web_agent_backend_get_url", {})

    async def get_viewport_size(self) -> WebActionCommandResult:
        """Read the remote viewport dimensions and device pixel ratio."""

        return await self._dispatch("web.web_agent_viewport", {})

    async def wait(
        self,
        params: WebWaitParams,
        *,
        timeout_seconds: float | None = None,
    ) -> WebActionCommandResult:
        """Wait in the remote browser session.

        Args:
            params: Typed wait condition serialized for the device tool.
            timeout_seconds: Optional controller-side timeout hint used by
                best-effort settle probes. Model-visible waits use the
                default remote command timeout.

        Returns:
            backend-neutral command result after the wait resolves.
        """

        return await self._dispatch(
            "web.web_agent_backend_wait",
            params.model_dump(mode="json", exclude_none=True),
            timeout_seconds=timeout_seconds,
        )

    async def screenshot(self, *, annotate: bool = False) -> WebScreenshotObservation:
        """Collect and normalize a remote screenshot.

        Args:
            annotate: Whether element annotations should be rendered.

        Returns:
            Typed screenshot observation normalized from remote output.
        """

        return self.screenshot_normalizer.normalize(
            await self._dispatch_payload(
                "web.web_agent_backend_screenshot",
                {"annotate": annotate},
            )
        )

    async def list_tabs(self) -> WebActionCommandResult:
        """List open remote browser tabs."""

        return await self._dispatch("web.web_agent_backend_list_tabs", {})

    async def new_tab(self, *, url: str | None = None, label: str | None = None) -> WebActionCommandResult:
        """Open a new remote browser tab."""

        payload: JsonObject = {}
        if url is not None:
            payload["url"] = url
        if label is not None:
            payload["label"] = label
        return await self._dispatch("web.web_agent_backend_new_tab", payload)

    async def close_tab(self, tab_ref: str | None = None) -> WebActionCommandResult:
        """Close the active remote browser tab or a tab ref."""

        payload: JsonObject = {}
        if tab_ref is not None:
            payload["tab_ref"] = tab_ref
        return await self._dispatch("web.web_agent_backend_close_tab", payload)

    async def focus_tab(self, tab_ref: str) -> WebActionCommandResult:
        """Switch focus to a remote browser tab ref."""

        return await self._dispatch("web.web_agent_backend_focus_tab", {"tab_ref": tab_ref})

    async def close(self) -> WebActionCommandResult:
        """Close the remote browser session.

        Returns:
            backend-neutral command result from the close-session tool.
        """

        return await self._dispatch("web.web_close_session", {})

    async def _dispatch(
        self,
        tool_id: str,
        parameters: JsonObject,
        *,
        timeout_seconds: float | None = None,
    ) -> WebActionCommandResult:
        payload: JsonObject = dict(parameters)
        if self.web_settings:
            payload[AGENTKIT_SETTINGS_PARAM] = dict(self.web_settings)
        result = await self.dispatcher.dispatch(
            tool_id=tool_id,
            parameters=payload,
            timeout_seconds=timeout_seconds or self.timeout_seconds,
        )
        record = command_result_record(result.command_id, result)
        if record.status != ResultStatus.SUCCESS:
            return WebActionCommandResult(success=False, error=record.error or "remote command failed")
        normalized_payload = normalize_remote_tool_payload(record.result)
        if isinstance(normalized_payload, dict) and str(normalized_payload.get("status") or "").lower() == "error":
            error = str(
                normalized_payload.get("error")
                or normalized_payload.get("text")
                or normalized_payload.get("message")
                or "remote tool failed"
            )
            return WebActionCommandResult(success=False, data=normalized_payload, error=error)
        command_data: object | None = normalized_payload
        if isinstance(normalized_payload, dict) and str(normalized_payload.get("status") or "").lower() == "ok":
            nested = normalized_payload.get("result")
            command_data = (
                _merge_parent_artifact_fields(nested, normalized_payload)
                if isinstance(nested, dict)
                else normalized_payload
            )
        return WebActionCommandResult(success=True, data=command_data)

    async def _dispatch_payload(self, tool_id: str, parameters: JsonObject) -> JsonObject:
        result = await self._dispatch(tool_id, parameters)
        result.require_success()
        payload = result.data
        if isinstance(payload, dict):
            nested = payload.get("result")
            return nested if isinstance(nested, dict) else payload
        return {}


def _merge_parent_artifact_fields(nested: JsonObject, parent: JsonObject) -> JsonObject:
    """Preserve parent-level artifact fields after unwrapping ``status: ok`` payloads."""

    merged = dict(nested)
    for key, value in parent.items():
        if key == "result":
            continue
        if key.startswith("screenshot_") or key.startswith("image_") or key in {"screenshot_url", "image_url"}:
            merged.setdefault(key, value)
    return merged
