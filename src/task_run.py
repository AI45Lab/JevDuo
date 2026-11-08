"""单题 agent 运行器（在仓库 venv 内执行，依赖上游源码与 NexAU）。

等价于上游的 desktop run，但把终态写成 JSON，供父进程映射为 OSWorld 的 DONE/FAIL。
只依赖标准库与上游包。

鉴权：模型网关需要 Bearer key。launch.py 通过 JEVDUO_API_KEY 环境变量注入，
agent.yaml 用 ${env.JEVDUO_API_KEY} 引用。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import traceback
from pathlib import Path


def _redact(text: str) -> str:
    key = os.environ.get("JEVDUO_API_KEY", "")
    return text.replace(key, "***") if key else text


async def run(args: argparse.Namespace) -> dict:
    from northcua.agentinfra.runtime.protocol import DesktopRunRequest
    from northcua.agents.desktop.application import NorthCUAApplication

    app = NorthCUAApplication.from_app_config(
        args.app_config, session_id=args.session_id, artifact_root_dir_override=args.log_dir)
    info = {"session_id": args.session_id, "artifact_root": str(app.artifact_store.root)}
    started = time.time()
    try:
        result = await app.run_query(DesktopRunRequest(
            session_id=args.session_id, query=args.query,
            max_steps=args.max_steps, max_session_steps=0))
        info.update(
            terminal_status=result.terminal_status,
            hit_step_limit=result.hit_step_limit,
            step_count=result.step_count,
            terminal_decision=dict(result.terminal_decision),
            step_log_path=str(result.step_log_path),
            runtime_log_path=str(result.runtime_log_path),
        )
    finally:
        await app.stop()
    info["seconds"] = round(time.time() - started, 3)
    return info


def main() -> int:
    parser = argparse.ArgumentParser(prog="task_run")
    parser.add_argument("--app-config", required=True)
    parser.add_argument("--log-dir", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--max-steps", type=int, required=True)
    parser.add_argument("--result", required=True, help="终态 JSON 输出路径")
    parser.add_argument("--query", required=True)
    args = parser.parse_args()
    try:
        info = asyncio.run(run(args))
        code = 0
    except BaseException as exc:
        info = {
            "session_id": args.session_id,
            "error": _redact(f"{type(exc).__name__}: {exc}")[:2000],
            "traceback": _redact(traceback.format_exc())[-3000:],
        }
        code = 2
    Path(args.result).write_text(
        json.dumps(info, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
