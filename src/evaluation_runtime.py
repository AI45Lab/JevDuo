"""Configure the official evaluator's supported model facade in this worker only."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def configure(spec: dict, output_root: Path) -> None:
    config = spec.get("evaluation_model")
    if not config:
        return
    import httpx
    from desktop_env.evaluators import model_client

    for field, variable in {
        "provider": "OSWORLD_EVAL_MODEL_PROVIDER",
        "model": "OSWORLD_EVAL_MODEL_NAME",
        "base_url": "OSWORLD_EVAL_MODEL_BASE_URL",
        "api_key": "OSWORLD_EVAL_MODEL_API_KEY",
    }.items():
        os.environ[variable] = str(config[field])
    original = model_client.generate_text
    original_backend = model_client.create_backend
    log_path = output_root / "worker" / "evaluation_model.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def create_backend(backend_config):
        backend = original_backend(backend_config)
        if hasattr(backend, "_client"):
            previous = backend._client
            backend._client = previous.with_options(
                timeout=float(config.get("timeout_seconds", 180)), max_retries=0,
                http_client=httpx.Client(trust_env=not config.get("direct_api", True)),
            )
            previous.close()
        return backend

    model_client.create_backend = create_backend

    def generate(*args, **kwargs):
        # Official tasks sometimes request only 200 output tokens. A reasoning
        # model needs a larger generation budget to return the same JSON verdict.
        options = dict(kwargs.get("options") or {})
        options["max_tokens"] = max(int(options.get("max_tokens", 0)),
                                    int(config.get("min_output_tokens", 8192)))
        kwargs["options"] = options
        started = time.monotonic()
        event = {"model": config["model"], "provider": config["provider"],
                 "requested_max_tokens": options["max_tokens"], "at": time.time()}
        try:
            result = original(*args, **kwargs)
            if not result or not result.strip():
                raise RuntimeError("Official evaluator model returned empty content")
            event.update(status="ok", response_chars=len(result))
            return result
        except Exception as error:
            event.update(status="error", error_type=type(error).__name__)
            raise
        finally:
            event["seconds"] = round(time.monotonic() - started, 3)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event) + "\n")

    model_client.generate_text = generate


def log_offset(output_root: Path) -> int:
    """Mark a task's start without clearing the job's evaluator evidence."""
    path = output_root / "worker" / "evaluation_model.jsonl"
    return path.stat().st_size if path.exists() else 0


def check_failures(output_root: Path, *, since: int = 0) -> None:
    """Reject swallowed judge outages recorded since this task began."""
    path = output_root / "worker" / "evaluation_model.jsonl"
    if path.exists():
        with path.open("rb") as stream:
            stream.seek(since)
            for line in stream:
                if json.loads(line).get("status") == "error":
                    raise RuntimeError("Official evaluator model failed; see worker/evaluation_model.jsonl")
