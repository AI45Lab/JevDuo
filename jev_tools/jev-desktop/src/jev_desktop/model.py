"""TypeSafe-compatible decisions client, separate from loop policy."""

import http.client
import ipaddress
import os
import time
from urllib.parse import urlsplit

from .http import HttpStatusError, post_json


def model_endpoint(value, *, setting="TYPESAFE_BASE_URL"):
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        loopback = host == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(host).is_loopback
        except ValueError:
            pass
        if (
            not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.scheme not in ("https", "http")
            or (parsed.scheme == "http" and not loopback)
            or (port is not None and not 0 < port <= 65535)
        ):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError(
            f"{setting} must use HTTPS, or HTTP on a loopback address, without credentials"
        ) from None
    return value


def resolve_model(model, endpoint):
    host = urlsplit(endpoint).hostname
    return f"~typesafe/{model}" if "/" not in model and host == "openrouter.ai" else model


def post_model_json(endpoint, api_key, request, *, sleep=time.sleep):
    for attempt in range(3):
        try:
            return post_json(endpoint, request, headers={"Authorization": f"Bearer {api_key}"})
        except HttpStatusError as error:
            if error.status in (429, 503, 529) and attempt < 2:
                sleep(0.5 * 2**attempt)
                continue
            raise RuntimeError(
                f"the model returned HTTP {error.status}; no action dispatched for this decision"
            ) from None
        except (ValueError, UnicodeError):
            raise RuntimeError(
                "the model returned an invalid response; no action dispatched for this decision"
            ) from None
        except (OSError, http.client.HTTPException):
            raise RuntimeError(
                "the model could not be reached; no action dispatched for this decision"
            ) from None
    raise RuntimeError("the model stayed unavailable")


class TypeSafeModel:
    def __init__(
        self,
        api_key,
        *,
        endpoint="https://api.typesafe.ai/v1/systemone",
        model="jev-latest",
        sleep=time.sleep,
    ):
        if not api_key:
            raise ValueError("TYPESAFE_API_KEY unset")
        self.endpoint = model_endpoint(endpoint)
        self.api_key = api_key
        self.model = resolve_model(model, self.endpoint)
        self.sleep = sleep

    @classmethod
    def from_env(cls):
        return cls(
            os.environ.get("TYPESAFE_API_KEY"),
            endpoint=os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai/v1/systemone"),
            model=os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        )

    def ask(self, request):
        result = post_model_json(
            self.endpoint, self.api_key, {**request, "model": self.model}, sleep=self.sleep
        )
        if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
            raise RuntimeError(
                "the model returned an invalid response; no action dispatched for this decision"
            )
        return result
