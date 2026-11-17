"""Bounded JSON POST transport. Redirects are never followed."""

import http.client
import json
import time
import urllib.error
import urllib.request
from contextlib import closing

from .deadline import DeadlineHandler

MAX_RESPONSE = 64 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpStatusError(RuntimeError):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


def post_json(url, body, *, headers=None, timeout=30, proxy=True):
    if not proxy:
        return post_json_direct(url, body, timeout=timeout)
    handlers = [NoRedirect(), DeadlineHandler(timeout)]
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(
        url,
        data=json.dumps(body, allow_nan=False).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            payload = response.read(MAX_RESPONSE + 1)
            if len(payload) > MAX_RESPONSE:
                raise ValueError("response exceeds 64 MiB")
            return json.loads(payload)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise HttpStatusError(status) from None


def post_json_direct(url, body, *, timeout):
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    transport = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    deadline = time.monotonic() + timeout

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("HTTP deadline exceeded")
        return value

    with closing(transport(parsed.hostname, parsed.port, timeout=remaining())) as connection:
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection.request(
            "POST",
            path,
            body=json.dumps(body, allow_nan=False).encode(),
            headers={"Content-Type": "application/json"},
        )
        channel = connection.sock
        channel.settimeout(remaining())
        with connection.getresponse() as response:
            if response.status != 200:
                raise HttpStatusError(response.status)
            parts, size = [], 0
            while not response.isclosed():
                channel.settimeout(remaining())
                part = response.read1(min(65536, MAX_RESPONSE + 1 - size))
                if not part:
                    break
                parts.append(part)
                size += len(part)
                if size > MAX_RESPONSE:
                    raise ValueError("response exceeds 64 MiB")
            if response.length not in (None, 0):
                raise ValueError("HTTP response was truncated")
            return json.loads(b"".join(parts))
