"""Slow-model transport failures use the existing bounded retry path."""

import base64
import http.client
import io
import json
import random
import urllib.request

import pytest
from PIL import Image

from tandem.config import SessionConfig
from tandem.models.slow import ModelResponseError, SlowModelClient


class _Response:
    status = 200
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps({
            "choices": [{"message": {"content": '{"done": true}'}, "finish_reason": "stop"}],
        }).encode()


def test_remote_disconnect_is_retried(tmp_path, monkeypatch):
    attempts = []

    def opener(request, timeout=None):
        attempts.append(request)
        if len(attempts) == 1:
            raise http.client.RemoteDisconnected("remote closed")
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    monkeypatch.setattr("tandem.models.slow.time.sleep", lambda seconds: None)
    client = SlowModelClient({"base_url": "http://slow"}, SessionConfig(), tmp_path / "audit.jsonl")
    value = client.json_request([{"role": "user", "content": "x"}], lambda value: True, role="plan_stage")
    assert value == {"done": True}
    assert len(attempts) == 2
    events = [json.loads(line)["event"] for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert events.count("request_start") == 2
    assert "transport_or_validation_error" in events


@pytest.mark.parametrize("model_content", [False, True])
def test_api_envelope_failure_is_not_classified_as_invalid_model_content(
        tmp_path, monkeypatch, model_content):
    attempts = []

    class Response(_Response):
        def read(self):
            raw = {"choices": [{"message": {"content": "not JSON"}}]} if model_content else {}
            return json.dumps(raw).encode()

    def opener(request, **kwargs):
        attempts.append(request)
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    monkeypatch.setattr("tandem.models.slow.time.sleep", lambda seconds: None)
    client = SlowModelClient({"base_url": "http://synthetic"}, SessionConfig(), tmp_path / "audit.jsonl")
    with pytest.raises(ValueError) as raised:
        client.json_request([{"role": "user", "content": "plan"}], lambda value: True, role="plan_stage")
    assert isinstance(raised.value, ModelResponseError) is model_content
    assert len(attempts) == 3


@pytest.mark.parametrize("mode,second_effort", [("goal", "low"), ("step", "max")])
def test_truncated_goal_plan_requests_a_compact_repair_without_using_partial_output(
        tmp_path, monkeypatch, mode, second_effort):
    payloads, validated = [], []

    class Response(_Response):
        def read(self):
            first = len(payloads) == 1
            return json.dumps({"choices": [{"finish_reason": "length" if first else "stop",
                               "message": {"content": '{"done":' if first else '{"done":true}'}}],
                               "usage": {"completion_tokens": 32768 if first else 10}}).encode()

    def opener(request, **kwargs):
        payloads.append(json.loads(request.data))
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    monkeypatch.setattr("tandem.models.slow.time.sleep", lambda seconds: None)
    config = SessionConfig(execution_mode=mode, slow_reasoning_effort="max", slow_max_tokens=32768)
    client = SlowModelClient({"base_url": "http://synthetic"}, config, tmp_path / "audit.jsonl")
    value = client.json_request([{"role": "user", "content": "plan"}],
                                lambda v: validated.append(v) or True, role="plan_stage")
    assert value == {"done": True} and validated == [value]
    assert [p["reasoning_effort"] for p in payloads] == ["max", second_effort]
    assert ("NOTHING" in payloads[-1]["messages"][-1]["content"]) is (mode == "goal")


def test_task_deadline_bounds_api_timeout_and_stops_retry_before_worker_is_killed(tmp_path, monkeypatch):
    clock = [100.0]
    timeouts = []

    def opener(request, timeout=None):
        timeouts.append(timeout)
        clock[0] += timeout
        raise TimeoutError("read timed out")

    monkeypatch.setattr("tandem.models.slow.time.perf_counter", lambda: clock[0])
    monkeypatch.setattr(urllib.request, "urlopen", opener)
    client = SlowModelClient({"base_url": "http://synthetic"}, SessionConfig(), tmp_path / "audit.jsonl")
    client.set_deadline(105.0)
    with pytest.raises(TimeoutError, match="deadline"):
        client.json_request([{"role": "user", "content": "plan"}], lambda v: True, role="plan_stage")
    assert timeouts == [5.0]


@pytest.mark.parametrize("size", [(960, 540), (1920, 1080)])
def test_goal_rejects_oversized_images_at_resize_floor_before_sending(tmp_path, monkeypatch, size):
    parts = []
    for seed in range(3):
        pixels = random.Random(seed).randbytes(size[0] * size[1])
        frame = Image.frombytes("L", size, pixels)
        encoded = io.BytesIO()
        frame.save(encoded, format="PNG")
        url = "data:image/png;base64," + base64.b64encode(encoded.getvalue()).decode("ascii")
        parts.append({"type": "image_url", "image_url": {"url": url}})

    def opener(*args, **kwargs):
        pytest.fail("An oversized request must not reach the API")

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    config = SessionConfig(execution_mode="goal", slow_max_request_bytes=300_000)
    client = SlowModelClient({"base_url": "http://synthetic"}, config, tmp_path / "audit.jsonl")
    with pytest.raises(ValueError, match=r"exceeds slow_max_request_bytes: \d+ > 300000 bytes"):
        client.json_request([{"role": "user", "content": parts}], lambda v: True, role="plan_stage")
    for part in parts:
        raw = base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
        with Image.open(io.BytesIO(raw)) as image:
            assert image.size == (960, 540)


def test_goal_rejects_oversized_text_without_images(tmp_path, monkeypatch):
    def opener(*args, **kwargs):
        pytest.fail("An oversized request must not reach the API")

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    config = SessionConfig(execution_mode="goal", slow_max_request_bytes=300_000)
    client = SlowModelClient({"base_url": "http://synthetic"}, config, tmp_path / "audit.jsonl")
    with pytest.raises(ValueError, match="exceeds slow_max_request_bytes"):
        client.json_request([{"role": "user", "content": "x" * 300_001}],
                            lambda v: True, role="plan_stage")


def test_goal_allows_request_at_exact_body_limit(tmp_path, monkeypatch):
    requests = []

    def opener(request, **kwargs):
        requests.append(request)
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    config = SessionConfig(execution_mode="goal")
    client = SlowModelClient({"base_url": "http://synthetic"}, config, tmp_path / "audit.jsonl")
    messages = [{"role": "user", "content": "plan"}]
    client.json_request(messages, lambda v: True, role="plan_stage")
    config = SessionConfig(execution_mode="goal", slow_max_request_bytes=len(requests[0].data))
    client = SlowModelClient({"base_url": "http://synthetic"}, config, tmp_path / "audit.jsonl")
    assert client.json_request(messages, lambda v: True, role="plan_stage") == {"done": True}
    assert len(requests) == 2
    assert len(requests[-1].data) == config.slow_max_request_bytes


def test_goal_rechecks_body_limit_after_json_repair(tmp_path, monkeypatch):
    requests = []
    config = SessionConfig(execution_mode="goal", slow_max_request_bytes=200)

    class Response(_Response):
        def read(self):
            return json.dumps({"choices": [{"message": {"content": '{"done":'},
                                            "finish_reason": "length"}]}).encode()

    def opener(request, **kwargs):
        requests.append(request)
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    monkeypatch.setattr("tandem.models.slow.time.sleep", lambda seconds: None)
    client = SlowModelClient({"base_url": "http://synthetic"}, config, tmp_path / "audit.jsonl")
    messages = [{"role": "user", "content": "plan"}]
    with pytest.raises(ValueError, match="exceeds slow_max_request_bytes"):
        client.json_request(messages, lambda v: True, role="plan_stage")
    assert len(requests) == 1
    assert len(requests[0].data) <= config.slow_max_request_bytes
    assert "NOTHING" in messages[-1]["content"]
