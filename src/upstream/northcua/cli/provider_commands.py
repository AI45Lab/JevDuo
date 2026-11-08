"""Provider compatibility probe CLI command.

The command builds a provider config from CLI overrides or an agent config,
runs live compatibility probes, and prints a compact artifact summary.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .shared_args import resolve_log_root


def register_provider_probe_command(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register the live provider compatibility probe command.

    Args:
        subparsers: Shared argparse subparser collection.
    """

    provider_parser = subparsers.add_parser("provider", help="Provider compatibility utilities")
    provider_subparsers = provider_parser.add_subparsers(dest="provider_command", required=True)
    probe_parser = provider_subparsers.add_parser("probe", help="Run live compatibility probes for one provider config")
    probe_parser.add_argument("--agent-config", default=None, help="Path to a standalone agent.yaml to probe")
    probe_parser.add_argument("--model", default=None, help="Override model name")
    probe_parser.add_argument("--base-url", default=None, help="Override base URL")
    probe_parser.add_argument("--api-key", default=None, help="Override API key")
    probe_parser.add_argument(
        "--api-type",
        default=None,
        choices=["openai_chat_completion", "openai_responses", "anthropic_chat_completion"],
        help="Override api_type",
    )
    probe_parser.add_argument("--provider-profile", default=None, help="Optional provider profile label")
    probe_parser.add_argument(
        "--transcode-images-to-jpeg",
        action="store_true",
        help="Force PNG/data-url image inputs to JPEG before provider payload probing",
    )
    probe_parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory for structured probe outputs. Plain names are created under NorthCUA/logs/<name>.",
    )
    probe_parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
        help="Per-request timeout for live provider probes.",
    )


def run_provider_probe_command(args: argparse.Namespace) -> int:
    """Run the provider compatibility harness and print a compact summary.

    Args:
        args: Parsed provider-probe CLI arguments.

    Returns:
        Process-style exit code.

    Raises:
        SystemExit: If model, base_url, or api_key cannot be resolved.
    """

    from nexau.archs.llm.llm_config import LLMConfig

    from northcua.agentinfra.providers.provider_probe import run_provider_probe_suite
    from northcua.core.config_loader import NorthCUAConfigLoader

    if args.agent_config:
        agent_config = NorthCUAConfigLoader._load_agent_config(  # noqa: SLF001
            Path(args.agent_config).resolve(),
            strip_desktop_owned_prompt=False,
        )
        llm_config = agent_config.llm_config
    else:
        llm_config = LLMConfig(
            model=args.model,
            base_url=args.base_url,
            api_key=args.api_key,
            api_type=args.api_type or "openai_chat_completion",
        )
    if llm_config is None:
        raise SystemExit("provider-probe requires an LLM config (directly or via --agent-config).")

    if args.model:
        llm_config.model = args.model
    if args.base_url:
        llm_config.base_url = args.base_url
    if args.api_key:
        llm_config.api_key = args.api_key
    if args.api_type:
        llm_config.api_type = args.api_type
    if args.provider_profile or args.transcode_images_to_jpeg:
        extra_params = dict(getattr(llm_config, "extra_params", {}) or {})
        if args.provider_profile:
            extra_params["provider_profile"] = args.provider_profile
        if args.transcode_images_to_jpeg:
            extra_params["transcode_images_to_jpeg"] = True
        llm_config.extra_params = extra_params

    if not llm_config.model or not llm_config.base_url or not llm_config.api_key:
        raise SystemExit("provider-probe requires model, base_url, and api_key (directly or via --agent-config).")

    output_dir = resolve_log_root(args.output_dir) or (Path.cwd() / "logs" / f"provider-probe-{llm_config.api_type}")
    report = run_provider_probe_suite(
        llm_config=llm_config,
        output_dir=output_dir,
        timeout_seconds=args.timeout_seconds,
    )
    print(f"provider_mode: {llm_config.api_type}", flush=True)
    print(f"provider_profile: {report.profile.gateway_name or 'generic'}", flush=True)
    print(f"report: {output_dir / 'provider_probe_report.json'}", flush=True)
    for result in report.results:
        print(
            f"- {result.case_id}: {result.status}"
            + (f" (http={result.http_status})" if result.http_status is not None else "")
            + (f" excerpt={result.response_excerpt!r}" if result.response_excerpt else ""),
            flush=True,
        )
    return 0
