"""Single-pass multimodal choice for bounded desktop control."""

from __future__ import annotations

import base64
import io
import json
import time
import urllib.request
from pathlib import Path

from PIL import Image


class ChoiceUnavailable(BaseException):
    """A fast-model API failure is unscored, not a GUI action to retry blindly."""


class LocalChoicePolicy:
    """Use the trained choice head without an operation pre-pass or image staging."""

    def __init__(self, endpoint_file: str = "", timeout_seconds: float = 30, opener=None, *, endpoint=""):
        self.endpoint_file = Path(endpoint_file) if endpoint_file else None
        self.endpoint = endpoint
        self.timeout = timeout_seconds
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def choose(self, *, goal, screenshot: bytes, candidates, recent, reactive=False):
        try:
            return self._choose(goal=goal, screenshot=screenshot, candidates=candidates,
                                recent=recent, reactive=reactive)
        except (OSError, ValueError, KeyError) as error:
            raise ChoiceUnavailable(f"JEV choice failed: {type(error).__name__}") from error

    def _choose(self, *, goal, screenshot, candidates, recent, reactive):
        with Image.open(io.BytesIO(screenshot)) as image:
            image = image.convert("RGB")
            image.thumbnail((640, 360) if reactive else (1280, 960))
            size = list(image.size)
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=85)
        payload = {
            "state": json.dumps({"local_goal": goal, "recent_actions": recent[-6:]}, ensure_ascii=False),
            "question": "Which complete concrete action should be performed next?",
            "options": [candidate.description for candidate in candidates],
            "image_path": "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"),
        }
        endpoint = self.endpoint
        if not endpoint:
            service = json.loads(self.endpoint_file.read_text())
            if service.get("ready") is False:
                raise RuntimeError("The configured JEV service is not ready")
            endpoint = service["base_url"]
        endpoint = endpoint.rstrip("/")
        request = urllib.request.Request(endpoint + "/rank", data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
        started = time.monotonic()
        with self.opener.open(request, timeout=self.timeout) as response:
            result = json.load(response)
        scores = result.get("scores", [])
        if len(scores) != len(candidates) or not result.get("used_image"):
            raise ValueError("Local JEV response must score every candidate and use the screenshot")
        ranking = {
            "winner": None,
            "action_ranked": [{"id": c.id, "score": s} for c, s in zip(candidates, scores, strict=False)],
            "latency_ms": (time.monotonic() - started) * 1000,
            "used_image": bool(result.get("used_image")),
            "score_is_calibrated_correctness_probability": False,
            "server_timing": result.get("timing", {}),
            "image_encoding": {"format": "JPEG", "quality": 85, "size": size},
            "action_request": {**payload, "image_path": "[inline image; see event screenshot]"},
        }
        winner = candidates[max(range(len(scores)), key=scores.__getitem__)] if scores and not result.get(
            "stale") else None
        ranking["winner"] = winner.compact() if winner else None
        return winner, ranking
