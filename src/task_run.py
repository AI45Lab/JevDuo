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


_MISSING = "[missing tool output; the call did not produce a result]"
_FAULT_DIR: Path | None = None


def _repair_tool_pairs(messages: list) -> tuple[list, int]:
    """按「紧邻」口径补齐 tool 回执，返回 (新列表, 修补数)。

    网关（k3.1-pro）对 OpenAI Chat 格式的校验是位置相关的，报错原文：
    "an assistant message with 'tool_calls' must be followed by tool messages
    responding to each 'tool_call_id' ... did not have response messages: wait:392"。
    其中 392 是请求内 tool_call 的累计序号。

    因此「id 在列表里出现过」不等于合格：回执被挪到别处、或整轮回执没进请求，
    都会 400。这里逐轮检查紧跟其后的连续 tool 段，缺的补上、错位的搬回来。
    补出来的消息必须紧跟本轮已有回执之后，追加到列表末尾同样会被拒绝
    （已用真网关验证：末尾追加 400，紧跟轮次 200）。
    """
    if not isinstance(messages, list):
        return messages, 0

    positions: dict[str, list[int]] = {}
    for index, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if isinstance(call_id, str) and call_id:
                positions.setdefault(call_id, []).append(index)

    total = len(messages)
    moved: set[int] = set()
    # 键是「插在原始列表的哪个下标之后」，值是要插入的消息
    inserts: dict[int, list] = {}
    repaired_count = 0

    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        calls = [
            call.get("id") for call in (message.get("tool_calls") or [])
            if isinstance(call, dict) and isinstance(call.get("id"), str) and call["id"]
        ]
        if not calls:
            continue
        cursor = index + 1
        covered: list = []
        while (cursor < total and isinstance(messages[cursor], dict)
               and messages[cursor].get("role") == "tool"):
            covered.append(messages[cursor].get("tool_call_id"))
            cursor += 1
        additions = []
        for call_id in calls:
            if call_id in covered:
                continue
            later = [pos for pos in positions.get(call_id, [])
                     if pos >= cursor and pos not in moved]
            if later:
                # 回执存在但错位：搬到本轮后面，别让它留在远处
                additions.append(messages[later[0]])
                moved.add(later[0])
            else:
                additions.append({"role": "tool", "tool_call_id": call_id, "content": _MISSING})
            repaired_count += 1
        if additions:
            inserts[cursor - 1] = additions

    if not repaired_count:
        return messages, 0

    repaired: list = []
    for index, message in enumerate(messages):
        if index in moved:
            continue
        repaired.append(message)
        repaired.extend(inserts.get(index, ()))
    print(json.dumps({"event": "tool_pair_repair", "count": repaired_count},
                     ensure_ascii=False), flush=True)
    return repaired, repaired_count


def _dump_fault(messages: list) -> None:
    """把出站消息的结构落一份现场：只为定位，不含正文与图片。"""
    if _FAULT_DIR is None:
        return
    shape = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            shape.append({"index": index, "role": type(message).__name__})
            continue
        item = {"index": index, "role": message.get("role")}
        calls = message.get("tool_calls") or []
        if calls:
            item["tool_calls"] = [call.get("id") for call in calls if isinstance(call, dict)]
        if message.get("tool_call_id"):
            item["tool_call_id"] = message["tool_call_id"]
        content = message.get("content")
        item["content_chars"] = len(content) if isinstance(content, str) else type(content).__name__
        shape.append(item)
    path = _FAULT_DIR / "tool_pair_fault.json"
    try:
        path.write_text(json.dumps(shape, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"event": "tool_pair_fault", "path": str(path)}, ensure_ascii=False), flush=True)
    except OSError:
        pass


def _install_tool_pair_repair(enabled: bool, fault_dir: Path | None = None) -> None:
    """在 NexAU 的模型调用入口外面包一层，出站前补齐 tool 回执。"""
    if not enabled:
        return
    global _FAULT_DIR
    _FAULT_DIR = fault_dir
    from nexau.archs.main_sub.execution import llm_caller

    original = llm_caller.call_llm_with_different_client

    def guarded(client, llm_config, kwargs, **extra):
        messages, count = _repair_tool_pairs(kwargs.get("messages") or [])
        if count:
            kwargs = {**kwargs, "messages": messages}
        try:
            return original(client, llm_config, kwargs, **extra)
        except Exception as error:
            # 网关仍拒收时留下现场，避免下次还要靠猜
            if "did not have response messages" in str(error):
                _dump_fault(kwargs.get("messages") or [])
            raise

    guarded.__wrapped__ = original
    llm_caller.call_llm_with_different_client = guarded


async def run(args: argparse.Namespace) -> dict:
    from northcua.agentinfra.runtime.protocol import DesktopRunRequest
    from northcua.agents.desktop.application import NorthCUAApplication

    _install_tool_pair_repair(not getattr(args, "no_tool_pair_repair", False),
                              fault_dir=Path(args.log_dir))
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
