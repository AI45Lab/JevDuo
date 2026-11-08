"""HTTP transport helpers for the OSWorld desktop backend."""

from __future__ import annotations

import logging
import time

import requests
from urllib3.util import Timeout

from northcua_protocol.state import CommandState
from northcua_runtime_shared.execution import current_execution, fail_execution

from ._models import DesktopNativeError
from ._types import JsonObject

logger = logging.getLogger(__name__)


class OSWorldHttpMixin:
    """Shared HTTP request helpers for OSWorld backend mixins."""

    base_url: str
    retry_times: int
    retry_interval_seconds: float
    _session: requests.Session

    def _get_bytes(self, path: str, *, timeout: float) -> bytes:
        response = self._request("GET", path, timeout=timeout)
        return response.content

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_payload: JsonObject | None = None,
        timeout: float,
    ) -> requests.Response:
        url = self.base_url + path
        context = current_execution()
        readonly = method.upper() in {"GET", "HEAD", "OPTIONS"}
        attempts = self.retry_times if readonly else 1
        # The configured HTTP timeout is a per-attempt ceiling, not permission
        # to shorten a healthy command's total budget (screenshots may retry).
        # Standalone callers retain the existing finite retry envelope.
        deadline = (
            context.deadline
            if context
            else time.monotonic() + timeout * attempts + self.retry_interval_seconds * (attempts - 1)
        )
        for attempt in range(attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                fail_execution("COMMAND_TIMED_OUT", status=CommandState.TIMED_OUT)
            try:
                response = self._session.request(
                    method,
                    url,
                    json=json_payload,
                    # Bound connect and read by the same remaining budget.
                    # This is not thread/remote-process cancellation: the
                    # device deadline remains the outer response bound.
                    # requests.adapters accepts urllib3.Timeout, though its
                    # third-party stubs only advertise float/tuple.
                    timeout=Timeout(total=min(timeout, remaining)),  # pyright: ignore[reportArgumentType]
                )
                response.raise_for_status()
                if time.monotonic() >= deadline:
                    response.close()
                    fail_execution(
                        "COMMAND_TIMED_OUT" if readonly else "COMMAND_DELIVERY_UNKNOWN",
                        status=CommandState.TIMED_OUT if readonly else CommandState.FAILED,
                    )
                return response
            except requests.RequestException as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                if exc.response is not None:
                    exc.response.close()
                # Never log raw requests, exception text or URLs; requests
                # exceptions can include credentials and endpoint query data.
                logger.warning(
                    "OSWorld desktop transport failed",
                    extra={
                        "method": method,
                        "attempt": attempt + 1,
                        "error_type": type(exc).__name__,
                        "status_code": status_code,
                    },
                )
                if isinstance(exc, requests.HTTPError):
                    if status_code in {400, 422}:
                        # A structured HTTP parameter rejection is an ordinary
                        # tool failure the agent may correct, not lost delivery.
                        raise DesktopNativeError(f"OSWorld command rejected with HTTP {status_code}") from None
                    if not readonly or status_code in {401, 403, 404}:
                        # Received an explicit service/auth/route failure. The
                        # runtime owner handles it; never replay a POST whose
                        # execution effects are not confirmed by that response.
                        fail_execution("COMMAND_TRANSPORT_FAILED")
                if not readonly:
                    # A lost response is not proof that /run_python did not
                    # execute. Only the runtime owner may retry after cleanup.
                    fail_execution("COMMAND_DELIVERY_UNKNOWN")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    fail_execution("COMMAND_TIMED_OUT", status=CommandState.TIMED_OUT)
                if attempt + 1 < attempts:
                    time.sleep(min(self.retry_interval_seconds, remaining))
        fail_execution("COMMAND_TRANSPORT_FAILED")
