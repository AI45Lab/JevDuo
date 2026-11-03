"""infra/config.json 的 schema 解释器：校验 + 按名字选 API。

配置分三段，职责互不重叠：

- `harness`：慢模型（大脑）/ 快模型（小脑）/ 路由 / 预算；
- `eval`：集群与基准（rjob、镜像、基准资产、registry 凭据）；
- `log`：结果落盘（唯一落盘位置 + 记录粒度）。

慢模型与快模型都是**命名 API 登记表**（`apis`）：每个条目一个 API，
启动命令用 `--slow-model` / `--fast-model` 按名字挑，默认取各自的 `default`。
登记多个 API 便于横向比较"不同大脑 × 不同小脑"的性能差距——
换组合只改命令行，不改配置结构。

本模块只管"配置长什么样、怎么选"，不碰提交与运行流程。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tandem.models.key_pool import POOL_BASE_URLS, validate_key_ids

#: 必填路径（点号路径，逐段下钻；None / 空串 / 空列表视为缺失）
REQUIRED_PATHS = (
    "harness.slow_model.default",
    "harness.fast_model.default",
    "harness.route.policy",
    "harness.route.allow_cli",
    "harness.route.cli_timeout_seconds",
    "harness.budget.max_steps",
    "harness.budget.max_wall_seconds",
    "eval.repo_root",
    "eval.deploy_root",
    "eval.mounts",
    "eval.rjob.image",
    "eval.inner_image",
    "eval.benchmarks.osworld1.root",
    "eval.benchmarks.osworld1.vm",
    "eval.benchmarks.osworld2.root",
    "eval.benchmarks.osworld2.vm",
    "eval.benchmarks.osworld2.asset_root",
    "eval.benchmarks.osworld21.root",
    "eval.benchmarks.osworld21.vm",
    "eval.benchmarks.osworld21.asset_root",
    "eval.benchmarks.osworld21.shared_env",
    "eval.shared_env",
    "eval.registry",
    "log.results_root",
)

#: 必须是绝对路径的键（纪律：配置里所有路径都是绝对路径，开发机与 worker 内一致）
ABSOLUTE_PATH_PATHS = (
    "eval.repo_root",
    "eval.deploy_root",
    "eval.shared_env",
    "log.results_root",
    "eval.benchmarks.osworld1.root",
    "eval.benchmarks.osworld1.vm",
    "eval.benchmarks.osworld2.root",
    "eval.benchmarks.osworld2.vm",
    "eval.benchmarks.osworld2.asset_root",
    "eval.benchmarks.osworld21.root",
    "eval.benchmarks.osworld21.vm",
    "eval.benchmarks.osworld21.asset_root",
    "eval.benchmarks.osworld21.shared_env",
    "eval.benchmarks.osworld21.gitlab.private_token_file",
    "harness.fast_model.image_stage.write",
    "harness.fast_model.image_stage.read",
)

#: 选择后端：jev /rank、kev /v1/systemone；planner 复用 slow_model，不需要独立 endpoint。
FAST_BACKENDS = ("jev", "kev", "planner")

#: 慢模型每个 API 条目必填的键
SLOW_API_KEYS = ("base_url", "model")

#: 思考强度档位（与 harness 侧 SessionConfig.slow_reasoning_effort 保持一致）
REASONING_EFFORTS = ("off", "low", "medium", "high", "max")


class ConfigError(ValueError):
    """配置缺项或取值非法（配置问题，不是运行问题）。"""


def get_path(config: dict, path: str) -> Any:
    """点号路径取值；任一段不存在返回 None。"""
    node: Any = config
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return not value
    return False


def _require(config: dict, path: str) -> list[str]:
    return [path] if _is_missing(get_path(config, path)) else []


def _validate_slow_models(config: dict) -> list[str]:
    problems: list[str] = []
    apis = get_path(config, "harness.slow_model.apis")
    if not isinstance(apis, dict) or not apis:
        return ["harness.slow_model.apis"]
    for name, api in apis.items():
        for key in SLOW_API_KEYS:
            value = api.get(key) if isinstance(api, dict) else None
            if _is_missing(value):
                problems.append(f"harness.slow_model.apis.{name}.{key}")
        if not isinstance(api, dict):
            continue
        base_url = api.get("base_url")
        if isinstance(base_url, str) and base_url.strip() and not base_url.startswith("http"):
            problems.append(f"harness.slow_model.apis.{name}.base_url（应为 http(s) 地址）")
        pool = api.get("key_pool")
        if isinstance(pool, dict):
            try:
                validate_key_ids(pool.get("key_ids", []))
            except ValueError:
                problems.append(f"harness.slow_model.apis.{name}.key_pool.key_ids（应为不重复的正整数列表）")
            if api.get("api_key") and pool.get("key_ids"):
                problems.append(f"harness.slow_model.apis.{name}（api_key 与 key_ids 不能同时指定）")
        if _is_missing(api.get("api_key")):
            prefix = f"harness.slow_model.apis.{name}.key_pool"
            if not isinstance(pool, dict):
                problems.append(f"harness.slow_model.apis.{name}.api_key / .key_pool（至少给一个）")
            else:
                provider = pool.get("provider")
                if provider not in POOL_BASE_URLS:
                    problems.append(f"{prefix}.provider（应为 minimax / kimi）")
                elif not isinstance(base_url, str) or base_url.rstrip("/") != POOL_BASE_URLS[provider]:
                    problems.append(f"{prefix}.provider 与官方 base_url 不匹配")
                for key in ("credentials_file", "state_file"):
                    path = pool.get(key)
                    if not isinstance(path, str) or not path.startswith("/"):
                        problems.append(f"{prefix}.{key}（必须是绝对路径）")
        # 思考强度必须落在 harness 认识的档位里，否则 SessionConfig 会在运行期才报错
        effort = api.get("reasoning_effort")
        if effort is not None and effort not in REASONING_EFFORTS:
            problems.append(
                f"harness.slow_model.apis.{name}.reasoning_effort"
                f"（应为 {' / '.join(REASONING_EFFORTS)}）"
            )
    default = get_path(config, "harness.slow_model.default")
    if default not in apis:
        problems.append(f"harness.slow_model.default={default!r}（必须在 apis 里）")
    return problems


def _validate_fast_models(config: dict) -> list[str]:
    problems: list[str] = []
    apis = get_path(config, "harness.fast_model.apis")
    if not isinstance(apis, dict) or not apis:
        return ["harness.fast_model.apis"]
    for name, api in apis.items():
        if not isinstance(api, dict):
            problems.append(f"harness.fast_model.apis.{name}")
            continue
        if api.get("backend") not in FAST_BACKENDS:
            problems.append(
                f"harness.fast_model.apis.{name}.backend"
                f"（应为 {' 或 '.join(FAST_BACKENDS)}）"
            )
        endpoint = api.get("endpoint")
        endpoint_file = api.get("endpoint_file")
        # endpoint 允许留空 = 自动从发现文件读取最新地址（服务闲时部署，地址会变）
        if api.get("backend") != "planner" and _is_missing(endpoint) and _is_missing(endpoint_file):
            problems.append(
                f"harness.fast_model.apis.{name}.endpoint / .endpoint_file（至少给一个）"
            )
        if isinstance(endpoint, str) and endpoint.strip() and not endpoint.startswith("http"):
            problems.append(f"harness.fast_model.apis.{name}.endpoint（应为 http(s) 地址）")
        if isinstance(endpoint_file, str) and endpoint_file.strip():
            if not endpoint_file.startswith("/"):
                problems.append(f"harness.fast_model.apis.{name}.endpoint_file（必须是绝对路径）")
    default = get_path(config, "harness.fast_model.default")
    if default not in apis:
        problems.append(f"harness.fast_model.default={default!r}（必须在 apis 里）")
    return problems


def _validate_absolute_paths(config: dict) -> list[str]:
    problems: list[str] = []
    for path in ABSOLUTE_PATH_PATHS:
        value = get_path(config, path)
        if isinstance(value, str) and value.strip() and not value.startswith("/"):
            problems.append(f"{path}（必须是绝对路径）")
    return problems


def _validate_osworld21(config: dict) -> list[str]:
    """OSW2.1 专项校验：镜像必须发布固定（禁 latest），代理与卷参数合法。

    inner_image 允许留空——它只在真正跑 osworld21 时才需要，届时
    build_job_spec 会明确报错，而不是静默拿错镜像。
    """
    problems: list[str] = []
    section = get_path(config, "eval.benchmarks.osworld21") or {}
    image = section.get("inner_image")
    if isinstance(image, str) and image.strip():
        if ":latest" in image:
            problems.append("eval.benchmarks.osworld21.inner_image（不能用 latest，必须发布固定版本）")
        elif "@sha256:" not in image:
            problems.append("eval.benchmarks.osworld21.inner_image（应固定到 digest：<repo>@sha256:…）")
    proxy = section.get("proxy")
    if not _is_missing(proxy) and not str(proxy).startswith("http"):
        problems.append("eval.benchmarks.osworld21.proxy（应为 http(s) 地址）")
    volume_size = section.get("volume_size")
    if volume_size is not None and (not isinstance(volume_size, int) or volume_size <= 0):
        problems.append("eval.benchmarks.osworld21.volume_size（应为正整数）")
    for role in ("judge", "user_simulator"):
        api = section.get(role, {})
        if api.get("api_key"):
            for key in ("provider", "base_url", "model"):
                if _is_missing(api.get(key)):
                    problems.append(f"eval.benchmarks.osworld21.{role}.{key}")
    return problems


def validate(config: dict) -> list[str]:
    """返回全部问题（空列表 = 配置合法）。"""
    problems: list[str] = []
    for path in REQUIRED_PATHS:
        problems += _require(config, path)
    problems += _validate_slow_models(config)
    problems += _validate_fast_models(config)
    problems += _validate_absolute_paths(config)
    problems += _validate_osworld21(config)
    execution = get_path(config, "harness.execution") or {}
    if not isinstance(execution, dict):
        problems.append("harness.execution（应为对象）")
    else:
        if execution.get("mode", "step") not in {"step", "stage", "goal"}:
            problems.append("harness.execution.mode（应为 step / stage / goal）")
        limit = execution.get("stage_max_actions", 24)
        if type(limit) is not int or limit < 1:
            problems.append("harness.execution.stage_max_actions（应为正整数）")
    return problems


def load_config(path: str | Path) -> dict:
    """加载唯一配置文件（infra/config.json）并校验 schema。"""
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    problems = validate(config)
    if problems:
        raise ConfigError(
            "配置缺少必填键或取值非法："
            + "；".join(problems)
            + "（见 infra/config.json）"
        )
    return config


def api_names(config: dict, kind: str) -> list[str]:
    """某个角色（slow_model / fast_model）登记的 API 名字。"""
    apis = get_path(config, f"harness.{kind}.apis")
    return sorted(apis) if isinstance(apis, dict) else []


def select_api(config: dict, kind: str, name: str | None = None) -> tuple[str, dict]:
    """按名字挑一个 API 条目；name 为空取该角色的 default。

    返回 (名字, 条目)。名字不存在时列出全部可选名——切换慢/快模型时
    最常犯的错就是记错名字。
    """
    section = get_path(config, f"harness.{kind}") or {}
    apis = section.get("apis") or {}
    key = name or section.get("default")
    if key not in apis:
        choices = "、".join(sorted(apis)) or "（无）"
        raise ConfigError(f"harness.{kind}.apis 里没有 {key!r}；可选：{choices}")
    return key, dict(apis[key])
