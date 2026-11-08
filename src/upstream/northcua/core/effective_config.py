"""Helpers for producing validated runtime app configs for live harnesses."""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from northcua_protocol.capabilities import JsonObject

from .config import NorthCUAAppConfig


def ensure_evaluation_config(
    *,
    source_path: str | Path,
    default_path: str | Path,
    output_path: str | Path,
) -> NorthCUAAppConfig:
    """Write a validated app config, adding default evaluation settings if absent.

    Live PiP harnesses often provide a temporary app config to point at local
    agent/model files. This helper keeps that override but prevents accidentally
    dropping NorthCUA's round evaluator block.

    Args:
        source_path: App config path supplied by the live harness.
        default_path: Default app config path used as the evaluator fallback.
        output_path: Destination path for the validated effective config.

    Returns:
        Parsed and validated application config written to ``output_path``.

    Raises:
        ValueError: If either YAML file is not a mapping, or defaults do not
            provide an ``evaluation`` mapping when the source omits it.
        pydantic.ValidationError: If the merged config fails app validation.
    """

    source = _load_mapping(Path(source_path))
    if "evaluation" not in source:
        default = _load_mapping(Path(default_path))
        evaluation = default.get("evaluation")
        if not isinstance(evaluation, dict):
            raise ValueError(f"{default_path} does not define an evaluation mapping")
        source["evaluation"] = evaluation
    config = NorthCUAAppConfig.model_validate(source)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return config


def _load_mapping(path: Path) -> JsonObject:
    payload: object = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return {str(key): value for key, value in payload.items()}


def main() -> None:
    """Build and write an effective app config from command-line arguments."""

    parser = argparse.ArgumentParser(description="Build a validated NorthCUA app config for PiP live harnesses.")
    parser.add_argument("--source", required=True, help="Input app config path.")
    parser.add_argument("--defaults", required=True, help="Default app config path used for missing evaluation block.")
    parser.add_argument("--output", required=True, help="Effective app config output path.")
    args = parser.parse_args()
    config = ensure_evaluation_config(
        source_path=args.source,
        default_path=args.defaults,
        output_path=args.output,
    )
    print(
        f"wrote {args.output}; evaluation.enabled={config.evaluation.enabled}; "
        f"schedule={config.evaluation.schedule.value}"
    )


if __name__ == "__main__":
    main()
