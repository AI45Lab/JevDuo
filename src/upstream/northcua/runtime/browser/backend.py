"""Shared Playwright browser backend used by browser-facing runtimes and MCP servers."""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Sequence

try:
    from typing import TypeAlias
except ImportError:  # pragma: no cover - compatibility for older ad-hoc test interpreters
    from typing_extensions import TypeAlias

from fastmcp.exceptions import ToolError

from northcua.core.enums import PlaywrightBrowserEngine

from .snapshot_script import BROWSER_SNAPSHOT_SCRIPT

PLAYWRIGHT_BROWSER_ENV_VAR = "NORTHCUA_PLAYWRIGHT_BROWSER"
PLAYWRIGHT_HEADLESS_ENV_VAR = "NORTHCUA_PLAYWRIGHT_HEADLESS"
PLAYWRIGHT_VIEWPORT_ENV_VAR = "NORTHCUA_PLAYWRIGHT_VIEWPORT"
logger = logging.getLogger(__name__)


def _browser_env(name: str, default: str) -> str:
    """Prefer canonical settings while accepting the pre-rename environment keys."""
    return os.getenv(name, os.getenv(name.replace("NORTHCUA_", "NEXCUA_", 1), default))


ClickOptions: TypeAlias = dict[str, object]


@dataclass(slots=True)
class BrowserTabInfo:
    """Browser tab metadata exposed by browser runtimes.

    Attributes:
        index: Zero-based tab index in the current browser context.
        title: Current page title, or an empty string when unavailable.
        url: Current page URL.
        is_active: Whether this tab is selected by the backend.
    """

    index: int
    title: str
    url: str
    is_active: bool


@dataclass(slots=True)
class BrowserElementInfo:
    """One semantic element captured during a browser snapshot.

    Attributes:
        selector: Stable-ish CSS selector generated for the element.
        role: Explicit or inferred accessibility role.
        name: User-visible accessible name approximation.
        tag_name: Lowercase DOM tag name.
        depth: Depth relative to the snapshot root.
    """

    selector: str
    role: str
    name: str
    tag_name: str
    depth: int


@dataclass(slots=True)
class BrowserSnapshotResult:
    """Structured page snapshot payload.

    Attributes:
        title: Current page title.
        url: Current page URL.
        elements: Semantic elements discovered in traversal order.
    """

    title: str
    url: str
    elements: list[BrowserElementInfo] = field(default_factory=list)


class PlaywrightBrowserBackend(ABC):
    """Abstract backend for browser-native Playwright tools."""

    @abstractmethod
    def list_tabs(self) -> list[BrowserTabInfo]:
        """List the tabs in the current browser context.

        Returns:
            Tab metadata in context order.
        """

    @abstractmethod
    def new_tab(self) -> BrowserTabInfo:
        """Create a new tab and select it.

        Returns:
            Metadata for the newly selected tab.
        """

    @abstractmethod
    def close_tab(self, index: int | None = None) -> None:
        """Close one tab.

        Args:
            index: Optional zero-based tab index; defaults to the active tab.
        """

    @abstractmethod
    def select_tab(self, index: int) -> BrowserTabInfo:
        """Select a tab by index.

        Args:
            index: Zero-based tab index to activate.

        Returns:
            Metadata for the selected tab.
        """

    @abstractmethod
    def navigate(self, url: str) -> str:
        """Navigate the current tab to a URL.

        Args:
            url: Destination URL accepted by Playwright.

        Returns:
            Final page URL after navigation.
        """

    @abstractmethod
    def navigate_back(self) -> None:
        """Navigate back in history."""

    @abstractmethod
    def navigate_forward(self) -> None:
        """Navigate forward in history."""

    @abstractmethod
    def reload(self) -> None:
        """Reload the current page."""

    @abstractmethod
    def capture_snapshot(self, selector: str | None = None, depth: int | None = None) -> BrowserSnapshotResult:
        """Capture a semantic page snapshot.

        Args:
            selector: Optional CSS selector for the snapshot root.
            depth: Optional maximum DOM traversal depth.

        Returns:
            Structured page snapshot payload.
        """

    @abstractmethod
    def click(
        self,
        selector: str,
        *,
        button: str | None = None,
        double_click: bool = False,
        modifiers: Sequence[str] | None = None,
    ) -> None:
        """Click an element.

        Args:
            selector: CSS selector resolved in the current page.
            button: Optional mouse button name.
            double_click: Whether to perform a double click.
            modifiers: Optional keyboard modifiers held during the click.
        """

    @abstractmethod
    def drag(self, start_selector: str, end_selector: str) -> None:
        """Drag from one element to another.

        Args:
            start_selector: CSS selector for the drag source.
            end_selector: CSS selector for the drop target.
        """

    @abstractmethod
    def hover(self, selector: str) -> None:
        """Hover an element.

        Args:
            selector: CSS selector resolved in the current page.
        """

    @abstractmethod
    def select_option(self, selector: str, values: Sequence[str]) -> None:
        """Select dropdown values.

        Args:
            selector: CSS selector for a select-like element.
            values: Option values to select.
        """

    @abstractmethod
    def set_checked(self, selector: str, checked: bool) -> None:
        """Check or uncheck a checkbox/radio control.

        Args:
            selector: CSS selector for a checkbox or radio control.
            checked: Desired checked state.
        """

    @abstractmethod
    def stop(self) -> None:
        """Release browser resources owned by the backend."""


class PythonPlaywrightBrowserBackend(PlaywrightBrowserBackend):
    """Runtime Playwright backend implemented with `playwright.sync_api`."""

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._context = None
        self._current_tab_index = 0
        self._browser_name = resolve_playwright_browser_name()
        self._headless = _browser_env(PLAYWRIGHT_HEADLESS_ENV_VAR, "1").lower() not in {"0", "false", "no"}
        self._viewport = self._parse_viewport(_browser_env(PLAYWRIGHT_VIEWPORT_ENV_VAR, "1280x720"))

    def list_tabs(self) -> list[BrowserTabInfo]:
        """List tabs in the lazily-created Playwright context.

        Returns:
            Tab metadata in context order.
        """

        context = self._ensure_context()
        return [
            BrowserTabInfo(
                index=index,
                title=self._safe_page_title(page),
                url=str(page.url or ""),
                is_active=index == self._current_tab_index,
            )
            for index, page in enumerate(context.pages)
        ]

    def new_tab(self) -> BrowserTabInfo:
        """Create and select a new Playwright page.

        Returns:
            Metadata for the newly selected tab.
        """

        context = self._ensure_context()
        page = context.new_page()
        self._current_tab_index = len(context.pages) - 1
        return BrowserTabInfo(
            index=self._current_tab_index,
            title=self._safe_page_title(page),
            url=str(page.url or ""),
            is_active=True,
        )

    def close_tab(self, index: int | None = None) -> None:
        """Close a tab while preserving the active-index fallback behavior.

        Args:
            index: Optional zero-based tab index; defaults to the active tab.
        """

        context = self._ensure_context()
        if not context.pages:
            return
        target_index = self._current_tab_index if index is None else int(index)
        page = self._page_at_index(target_index)
        page.close()
        self._current_tab_index = min(self._current_tab_index, len(context.pages) - 1) if context.pages else 0

    def select_tab(self, index: int) -> BrowserTabInfo:
        """Select an existing tab by index.

        Args:
            index: Zero-based tab index to activate.

        Returns:
            Metadata for the selected tab.
        """

        page = self._page_at_index(index)
        self._current_tab_index = int(index)
        page.bring_to_front()
        return BrowserTabInfo(
            index=self._current_tab_index,
            title=self._safe_page_title(page),
            url=str(page.url or ""),
            is_active=True,
        )

    def navigate(self, url: str) -> str:
        """Navigate the active tab and return the resulting URL.

        Args:
            url: Destination URL passed through to Playwright.

        Returns:
            Current page URL after navigation completes.
        """

        page = self._ensure_page()
        page.goto(url, wait_until="domcontentloaded")
        return str(page.url or url)

    def navigate_back(self) -> None:
        """Navigate the active tab back one history entry."""

        self._ensure_page().go_back(wait_until="domcontentloaded")

    def navigate_forward(self) -> None:
        """Navigate the active tab forward one history entry."""

        self._ensure_page().go_forward(wait_until="domcontentloaded")

    def reload(self) -> None:
        """Reload the active tab."""

        self._ensure_page().reload(wait_until="domcontentloaded")

    def capture_snapshot(self, selector: str | None = None, depth: int | None = None) -> BrowserSnapshotResult:
        """Capture a semantic snapshot from the active tab.

        Args:
            selector: Optional CSS selector for the snapshot root.
            depth: Optional maximum DOM traversal depth.

        Returns:
            Structured page snapshot payload.
        """

        page = self._ensure_page()
        snapshot_payload = page.evaluate(BROWSER_SNAPSHOT_SCRIPT, {"selector": selector, "depth": depth})
        elements = [
            BrowserElementInfo(
                selector=str(item.get("selector") or ""),
                role=str(item.get("role") or ""),
                name=str(item.get("name") or ""),
                tag_name=str(item.get("tag_name") or ""),
                depth=int(item.get("depth") or 0),
            )
            for item in snapshot_payload.get("elements", [])
            if str(item.get("selector") or "").strip()
        ]
        return BrowserSnapshotResult(
            title=str(snapshot_payload.get("title") or ""),
            url=str(snapshot_payload.get("url") or page.url or ""),
            elements=elements,
        )

    def click(
        self,
        selector: str,
        *,
        button: str | None = None,
        double_click: bool = False,
        modifiers: Sequence[str] | None = None,
    ) -> None:
        """Click an element in the active tab.

        Args:
            selector: CSS selector resolved by Playwright.
            button: Optional mouse button name.
            double_click: Whether to perform a double click.
            modifiers: Optional keyboard modifiers held during the click.
        """

        locator = self._ensure_page().locator(selector).first
        options: ClickOptions = {}
        if button:
            options["button"] = button
        if modifiers:
            options["modifiers"] = list(modifiers)
        if double_click:
            locator.dblclick(**options)
        else:
            locator.click(**options)

    def drag(self, start_selector: str, end_selector: str) -> None:
        """Drag from one element to another in the active tab.

        Args:
            start_selector: CSS selector for the drag source.
            end_selector: CSS selector for the drop target.
        """

        page = self._ensure_page()
        page.locator(start_selector).first.drag_to(page.locator(end_selector).first)

    def hover(self, selector: str) -> None:
        """Hover an element in the active tab.

        Args:
            selector: CSS selector resolved by Playwright.
        """

        self._ensure_page().locator(selector).first.hover()

    def select_option(self, selector: str, values: Sequence[str]) -> None:
        """Select option values in the active tab.

        Args:
            selector: CSS selector for a select-like element.
            values: Option values to select.
        """

        self._ensure_page().locator(selector).first.select_option(list(values))

    def set_checked(self, selector: str, checked: bool) -> None:
        """Set a checkbox or radio control state in the active tab.

        Args:
            selector: CSS selector for a checkbox or radio control.
            checked: Desired checked state.
        """

        locator = self._ensure_page().locator(selector).first
        if checked:
            locator.check()
        else:
            locator.uncheck()

    def stop(self) -> None:
        """Release Playwright resources and reset active-tab bookkeeping."""

        if self._context is not None:
            self._context.close()
            self._context = None
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None
        self._current_tab_index = 0

    def _ensure_context(self) -> object:
        """Return the active Playwright context, creating it lazily.

        Returns:
            Started Playwright browser context.

        Raises:
            ToolError: If Playwright is unavailable or the configured browser is unsupported.
        """

        if self._context is not None:
            return self._context
        try:
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:  # pragma: no cover - environment-dependent
            raise ToolError(
                "Playwright is not installed. Add the 'playwright' Python package and run "
                f"'python -m playwright install {self._browser_name}'."
            ) from exc
        self._playwright = sync_playwright().start()
        browser_launcher = resolve_playwright_launcher(self._playwright, self._browser_name)
        self._browser = browser_launcher.launch(headless=self._headless)
        self._context = self._browser.new_context(viewport=self._viewport)
        self._context.new_page()
        return self._context

    def _ensure_page(self) -> object:
        """Return the active page, creating a fallback tab if needed.

        Returns:
            Active Playwright page object.
        """

        context = self._ensure_context()
        if not context.pages:
            context.new_page()
            self._current_tab_index = 0
        self._current_tab_index = min(self._current_tab_index, len(context.pages) - 1)
        return context.pages[self._current_tab_index]

    def _page_at_index(self, index: int) -> object:
        """Return a page by zero-based tab index.

        Args:
            index: Tab index in the current context.

        Returns:
            Playwright page object at the requested index.

        Raises:
            ToolError: If the tab index is outside the current context.
        """

        context = self._ensure_context()
        if index < 0 or index >= len(context.pages):
            raise ToolError(f"Tab index {index} is out of range.")
        return context.pages[index]

    @staticmethod
    def _safe_page_title(page: object) -> str:
        """Read a page title without failing tab listing.

        Args:
            page: Playwright page-like object.

        Returns:
            Page title, or an empty string when title lookup fails.
        """

        try:
            return str(page.title() or "")
        except Exception:
            logger.debug("Playwright page title lookup failed.", exc_info=True)
            return ""

    @staticmethod
    def _parse_viewport(viewport_value: str) -> dict[str, int]:
        """Parse a ``WIDTHxHEIGHT`` viewport string with a stable fallback.

        Args:
            viewport_value: Environment-provided viewport value.

        Returns:
            Playwright viewport dictionary.
        """

        width_str, _, height_str = str(viewport_value or "1280x720").partition("x")
        try:
            return {"width": int(width_str), "height": int(height_str)}
        except ValueError:
            return {"width": 1280, "height": 720}


def resolve_playwright_browser_name(
    browser_name: str | PlaywrightBrowserEngine | None = None,
) -> str:
    """Resolve a browser engine name from an explicit override or environment.

    Args:
        browser_name: Optional explicit browser name or enum value.

    Returns:
        Normalized Playwright engine name.

    Raises:
        ToolError: If the requested browser is not supported.
    """

    raw_name = browser_name or _browser_env(PLAYWRIGHT_BROWSER_ENV_VAR, PlaywrightBrowserEngine.CHROMIUM.value)
    if isinstance(raw_name, PlaywrightBrowserEngine):
        resolved = raw_name.value
    else:
        resolved = str(raw_name).strip().lower()
        enum_prefix = f"{PlaywrightBrowserEngine.__name__.lower()}."
        if resolved.startswith(enum_prefix):
            resolved = resolved.removeprefix(enum_prefix)
    if resolved not in {member.value for member in PlaywrightBrowserEngine}:
        raise ToolError(f"Unsupported Playwright browser '{resolved}'. Expected one of chromium, firefox, or webkit.")
    return resolved


def resolve_playwright_launcher(
    playwright: object,
    browser_name: str | PlaywrightBrowserEngine | None = None,
) -> object:
    """Resolve the browser launcher object for one Playwright engine name.

    Args:
        playwright: Started Playwright sync API object.
        browser_name: Optional explicit browser name or enum value.

    Returns:
        Browser launcher object for the resolved engine.

    Raises:
        ToolError: If Playwright does not expose a launcher for the engine.
    """

    resolved_name = resolve_playwright_browser_name(browser_name)
    browser_launcher = getattr(playwright, resolved_name, None)
    if browser_launcher is None:
        raise ToolError(
            f"Unsupported Playwright browser '{resolved_name}'. Expected one of chromium, firefox, or webkit."
        )
    return browser_launcher
