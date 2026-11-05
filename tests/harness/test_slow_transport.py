"""Slow-model transport failures use the existing bounded retry path."""

import base64
import http.client
import io
import json
import random
import urllib.error
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


@pytest.fixture
def endpoint_client(tmp_path, monkeypatch):
    monkeypatch.setattr("tandem.models.slow.time.sleep", lambda seconds: None)
    return SlowModelClient({
        "base_url": "http://gateway/v1/", "model": "MiniMax-M3.1-Flash",
        "api_key": "test-only-gateway-key",
    }, SessionConfig(slow_reasoning_effort="low"), tmp_path / "audit.jsonl")


@pytest.mark.parametrize("role", ["plan_stage", "verify_stage", "ground_visual"])
@pytest.mark.parametrize("failure", [TimeoutError, http.client.RemoteDisconnected,
                                     urllib.error.URLError, http.client.IncompleteRead])
def test_transport_failure_retries_endpoint_without_changing_reasoning_or_replaying_actions(
        endpoint_client, monkeypatch, role, failure):
    requests = []

    def opener(request, **kwargs):
        requests.append(request)
        if len(requests) == 1:
            if failure is http.client.IncompleteRead:
                raise failure(b"synthetic partial response")
            raise failure("synthetic transport failure")
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    assert endpoint_client.json_request([{"role": "user", "content": "plan"}], lambda v: True,
                                      role=role) == {"done": True}
    assert len(requests) == 2
    assert all(r.full_url == "http://gateway/v1/chat/completions" for r in requests)
    assert all(r.get_header("Authorization") == "Bearer test-only-gateway-key" for r in requests)
    assert requests[0].data == requests[1].data
    payload = json.loads(requests[0].data)
    assert payload["model"] == "MiniMax-M3.1-Flash" and payload["stream"] is False
    events = [json.loads(line) for line in endpoint_client.audit_path.read_text().splitlines()]
    assert not any(e["event"] in {"key_cooldown", "key_changed"} for e in events)
    assert "test-only-gateway-key" not in endpoint_client.audit_path.read_text()
    assert sorted(p.name for p in endpoint_client.audit_path.parent.iterdir()) == ["audit.jsonl"]


def test_temporary_server_error_retries_same_endpoint(endpoint_client, monkeypatch):
    requests = []

    class Unavailable(_Response):
        status = 503

        def read(self):
            return b'{"error":{"message":"synthetic temporary outage"}}'

    def opener(request, **kwargs):
        requests.append(request)
        return Unavailable() if len(requests) == 1 else _Response()

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    assert endpoint_client.json_request([{"role": "user", "content": "plan"}], lambda v: True,
                                      role="plan_stage") == {"done": True}
    assert len(requests) == 2
    assert requests[0].get_header("Authorization") == requests[1].get_header("Authorization")


@pytest.mark.parametrize("failure", [TimeoutError, http.client.IncompleteRead])
def test_endpoint_transport_retries_are_bounded(endpoint_client, monkeypatch, failure):
    requests = []

    def opener(request, **kwargs):
        requests.append(request)
        if failure is http.client.IncompleteRead:
            raise failure(b"synthetic partial response")
        raise TimeoutError("synthetic read timeout")

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    with pytest.raises(failure):
        endpoint_client.json_request([{"role": "user", "content": "plan"}], lambda v: True,
                                   role="plan_stage")
    assert len(requests) == 3
    assert len({r.get_header("Authorization") for r in requests}) == 1


def test_invalid_json_repair_uses_same_endpoint(endpoint_client, monkeypatch):
    requests = []

    class Invalid(_Response):
        def read(self):
            return b'{"choices":[{"message":{"content":"not JSON"}}]}'

    def opener(request, **kwargs):
        requests.append(request)
        return Invalid() if len(requests) == 1 else _Response()

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    assert endpoint_client.json_request([{"role": "user", "content": "plan"}], lambda v: True,
                                      role="plan_stage") == {"done": True}
    assert requests[0].get_header("Authorization") == requests[1].get_header("Authorization")
    assert "key_cooldown" not in endpoint_client.audit_path.read_text()


@pytest.mark.parametrize("status", [400, 401, 402, 403])
def test_gateway_errors_are_redacted_without_local_account_state(endpoint_client, monkeypatch, status):
    calls, captured = [], []
    secret = endpoint_client.credentials["api_key"]
    endpoint_client.llm_sink = lambda **record: captured.append(record)

    def opener(request, **kwargs):
        calls.append(request)
        body = json.dumps({"error": {"message": "gateway rejected " + secret}}).encode()
        raise urllib.error.HTTPError(request.full_url, status, "gateway error", {}, io.BytesIO(body))

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    with pytest.raises(RuntimeError, match=f"status={status}") as raised:
        endpoint_client.json_request([{"role": "user", "content": "plan"}], lambda v: True,
                                     role="plan_stage")
    assert len(calls) == 1 and captured[0]["http_status"] == status
    assert secret not in str(raised.value) + endpoint_client.audit_path.read_text() + json.dumps(captured)
    assert "[REDACTED]" in json.dumps(captured)
    assert sorted(p.name for p in endpoint_client.audit_path.parent.iterdir()) == ["audit.jsonl"]


@pytest.mark.parametrize("status", [408, 429, 503])
@pytest.mark.parametrize("headers,delay", [({}, 30), ({"Retry-After": "2"}, 2)])
def test_gateway_retries_honor_retry_after_and_remain_bounded(
        endpoint_client, monkeypatch, status, headers, delay):
    calls, waits = [], []

    def opener(request, **kwargs):
        calls.append(request)
        raise urllib.error.HTTPError(request.full_url, status, "busy", headers,
                                     io.BytesIO(b'{"error":{"message":"busy"}}'))

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    monkeypatch.setattr("tandem.models.slow.time.sleep", waits.append)
    with pytest.raises(RuntimeError, match=f"status={status}"):
        endpoint_client.json_request([{"role": "user", "content": "plan"}], lambda v: True,
                                     role="plan_stage")
    assert len(calls) == 3 and waits == [delay, delay]
    assert len({r.get_header("Authorization") for r in calls}) == 1


def test_gateway_429_can_recover_on_last_attempt(endpoint_client, monkeypatch):
    calls, waits = [], []

    def opener(request, **kwargs):
        calls.append(request)
        if len(calls) < 3:
            raise urllib.error.HTTPError(request.full_url, 429, "busy", {},
                                         io.BytesIO(b'{"error":{"message":"rate limited"}}'))
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    monkeypatch.setattr("tandem.models.slow.time.sleep", waits.append)
    assert endpoint_client.json_request([{"role": "user", "content": "plan"}], lambda v: True,
                                        role="plan_stage") == {"done": True}
    assert len(calls) == 3 and waits == [30, 30]
    assert len({r.data for r in calls}) == 1
