"""结果目录契约执行者（契约文档：docs/RESULT_DIR_CONTRACT.md）。

episode 目录自包含：task.json 副本、traj.jsonl、llm_calls.jsonl（全量请求/响应，脱敏）、
screenshots/、a11y/、browser/、events.jsonl、deliverables/、evaluation.json、result.txt、
episode.json、timing.json。run 级：summary.json、heartbeat.json、code_version.json。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import traceback
from pathlib import Path
from typing import Any

from tandem import __version__
from tandem.protocol import PROTOCOL_VERSION
from tandem.protocol.models import Action, Observation, Receipt

_SECRET_PATTERNS = re.compile(r"(Bearer\s+)[^\s\"']+")


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET_PATTERNS.sub(r"\1[REDACTED]", value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    return value


_SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9._-]{6,}")


def _redact_text(text: str, limit: int = 500) -> str:
    """异常消息可能带出凭据，落盘前脱敏并截断。"""
    return _SECRET_PATTERN.sub("[REDACTED]", text)[:limit]


def _traceback_tail(exception: BaseException, limit: int = 8) -> list[str]:
    """异常栈尾几行——只记类型不够，事后没法判断是哪一步炸的。"""
    lines = traceback.format_exception(type(exception), exception, exception.__traceback__)
    return [_redact_text(line.rstrip(), 300) for line in lines[-limit:]]


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class EpisodeRecorder:
    def __init__(self, episode_dir: Path, task: dict, *, a11y_full: bool = False):
        self.dir = episode_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.task = task
        self.a11y_full = a11y_full
        self.executed_steps = 0
        self.environment_seconds = {}
        self.model_usage = {}
        self.fast_model = {"choice_cycles": 0, "seconds": 0.0, "cycles_with_image": 0}
        self.started = time.perf_counter()
        self._traj = (self.dir / "traj.jsonl").open("w", encoding="utf-8")
        self._events = (self.dir / "events.jsonl").open("w", encoding="utf-8")
        self._llm = (self.dir / "llm_calls.jsonl").open("w", encoding="utf-8")
        _write_json(self.dir / "task.json", task)

    # ------------------------------------------------------------------ 逐步记录
    def traj(self, observation: Observation, action: Action, receipt: Receipt | None) -> None:
        self.executed_steps += 1
        browser_snapshot = observation.browser_snapshot
        browser_record = browser_snapshot.to_dict() if browser_snapshot else None
        if browser_snapshot and browser_snapshot.present and browser_snapshot.path:
            target = self.dir / "browser" / f"step_{observation.step_idx - 1:02d}.json"
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(browser_snapshot.path, target)
            browser_record["path"] = str(target)
        record = {
            "step_idx": observation.step_idx,
            "wall_time": round(time.time(), 3),
            "protocol_version": PROTOCOL_VERSION,
            "observation": {
                "screenshot": observation.screenshot.to_dict(),
                "accessibility_tree": observation.accessibility_tree.to_dict(),
                "browser_snapshot": browser_record,
                "budget": observation.budget.to_dict(),
                "last_receipt": receipt.to_dict() if receipt else None,
            },
            "action": action.to_dict(),
        }
        self._traj.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._traj.flush()

    def event(self, event: dict) -> None:
        if event.get("route") == "environment_timing":
            phase = event["phase"]
            self.environment_seconds[phase] = self.environment_seconds.get(phase, 0.0) + event["seconds"]
        if event.get("route") == "gui" and event.get("ranking"):
            ranking = event["ranking"]
            self.fast_model["choice_cycles"] += 1
            self.fast_model["seconds"] += ranking["latency_ms"] / 1000
            self.fast_model["cycles_with_image"] += int(ranking["used_image"])
        if event.get("route") == "stage_verified":
            event = self._persist_evidence(event)
        self._events.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._events.flush()

    def task_memory(self, state: dict) -> None:
        """Persist the latest host memory and retain its transient evidence before cleanup."""
        _write_json(self.dir / "task_memory.json", self._persist_evidence(state))

    def _persist_evidence(self, state):
        def persist(value):
            if isinstance(value, dict):
                return {k: persist(v) for k, v in value.items()}
            if isinstance(value, list):
                return [persist(v) for v in value]
            if (isinstance(value, str) and any(part in value for part in ("/obs_stage/", "/cli_outputs/"))
                    and Path(value).is_file()):
                target = self.dir / "evidence" / Path(value).name
                target.parent.mkdir(exist_ok=True)
                if not target.exists():
                    shutil.copy2(value, target)
                return str(target)
            return value

        return persist(state)

    def record_llm_call(self, *, role: str, request: Any, response: Any, **meta: Any) -> None:
        """慢模型客户端的 llm_sink；全量请求/响应落盘，密钥脱敏。"""
        self._llm.write(
            json.dumps(
                _redact(
                    {
                        "role": role,
                        "time": time.time(),
                        "request": request,
                        "response": response,
                        **meta,
                    }
                ),
                ensure_ascii=False,
            )
            + "\n"
        )
        self._llm.flush()
        stats = self.model_usage.setdefault(role, {"calls": 0, "seconds": 0.0, "prompt_tokens": 0,
            "completion_tokens": 0, "reasoning_tokens": 0, "cached_tokens": 0, "calls_missing_usage": 0})
        stats["calls"] += 1
        stats["seconds"] += meta.get("elapsed_seconds", 0.0)
        usage = response.get("usage") or {}
        stats["calls_missing_usage"] += int(not usage)
        for key in ("prompt_tokens", "completion_tokens"):
            stats[key] += usage.get(key, 0)
        stats["reasoning_tokens"] += (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)
        stats["cached_tokens"] += (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)

    def screenshot(self, step_idx: int, data: bytes) -> None:
        target = self.dir / "screenshots" / f"step_{step_idx:02d}.png"
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(data)

    def a11y(self, step_idx: int, xml: str) -> None:
        digest = hashlib.sha256(xml.encode()).hexdigest()
        if self.a11y_full:
            target = self.dir / "a11y" / f"step_{step_idx:02d}.xml"
            target.parent.mkdir(exist_ok=True)
            target.write_text(xml, encoding="utf-8")
        (self.dir / "a11y_digests.jsonl").open("a", encoding="utf-8").write(
            json.dumps({"step_idx": step_idx, "sha256": digest, "node_count": xml.count("<")})
            + "\n"
        )

    # ------------------------------------------------------------------ 终态
    def finalize(
        self,
        *,
        stop_reason: str,
        outputs: list[dict],
        raw_score: Any,
        score: float | None,
        status: str,
        exception: BaseException | None = None,
        steps: int = 0,
    ) -> dict:
        _write_json(self.dir / "artifacts.json", outputs)
        if raw_score is not None:
            _write_json(
                self.dir / "evaluation.json",
                {"source": "official_benchmark_evaluator", "raw": raw_score, "score": score},
            )
            (self.dir / "result.txt").write_text(f"{score}\n", encoding="utf-8")
        episode = {
            "task_id": self.task["id"],
            "harness": f"tandem {__version__}",
            "stop_reason": stop_reason,
            "steps": self.executed_steps if exception is not None else steps,
            "status": status,
            "score": score,
            "exception_type": type(exception).__name__ if exception else None,
        }
        if exception is not None:
            episode["error"] = _redact_text(str(exception))
            episode["error_traceback"] = _traceback_tail(exception)
        _write_json(self.dir / "episode.json", episode)
        _write_json(
            self.dir / "timing.json",
            {
                "wall_seconds": round(time.perf_counter() - self.started, 3),
                "environment_seconds": self.environment_seconds,
                "model_usage_by_role": self.model_usage,
                "fast_model": self.fast_model,
                "status": status,
            },
        )
        self.close()
        return episode

    def close(self) -> None:
        for handle in (self._traj, self._events, self._llm):
            if not handle.closed:
                handle.close()


def write_summary(run_root: Path, episodes: list[dict], spec: dict) -> None:
    scores = [item["score"] for item in episodes if item.get("score") is not None]
    _write_json(
        run_root / "summary.json",
        {
            "run_id": spec.get("run_id"),
            "benchmark": spec.get("benchmark"),
            "harness": spec.get("harness"),
            "total": len(episodes),
            "scored": len(scores),
            "failed_unscored": len(episodes) - len(scores),
            "average_score": round(sum(scores) / len(scores), 4) if scores else None,
            "tasks": episodes,
        },
    )


def write_heartbeat(run_root: Path, *, done: int, total: int, current: str) -> None:
    _write_json(
        run_root / "heartbeat.json",
        {
            "time": time.time(),
            "done": done,
            "total": total,
            "current_task": current,
        },
    )


def write_code_version(run_root: Path, info: dict) -> None:
    _write_json(
        run_root / "code_version.json",
        {
            "harness_version": __version__,
            "protocol_version": PROTOCOL_VERSION,
            **info,
        },
    )
