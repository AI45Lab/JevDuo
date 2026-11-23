"""Local and VM process transports. A mutation is submitted at most once."""

import http.client
import json
import os
import subprocess
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from .http import post_json


def uncertain(code):
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": "agent-desktop execution could not be confirmed; inspect the desktop before retrying",
            "disposition": {"delivery": "delivery_uncertain", "retry": "unsafe"},
        },
    }


def execution_result(output, returncode):
    result = json.loads(output)
    if (
        type(returncode) is not int
        or not isinstance(result, dict)
        or type(result.get("ok")) is not bool
        or (result["ok"] and returncode != 0)
        or (not result["ok"] and not isinstance(result.get("error"), dict))
    ):
        raise ValueError("invalid execution envelope")
    return result


def remote_endpoint(value):
    try:
        url = urlsplit(value)
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or url.path not in ("", "/", "/execute")
            or (url.port is not None and not 0 < url.port <= 65535)
        ):
            raise ValueError()
        return urlunsplit((url.scheme, url.netloc, "/execute", "", ""))
    except (ValueError, TypeError):
        raise ValueError(
            "AGENT_DESKTOP_VM_URL must name the VM root or /execute without credentials or query parameters"
        ) from None


@dataclass(frozen=True)
class LocalTransport:
    env: dict[str, str] | None = None

    def invoke(self, executable, argv, timeout):
        try:
            process = subprocess.run(
                [executable, *argv], capture_output=True, timeout=timeout, env=self.env, check=False
            )
            return execution_result(process.stdout, process.returncode)
        except (OSError, subprocess.SubprocessError, ValueError, UnicodeError):
            return uncertain("SPAWN_FAILED")


class RemoteTransport:
    def __init__(self, url):
        self.endpoint = remote_endpoint(url)

    def invoke(self, executable, argv, timeout):
        try:
            envelope = post_json(
                self.endpoint, {"command": [executable, *argv], "shell": False}, timeout=timeout, proxy=False
            )
            return execution_result(envelope["output"], envelope["returncode"])
        except (
            OSError,
            ValueError,
            UnicodeError,
            KeyError,
            TypeError,
            RuntimeError,
            http.client.HTTPException,
        ):
            return uncertain("REMOTE_EXECUTION_UNCONFIRMED")


@dataclass(frozen=True)
class DesktopConfig:
    binary: str = "agent-desktop"
    snapshot_timeout_ms: int = 3000
    verify_ms: float = 2000
    session: str | None = None
    agent_id: str | None = None
    headed: bool = False
    action_timeout_ms: int | None = None
    clipboard_text: bool = False

    def __post_init__(self):
        import math

        if type(self.clipboard_text) is not bool:
            raise ValueError("clipboard_text must be a boolean")
        if type(self.snapshot_timeout_ms) is not int or not 1 <= self.snapshot_timeout_ms <= 300000:
            raise ValueError("snapshot timeout must be an integer from 1 to 300000 ms")
        if (
            type(self.verify_ms) not in (int, float)
            or not math.isfinite(self.verify_ms)
            or not 0 <= self.verify_ms <= 60000
        ):
            raise ValueError("JEV_VERIFY_MS must be a number of milliseconds from 0 to 60000")
        if self.action_timeout_ms is not None and (
            type(self.action_timeout_ms) is not int or not 1 <= self.action_timeout_ms <= 300000
        ):
            raise ValueError("action timeout must be an integer from 1 to 300000 ms")
        if not self.binary:
            raise ValueError("an agent-desktop binary is required")

    @classmethod
    def from_env(cls, *, remote=False, overrides=None):
        values = {
            "binary": os.environ.get("AGENT_DESKTOP_BIN", "agent-desktop"),
            "snapshot_timeout_ms": os.environ.get(
                "AGENT_DESKTOP_SNAPSHOT_TIMEOUT_MS", "15000" if remote else "3000"
            ),
            "verify_ms": os.environ.get("JEV_VERIFY_MS", "2000"),
            "action_timeout_ms": os.environ.get("AGENT_DESKTOP_ACTION_TIMEOUT_MS"),
            "session": os.environ.get("AGENT_DESKTOP_SESSION"),
            "agent_id": os.environ.get("AGENT_DESKTOP_AGENT_ID"),
            **(overrides or {}),
        }
        if isinstance(values["snapshot_timeout_ms"], str):
            values["snapshot_timeout_ms"] = int(values["snapshot_timeout_ms"])
        if isinstance(values["action_timeout_ms"], str):
            values["action_timeout_ms"] = int(values["action_timeout_ms"])
        if isinstance(values["verify_ms"], str):
            values["verify_ms"] = float(values["verify_ms"])
        return cls(**values)
