"""OSWorld-backed desktop-native backend composition root."""

from __future__ import annotations

import os

import requests

from northcua.core.desktop_runtime_environment import (
    OSWORLD_COMMAND_TIMEOUT_ENV,
    OSWORLD_RETRY_INTERVAL_ENV,
    OSWORLD_RETRY_TIMES_ENV,
    OSWORLD_SCREENSHOT_TIMEOUT_ENV,
    OSWORLD_SERVER_PORT_ENV,
    OSWORLD_VM_IP_ENV,
)

from ._models import DesktopNativeBackend
from ._pyautogui_support import PyAutoGuiSupportMixin
from .osworld_actions import OSWorldActionMixin
from .osworld_http import OSWorldHttpMixin
from .osworld_windowing import OSWorldWindowMixin


class OSWorldDesktopBackend(
    OSWorldActionMixin,
    OSWorldWindowMixin,
    OSWorldHttpMixin,
    PyAutoGuiSupportMixin,
    DesktopNativeBackend,
):
    """Desktop backend that drives an OSWorld VM through its HTTP service.

    The class is intentionally a thin composition root. HTTP retries,
    screenshot/window metadata, and pyautogui action serialization live in
    focused mixins so OSWorld support follows the same modularity standards as
    the native desktop backend.
    """

    def __init__(self) -> None:
        self.vm_ip = os.environ.get(OSWORLD_VM_IP_ENV, "127.0.0.1").strip() or "127.0.0.1"
        self.server_port = int(os.environ.get(OSWORLD_SERVER_PORT_ENV, "5000") or "5000")
        self.command_timeout_seconds = float(os.environ.get(OSWORLD_COMMAND_TIMEOUT_ENV, "120") or "120")
        self.screenshot_timeout_seconds = float(os.environ.get(OSWORLD_SCREENSHOT_TIMEOUT_ENV, "10") or "10")
        self.retry_times = max(int(os.environ.get(OSWORLD_RETRY_TIMES_ENV, "3") or "3"), 1)
        self.retry_interval_seconds = max(float(os.environ.get(OSWORLD_RETRY_INTERVAL_ENV, "5") or "5"), 0.0)
        self.base_url = f"http://{self.vm_ip}:{self.server_port}"
        self._session = requests.Session()
        self._session.trust_env = False
