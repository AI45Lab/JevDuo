"""Create and validate portable public configurations."""

from __future__ import annotations

import argparse
from importlib.resources import files
from pathlib import Path


def register_config_commands(subparsers) -> None:
    parser = subparsers.add_parser("config", help="Create or validate configuration")
    commands = parser.add_subparsers(dest="config_action", required=True)
    init = commands.add_parser("init", help="Copy packaged examples into a writable directory")
    init.add_argument("--directory", default="config")
    init.add_argument("--force", action="store_true", help="Replace existing example files")
    init.add_argument("--model", help="Set the same model in Desktop and Web configs")
    init.add_argument("--base-url", help="Set the model API base URL in both agent configs")
    init.add_argument(
        "--api-type", choices=["openai_chat_completion", "openai_responses", "anthropic_chat_completion", "gemini_rest"]
    )
    init.add_argument("--api-key-env", help="Environment variable containing the API key (never pass the key itself)")
    validate = commands.add_parser("validate", help="Validate application and agent configuration")
    validate.add_argument("--app-config", required=True)
    schema = commands.add_parser("schema", help="Print the current JSON schema for application or batch configuration")
    schema.add_argument("--kind", choices=["app", "batch"], required=True)


def run_config_command(args: argparse.Namespace) -> int:
    if args.config_action == "schema":
        import json
        from northcua.core.config import NorthCUAAppConfig
        from northcua.batch.contracts import BatchManifest

        model = NorthCUAAppConfig if args.kind == "app" else BatchManifest
        print(json.dumps(model.model_json_schema(), indent=2))
        return 0
    if args.config_action == "init":
        import re
        import yaml

        key_env = getattr(args, "api_key_env", None)
        if key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
            raise ValueError("api-key-env must be an environment variable name")
        target = Path(args.directory).expanduser().resolve()
        examples = sorted(item for item in files("northcua.resources.config").iterdir() if item.name.endswith(".yaml"))
        collisions = [target / item.name for item in examples if (target / item.name).exists()]
        if collisions and not args.force:
            raise SystemExit(f"Configuration already exists: {collisions[0]}; choose another directory or --force")
        target.mkdir(parents=True, exist_ok=True)
        for item in examples:
            content = item.read_text(encoding="utf-8")
            overrides = {key: getattr(args, key, None) for key in ("model", "base_url", "api_type")}
            if item.name in {"agent.example.yaml", "agent.web.example.yaml"} and (any(overrides.values()) or key_env):
                data = yaml.safe_load(content)
                data["llm_config"].update({key: value for key, value in overrides.items() if value})
                if key_env:
                    data["llm_config"]["api_key"] = "${env." + key_env + "}"
                content = yaml.safe_dump(data, sort_keys=False)
            (target / item.name).write_text(content, encoding="utf-8")
        print(f"Configuration: {target / 'app.example.yaml'}")
        return 0
    from northcua.agents.desktop.observation.coordinator import validate_local_observation_config
    from northcua.core.config_loader import NorthCUAConfigLoader
    from northcua.core.enums import RuntimeMode

    for kind in ("desktop", "web"):
        bootstrap = NorthCUAConfigLoader.from_app_config(args.app_config, agent_kind=kind)
        if kind == "desktop" and bootstrap.app.websocket.runtime_mode != RuntimeMode.REMOTE:
            # Local Desktop runs build these observation probes at startup; fail here instead.
            validate_local_observation_config(bootstrap.app, bootstrap.agent)
    print("Desktop and Web configuration valid.")
    return 0
