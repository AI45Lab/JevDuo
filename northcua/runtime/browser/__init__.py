"""Shared browser runtime utilities for Playwright-backed NorthCUA features."""

from .backend import (
    BROWSER_SNAPSHOT_SCRIPT,
    PLAYWRIGHT_BROWSER_ENV_VAR,
    PLAYWRIGHT_HEADLESS_ENV_VAR,
    BrowserElementInfo,
    BrowserSnapshotResult,
    BrowserTabInfo,
    PlaywrightBrowserBackend,
    PythonPlaywrightBrowserBackend,
    resolve_playwright_browser_name,
    resolve_playwright_launcher,
)

__all__ = [
    "BROWSER_SNAPSHOT_SCRIPT",
    "BrowserElementInfo",
    "BrowserSnapshotResult",
    "BrowserTabInfo",
    "PLAYWRIGHT_BROWSER_ENV_VAR",
    "PLAYWRIGHT_HEADLESS_ENV_VAR",
    "PlaywrightBrowserBackend",
    "PythonPlaywrightBrowserBackend",
    "resolve_playwright_browser_name",
    "resolve_playwright_launcher",
]
