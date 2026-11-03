"""慢模型的 JSON 传输客户端——OpenAI 兼容 /chat/completions 口径，
当前实现用 OpenAI 兼容方言（reasoning_effort / enable_thinking）——Kimi 与 MiniMax 都吃这套。含脱敏审计。
从旧仓库 providers.py 按契约移植。
全量请求/响应落盘（llm_calls.jsonl）属于 M2 的 recorder，此处保持审计日志。"""

from __future__ import annotations

import base64
import io
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

from .key_pool import ApiKeyPool, PoolExhausted, classify_api_error, retry_after_seconds

# 空正文（全留在思考通道）或截断时，下一次重试升档思考强度，避免原样重发重复失败
_REASON_EFFORT_LADDER = {"low": "medium", "medium": "high"}


class TaskBudgetExceeded(TimeoutError):
    """The agent's task budget ended, not an external API timeout."""


class ModelResponseError(ValueError):
    """Bounded repair could not produce a valid structured model response."""


def _pack_images(payload, quality, max_bytes):
    """Pack bounded-size send copies; only exactly identical decoded frames share an image.

    Mutate the existing messages list so validators can still append correction turns on retry.
    Never write source files or discard distinct evidence frames to meet the body budget.
    """
    frames, pixels = [], []
    stats = {"image_count": 0, "duplicate_images": 0, "source_image_bytes": 0}
    for message in payload["messages"]:
        if not isinstance(message.get("content"), list):
            continue
        for part in message["content"]:
            if part.get("type") != "image_url":
                continue
            url = part["image_url"]["url"]
            if not url.startswith("data:image/"):
                continue
            raw = base64.b64decode(url.split(",", 1)[1])
            with Image.open(io.BytesIO(raw)) as source:
                frame = source.convert("RGB")
            identity = (frame.size, frame.tobytes())
            stats["image_count"] += 1
            stats["source_image_bytes"] += len(raw)
            if identity in pixels:
                ordinal = pixels.index(identity) + 1
                part.clear()
                part.update(type="text", text=f"Same screenshot as supplied image #{ordinal} (exact pixels).")
                stats["duplicate_images"] += 1
            else:
                pixels.append(identity)
                frames.append((part, frame))
    while frames:
        image_bytes = 0
        for part, frame in frames:
            encoded = io.BytesIO()
            frame.save(encoded, format="JPEG", quality=quality, subsampling=0, optimize=True)
            raw = encoded.getvalue()
            image_bytes += len(raw)
            part["image_url"]["url"] = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
        stats.update(image_quality=quality, sent_image_bytes=image_bytes)
        if len(json.dumps(payload).encode("utf-8")) <= max_bytes or quality == 65:
            break
        quality = max(65, quality - 10)
    while frames and len(json.dumps(payload).encode("utf-8")) > max_bytes and max(
        max(frame.size) for _, frame in frames
    ) > 960:
        for part, frame in frames:
            frame.thumbnail((max(960, int(frame.width * 0.8)), max(540, int(frame.height * 0.8))))
            encoded = io.BytesIO()
            frame.save(encoded, format="JPEG", quality=quality, subsampling=0, optimize=True)
            part["image_url"]["url"] = "data:image/jpeg;base64," + base64.b64encode(
                encoded.getvalue()).decode("ascii")
    return {**stats, "unique_images": len(frames), "sent_image_sizes": [list(f.size) for _, f in frames]}



def extract_json_object(text: str):
    """从模型正文中抢救 JSON：剥围栏后，若剩余文本不是纯 JSON，抽取首个平衡的 {…}。
    返回解析后的 dict；无 JSON 或解析失败返回 None。"""
    stripped = text.strip()
    for prefix in ("```json", "```"):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):]
    if stripped.rstrip().endswith("```"):
        stripped = stripped.rstrip()[:-3]
    stripped = stripped.strip()
    try:
        value = json.loads(stripped)
        return value if isinstance(value, dict) else None
    except ValueError:
        pass
    start = stripped.find("{")
    while start != -1:
        depth = 0
        for index in range(start, len(stripped)):
            if stripped[index] == "{":
                depth += 1
            elif stripped[index] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(stripped[start:index + 1])
                        if isinstance(value, dict):
                            return value
                    except ValueError:
                        break
                    break
        start = stripped.find("{", start + 1)
    return None


def _api_failure(status: int, raw: dict):
    error = raw.get("error")
    base = raw.get("base_resp") or {}
    if status < 400 and not error and not base.get("status_code"):
        return None
    if isinstance(error, dict):
        return error.get("code") or error.get("type"), str(error.get("message", ""))
    return base.get("status_code"), str(error or base.get("status_msg") or json.dumps(raw))


class SlowModelClient:
    def __init__(self, credential_file, config, audit_path: Path, llm_sink=None):
        if isinstance(credential_file, dict):
            self.credentials = dict(credential_file)
        else:
            self.credentials = json.loads(Path(credential_file).read_text(encoding="utf-8"))
        self.key_pool = ApiKeyPool(self.credentials)
        self._call_id = 0
        self.config = config
        self.audit_path = audit_path
        # llm_sink(role=..., request=..., response=..., **meta)：全量请求/响应落盘钩子，
        # 由 tandem_eval.recorder 提供（llm_calls.jsonl）；None 时仅审计。
        self.llm_sink = llm_sink
        self.deadline = None

    def set_deadline(self, deadline):
        """Absolute monotonic task deadline, shared by requirements, planning and verification."""
        self.deadline = deadline

    def _remaining(self):
        remaining = self.deadline - time.perf_counter() if self.deadline is not None else float("inf")
        if remaining <= 0:
            raise TaskBudgetExceeded("Task deadline exhausted during slow-model execution")
        return remaining

    def _retry_wait(self, seconds):
        if seconds >= self._remaining():
            raise TaskBudgetExceeded("Task budget cannot accommodate another slow-model retry")
        time.sleep(seconds)

    def json_request(self, messages, validate, *, role):
        credentials = self.credentials
        self._call_id += 1
        call_id = self._call_id
        payload = {
            "model": credentials.get("model") or "unknown",
            "messages": messages,
            "stream": False,
            "max_tokens": (min(self.config.slow_max_tokens, self.config.slow_verify_max_tokens)
                           if role == "verify_stage" else self.config.slow_max_tokens),
        }
        if role == "gui_choice":
            payload["max_tokens"] = min(self.config.slow_max_tokens, 2048)
        effort = self.config.slow_reasoning_effort
        if role == "gui_choice":
            effort = "low"
        if role == "verify_stage" and self.config.slow_verify_reasoning_effort is not None:
            effort = self.config.slow_verify_reasoning_effort
        if effort == "off":
            payload["enable_thinking"] = False
        elif effort is not None:
            payload["reasoning_effort"] = effort
        image_stats = (_pack_images(payload, self.config.slow_image_quality,
                                    self.config.slow_max_request_bytes)
                       if self.config.execution_mode == "goal" else {})

        def build_request():
            return urllib.request.Request(
                credentials["base_url"].rstrip("/") + "/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    **(
                        {"Authorization": "Bearer " + key["api_key"]}
                        if key["api_key"]
                        else {}
                    ),
                    "Content-Type": "application/json",
                },
            )

        opener = (
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open
            if self.config.slow_direct_api
            else urllib.request.urlopen
        )

        def audit(event, started, attempt, **details):
            record = {
                "event": event,
                "provider": "slow_model",
                "api_provider": self.key_pool.provider,
                "role": role,
                "call_id": call_id,
                "key_id": key["id"],
                "key_priority": key["priority"],
                "attempt": attempt,
                "time": time.time(),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "request_bytes": len(request.data),
                **image_stats,
                **details,
            }
            text = json.dumps(self.key_pool.redact(record), ensure_ascii=False)
            print(text, flush=True)
            with self.audit_path.open("a", encoding="utf-8") as log:
                log.write(text + "\n")

        def public_headers(headers):
            allowed = {
                "server",
                "content-type",
                "via",
                "date",
                "retry-after",
                "x-request-id",
                "request-id",
                "x-acs-request-id",
                "x-trace-id",
                "x-envoy-upstream-service-time",
            }
            return {name.lower(): value for name, value in headers.items() if name.lower() in allowed}

        excluded: set[str] = set()
        key = self.key_pool.select()
        attempt = 0
        failures = 0
        while True:
            timeout = min(self.config.slow_timeout_seconds, self._remaining())
            attempt += 1
            started = time.perf_counter()
            request = build_request()  # 每次重试重建：升档思考强度需重新序列化
            audit("request_start", started, attempt)
            try:
                try:
                    with opener(request, timeout=timeout) as response:
                        status = response.status
                        headers = public_headers(response.headers)
                        raw = json.load(response)
                except urllib.error.HTTPError as error:
                    status = error.code
                    headers = public_headers(error.headers)
                    body = self.key_pool.redact(error.read().decode("utf-8", errors="replace"))
                    try:
                        raw = json.loads(body)
                    except ValueError:
                        raw = {"error": {"message": body[:2000]}}
                if not isinstance(raw, dict):
                    raise ValueError("slow model response must be a JSON object")
                if self.llm_sink is not None:
                    self.llm_sink(
                        role=role,
                        request=self.key_pool.redact({"payload": payload, "messages": messages}),
                        response=self.key_pool.redact(raw),
                        attempt=attempt, call_id=call_id,
                        key_id=key["id"], key_priority=key["priority"],
                        http_status=status,
                        elapsed_seconds=round(time.perf_counter() - started, 3),
                    )
                failure = _api_failure(status, raw)
                if failure is not None:
                    code, message = failure
                    category, cooldown = classify_api_error(self.key_pool.provider, status, code, message)
                    audit("api_error", started, attempt, http_status=status, headers=headers,
                          error_code=code, category=category, error=message[:2000], usage=raw.get("usage"))
                    error = RuntimeError(self.key_pool.redact(
                        f"slow model API error: status={status}, code={code}, message={message[:2000]}"
                    ))
                    if cooldown:
                        cooldown = retry_after_seconds(headers.get("retry-after"), cooldown)
                        self.key_pool.cooldown(key["id"], cooldown, category)
                        excluded.add(key["id"])
                        audit("key_cooldown", started, attempt, category=category,
                              cooldown_seconds=cooldown)
                        try:
                            key = self.key_pool.select(excluded)
                        except PoolExhausted as exhausted:
                            raise exhausted from error
                        continue
                    if category != "transient":
                        raise error
                    failures += 1
                    if failures >= 3:
                        raise error
                    delay = retry_after_seconds(headers.get("retry-after"), 30)
                    audit("retry_wait", started, attempt, retry_wait_seconds=delay)
                    self._retry_wait(delay)
                    continue
                choices = raw.get("choices") or []
                if not choices:
                    raise ValueError("slow model response contains no choices")
                choice = choices[0]
                finish_reason = choice.get("finish_reason")
                content = (choice.get("message", {}).get("content") or "").strip()
                audit("http_response", started, attempt, http_status=status, headers=headers,
                      finish_reason=finish_reason, usage=raw.get("usage"))
                if finish_reason == "length":
                    completion_tokens = (raw.get("usage") or {}).get("completion_tokens")
                    raise ModelResponseError(
                        f"slow model response truncated by max_tokens (finish_reason=length, "
                        f"completion_tokens={completion_tokens}); raise slow_max_tokens"
                    )
                if not content and choice.get("message", {}).get("reasoning_content"):
                    raise ModelResponseError(
                        "slow model returned empty content (all completion tokens stayed in the "
                        "reasoning channel); escalating reasoning_effort for the next attempt"
                    )
                value = extract_json_object(content)
                if value is None:
                    raise ModelResponseError("slow model response contains no JSON object")
                if not validate(value):
                    correction = messages[-1].get("content", "") if messages else ""
                    detail = (correction if isinstance(correction, str)
                              and correction.startswith("Invalid") else "")
                    raise ModelResponseError("slow model response failed validation: " + detail[:600])
                return value
            except (urllib.error.URLError, ConnectionError, TimeoutError, ValueError) as error:
                audit(
                    "transport_or_validation_error",
                    started,
                    attempt,
                    error_type=type(error).__name__,
                    error=self.key_pool.redact(str(error))[:1000],
                )
                failures += 1
                if failures >= 3:
                    raise
                # 内容级失败（空正文/截断/非法 JSON）时升档思考强度后重试，不原样重发
                current_effort = payload.get("reasoning_effort")
                if (self.config.execution_mode == "goal" and role == "plan_stage"
                        and "finish_reason=length" in str(error)):
                    payload["reasoning_effort"] = "low"
                    messages.append({"role": "user", "content":
                        "The previous response was truncated and NOTHING from it was executed. "
                        "Return one COMPLETE compact goal-stage JSON object, without reasoning or "
                        "a long script. Use one bounded edit/read, reuse memory, and preserve the "
                        "original requirements. Do not repeat the unbounded response."})
                elif current_effort in _REASON_EFFORT_LADDER:
                    payload["reasoning_effort"] = _REASON_EFFORT_LADDER[current_effort]
                audit("retry_wait", started, attempt, retry_wait_seconds=30)
                self._retry_wait(30)
