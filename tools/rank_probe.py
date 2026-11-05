#!/usr/bin/env python3
"""受控对比：同一份 state / 候选 / 截图，分别发给 kev 与 jev，看两个排序服务
给"回退操作"（visual_fallback / return_to_planner）的打分差异。

这是判断"kev 提前判死"根因的决定性实验：输入完全相同，差异只来自排序服务。

从真实 episode 里取：
  - state：慢模型请求里的 user 文本（目标 + a11y 树）
  - options：按 operation 聚合的候选（用 harness 的 OPERATION_DESCRIPTIONS 复刻）
两边发同一份 state/options，比较每个 operation 的得分。
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

# 与 harness 一致：直连，不走环境里的系统代理
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from jevduo.candidates import OPERATION_DESCRIPTIONS  # noqa: E402

SA = Path("/mnt/shared-storage-user/evobox-share-gpfs2/xuyuanxun/shared_resources/sa_cua")
# 排序服务宿主机能读的截图视角（harness 的 image_stage.read）
STAGE = Path("/mnt/shared-storage-gpfs2/evobox-share-gpfs2/chenxinquan/tandem_jev_images")
_shots = sorted(STAGE.glob("step_*.png")) if STAGE.is_dir() else []
IMAGE = str(_shots[-1]) if _shots else ""


def service(name: str) -> dict:
    return json.loads((SA / "services" / name).read_text(encoding="utf-8"))


def pick_episode(task: str) -> tuple[str, list[dict]]:
    """取该题最后一个 action 事件里的候选，与慢模型请求里的 user 文本。"""
    state = ""
    for ep in sorted((REPO / "rjob_logs").glob(f"osw21-500-kev*/episodes/{task}/llm_calls.jsonl")):
        for line in ep.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("role") != "plan_step":
                continue
            for m in (d.get("request", {}).get("payload", {}) or {}).get("messages", []):
                if m.get("role") != "user":
                    continue
                c = m.get("content")
                text = ""
                if isinstance(c, str):
                    text = c
                elif isinstance(c, list):
                    text = "".join(str(p.get("text") or "") for p in c if p.get("type") == "text")
                if len(text) > len(state):
                    state = text
    candidates: list[dict] = []
    for ep in sorted((REPO / "rjob_logs").glob(f"osw21-500-kev*/episodes/{task}/events.jsonl")):
        for line in ep.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except Exception:
                continue
            p = d.get("payload") or d
            if p.get("kind") == "action" or "candidates" in p:
                if p.get("candidates"):
                    candidates = p["candidates"]
    return state, candidates


def operation_options(candidates: list[dict]) -> tuple[list[str], list[str]]:
    ops: list[str] = []
    for c in candidates:
        op = c.get("operation") or c.get("action_type") or "unknown"
        if op not in ops:
            ops.append(op)
    options = [f"Choose operation {op}: {OPERATION_DESCRIPTIONS.get(op, op)}" for op in ops]
    return ops, options


def ask_kev(state: str, options: list[str], ops: list[str]) -> dict[str, float]:
    svc = service("kev.json")
    criteria = {
        op: (text.split(": ", 1)[1] if ": " in text else text)
        for op, text in zip(ops, options, strict=True)
    }
    payload = {
        "state": state,
        "model": "kev-latest",
        "questions": {"q": {"type": "choice",
                            "instructions": "Which operation should be performed next?",
                            "criteria": criteria}},
    }
    req = urllib.request.Request(svc["base_url"].rstrip("/") + svc.get("api", "/v1/systemone"),
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with OPENER.open(req, timeout=120) as resp:
        body = json.load(resp)
    probs = body["answers"]["q"]["probabilities"]
    return {op: float(probs.get(op, 0.0)) for op in ops}


def ask_jev(state: str, options: list[str], ops: list[str]) -> dict[str, float]:
    svc = service("qwen_jev.json")
    payload = {"state": state, "question": "Which operation should be performed next?",
               "options": options, "image_path": IMAGE or ""}
    req = urllib.request.Request(svc["base_url"].rstrip("/") + svc.get("api", "/rank"),
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with OPENER.open(req, timeout=120) as resp:
        body = json.load(resp)
    # jev /rank 返回 {"scores": [与 options 对齐的分数], "used_image": bool}
    scores = body.get("scores")
    if isinstance(scores, list):
        return {op: float(scores[i]) for i, op in enumerate(ops) if i < len(scores)}
    probs = body.get("probabilities") or {}
    return {op: float(probs.get(op, 0.0)) for op in ops}


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    drop_fallback = "--no-fallback" in sys.argv
    task = args[0] if args else "004"
    state, candidates = pick_episode(task)
    if not candidates:
        print(f"题 {task} 没找到候选")
        return
    ops, options = operation_options(candidates)
    if "--refine-fallback" in sys.argv:
        # 变体：保留回退项，但把描述改写成"最后手段"语义（harness 侧可落地的改法）
        refined = {
            "visual_fallback": (
                "Choose operation visual_fallback: LAST RESORT ONLY — use this only when "
                "no semantic target in the current UI can carry out the goal at all."),
            "return_to_planner": (
                "Choose operation return_to_planner: LAST RESORT ONLY — use this only when "
                "the current stage assumption is provably wrong and every listed "
                "operation would fail."),
        }
        options = [refined.get(op, opt)
                   for op, opt in zip(ops, options, strict=True)]
    if drop_fallback:
        keep = [(op, opt) for op, opt in zip(ops, options, strict=True)
                if op not in ("visual_fallback", "return_to_planner")]
        ops = [op for op, _ in keep]
        options = [opt for _, opt in keep]
    print(f"=== 题 {task}：{len(ops)} 个 operation 候选"
          f"{'（已剔除回退项）' if drop_fallback else ''}，state {len(state):,} 字符 ===")
    for name, fn in (("kev", ask_kev), ("jev", ask_jev)):
        try:
            scores = fn(state, options, ops)
        except Exception as exc:
            print(f"  {name}: 调用失败 {type(exc).__name__}: {exc}")
            continue
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        print(f"  --- {name} ---")
        for op, sc in ranked:
            mark = ""
            if op in ("visual_fallback", "return_to_planner"):
                mark = "  ← 回退项"
            print(f"    {op:<22} {sc:.4f}{mark}")
        fb = sum(scores.get(o, 0.0) for o in ("visual_fallback", "return_to_planner"))
        print(f"    回退项得分合计 {fb:.4f}")


if __name__ == "__main__":
    main()
