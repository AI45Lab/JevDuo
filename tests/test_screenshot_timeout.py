"""Real FastMCP screenshot wrapper must not discard a successful read after ten seconds."""

import time
import unittest
from types import SimpleNamespace

from fastmcp import FastMCP
from northcua.runtime.mcp.desktop_native.backend import DesktopCoordinateFrame
from northcua.runtime.mcp.desktop_native.data_tools import _register_desktop_native_data_tools
from northcua.runtime.mcp.desktop_native.osworld_backend import OSWorldDesktopBackend
from northcua_runtime_shared.desktop_surface.data_contracts import desktop_data_contracts


class SlowRead(OSWorldDesktopBackend):
    def __init__(self):
        self.screenshot_timeout_seconds = 30
        self.retry_times = 3
        self.retry_interval_seconds = 3
        self.command_timeout_seconds = 300
        self.calls = 0

    def capture_desktop_screenshot(self, all_screens=True):
        self.calls += 1
        time.sleep(10.05)
        return 'data:image/png;base64,aW1hZ2UtZml4dHVyZQ==', DesktopCoordinateFrame(width=1, height=1)


class ScreenshotTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_slow_successful_read_is_retained_by_real_fastmcp_without_replay(self):
        backend = SlowRead()
        server = FastMCP('isolated-data-read')
        _register_desktop_native_data_tools(server, SimpleNamespace(), backend)
        capture = await server.get_tool('capture_desktop_screenshot')
        self.assertEqual(capture.timeout, 101)
        result = await capture.run({'all_screens': False})
        self.assertIn('data:image/png;base64', result.content[0].text)
        self.assertEqual(backend.calls, 1)
        window = await server.get_tool('capture_window_screenshot')
        self.assertEqual(window.timeout, 136)

    async def test_native_defaults_and_other_data_tools_are_unchanged(self):
        server = FastMCP('native-defaults')
        _register_desktop_native_data_tools(server, SimpleNamespace(), SimpleNamespace())
        for contract in desktop_data_contracts():
            tool = await server.get_tool(contract.tool_id)
            self.assertEqual(tool.timeout, 10)
        backend = SlowRead()
        server = FastMCP('osworld-metadata')
        _register_desktop_native_data_tools(server, SimpleNamespace(), backend)
        metadata = await server.get_tool('get_desktop_app_target_info')
        self.assertEqual(metadata.timeout, 10)
