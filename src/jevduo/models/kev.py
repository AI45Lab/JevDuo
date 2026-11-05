"""KevPolicy：Kev-9B 排序后端（System One API，POST /v1/systemone）。

与 JevOmniPolicy 同构的两阶段排序（operation → action），复用 build_choice_requests；
差异仅在传输：每个 choice_request 变成一个 choice 问题，criteria 为 {option_id: 描述}，
probabilities 按 option 顺序取回作为 scores。Kev 为纯文本决策模型：不发送截图，
used_image 恒 false（候选描述含坐标/上下文，信息损失见 DEVIATIONS）。

协议样例（README §POST /v1/systemone）：
  请求 {"state": …, "model": "kev-latest", "questions": {"q": {"type": "choice",
        "instructions": …, "criteria": {option: description}}}}
  响应 {"answers": {"q": {"choice": top, "confidence": c, "probabilities": {option: p}}}}
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .ranking import Ranking, build_choice_requests

RECOVERY_DEADLINE_SECONDS = 1200


class KevPolicy:
    def __init__(self, endpoint: str, timeout_seconds: float = 300, opener=None):
        self.endpoint = str(endpoint).rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        deadline = time.monotonic() + RECOVERY_DEADLINE_SECONDS
        while True:
            try:
                request = urllib.request.Request(
                    self.endpoint + "/v1/systemone",
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with self.opener.open(request, timeout=self.timeout_seconds) as response:
                    return json.load(response)
            except (OSError, json.JSONDecodeError) as error:
                if isinstance(error, urllib.error.HTTPError) or time.monotonic() >= deadline:
                    raise
                print(
                    json.dumps(
                        {
                            "event": "kev_service_recovery_wait",
                            "error": str(error),
                            "remaining_seconds": round(deadline - time.monotonic()),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                time.sleep(15)

    def _predict(self, choice_request: dict[str, Any]) -> tuple[list[float], bool]:
        criteria = {
            option_id: text.split(": ", 1)[1] if ": " in text else text
            for option_id, text in zip(choice_request["option_ids"], choice_request["options"], strict=True)
        }
        payload = {
            "state": json.loads(choice_request["state"]),
            "model": "kev-latest",
            "questions": {
                "q": {
                    "type": "choice",
                    "instructions": choice_request["question"],
                    "criteria": criteria,
                }
            },
        }
        response = self._post(payload)
        try:
            probabilities = response["answers"]["q"]["probabilities"]
        except (KeyError, TypeError) as error:
            raise ValueError(f"fast model 响应缺少 answers.q.probabilities: {response}") from error
        scores = [float(probabilities.get(option_id, 0.0))
                  for option_id in choice_request["option_ids"]]
        return scores, False  # 纯文本后端，used_image 恒 false

    def rank(
        self,
        *,
        stage_goal: str,
        intent: str,
        active_window: str,
        targets,
        action_space,
        recent_actions: list[dict[str, Any]],
        screenshot: Path | None = None,
    ) -> Ranking:
        candidates = action_space.candidates()
        operation_request, action_requests = build_choice_requests(
            stage_goal=stage_goal,
            intent=intent,
            active_window=active_window,
            targets=targets,
            action_space=action_space,
            recent_actions=recent_actions,
        )
        started = time.perf_counter()
        operation_scores, _ = self._predict(operation_request)
        operation_ranked = sorted(
            (
                {"operation": operation, "score": score}
                for operation, score in zip(
                    operation_request["option_ids"], operation_scores, strict=True
                )
            ),
            key=lambda item: item["score"],
            reverse=True,
        )
        selected_operation = operation_ranked[0]["operation"] if operation_ranked else None
        selected_candidates = [
            candidate for candidate in candidates if candidate.operation == selected_operation
        ]
        action_request = action_requests[selected_operation]
        if len(selected_candidates) == 1:
            action_scores = [1.0]
        else:
            action_scores, _ = self._predict(action_request)
        ranked = sorted(
            (
                {"id": candidate.id, "operation": candidate.operation, "score": score}
                for candidate, score in zip(selected_candidates, action_scores, strict=True)
            ),
            key=lambda item: item["score"],
            reverse=True,
        )
        by_id = {candidate.id: candidate for candidate in selected_candidates}
        winner = by_id[ranked[0]["id"]] if ranked else None
        return Ranking(
            winner=winner,
            action_ranked=ranked,
            operation_ranked=operation_ranked,
            operation_margin=(
                operation_ranked[0]["score"] - operation_ranked[1]["score"]
                if len(operation_ranked) > 1
                else 0.0
            ),
            target_margin=(ranked[0]["score"] - ranked[1]["score"] if len(ranked) > 1 else 0.0),
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            used_image=False,
            operation_request=operation_request,
            action_request=action_request,
        )
