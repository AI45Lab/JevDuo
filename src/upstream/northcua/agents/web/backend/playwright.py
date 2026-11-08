"""Async Playwright implementation of the web-agent action backend."""

from __future__ import annotations

import asyncio
import base64
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from northcua.core.config import WebAgentConfig

from ..domain.actionability import classify_actionability_message
from ..domain.actions import WebMouseButton, WebScrollDirection, WebWaitParams
from ..domain.key_names import canonical_web_key_name
from ..domain.observations import WebElementRef, WebScreenshotObservation, WebSnapshotObservation
from .errors import WebActionBackendError, WebActionBackendUnavailableError, WebActionCommandError
from .result import WebActionCommandResult
from .snapshot_scripts import FAST_SNAPSHOT_SCRIPT, REF_PROBE_SCRIPT, SNAPSHOT_SCRIPT

_PNG_DATA_URL_PREFIX = "data:image/png;base64,"
_REF_ATTR = "data-northcua-web-agent-ref"
_DEFAULT_VIEWPORT = {"width": 1280, "height": 720}
_NEW_PAGE_DETECTION_TIMEOUT_SECONDS = 1.0
_NEW_PAGE_POLL_INTERVAL_SECONDS = 0.01


@dataclass(slots=True, frozen=True)
class _RefTarget:
    frame_index: int
    selector: str


class PlaywrightWebActionBackend:
    """Persistent async Playwright backend for one web-agent session."""

    def __init__(self, *, config: WebAgentConfig, session_id: str) -> None:
        self.config = config
        self.session_id = session_id
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None
        self._active_page_index = 0
        self._ref_targets_by_page: dict[int, dict[str, _RefTarget]] = {}
        self._tab_labels_by_page: dict[int, str] = {}

    async def open(self, url: str) -> WebActionCommandResult:
        return await self._command(lambda: self._open(url))

    async def snapshot(
        self,
        *,
        interactive: bool | None = None,
        compact: bool | None = None,
        depth: int | None = None,
    ) -> WebSnapshotObservation:
        del compact
        page = await self._page()
        elements: list[WebElementRef] = []
        ref_map: dict[str, _RefTarget] = {}
        snapshots: list[str] = []
        title = ""
        url = page.url
        origin = ""
        next_ref_index = 1
        frame_errors: list[dict[str, object]] = []
        frame_meta: list[dict[str, object]] = []
        base_args = {
            "attrName": _REF_ATTR,
            "depth": self.config.snapshot_depth if depth is None else depth,
            "interactiveOnly": self.config.snapshot_interactive if interactive is None else interactive,
            "maxRefs": self.config.snapshot_max_refs,
            "maxVisited": self.config.snapshot_max_visited,
            "maxVisibleTextChars": self.config.snapshot_visible_text_max_chars,
        }
        visible_text_frames: list[dict[str, object]] = []
        for frame_index, frame in enumerate(page.frames):
            args = {**base_args, "startIndex": next_ref_index}
            raw, error = await self._evaluate_snapshot_frame(frame, args)
            if error:
                frame_errors.append({"frame_index": frame_index, "error": error})
            if raw is None:
                continue
            raw_fallback = str(raw.get("fallback") or "")
            frame_meta.append(
                {
                    "frame_index": frame_index,
                    "url": str(raw.get("url") or ""),
                    "ref_count": int(raw.get("refCount") or 0),
                    "visited": int(raw.get("visited") or 0),
                    "truncated": bool(raw.get("truncated")),
                    "fallback": raw_fallback,
                }
            )
            if frame_index == 0:
                title = str(raw.get("title") or "")
                url = str(raw.get("url") or page.url)
                origin = str(raw.get("origin") or "")
            frame_snapshot = str(raw.get("snapshot") or "")
            if frame_snapshot:
                snapshots.append(frame_snapshot if frame_index == 0 else f"[frame {frame_index}]\n{frame_snapshot}")
            visible_text = str(raw.get("visibleText") or "")
            if visible_text:
                visible_text_frames.append(
                    {
                        "frame_index": frame_index,
                        "url": str(raw.get("url") or ""),
                        "text": visible_text,
                        "chars": int(raw.get("visibleTextChars") or len(visible_text)),
                        "truncated": bool(raw.get("visibleTextTruncated")),
                    }
                )
            refs_payload = raw.get("refs")
            before_count = len(elements)
            if isinstance(refs_payload, Mapping):
                for raw_ref, payload in refs_payload.items():
                    ref_id = str(raw_ref)
                    model_ref = ref_id if ref_id.startswith("@") else f"@{ref_id}"
                    ref_map[model_ref] = _RefTarget(
                        frame_index=frame_index,
                        selector=f"[{_REF_ATTR}='{ref_id}']",
                    )
                    if isinstance(payload, Mapping):
                        attributes = payload.get("attributes")
                        elements.append(
                            WebElementRef(
                                ref_id=ref_id,
                                role=str(payload.get("role") or ""),
                                name=str(payload.get("name") or ""),
                                text=str(payload.get("text") or ""),
                                tag=str(payload.get("tag") or ""),
                                attributes=dict(attributes) if isinstance(attributes, Mapping) else {},
                            )
                        )
            frame_ref_count = len(elements) - before_count
            next_ref_index = self._next_ref_index(
                raw.get("nextRefIndex"),
                fallback=next_ref_index + frame_ref_count,
            )
        if not title:
            title = await self._safe_page_title(page)
        self._ref_targets_by_page[id(page)] = ref_map
        return WebSnapshotObservation(
            url=url,
            title=title,
            origin=origin,
            snapshot="\n".join(snapshots),
            elements=elements,
            raw={
                "backend": "playwright",
                "url": url,
                "title": title,
                "frame_count": len(page.frames),
                "frame_errors": frame_errors,
                "frame_meta": frame_meta,
                "snapshot_eval_timeout_seconds": self.config.snapshot_eval_timeout_seconds,
                "snapshot_max_refs": self.config.snapshot_max_refs,
                "snapshot_max_visited": self.config.snapshot_max_visited,
                "snapshot_fallback_max_refs": self.config.snapshot_fallback_max_refs,
                "fallback_used": any(meta.get("fallback") for meta in frame_meta),
                "truncated": any(bool(meta.get("truncated")) for meta in frame_meta),
                "visible_text_frames": visible_text_frames,
                "snapshot_visible_text_max_chars": self.config.snapshot_visible_text_max_chars,
            },
        )

    async def _evaluate_snapshot_frame(
        self,
        frame: Any,
        args: Mapping[str, object],
    ) -> tuple[Mapping[str, object] | None, str | None]:
        """Evaluate the full snapshot script, falling back only after timeout/error.

        Normal snapshots are intentionally not truncated by default. The
        bounded query fallback is explicitly marked in raw metadata so a large
        page never masquerades as a complete DOM/ref snapshot.
        """

        try:
            raw = await asyncio.wait_for(
                frame.evaluate(SNAPSHOT_SCRIPT, dict(args)),
                timeout=self.config.snapshot_eval_timeout_seconds,
            )
        except asyncio.TimeoutError:
            fallback = await self._evaluate_fast_snapshot_frame(frame, args)
            return fallback, "full_snapshot_timeout"
        except Exception as exc:
            fallback = await self._evaluate_fast_snapshot_frame(frame, args)
            return fallback, f"full_snapshot_error:{type(exc).__name__}: {exc}"
        if isinstance(raw, Mapping):
            return raw, None
        return None, "full_snapshot_non_object"

    async def _evaluate_fast_snapshot_frame(
        self,
        frame: Any,
        args: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        fallback_timeout = min(2.0, max(0.25, self.config.snapshot_eval_timeout_seconds / 4))
        fallback_args = {
            "attrName": _REF_ATTR,
            "startIndex": args.get("startIndex"),
            "maxRefs": self.config.snapshot_fallback_max_refs,
            "maxVisibleTextChars": self.config.snapshot_visible_text_max_chars,
        }
        try:
            raw = await asyncio.wait_for(
                frame.evaluate(FAST_SNAPSHOT_SCRIPT, fallback_args),
                timeout=fallback_timeout,
            )
        except Exception:
            return None
        return raw if isinstance(raw, Mapping) else None

    async def _safe_page_title(self, page: Any) -> str:
        try:
            title = await page.title()
        except Exception:
            return ""
        return str(title or "")

    async def click_ref(self, ref: str) -> WebActionCommandResult:
        return await self._command(lambda: self._click_ref(ref))

    async def hover_ref(self, ref: str) -> WebActionCommandResult:
        return await self._command(lambda: self._hover_ref(ref))

    async def fill_ref(self, ref: str, text: str) -> WebActionCommandResult:
        return await self._command(lambda: self._fill_ref(ref, text))

    async def select_option_ref(self, ref: str, values: list[str]) -> WebActionCommandResult:
        return await self._command(lambda: self._select_option_ref(ref, values))

    async def check_ref(self, ref: str) -> WebActionCommandResult:
        return await self._command(lambda: self._set_checked(ref, True))

    async def uncheck_ref(self, ref: str) -> WebActionCommandResult:
        return await self._command(lambda: self._set_checked(ref, False))

    async def drag_ref(self, from_ref: str, to_ref: str) -> WebActionCommandResult:
        return await self._command(lambda: self._drag_ref(from_ref, to_ref))

    async def click_point(self, x: float, y: float, button: WebMouseButton) -> WebActionCommandResult:
        return await self._command(lambda: self._click_point(x, y, button, click_count=1))

    async def double_click_point(self, x: float, y: float, button: WebMouseButton) -> WebActionCommandResult:
        return await self._command(lambda: self._click_point(x, y, button, click_count=2))

    async def drag_point(self, from_x: float, from_y: float, to_x: float, to_y: float) -> WebActionCommandResult:
        return await self._command(lambda: self._drag_point(from_x, from_y, to_x, to_y))

    async def type_text(self, text: str) -> WebActionCommandResult:
        return await self._command(lambda: self._type_text(text))

    async def press(self, key: str) -> WebActionCommandResult:
        return await self._command(lambda: self._press(key))

    async def scroll(self, direction: WebScrollDirection, amount: int) -> WebActionCommandResult:
        return await self._command(lambda: self._scroll(direction, amount))

    async def get_text(self, ref: str) -> WebActionCommandResult:
        return await self._command(lambda: self._get_text(ref))

    async def get_box(self, ref: str) -> WebActionCommandResult:
        return await self._command(lambda: self._get_box(ref))

    async def get_boxes(self, refs: list[str]) -> WebActionCommandResult:
        return await self._command(lambda: self._get_boxes(refs))

    async def get_ref_states(self, refs: list[str]) -> WebActionCommandResult:
        return await self._command(lambda: self._get_ref_states(refs))

    async def get_url(self) -> WebActionCommandResult:
        return await self._command(lambda: self._get_url())

    async def get_viewport_size(self) -> WebActionCommandResult:
        return await self._command(lambda: self._get_viewport_size())

    async def wait(
        self,
        params: WebWaitParams,
        *,
        timeout_seconds: float | None = None,
    ) -> WebActionCommandResult:
        return await self._command(lambda: self._wait(params, timeout_seconds=timeout_seconds))

    async def screenshot(self, *, annotate: bool = False) -> WebScreenshotObservation:
        del annotate
        page = await self._page()
        try:
            data = await page.screenshot(
                type="png",
                timeout=self.config.screenshot_timeout_seconds * 1000,
            )
            raw: dict[str, object] = {"backend": "playwright"}
        except Exception as exc:
            try:
                data = await self._chromium_cdp_screenshot(page)
            except Exception as fallback_exc:
                raise exc from fallback_exc
            raw = {
                "backend": "playwright",
                "fallback": "chromium_cdp_capture_screenshot",
                "fallback_reason": type(exc).__name__,
            }
        viewport = page.viewport_size or {}
        width = viewport.get("width") if isinstance(viewport, Mapping) else None
        height = viewport.get("height") if isinstance(viewport, Mapping) else None
        return WebScreenshotObservation(
            data_url=f"{_PNG_DATA_URL_PREFIX}{base64.b64encode(data).decode('ascii')}",
            width=self._optional_int(width),
            height=self._optional_int(height),
            raw=raw,
        )

    async def _chromium_cdp_screenshot(self, page: Any) -> bytes:
        """Capture a screenshot through CDP when Playwright waits on fonts too long.

        Playwright's high-level screenshot API waits for fonts before capture.
        Real pages can leave webfont requests pending long enough to exceed our
        prompt hot-path timeout. Chromium's CDP screenshot endpoint captures the
        current compositor surface and gives us a useful observation without
        creating a second browser context.
        """

        if self.config.playwright_browser != "chromium":
            raise WebActionBackendError("CDP screenshot fallback is only available for chromium")
        context = getattr(page, "context", None)
        if context is None:
            context = await self._browser_context()
        new_cdp_session = getattr(context, "new_cdp_session", None)
        if not callable(new_cdp_session):
            raise WebActionBackendError("chromium context does not expose CDP sessions")
        session = await new_cdp_session(page)
        try:
            params: dict[str, object] = {
                "format": "png",
                "fromSurface": True,
                "captureBeyondViewport": False,
            }
            response = await session.send("Page.captureScreenshot", params)
            if not isinstance(response, Mapping) or not isinstance(response.get("data"), str):
                raise WebActionBackendError("CDP screenshot response did not include image data")
            return base64.b64decode(response["data"])
        finally:
            detach = getattr(session, "detach", None)
            if callable(detach):
                await detach()

    async def list_tabs(self) -> WebActionCommandResult:
        return await self._command(lambda: self._list_tabs())

    async def new_tab(self, *, url: str | None = None, label: str | None = None) -> WebActionCommandResult:
        return await self._command(lambda: self._new_tab(url=url, label=label))

    async def close_tab(self, tab_ref: str | None = None) -> WebActionCommandResult:
        return await self._command(lambda: self._close_tab(tab_ref))

    async def focus_tab(self, tab_ref: str) -> WebActionCommandResult:
        return await self._command(lambda: self._focus_tab(tab_ref))

    async def close(self) -> WebActionCommandResult:
        errors: list[Exception] = []
        for resource, method in ((self._context, "close"), (self._browser, "close"), (self._playwright, "stop")):
            if resource is not None:
                try:
                    await getattr(resource, method)()
                except Exception as exc:
                    errors.append(exc)
        self._context = None
        self._browser = None
        self._playwright = None
        self._ref_targets_by_page.clear()
        self._tab_labels_by_page.clear()
        if errors:
            return self._failed(ExceptionGroup("Browser cleanup failed", errors))
        return self._ok({"closed": True})

    async def _command(self, operation: Callable[[], Awaitable[object | None]]) -> WebActionCommandResult:
        try:
            payload = await operation()
            return self._ok(payload)
        except WebActionBackendUnavailableError:
            raise
        except Exception as exc:
            return self._failed(exc)

    async def _open(self, url: str) -> dict[str, object]:
        page = await self._page()
        await page.goto(url, wait_until="domcontentloaded", timeout=self.config.command_timeout_seconds * 1000)
        return {"url": page.url}

    async def _click_ref(self, ref: str) -> dict[str, object]:
        locator = await self._locator_for_ref(ref)
        await self._preflight_ref_action(locator, ref=ref, action="click_ref")
        opens_new_page = await self._locator_opens_new_page(locator)

        async def operation() -> dict[str, object]:
            await locator.click(timeout=self.config.command_timeout_seconds * 1000)
            return {"ref": self._canonical_ref(ref)}

        return await self._run_and_activate_new_page(operation, wait_for_new_page=opens_new_page)

    async def _hover_ref(self, ref: str) -> dict[str, object]:
        locator = await self._locator_for_ref(ref)
        await self._preflight_ref_action(locator, ref=ref, action="hover_ref")
        await locator.hover(timeout=self.config.command_timeout_seconds * 1000)
        return {"ref": self._canonical_ref(ref)}

    async def _fill_ref(self, ref: str, text: str) -> dict[str, object]:
        locator = await self._locator_for_ref(ref)
        await self._preflight_ref_action(locator, ref=ref, action="fill_ref")
        await locator.fill(text, timeout=self.config.command_timeout_seconds * 1000)
        return {"ref": self._canonical_ref(ref), "text_length": len(text)}

    async def _select_option_ref(self, ref: str, values: list[str]) -> dict[str, object]:
        locator = await self._locator_for_ref(ref)
        await self._preflight_ref_action(locator, ref=ref, action="select_option_ref")
        selected = await locator.select_option(values, timeout=self.config.command_timeout_seconds * 1000)
        return {"ref": self._canonical_ref(ref), "values": selected}

    async def _set_checked(self, ref: str, checked: bool) -> dict[str, object]:
        locator = await self._locator_for_ref(ref)
        await self._preflight_ref_action(locator, ref=ref, action="check_ref" if checked else "uncheck_ref")
        if checked:
            await locator.check(timeout=self.config.command_timeout_seconds * 1000)
        else:
            await locator.uncheck(timeout=self.config.command_timeout_seconds * 1000)
        return {"ref": self._canonical_ref(ref), "checked": checked}

    async def _drag_ref(self, from_ref: str, to_ref: str) -> dict[str, object]:
        source = await self._locator_for_ref(from_ref)
        target = await self._locator_for_ref(to_ref)
        await self._preflight_ref_action(source, ref=from_ref, action="drag_ref_source")
        await self._preflight_ref_action(target, ref=to_ref, action="drag_ref_target")
        await source.drag_to(target, timeout=self.config.command_timeout_seconds * 1000)
        return {"from_ref": self._canonical_ref(from_ref), "to_ref": self._canonical_ref(to_ref)}

    async def _click_point(
        self,
        x: float,
        y: float,
        button: WebMouseButton,
        *,
        click_count: int,
    ) -> dict[str, object]:
        async def operation() -> dict[str, object]:
            await self._validate_point(x, y)
            page = await self._page()
            await page.mouse.click(x, y, button=button.value, click_count=click_count)
            return {"x": x, "y": y, "button": button.value, "click_count": click_count}

        return await self._run_and_activate_new_page(operation)

    async def _drag_point(self, from_x: float, from_y: float, to_x: float, to_y: float) -> dict[str, object]:
        await self._validate_point(from_x, from_y)
        await self._validate_point(to_x, to_y)
        page = await self._page()
        await page.mouse.move(from_x, from_y)
        await page.mouse.down()
        await page.mouse.move(to_x, to_y)
        await page.mouse.up()
        return {"from": {"x": from_x, "y": from_y}, "to": {"x": to_x, "y": to_y}}

    async def _type_text(self, text: str) -> dict[str, object]:
        page = await self._page()
        await page.keyboard.type(text)
        return {"text_length": len(text)}

    async def _press(self, key: str) -> dict[str, object]:
        canonical_key = canonical_web_key_name(key)

        async def operation() -> dict[str, object]:
            page = await self._page()
            await page.keyboard.press(canonical_key)
            return {"key": canonical_key}

        return await self._run_and_activate_new_page(operation)

    async def _scroll(self, direction: WebScrollDirection, amount: int) -> dict[str, object]:
        page = await self._page()
        delta_x = 0
        delta_y = 0
        if direction == WebScrollDirection.UP:
            delta_y = -amount
        elif direction == WebScrollDirection.DOWN:
            delta_y = amount
        elif direction == WebScrollDirection.LEFT:
            delta_x = -amount
        else:
            delta_x = amount
        await page.mouse.wheel(delta_x, delta_y)
        return {"direction": direction.value, "amount": amount}

    async def _get_text(self, ref: str) -> dict[str, object]:
        locator = await self._locator_for_ref(ref)
        text = await locator.inner_text(timeout=self.config.command_timeout_seconds * 1000)
        return {"ref": self._canonical_ref(ref), "text": text}

    async def _get_box(self, ref: str) -> dict[str, object]:
        locator = await self._locator_for_ref(ref)
        box = await locator.bounding_box(timeout=self.config.command_timeout_seconds * 1000)
        if box is None:
            raise RuntimeError(f"element not visible for ref {self._canonical_ref(ref)}")
        return dict(box)

    async def _get_boxes(self, refs: list[str]) -> dict[str, object]:
        boxes: dict[str, object] = {}
        errors: dict[str, str] = {}
        canonical_refs = list(dict.fromkeys(self._canonical_ref(item) for item in refs))
        probed = await self._batch_probe_refs(canonical_refs)
        boxes.update(probed["boxes"])
        errors.update(probed["errors"])
        for ref in probed["fallback_refs"]:
            try:
                boxes[ref] = await self._get_box(ref)
            except Exception as exc:
                errors[ref] = str(exc)
        return {"boxes": boxes, "errors": errors}

    async def _get_ref_states(self, refs: list[str]) -> dict[str, object]:
        states: dict[str, dict[str, bool]] = {}
        errors: dict[str, str] = {}
        canonical_refs = list(dict.fromkeys(self._canonical_ref(item) for item in refs))
        probed = await self._batch_probe_refs(canonical_refs)
        states.update(probed["states"])
        errors.update(probed["errors"])
        for ref in probed["fallback_refs"]:
            try:
                locator = await self._locator_for_ref(ref)
                states[ref] = {
                    "visible": bool(await locator.is_visible(timeout=self.config.command_timeout_seconds * 1000)),
                    "enabled": bool(await locator.is_enabled(timeout=self.config.command_timeout_seconds * 1000)),
                }
            except Exception as exc:
                states.setdefault(ref, {})
                errors[ref] = str(exc)
        return {"states": states, "errors": errors}

    async def _get_url(self) -> dict[str, object]:
        page = await self._page()
        return {"url": page.url}

    async def _get_viewport_size(self) -> dict[str, object]:
        page = await self._page()
        viewport = page.viewport_size or _DEFAULT_VIEWPORT
        dpr = await page.evaluate("() => window.devicePixelRatio || 1")
        return {"width": viewport["width"], "height": viewport["height"], "device_pixel_ratio": dpr}

    async def _wait(self, params: WebWaitParams, *, timeout_seconds: float | None) -> dict[str, object]:
        timeout_ms = (timeout_seconds or self.config.command_timeout_seconds) * 1000
        page = await self._page()
        if params.duration_ms is not None:
            requested_ms = max(0, int(params.duration_ms))
            waited_ms = min(requested_ms, max(0, int(timeout_ms)))
            await asyncio.sleep(waited_ms / 1000)
            payload: dict[str, object] = {"waited_ms": waited_ms}
            if waited_ms != requested_ms:
                payload["requested_ms"] = requested_ms
                payload["clamped"] = True
            return payload
        if params.load_state:
            await page.wait_for_load_state(params.load_state, timeout=timeout_ms)
            return {"load_state": params.load_state}
        if params.text:
            await page.get_by_text(params.text).first.wait_for(state="visible", timeout=timeout_ms)
            return {"text": params.text}
        if params.url_pattern:
            await page.wait_for_url(params.url_pattern, timeout=timeout_ms)
            return {"url_pattern": params.url_pattern, "url": page.url}
        if params.selector:
            await page.wait_for_selector(params.selector, state="attached", timeout=timeout_ms)
            return {"selector": params.selector}
        raise ValueError("web_wait expects exactly one wait target.")

    async def _list_tabs(self) -> dict[str, object]:
        context = await self._browser_context()
        tabs: list[dict[str, object]] = []
        for index, page in enumerate(context.pages):
            title = ""
            try:
                title = await page.title()
            except Exception:
                title = ""
            tab_ref = self._tab_ref(index)
            tabs.append(
                {
                    "ref": tab_ref,
                    "id": tab_ref,
                    "label": self._tab_labels_by_page.get(id(page), ""),
                    "index": index,
                    "title": title,
                    "url": page.url,
                    "active": index == self._active_page_index,
                }
            )
        return {"tabs": tabs, "active": self._tab_ref(self._active_page_index)}

    async def _new_tab(self, *, url: str | None, label: str | None) -> dict[str, object]:
        context = await self._browser_context()
        page = await context.new_page()
        self._active_page_index = len(context.pages) - 1
        if label:
            self._tab_labels_by_page[id(page)] = label
        if url:
            await page.goto(url, wait_until="domcontentloaded", timeout=self.config.command_timeout_seconds * 1000)
        return {"tab": self._tab_info(page, self._active_page_index)}

    async def _close_tab(self, tab_ref: str | None) -> dict[str, object]:
        context = await self._browser_context()
        page, index = await self._resolve_tab(tab_ref)
        await page.close()
        self._ref_targets_by_page.pop(id(page), None)
        self._tab_labels_by_page.pop(id(page), None)
        pages = context.pages
        if not pages:
            await context.new_page()
            pages = context.pages
        self._active_page_index = min(index, len(pages) - 1)
        await pages[self._active_page_index].bring_to_front()
        return {"closed": tab_ref or self._tab_ref(index), "active": self._tab_ref(self._active_page_index)}

    async def _focus_tab(self, tab_ref: str) -> dict[str, object]:
        page, index = await self._resolve_tab(tab_ref)
        self._active_page_index = index
        await page.bring_to_front()
        return {"tab": self._tab_info(page, index)}

    async def _preflight_ref_action(self, locator: Any, *, ref: str, action: str) -> None:
        """Fail fast on deterministic ref-action blockers.

        The real Playwright action remains the authority. This preflight only
        short-circuits cases where Playwright can already tell us the target is
        disabled, hidden, type-incompatible, or unable to receive pointer
        events within a short budget.
        """

        if not self.config.action_preflight_enabled:
            return
        timeout_ms = int(self.config.action_preflight_timeout_ms)
        if timeout_ms <= 0:
            return

        await self._preflight_static_blockers(
            locator,
            ref=ref,
            action=action,
            timeout_ms=timeout_ms,
            require_enabled=action in {"click_ref", "fill_ref", "select_option_ref", "check_ref", "uncheck_ref"},
        )
        if action == "fill_ref":
            if not await self._preflight_element_matches(locator, timeout_ms=timeout_ms, kind="fillable"):
                self._raise_preflight_error(
                    ref=ref,
                    action=action,
                    code="wrong_action_for_element",
                    message="element is not editable",
                )
            return
        if action == "select_option_ref":
            if not await self._preflight_element_matches(locator, timeout_ms=timeout_ms, kind="select"):
                self._raise_preflight_error(
                    ref=ref,
                    action=action,
                    code="wrong_action_for_element",
                    message="not a <select> element",
                )
            return
        if action in {"check_ref", "uncheck_ref"}:
            if not await self._preflight_element_matches(locator, timeout_ms=timeout_ms, kind="checkable"):
                self._raise_preflight_error(
                    ref=ref,
                    action=action,
                    code="wrong_action_for_element",
                    message="not a checkbox or radio button",
                )
            return
        if action in {"click_ref"} and self.config.action_preflight_hit_test:
            await self._preflight_hit_test_static(locator, ref=ref, action=action, timeout_ms=timeout_ms)
            return
        if action in {"hover_ref", "drag_ref_source", "drag_ref_target"} and self.config.action_preflight_hit_test:
            await self._preflight_hit_test_static(locator, ref=ref, action=action, timeout_ms=timeout_ms)

    async def _preflight_static_blockers(
        self,
        locator: Any,
        *,
        ref: str,
        action: str,
        timeout_ms: int,
        require_enabled: bool,
    ) -> None:
        try:
            payload = await locator.evaluate(
                """(el) => {
                  const target = el.tagName && el.tagName.toLowerCase() === "label" && el.control ? el.control : el;
                  const ariaHidden = String(el.getAttribute("aria-hidden") || "").toLowerCase() === "true";
                  const ariaDisabled = String(target.getAttribute("aria-disabled") || "").toLowerCase() === "true";
                  return {
                    hidden: el.hasAttribute("hidden") || ariaHidden,
                    disabled: target.disabled === true || target.hasAttribute("disabled") || ariaDisabled || target.hasAttribute("inert")
                  };
                }""",
                timeout=timeout_ms,
            )
            if isinstance(payload, Mapping) and payload.get("hidden") is True:
                self._raise_preflight_error(
                    ref=ref,
                    action=action,
                    code="element_not_visible",
                    message="element is hidden",
                )
            if require_enabled and isinstance(payload, Mapping) and payload.get("disabled") is True:
                self._raise_preflight_error(
                    ref=ref,
                    action=action,
                    code="element_disabled",
                    message="element is not enabled",
                )
        except WebActionCommandError:
            raise
        except Exception as exc:
            self._raise_if_actionability_error(exc, ref=ref, action=action)

    async def _preflight_trial(
        self,
        locator: Any,
        *,
        ref: str,
        action: str,
        timeout_ms: int,
        trial: str,
    ) -> None:
        try:
            if trial == "click":
                await locator.click(timeout=timeout_ms, trial=True)
            elif trial == "hover":
                await locator.hover(timeout=timeout_ms, trial=True)
            elif trial == "check_ref":
                await locator.check(timeout=timeout_ms, trial=True)
            elif trial == "uncheck_ref":
                await locator.uncheck(timeout=timeout_ms, trial=True)
        except Exception as exc:
            self._raise_if_actionability_error(exc, ref=ref, action=action)

    async def _preflight_hit_test_static(self, locator: Any, *, ref: str, action: str, timeout_ms: int) -> None:
        try:
            payload = await locator.evaluate(
                """(el) => {
                  const rect = el.getBoundingClientRect();
                  const style = window.getComputedStyle(el);
                  if (!rect || rect.width <= 0 || rect.height <= 0 || !style) return {blocked: false};
                  return {
                    blocked: style.pointerEvents === "none",
                    reason: style.pointerEvents === "none" ? "target has pointer-events:none" : ""
                  };
                }""",
                timeout=timeout_ms,
            )
            if isinstance(payload, Mapping) and payload.get("blocked") is True:
                self._raise_preflight_error(
                    ref=ref,
                    action=action,
                    code="element_not_receiving_events",
                    message=str(payload.get("reason") or "does not receive pointer events"),
                )
        except WebActionCommandError:
            raise
        except Exception as exc:
            self._raise_if_actionability_error(exc, ref=ref, action=action)

    async def _preflight_element_matches(self, locator: Any, *, timeout_ms: int, kind: str) -> bool:
        try:
            payload = await locator.evaluate(
                """(el, kind) => {
                  const label = el.closest ? el.closest("label") : null;
                  const associatedControl = el.control || (label ? label.control : null);
                  const target = associatedControl || el;
                  const targetTag = String(target.tagName || "").toLowerCase();
                  const type = String(target.getAttribute("type") || "").toLowerCase();
                  const role = String(target.getAttribute("role") || "").toLowerCase();
                  const contentEditable = String(target.getAttribute("contenteditable") || "").toLowerCase();
                  const hasContentEditable = target.hasAttribute("contenteditable");
                  if (kind === "select") return targetTag === "select";
                  if (kind === "checkable") return targetTag === "input" && (type === "checkbox" || type === "radio");
                  if (kind === "fillable") {
                    return targetTag === "textarea"
                      || (targetTag === "input" && !["button", "checkbox", "color", "file", "hidden", "image", "radio", "range", "reset", "submit"].includes(type))
                      || (hasContentEditable && (contentEditable === "" || contentEditable === "true"))
                      || role === "textbox";
                  }
                  return true;
                }""",
                kind,
                timeout=timeout_ms,
            )
        except Exception:
            return True
        return bool(payload)

    def _raise_if_actionability_error(self, exc: Exception, *, ref: str, action: str) -> None:
        feedback = classify_actionability_message(str(exc), source="preflight")
        if feedback is None:
            return
        raise WebActionCommandError(
            f"{action} preflight failed for {self._canonical_ref(ref)}: {feedback.concise_log_summary or feedback.code}",
            code=feedback.code,
            reason_code=feedback.code,
            hint=feedback.hint,
            concise_log_summary=feedback.concise_log_summary,
            feedback_source=feedback.feedback_source,
            debug_error=str(exc),
        ) from exc

    def _raise_preflight_error(self, *, ref: str, action: str, code: str, message: str) -> None:
        feedback = classify_actionability_message(message, source="preflight")
        hint = feedback.hint if feedback is not None else "Choose an action that matches the latest visible ref."
        summary = feedback.concise_log_summary if feedback is not None else message
        raise WebActionCommandError(
            f"{action} preflight failed for {self._canonical_ref(ref)}: {summary}",
            code=code,
            reason_code=code,
            hint=hint,
            concise_log_summary=summary,
            feedback_source="preflight",
            debug_error=message,
        )

    async def _locator_for_ref(self, ref: str) -> Any:
        page = await self._page()
        canonical_ref = self._canonical_ref(ref)
        target = self._ref_targets_by_page.get(id(page), {}).get(canonical_ref)
        if target is None:
            raise RuntimeError(f"unknown ref {canonical_ref}; capture a fresh snapshot before using ref actions")
        if target.frame_index >= len(page.frames):
            raise RuntimeError(f"stale ref {canonical_ref}; frame no longer exists")
        locator = page.frames[target.frame_index].locator(target.selector).first
        if await locator.count() <= 0:
            raise RuntimeError(f"stale ref {canonical_ref}; element not found in current page")
        return locator

    async def _batch_probe_refs(self, refs: list[str]) -> dict[str, object]:
        page = await self._page()
        targets = self._ref_targets_by_page.get(id(page), {})
        main_frame_refs: list[str] = []
        fallback_refs: list[str] = []
        errors: dict[str, str] = {}
        for ref in refs:
            target = targets.get(ref)
            if target is None:
                errors[ref] = f"unknown ref {ref}; capture a fresh snapshot before using ref actions"
                continue
            if target.frame_index >= len(page.frames):
                errors[ref] = f"stale ref {ref}; frame no longer exists"
                continue
            if target.frame_index == 0:
                main_frame_refs.append(ref)
            else:
                fallback_refs.append(ref)

        boxes: dict[str, object] = {}
        states: dict[str, dict[str, bool]] = {}
        if main_frame_refs:
            try:
                raw = await page.frames[0].evaluate(
                    REF_PROBE_SCRIPT,
                    {"attrName": _REF_ATTR, "refs": [ref.removeprefix("@") for ref in main_frame_refs]},
                )
            except Exception:
                fallback_refs.extend(main_frame_refs)
            else:
                if isinstance(raw, Mapping):
                    raw_boxes = raw.get("boxes")
                    if isinstance(raw_boxes, Mapping):
                        for ref, payload in raw_boxes.items():
                            boxes[str(ref)] = payload
                    raw_states = raw.get("states")
                    if isinstance(raw_states, Mapping):
                        for ref, payload in raw_states.items():
                            if isinstance(payload, Mapping):
                                states[str(ref)] = {
                                    "visible": bool(payload.get("visible")),
                                    "enabled": bool(payload.get("enabled")),
                                }
                    raw_errors = raw.get("errors")
                    if isinstance(raw_errors, Mapping):
                        for ref, message in raw_errors.items():
                            errors[str(ref)] = str(message)
                    unresolved_refs = [ref for ref in main_frame_refs if ref not in boxes and ref not in errors]
                    fallback_refs.extend(unresolved_refs)
                else:
                    fallback_refs.extend(main_frame_refs)

        return {
            "boxes": boxes,
            "states": states,
            "errors": errors,
            "fallback_refs": list(dict.fromkeys(fallback_refs)),
        }

    async def _run_and_activate_new_page(
        self,
        operation: Callable[[], Awaitable[dict[str, object]]],
        *,
        wait_for_new_page: bool = False,
    ) -> dict[str, object]:
        context = await self._browser_context()
        before_page_ids = {id(page) for page in context.pages}
        payload = await operation()
        if wait_for_new_page:
            await self._wait_for_new_page(context=context, before_page_ids=before_page_ids)
        await self._activate_new_page_if_created(context=context, before_page_ids=before_page_ids)
        return payload

    async def _wait_for_new_page(self, *, context: Any, before_page_ids: set[int]) -> None:
        """Wait briefly for an expected target=_blank page to enter the context."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(
            _NEW_PAGE_DETECTION_TIMEOUT_SECONDS,
            max(_NEW_PAGE_POLL_INTERVAL_SECONDS, float(self.config.command_timeout_seconds)),
        )
        while loop.time() < deadline:
            if any(id(page) not in before_page_ids for page in context.pages):
                return
            await asyncio.sleep(_NEW_PAGE_POLL_INTERVAL_SECONDS)

    async def _locator_opens_new_page(self, locator: Any) -> bool:
        """Return whether a ref locator explicitly targets a new browsing context."""

        try:
            target = await locator.get_attribute("target", timeout=self.config.action_preflight_timeout_ms)
        except Exception:
            return False
        return str(target or "").strip().lower() == "_blank"

    async def _activate_new_page_if_created(self, *, context: Any, before_page_ids: set[int]) -> None:
        new_pages = [(index, page) for index, page in enumerate(context.pages) if id(page) not in before_page_ids]
        if not new_pages:
            return
        index, page = new_pages[-1]
        self._active_page_index = index
        try:
            await page.wait_for_load_state(
                "domcontentloaded", timeout=min(self.config.command_timeout_seconds, 2) * 1000
            )
        except Exception:
            pass
        try:
            await page.bring_to_front()
        except Exception:
            pass

    async def _validate_point(self, x: float, y: float) -> None:
        page = await self._page()
        viewport = page.viewport_size or _DEFAULT_VIEWPORT
        if x < 0 or y < 0 or x > float(viewport["width"]) or y > float(viewport["height"]):
            raise RuntimeError(
                f"point outside viewport: ({x}, {y}) not within {viewport['width']}x{viewport['height']}"
            )

    async def _page(self) -> Any:
        context = await self._browser_context()
        if not context.pages:
            await context.new_page()
        self._active_page_index = min(self._active_page_index, len(context.pages) - 1)
        page = context.pages[self._active_page_index]
        await page.bring_to_front()
        return page

    async def _browser_context(self) -> Any:
        if self._context is not None:
            return self._context
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - dependency is present in normal environments
            raise WebActionBackendUnavailableError(
                "Playwright is not installed. Install the 'playwright' package and browser binaries."
            ) from exc
        try:
            self._playwright = await async_playwright().start()
            launcher = getattr(self._playwright, self.config.playwright_browser)
            launch_options: dict[str, Any] = {"headless": not self.config.headed}
            if self.config.browser_executable_path:
                launch_options["executable_path"] = self.config.browser_executable_path
            if self.config.browser_channel is not None:
                if self.config.playwright_browser != "chromium":
                    raise ValueError("browser_channel requires playwright_browser=chromium")
                launch_options["channel"] = self.config.browser_channel
            self._browser = await launcher.launch(**launch_options)
            self._context = await self._browser.new_context(viewport=_DEFAULT_VIEWPORT)
            self._context.set_default_timeout(self.config.command_timeout_seconds * 1000)
            self._context.set_default_navigation_timeout(self.config.command_timeout_seconds * 1000)
            await self._context.new_page()
            return self._context
        except Exception as exc:
            await self.close()
            raise WebActionBackendUnavailableError(
                f"Playwright backend failed to start {self.config.playwright_browser}: {exc}"
            ) from exc

    async def _resolve_tab(self, tab_ref: str | None) -> tuple[Any, int]:
        context = await self._browser_context()
        pages = context.pages
        if not pages:
            raise RuntimeError("tab not found: no browser tabs are open")
        if tab_ref is None:
            index = min(self._active_page_index, len(pages) - 1)
            return pages[index], index
        normalized = tab_ref.strip()
        for index, page in enumerate(pages):
            if normalized == self._tab_ref(index) or normalized == str(index) or normalized == str(index + 1):
                return page, index
            if self._tab_labels_by_page.get(id(page)) == normalized:
                return page, index
        raise RuntimeError(f"tab not found: {tab_ref}")

    def _tab_info(self, page: Any, index: int) -> dict[str, object]:
        tab_ref = self._tab_ref(index)
        return {
            "ref": tab_ref,
            "id": tab_ref,
            "label": self._tab_labels_by_page.get(id(page), ""),
            "index": index,
            "url": page.url,
            "active": index == self._active_page_index,
        }

    def _tab_ref(self, index: int) -> str:
        return f"t{index + 1}"

    def _ok(self, data: object | None = None) -> WebActionCommandResult:
        return WebActionCommandResult(success=True, data={} if data is None else data)

    def _failed(self, exc: Exception) -> WebActionCommandResult:
        if isinstance(exc, WebActionCommandError):
            return WebActionCommandResult(
                success=False,
                error=self._format_error(exc),
                code=exc.code,
                reason_code=exc.reason_code,
                hint=exc.hint,
                concise_log_summary=exc.concise_log_summary,
                feedback_source=exc.feedback_source,
                debug_error=exc.debug_error,
                returncode=1,
            )
        return WebActionCommandResult(success=False, error=self._format_error(exc), returncode=1)

    def _format_error(self, exc: Exception) -> str:
        message = str(exc).strip()
        if not message:
            message = type(exc).__name__
        return re.sub(r"\s+", " ", message)

    def _canonical_ref(self, ref: object) -> str:
        text = str(ref).strip()
        if not text:
            raise RuntimeError("unknown ref: empty ref")
        if text.startswith("@"):
            return text
        if text.isdigit():
            return f"@e{text}"
        return f"@{text}"

    def _optional_int(self, value: object) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return None

    def _next_ref_index(self, value: object, *, fallback: int) -> int:
        if isinstance(value, bool):
            return fallback
        if isinstance(value, (int, float)):
            return max(1, int(value))
        if isinstance(value, str) and value.isdigit():
            return max(1, int(value))
        return fallback
