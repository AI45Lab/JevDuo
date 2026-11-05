"""Provider response errors retain retry semantics without account state."""

import io
import json
import urllib.request

import pytest

from jevduo.config import SessionConfig
from jevduo.models.slow import SlowModelClient


class _Response(io.BytesIO):
    status = 200
    headers = {}


@pytest.fixture
def client(tmp_path):
    return SlowModelClient({
        "base_url": "https://api.minimax.cn/v1/", "api_key": "test-only-key",
        "model": "MiniMax-M3.1-Flash",
    }, SessionConfig(), tmp_path / "audit.jsonl")


@pytest.mark.parametrize("base_url", ["https://api.minimax.cn/v1/", "http://gateway/v1/"])
@pytest.mark.parametrize("code_type", [int, str])
@pytest.mark.parametrize("code", [1000, 1001, 1024, 1033, 2064, 2066])
def test_minimax_http_200_temporary_error_recovers_with_same_request(
        client, monkeypatch, base_url, code_type, code):
    client.credentials["base_url"] = base_url
    requests, waits = [], []

    def opener(request, **kwargs):
        requests.append(request)
        raw = ({"base_resp": {"status_code": code_type(code), "status_msg": "temporary failure"}}
               if len(requests) == 1 else
               {"choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}]})
        return _Response(json.dumps(raw).encode())

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    monkeypatch.setattr("jevduo.models.slow.time.sleep", waits.append)
    assert client.json_request([{"role": "user", "content": "probe"}], lambda v: v == {"ok": True},
                               role="plan_stage") == {"ok": True}
    assert len(requests) == 2 and waits == [30]
    assert requests[0].data == requests[1].data
    assert all(r.full_url == base_url.rstrip("/") + "/chat/completions" for r in requests)
    assert all(r.get_header("Authorization") == "Bearer test-only-key" for r in requests)
    assert sorted(p.name for p in client.audit_path.parent.iterdir()) == ["audit.jsonl"]


@pytest.mark.parametrize("code", [1000, 1001, 1024, 1033, 2064, 2066])
def test_minimax_temporary_error_retries_remain_bounded(client, monkeypatch, code):
    requests, waits = [], []

    def opener(request, **kwargs):
        requests.append(request)
        return _Response(json.dumps({"base_resp": {"status_code": code, "status_msg": "busy"}}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    monkeypatch.setattr("jevduo.models.slow.time.sleep", waits.append)
    with pytest.raises(RuntimeError, match=f"code={code}"):
        client.json_request([{"role": "user", "content": "probe"}], lambda v: True, role="plan_stage")
    assert len(requests) == 3 and waits == [30, 30]


@pytest.mark.parametrize("status,raw", [
    (200, {"base_resp": {"status_code": 1004, "status_msg": "invalid key"}}),
    (200, {"base_resp": {"status_code": 1008, "status_msg": "quota exhausted"}}),
    (200, {"base_resp": {"status_code": 9999, "status_msg": "unknown failure"}}),
    (200, {"error": {"code": 1000, "message": "unrelated provider error"}}),
    (400, {"base_resp": {"status_code": 1000, "status_msg": "bad request"}}),
    (401, {"base_resp": {"status_code": 1000, "status_msg": "unauthorized"}}),
])
def test_other_api_errors_do_not_gain_retries(client, monkeypatch, status, raw):
    requests, waits = [], []

    def opener(request, **kwargs):
        requests.append(request)
        response = _Response(json.dumps(raw).encode())
        response.status = status
        return response

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    monkeypatch.setattr("jevduo.models.slow.time.sleep", waits.append)
    with pytest.raises(RuntimeError):
        client.json_request([{"role": "user", "content": "probe"}], lambda v: True, role="plan_stage")
    assert len(requests) == 1 and waits == []
