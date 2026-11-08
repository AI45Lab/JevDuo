"""配置加载与 job spec 渲染（只在开发机跑：需要 PyYAML）。

worker 侧不 import 本模块——冻结的 OSWORLD_PYTHON 没有 PyYAML，
所以 pool 在这里把配置渲染成 JSON job spec，worker 只读 JSON。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import yaml

TASK_IDS = tuple(f"{number:03d}" for number in range(1, 109))


class ConfigError(ValueError):
    pass


def load(path: str | Path) -> dict:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError(f"配置不是映射结构: {path}")
    for section in ("project", "models", "harness", "pool", "cluster", "benchmark"):
        if section not in data:
            raise ConfigError(f"配置缺少段: {section}")
    return data


def select_model(config: dict, name: str | None) -> tuple[str, dict]:
    models = config["models"]
    key = name or models.get("default")
    apis = models["apis"]
    if key not in apis:
        raise ConfigError(f"未知模型 {key!r}；可选: {', '.join(apis)}")
    return key, dict(apis[key])


def resolve_tasks(config: dict, *, tasks: str, limit: int | None, shard: str | None) -> list[str]:
    refs = [item.strip() for item in (tasks or "").split(",") if item.strip()] or list(TASK_IDS)
    unsupported = [item for item in refs if item not in TASK_IDS]
    if unsupported:
        raise ConfigError(f"OSWorld 2.1 只有 001-108，收到越界题号: {unsupported}")
    if limit:
        refs = refs[:limit]
    if shard:
        index, _, total = shard.partition("/")
        try:
            index_int, total_int = int(index), int(total)
        except ValueError as error:
            raise ConfigError(f"--shard 应为 I/N，收到 {shard!r}") from error
        if not 1 <= index_int <= total_int:
            raise ConfigError(f"shard.index 必须在 1..{total_int}（收到 {index_int}）")
        refs = refs[index_int - 1::total_int]
    return refs


def chunk(tasks: list[str], per_job: int) -> list[list[str]]:
    """轮转切片：长尾题分散到不同 job，而不是全挤在最后一个。"""
    if per_job <= 0:
        raise ConfigError("tasks_per_job 必须为正")
    total_jobs = max(1, -(-len(tasks) // per_job)) if tasks else 0
    return [tasks[index::total_jobs] for index in range(total_jobs)] if total_jobs else []


def _key(api: dict) -> str:
    name = api.get("api_key_env") or ""
    value = os.environ.get(name, "") if name else ""
    if name and not value:
        raise ConfigError(f"环境变量 {name} 为空（模型 {api.get('model')} 需要 key）")
    return value


def _registry_auth(config: dict) -> dict:
    """docker config 要 base64(user:pass)；环境变量里写裸 user:pass 也接受，自动编码。"""
    result = {}
    for host, entry in ((config.get("cluster") or {}).get("registry_auth") or {}).items():
        raw = os.environ.get(entry.get("auth_env") or "", "").strip()
        if not raw:
            continue
        if ":" in raw and not _is_base64(raw):
            raw = base64.b64encode(raw.encode("utf-8")).decode("ascii")
        result[host] = {"auth": raw}
    return result


def _is_base64(value: str) -> bool:
    try:
        base64.b64decode(value + "=" * (-len(value) % 4), validate=True)
    except (ValueError, TypeError):
        return False
    return True


def render_spec(config: dict, *, run_id: str, job_id: int, tasks: list[str],
                api_name: str, api: dict, run_root: Path) -> dict:
    benchmark = config["benchmark"]
    harness = config["harness"]
    gitlab = ""
    token_file = benchmark.get("gitlab_token_file")
    if token_file and Path(token_file).is_file():
        gitlab = Path(token_file).read_text(encoding="utf-8").strip()
    return {
        "run_id": run_id,
        "job_id": job_id,
        "output_root": str(run_root / "jobs" / f"job-{job_id:02d}"),
        "tasks": list(tasks),
        "model": {
            "name": api_name,
            "base_url": api["base_url"],
            "model": api["model"],
            "api_key": _key(api),
            "max_tokens": int(api.get("max_tokens", 16384)),
            "max_retries": int(api.get("max_retries", 3)),
            "timeout_seconds": float(api.get("timeout_seconds", 600)),
            "reasoning_effort": api.get("reasoning_effort"),
            "direct_api": bool(api.get("direct_api", False)),
        },
        "harness": harness,
        "benchmark": {
            "name": benchmark["name"],
            "task_root": benchmark["task_root"],
            "vm": benchmark["vm"],
            "asset_root": benchmark.get("asset_root", ""),
            "shared_env": benchmark.get("shared_env", ""),
            "inner_image": benchmark.get("inner_image", ""),
            "proxy": benchmark.get("proxy", ""),
            "no_proxy": benchmark.get("no_proxy", ""),
            "startup_wait_seconds": float(benchmark.get("startup_wait_seconds", 30)),
        },
        "cluster": {"registry_auth": _registry_auth(config)},
        "log": {"screenshots": bool(config["log"].get("screenshots", True)),
                "llm_calls": bool(config["log"].get("llm_calls", True))},
        "secrets": {"gitlab_private_token": gitlab},
    }


def write_spec(spec: dict, spec_dir: Path) -> Path:
    spec_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = spec_dir / f"job-{spec['job_id']:02d}.json"
    payload = json.dumps(spec, ensure_ascii=False, indent=2)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(payload)
    return path


def masked(spec: dict) -> dict:
    """落盘副本：key 一律打码。"""
    copy = json.loads(json.dumps(spec, ensure_ascii=False))
    model = copy.get("model") or {}
    if model.get("api_key"):
        model["api_key"] = "***"
    secrets = copy.get("secrets") or {}
    if secrets.get("gitlab_private_token"):
        secrets["gitlab_private_token"] = "***"
    return copy


def describe(path: str | Path) -> str:
    data = Path(path).read_bytes()
    return hashlib.sha256(data).hexdigest()[:16]
