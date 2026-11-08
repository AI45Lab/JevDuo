"""Tool registration helpers for desktop-native action MCP tools."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from northcua.runtime.mcp.contracts import register_contract_handlers
from northcua.runtime.mcp.types import DesktopNativeActionResultPayload
from northcua_runtime_shared.desktop_surface.action_contracts import (
    DesktopClickParams,
    DesktopDoubleClickParams,
    DesktopDragParams,
    DesktopKeypressParams,
    DesktopMoveParams,
    DesktopNoteParams,
    DesktopScreenshotParams,
    DesktopScrollParams,
    DesktopTypeParams,
    DesktopWaitParams,
    desktop_action_contracts,
)
from northcua_runtime_shared.desktop_surface.models import TextTargetKind

from .backend import DesktopNativeBackend, DesktopNativePoint
from .image import build_action_result, build_screenshot_result
from .state import DesktopNativeServerState, backend_error

logger = logging.getLogger(__name__)


def _register_desktop_native_action_tools(
    action_mcp: FastMCP,
    state: DesktopNativeServerState,
    backend: DesktopNativeBackend,
) -> None:
    register_contract_handlers(action_mcp, desktop_action_contracts(), _desktop_action_handler_map(state, backend))


def _translate_point(state: DesktopNativeServerState, x: float, y: float) -> tuple[int, int]:
    return state.translate_point(x, y)


def _translate_frame_point(state: DesktopNativeServerState, x: float, y: float) -> tuple[int, int]:
    return state.translate_frame_point(x, y)


def _model_payload(**values: object) -> dict[str, object]:
    return {key: value for key, value in values.items() if value is not None}


def _elapsed_ms(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)


def _point_payloads(
    state: DesktopNativeServerState,
    *,
    x: float,
    y: float,
    **extra: object,
) -> tuple[int, int, dict[str, object], dict[str, object]]:
    frame_x, frame_y = _translate_frame_point(state, x, y)
    abs_x, abs_y = _translate_point(state, x, y)
    pixel_payload = {"x": frame_x, "y": frame_y, **extra}
    runtime_payload = {"x": abs_x, "y": abs_y, **extra}
    return abs_x, abs_y, pixel_payload, runtime_payload


def _translate_path(
    state: DesktopNativeServerState,
    path: Sequence[DesktopNativePoint],
) -> list[DesktopNativePoint]:
    return [
        DesktopNativePoint(
            x=runtime_x,
            y=runtime_y,
        )
        for point in path
        for runtime_x, runtime_y in [_translate_point(state, point.x, point.y)]
    ]


def _desktop_action_handler_map(
    state: DesktopNativeServerState,
    backend: DesktopNativeBackend,
) -> dict[str, Callable[..., object]]:
    return {
        "note": _note,
        "screenshot": lambda params: _screenshot(params, state, backend),
        "click": lambda params: _click(params, state, backend),
        "double_click": lambda params: _double_click(params, state, backend),
        "drag": lambda params: _drag(params, state, backend),
        "move": lambda params: _move(params, state, backend),
        "scroll": lambda params: _scroll(params, state, backend),
        "type": lambda params: _type_text(params, state, backend),
        "keypress": lambda params: _keypress(params, state, backend),
        "wait": lambda params: _wait(params, state, backend),
    }


def _note(params: DesktopNoteParams) -> DesktopNativeActionResultPayload:
    text = params.text
    reason = params.reason
    note_kind = params.kind
    clean_text = text.strip()
    clean_reason = reason.strip()
    if not clean_text:
        raise ToolError("note action requires non-empty `text`.")

    result_text = f"[{note_kind}] {clean_text}"
    if clean_reason:
        result_text = f"{result_text}\nReason: {clean_reason}"
    logger.info(
        "DesktopNativeActionExecutor recorded note kind=%s text_chars=%s reason_chars=%s",
        note_kind,
        len(clean_text),
        len(clean_reason),
    )
    return {
        "text": result_text,
        "status": "recorded",
        "kind": note_kind,
        "note_text": clean_text,
        "reason": clean_reason,
        "screenshot_policy": "none",
    }


def _screenshot(
    params: DesktopScreenshotParams,
    state: DesktopNativeServerState,
    backend: DesktopNativeBackend,
) -> DesktopNativeActionResultPayload:
    try:
        logger.info("DesktopNativeActionExecutor capturing screenshot (all_screens=%s)", params.all_screens)
        return build_screenshot_result(
            state,
            backend,
            "Captured a fresh desktop screenshot.",
            all_screens=params.all_screens,
        )
    except Exception as exc:  # pragma: no cover - thin error adaptation
        logger.exception("DesktopNativeActionExecutor screenshot failed")
        raise backend_error(exc)


def _click(
    params: DesktopClickParams,
    state: DesktopNativeServerState,
    backend: DesktopNativeBackend,
) -> DesktopNativeActionResultPayload:
    try:
        abs_x, abs_y, pixel_payload, runtime_payload = _point_payloads(
            state,
            x=params.x,
            y=params.y,
            button=params.button,
        )
        logger.info("DesktopNativeActionExecutor click at (%s, %s) with button=%s", abs_x, abs_y, params.button)
        action_started_at = time.perf_counter()
        action_text = backend.click(abs_x, abs_y, button=params.button)
        return build_action_result(
            state,
            backend,
            action_text,
            tool_input_model=params.model_dump(mode="json"),
            tool_input_pixels=pixel_payload,
            tool_input_runtime_pixels=runtime_payload,
            stage_timings={"backend_action_ms": _elapsed_ms(action_started_at)},
        )
    except Exception as exc:  # pragma: no cover - thin error adaptation
        logger.exception("DesktopNativeActionExecutor click failed")
        raise backend_error(exc)


def _double_click(
    params: DesktopDoubleClickParams,
    state: DesktopNativeServerState,
    backend: DesktopNativeBackend,
) -> DesktopNativeActionResultPayload:
    try:
        abs_x, abs_y, pixel_payload, runtime_payload = _point_payloads(
            state,
            x=params.x,
            y=params.y,
            button=params.button,
        )
        logger.info(
            "DesktopNativeActionExecutor double_click at (%s, %s) with button=%s",
            abs_x,
            abs_y,
            params.button,
        )
        action_started_at = time.perf_counter()
        action_text = backend.double_click(abs_x, abs_y, button=params.button)
        return build_action_result(
            state,
            backend,
            action_text,
            tool_input_model=params.model_dump(mode="json"),
            tool_input_pixels=pixel_payload,
            tool_input_runtime_pixels=runtime_payload,
            stage_timings={"backend_action_ms": _elapsed_ms(action_started_at)},
        )
    except Exception as exc:  # pragma: no cover - thin error adaptation
        logger.exception("DesktopNativeActionExecutor double_click failed")
        raise backend_error(exc)


def _drag(
    params: DesktopDragParams,
    state: DesktopNativeServerState,
    backend: DesktopNativeBackend,
) -> DesktopNativeActionResultPayload:
    try:
        params_button = params.button
        path = [DesktopNativePoint(x=point.x, y=point.y) for point in params.path]
        translated_path = _translate_path(state, path)
        frame_path = [
            {"x": frame_x, "y": frame_y}
            for point in path
            for frame_x, frame_y in [_translate_frame_point(state, point.x, point.y)]
        ]
        runtime_path = [{"x": point.x, "y": point.y} for point in translated_path]
        logger.info(
            "DesktopNativeActionExecutor drag with %s point(s) and button=%s",
            len(translated_path),
            params_button,
        )
        action_started_at = time.perf_counter()
        action_text = backend.drag(translated_path, button=params_button)
        return build_action_result(
            state,
            backend,
            action_text,
            tool_input_model=params.model_dump(mode="json"),
            tool_input_pixels={"path": frame_path, "button": params_button},
            tool_input_runtime_pixels={"path": runtime_path, "button": params_button},
            stage_timings={"backend_action_ms": _elapsed_ms(action_started_at)},
        )
    except ToolError:
        raise
    except Exception as exc:  # pragma: no cover - thin error adaptation
        logger.exception("DesktopNativeActionExecutor drag failed")
        raise backend_error(exc)


def _move(
    params: DesktopMoveParams,
    state: DesktopNativeServerState,
    backend: DesktopNativeBackend,
) -> DesktopNativeActionResultPayload:
    try:
        abs_x, abs_y, pixel_payload, runtime_payload = _point_payloads(state, x=params.x, y=params.y)
        logger.info("DesktopNativeActionExecutor move to (%s, %s)", abs_x, abs_y)
        action_started_at = time.perf_counter()
        action_text = backend.move(abs_x, abs_y)
        return build_action_result(
            state,
            backend,
            action_text,
            tool_input_model=params.model_dump(mode="json"),
            tool_input_pixels=pixel_payload,
            tool_input_runtime_pixels=runtime_payload,
            stage_timings={"backend_action_ms": _elapsed_ms(action_started_at)},
        )
    except Exception as exc:  # pragma: no cover - thin error adaptation
        logger.exception("DesktopNativeActionExecutor move failed")
        raise backend_error(exc)


def _scroll(
    params: DesktopScrollParams,
    state: DesktopNativeServerState,
    backend: DesktopNativeBackend,
) -> DesktopNativeActionResultPayload:
    try:
        abs_x: int | None = None
        abs_y: int | None = None
        pixel_payload = params.model_dump(mode="json")
        runtime_payload: dict[str, object] = {}
        if params.x is not None and params.y is not None:
            abs_x, abs_y, pixel_payload, runtime_payload = _point_payloads(
                state,
                x=params.x,
                y=params.y,
                delta_x=params.delta_x,
                delta_y=params.delta_y,
            )

        logger.info(
            "DesktopNativeActionExecutor scroll at (%s, %s) with deltas (%s, %s)",
            abs_x,
            abs_y,
            params.delta_x,
            params.delta_y,
        )
        action_started_at = time.perf_counter()
        action_text = backend.scroll(x=abs_x, y=abs_y, delta_x=params.delta_x, delta_y=params.delta_y)
        return build_action_result(
            state,
            backend,
            action_text,
            tool_input_model=params.model_dump(mode="json"),
            tool_input_pixels=pixel_payload,
            tool_input_runtime_pixels=runtime_payload,
            stage_timings={"backend_action_ms": _elapsed_ms(action_started_at)},
        )
    except Exception as exc:  # pragma: no cover - thin error adaptation
        logger.exception("DesktopNativeActionExecutor scroll failed")
        raise backend_error(exc)


def _type_text(
    params: DesktopTypeParams,
    state: DesktopNativeServerState,
    backend: DesktopNativeBackend,
) -> DesktopNativeActionResultPayload:
    try:
        resolved_target_kind = TextTargetKind.coerce(params.target_kind)
        text = params.text
        logger.info(
            "DesktopNativeActionExecutor typing %s character(s) target_kind=%s",
            len(str(text or "")),
            resolved_target_kind.value,
        )
        action_started_at = time.perf_counter()
        action_text = backend.type_text(text, target_kind=resolved_target_kind)
        return build_action_result(
            state,
            backend,
            action_text,
            tool_input_model={"text": text, "target_kind": resolved_target_kind.value},
            tool_input_pixels={"text": text, "target_kind": resolved_target_kind.value},
            stage_timings={"backend_action_ms": _elapsed_ms(action_started_at), "text_chars": len(str(text or ""))},
        )
    except Exception as exc:  # pragma: no cover - thin error adaptation
        logger.exception("DesktopNativeActionExecutor type failed")
        raise backend_error(exc)


def _keypress(
    params: DesktopKeypressParams,
    state: DesktopNativeServerState,
    backend: DesktopNativeBackend,
) -> DesktopNativeActionResultPayload:
    try:
        key = params.key
        keys = params.keys
        logger.info("DesktopNativeActionExecutor keypress with key=%s keys=%s", key, keys)
        input_payload = _model_payload(key=key, keys=keys)
        action_started_at = time.perf_counter()
        action_text = backend.keypress(key=key, keys=keys)
        return build_action_result(
            state,
            backend,
            action_text,
            tool_input_model=input_payload,
            tool_input_pixels=input_payload,
            stage_timings={"backend_action_ms": _elapsed_ms(action_started_at)},
        )
    except ToolError:
        raise
    except Exception as exc:  # pragma: no cover - thin error adaptation
        logger.exception("DesktopNativeActionExecutor keypress failed")
        raise backend_error(exc)


async def _wait(
    params: DesktopWaitParams,
    state: DesktopNativeServerState,
    backend: DesktopNativeBackend,
) -> DesktopNativeActionResultPayload:
    try:
        ms = params.ms
        if ms < 0:
            raise ToolError("wait action requires a non-negative `ms` value.")
        logger.info("DesktopNativeActionExecutor waiting for %s ms", ms)
        wait_started_at = time.perf_counter()
        await asyncio.sleep(ms / 1000.0)
        return build_action_result(
            state,
            backend,
            f"Waited for {ms} ms.",
            tool_input_model={"ms": ms},
            tool_input_pixels={"ms": ms},
            stage_timings={"backend_action_ms": _elapsed_ms(wait_started_at), "requested_wait_ms": ms},
        )
    except ToolError:
        raise
    except Exception as exc:  # pragma: no cover - thin error adaptation
        logger.exception("DesktopNativeActionExecutor wait failed")
        raise backend_error(exc)
