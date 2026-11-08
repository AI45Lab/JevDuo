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


def _repair_tool_pairs(messages: list) -> tuple[list, int]:
    """给没有回执的 tool_call 补一条说明性回执，返回 (新列表, 修补数)。

    网关（k3.1-pro）对 OpenAI Chat 格式做严格校验：assistant 的 tool_calls 必须
    每条都被紧随其后的 tool 消息回应，否则整次请求 400，报 "The following
    tool_call_ids did not have response messages: note:31"。NexAU 自带的补齐函数
    只处理 Message 对象，而 openai_chat_completion 这条路径拿到的是 dict，孤儿
    调用会原样发出去（实证：10 题里 5 题因此 failed_unscored）。

    位置很关键：补出来的 tool 消息必须紧跟在该 assistant 轮次已有的 tool 消息
    之后，追加到整个列表末尾同样会被网关拒绝（已用真网关验证：末尾追加 400，
    紧跟轮次 200）。
    """
    if not isinstance(messages, list):
        return messages, 0
    answered = {
        message.get("tool_call_id") for message in messages
        if isinstance(message, dict) and message.get("role") == "tool"
    }
    repaired: list = []
    inserted = 0
    index = 0
    total = len(messages)
    while index < total:
        message = messages[index]
        repaired.append(message)
        index += 1
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        missing = [
            call.get("id") for call in (message.get("tool_calls") or [])
            if isinstance(call, dict) and call.get("id") and call["id"] not in answered
        ]
        if not missing:
            continue
        # 原样搬走本轮已有的 tool 回执，保持它们原来的顺序
        while (index < total and isinstance(messages[index], dict)
               and messages[index].get("role") == "tool"):
            repaired.append(messages[index])
            index += 1
        # 缺失的回执紧跟在本轮之后补上
        for call_id in missing:
            repaired.append({"role": "tool", "tool_call_id": call_id,
                             "content": "[missing tool output; the call did not produce a result]"})
            answered.add(call_id)
            inserted += 1
    if not inserted:
        return messages, 0
    print(json.dumps({"event": "tool_pair_repair", "count": inserted}, ensure_ascii=False), flush=True)
    return repaired, inserted


def _install_tool_pair_repair(enabled: bool) -> None:
    """在 NexAU 的模型调用入口外面包一层，出站前补齐 tool 回执。"""
    if not enabled:
        return
    from nexau.archs.main_sub.execution import llm_caller

    original = llm_caller.call_llm_with_different_client

    def guarded(client, llm_config, kwargs, **extra):
        messages, count = _repair_tool_pairs(kwargs.get("messages") or [])
        if count:
            kwargs = {**kwargs, "messages": messages}
        return original(client, llm_config, kwargs, **extra)

    guarded.__wrapped__ = original
    llm_caller.call_llm_with_different_client = guarded


async def run(args: argparse.Namespace) -> dict:
    from northcua.agentinfra.runtime.protocol import DesktopRunRequest
    from northcua.agents.desktop.application import NorthCUAApplication

    _install_tool_pair_repair(not getattr(args, "no_tool_pair_repair", False))
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
    parser.add_argument("--no-tool-pair-repair", action="store_true",
                        help="关闭出站前的 tool 回执补齐（默认开启）")
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
