"""Shared coordinate-space mapping utilities for desktop and web runtimes.

The utilities resolve model-facing coordinate policies from config, context,
or environment values and map those coordinates into frame-relative or
runtime-absolute pixel positions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, MutableMapping

from northcua.core.config import CoordinatePolicyConfig, NorthCUAAppConfig
from northcua.core.enums import CoordinateSpace


class CoordinateTarget(str, Enum):
    """Execution surfaces that can resolve coordinate policies.

    Attributes:
        DESKTOP: Desktop-native CUA runtime.
    """

    DESKTOP = "desktop"


@dataclass(slots=True, frozen=True)
class CoordinatePolicy:
    """Resolved coordinate policy used by runtime mappers.

    Attributes:
        input_space: Coordinate space expected from model/tool input.
        clamp_to_bounds: Whether mapped coordinates are clamped to frame bounds.
    """

    input_space: CoordinateSpace
    clamp_to_bounds: bool = False


@dataclass(slots=True, frozen=True)
class CoordinatePoint:
    """One 2D coordinate point.

    Attributes:
        x: Horizontal coordinate.
        y: Vertical coordinate.
    """

    x: float
    y: float

    def as_int_tuple(self) -> tuple[int, int]:
        """Return the point as rounded integer coordinates.

        Returns:
            Rounded integer x/y tuple.
        """

        return int(round(self.x)), int(round(self.y))


@dataclass(slots=True, frozen=True)
class CoordinateFrame:
    """Runtime frame used when mapping model coordinates.

    Attributes:
        width: Frame width in pixels.
        height: Frame height in pixels.
        offset_x: Horizontal frame offset.
        offset_y: Vertical frame offset.
    """

    width: int
    height: int
    offset_x: float = 0.0
    offset_y: float = 0.0


@dataclass(slots=True, frozen=True)
class CoordinateEnvironmentSnapshot:
    """Previous coordinate-related environment values for restoration.

    Attributes:
        previous_values: Prior environment values keyed by variable name.
    """

    previous_values: dict[str, str | None]


_ENV_KEYS: dict[CoordinateTarget, tuple[str, str]] = {
    CoordinateTarget.DESKTOP: (
        "NORTHCUA_DESKTOP_COORDINATE_SPACE",
        "NORTHCUA_DESKTOP_COORDINATE_CLAMP_TO_BOUNDS",
    ),
}

_CONTEXT_KEYS: dict[CoordinateTarget, tuple[str, str]] = {
    CoordinateTarget.DESKTOP: (
        "northcua.desktop.coordinate_space",
        "northcua.desktop.coordinate_clamp_to_bounds",
    ),
}
_DEFAULT_POLICIES: dict[CoordinateTarget, CoordinatePolicy] = {
    CoordinateTarget.DESKTOP: CoordinatePolicy(input_space=CoordinateSpace.IMAGE_PIXELS),
}


class CoordinateMapper:
    """Map model-facing coordinates into frame-relative or runtime pixels."""

    @classmethod
    def to_frame_point(
        cls,
        point: CoordinatePoint,
        *,
        policy: CoordinatePolicy,
        frame: CoordinateFrame,
    ) -> CoordinatePoint:
        """Translate one model-space point into frame-relative pixels.

        Args:
            point: Model-facing coordinate point.
            policy: Coordinate interpretation policy.
            frame: Runtime coordinate frame.

        Returns:
            Frame-relative pixel coordinate point.
        """

        if policy.input_space == CoordinateSpace.ABSOLUTE_PIXELS:
            pixel_x = point.x - frame.offset_x
            pixel_y = point.y - frame.offset_y
        else:
            pixel_x = cls._to_pixels(point.x, policy.input_space, frame.width)
            pixel_y = cls._to_pixels(point.y, policy.input_space, frame.height)
        if policy.clamp_to_bounds:
            pixel_x = cls._clamp_frame_scalar(pixel_x, frame.width)
            pixel_y = cls._clamp_frame_scalar(pixel_y, frame.height)
        return CoordinatePoint(x=pixel_x, y=pixel_y)

    @classmethod
    def to_runtime_point(
        cls,
        point: CoordinatePoint,
        *,
        policy: CoordinatePolicy,
        frame: CoordinateFrame,
    ) -> CoordinatePoint:
        """Translate one model-space point into runtime-absolute pixels.

        Args:
            point: Model-facing coordinate point.
            policy: Coordinate interpretation policy.
            frame: Runtime coordinate frame.

        Returns:
            Runtime-absolute pixel coordinate point.
        """

        if policy.input_space == CoordinateSpace.ABSOLUTE_PIXELS:
            absolute_x = point.x
            absolute_y = point.y
            if policy.clamp_to_bounds:
                absolute_x = cls._clamp_absolute_scalar(
                    absolute_x,
                    offset=frame.offset_x,
                    size=frame.width,
                )
                absolute_y = cls._clamp_absolute_scalar(
                    absolute_y,
                    offset=frame.offset_y,
                    size=frame.height,
                )
            return CoordinatePoint(x=absolute_x, y=absolute_y)

        frame_point = cls.to_frame_point(point, policy=policy, frame=frame)
        return CoordinatePoint(
            x=frame.offset_x + frame_point.x,
            y=frame.offset_y + frame_point.y,
        )

    @staticmethod
    def _to_pixels(value: float, space: CoordinateSpace, size: int) -> float:
        if space == CoordinateSpace.IMAGE_PIXELS:
            return value
        if space == CoordinateSpace.NORMALIZED_UNIT:
            return value * max(size, 0)
        if space == CoordinateSpace.NORMALIZED_1000:
            return (value / 1000.0) * max(size, 0)
        return value

    @staticmethod
    def _clamp_frame_scalar(value: float, size: int) -> float:
        if size <= 0:
            return value
        return min(max(value, 0.0), float(size - 1))

    @staticmethod
    def _clamp_absolute_scalar(value: float, *, offset: float, size: int) -> float:
        if size <= 0:
            return value
        return min(max(value, offset), offset + float(size - 1))


def policy_from_config(config: CoordinatePolicyConfig) -> CoordinatePolicy:
    """Convert one pydantic config model into the lightweight runtime policy.

    Args:
        config: Pydantic coordinate policy config.

    Returns:
        Lightweight runtime coordinate policy.
    """

    return CoordinatePolicy(
        input_space=config.input_space,
        clamp_to_bounds=config.clamp_to_bounds,
    )


def coordinate_policy_context_values(
    target: CoordinateTarget,
    policy: CoordinatePolicy,
) -> dict[str, str]:
    """Return context extra values for one coordinate policy.

    Args:
        target: Runtime target whose context keys should be populated.
        policy: Coordinate policy to serialize.

    Returns:
        Context extra values keyed by runtime context field name.
    """

    space_key, clamp_key = _CONTEXT_KEYS[target]
    return {
        space_key: policy.input_space.value,
        clamp_key: _serialize_bool(policy.clamp_to_bounds),
    }


def non_default_coordinate_policy_context_values(
    target: CoordinateTarget,
    policy: CoordinatePolicy,
) -> dict[str, str]:
    """Return context values only when a policy differs from the target default.

    This keeps rolling upgrades quiet: default desktop pixel coordinates do not
    need to be sent over controller/device metadata for older workers to keep
    behaving in their historical mode.

    Args:
        target: Runtime target whose default policy should be compared.
        policy: Coordinate policy to serialize when it is non-default.

    Returns:
        Empty mapping for default policies, otherwise context values keyed by
        runtime context field name.
    """

    if _is_default_coordinate_policy(target, policy):
        return {}
    return coordinate_policy_context_values(target, policy)


def coordinate_policy_environment_values(
    target: CoordinateTarget,
    policy: CoordinatePolicy,
) -> dict[str, str]:
    """Return environment variable values for one coordinate policy.

    Args:
        target: Runtime target whose environment keys should be populated.
        policy: Coordinate policy to serialize.

    Returns:
        Environment variable values keyed by process environment name.
    """

    env_space_key, env_clamp_key = _ENV_KEYS[target]
    return {
        env_space_key: policy.input_space.value,
        env_clamp_key: _serialize_bool(policy.clamp_to_bounds),
    }


def _non_default_coordinate_policy_environment_values(
    target: CoordinateTarget,
    policy: CoordinatePolicy,
) -> dict[str, str]:
    """Return environment values only when a policy differs from the default.

    Args:
        target: Runtime target whose default policy should be compared.
        policy: Coordinate policy to serialize when it is non-default.

    Returns:
        Empty mapping for default policies, otherwise environment variable values.
    """

    if _is_default_coordinate_policy(target, policy):
        return {}
    return coordinate_policy_environment_values(target, policy)


non_default_coordinate_policy_environment_values = _non_default_coordinate_policy_environment_values


def _is_default_coordinate_policy(target: CoordinateTarget, policy: CoordinatePolicy) -> bool:
    """Return whether ``policy`` matches the target's implicit runtime default.

    Args:
        target: Runtime target whose default policy should be compared.
        policy: Coordinate policy to compare.

    Returns:
        True when the policy equals the target's implicit runtime default.
    """

    default_policy = _DEFAULT_POLICIES[target]
    return policy.input_space == default_policy.input_space and policy.clamp_to_bounds == default_policy.clamp_to_bounds


is_default_coordinate_policy = _is_default_coordinate_policy


def resolve_coordinate_policy(
    target: CoordinateTarget,
    *,
    context_values: Mapping[str, str] | None = None,
    environment: Mapping[str, str] | None = None,
    fallback_policy: CoordinatePolicy,
) -> CoordinatePolicy:
    """Resolve a policy from explicit context values, env, then fallback.

    Args:
        target: Runtime target whose policy should be resolved.
        context_values: Optional explicit context key/value mapping.
        environment: Optional environment mapping; defaults to process environment.
        fallback_policy: Fallback policy used when no override is present.

    Returns:
        Resolved coordinate policy.

    Raises:
        ValueError: If a resolved coordinate-space value is not supported.
    """

    space_key, clamp_key = _CONTEXT_KEYS[target]
    env_space_key, env_clamp_key = _ENV_KEYS[target]
    env = environment if environment is not None else os.environ

    resolved_space = (
        _get_mapping_value(context_values, space_key) or env.get(env_space_key) or fallback_policy.input_space.value
    )
    resolved_clamp = (
        _get_mapping_value(context_values, clamp_key)
        or env.get(env_clamp_key)
        or _serialize_bool(fallback_policy.clamp_to_bounds)
    )
    return CoordinatePolicy(
        input_space=CoordinateSpace(str(resolved_space)),
        clamp_to_bounds=_parse_bool(resolved_clamp, default=fallback_policy.clamp_to_bounds),
    )


def apply_app_coordinate_environment(
    app_config: NorthCUAAppConfig,
    *,
    environment: MutableMapping[str, str] | None = None,
) -> CoordinateEnvironmentSnapshot:
    """Export configured coordinate policies into the current process environment.

    Args:
        app_config: NorthCUA application config containing target policies.
        environment: Optional environment mapping; defaults to process environment.

    Returns:
        Snapshot of previous environment values for later restoration.
    """

    env = environment if environment is not None else os.environ
    policies = {
        CoordinateTarget.DESKTOP: policy_from_config(app_config.desktop.coordinate_policy),
    }
    previous: dict[str, str | None] = {}
    for target, policy in policies.items():
        env_space_key, env_clamp_key = _ENV_KEYS[target]
        previous[env_space_key] = env.get(env_space_key)
        previous[env_clamp_key] = env.get(env_clamp_key)
        env[env_space_key] = policy.input_space.value
        env[env_clamp_key] = _serialize_bool(policy.clamp_to_bounds)
    return CoordinateEnvironmentSnapshot(previous_values=previous)


def restore_coordinate_environment(
    snapshot: CoordinateEnvironmentSnapshot,
    *,
    environment: MutableMapping[str, str] | None = None,
) -> None:
    """Restore previously exported coordinate environment values.

    Args:
        snapshot: Previous environment values returned by apply_app_coordinate_environment.
        environment: Optional environment mapping; defaults to process environment.
    """

    env = environment if environment is not None else os.environ
    for key, previous in snapshot.previous_values.items():
        if previous is None:
            env.pop(key, None)
        else:
            env[key] = previous


def describe_coordinate_axis(
    policy: CoordinatePolicy,
    *,
    axis: str,
    relative_surface: str,
    absolute_surface: str,
) -> str:
    """Return an axis-specific description for tool schemas.

    Args:
        policy: Coordinate policy being described.
        axis: Axis label, usually x or y.
        relative_surface: Surface name for relative coordinate policies.
        absolute_surface: Surface name for absolute coordinate policies.

    Returns:
        Human-readable axis description.
    """

    if policy.input_space == CoordinateSpace.IMAGE_PIXELS:
        return f"{axis} pixel coordinate relative to the {relative_surface}."
    if policy.input_space == CoordinateSpace.NORMALIZED_UNIT:
        return f"{axis} coordinate normalized to the [0,1] range relative to the {relative_surface}."
    if policy.input_space == CoordinateSpace.NORMALIZED_1000:
        return f"{axis} coordinate normalized to the [0,1000] range relative to the {relative_surface}."
    return f"Absolute {absolute_surface} {axis.lower()} pixel coordinate."


def describe_coordinate_instruction(
    policy: CoordinatePolicy,
    *,
    relative_surface: str,
    absolute_surface: str,
) -> str:
    """Return a prompt-friendly summary of the active coordinate contract.

    Args:
        policy: Coordinate policy being described.
        relative_surface: Surface name for relative coordinate policies.
        absolute_surface: Surface name for absolute coordinate policies.

    Returns:
        Prompt-friendly coordinate instruction.
    """

    if policy.input_space == CoordinateSpace.IMAGE_PIXELS:
        return f"Return screenshot pixel x,y relative to the {relative_surface}."
    if policy.input_space == CoordinateSpace.NORMALIZED_UNIT:
        return f"Return x,y normalized to [0,1] relative to the {relative_surface}."
    if policy.input_space == CoordinateSpace.NORMALIZED_1000:
        return f"Return x,y normalized to [0,1000] relative to the {relative_surface}."
    return f"Return absolute pixel x,y in the {absolute_surface}."


def _get_mapping_value(values: Mapping[str, str] | None, key: str) -> str | None:
    if values is None:
        return None
    raw = values.get(key)
    if raw is None:
        return None
    return str(raw)


def _parse_bool(value: str | bool, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _serialize_bool(value: bool) -> str:
    return "true" if value else "false"
