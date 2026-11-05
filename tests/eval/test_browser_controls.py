"""Optional real-CDP smoke on a synthetic page; not a VM or benchmark acceptance test."""

import io
import json
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import Mock

import pytest

from tandem_eval.browser import VM_SCRIPT


def _wait_for_cdp_page(opener, process, active_port):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if active_port.exists():
            port = int(active_port.read_text().splitlines()[0])
            try:
                with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
                    pages = json.load(response)
            except (TimeoutError, urllib.error.URLError):
                pages = []
            page = next((p for p in pages if p.get("title") == "Synthetic target audit"), None)
            if page is not None:
                return port, page
        assert process.poll() is None, "Synthetic Chromium exited before CDP was ready"
        time.sleep(0.05)
    pytest.fail("Synthetic Chromium CDP page was not ready before the deadline")


@pytest.mark.parametrize("error", [TimeoutError("startup timeout"), urllib.error.URLError("starting")])
def test_cdp_readiness_retries_temporary_connection_failure(tmp_path, monkeypatch, error):
    active_port = tmp_path / "DevToolsActivePort"
    active_port.write_text("9222\n")
    page = {"id": "synthetic", "title": "Synthetic target audit"}
    opener = Mock()
    opener.open.side_effect = [error, io.BytesIO(json.dumps([page]).encode())]
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    assert _wait_for_cdp_page(opener, process, active_port) == (9222, page)
    assert opener.open.call_count == 2


def test_cdp_readiness_stops_at_deadline(tmp_path, monkeypatch):
    active_port = tmp_path / "DevToolsActivePort"
    active_port.write_text("9222\n")
    opener = Mock()
    opener.open.side_effect = TimeoutError("still starting")
    process = Mock()
    process.poll.return_value = None
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda seconds: clock.__setitem__(0, 15.0))
    with pytest.raises(pytest.fail.Exception, match="before the deadline"):
        _wait_for_cdp_page(opener, process, active_port)
    assert opener.open.call_count == 1


def test_real_cdp_discovers_click_handlers_without_promoting_plain_text(tmp_path):
    chrome = shutil.which("chromium") or shutil.which("chrome")
    if chrome is None:
        pytest.skip("Optional Chromium executable is unavailable")
    html = '''<!doctype html><title>Synthetic target audit</title>
      <style>div {width:180px;height:32px;margin:8px} td,p {cursor:pointer}</style>
      <nav aria-label="Workspace">
        <div onclick="document.title='clicked'">Metrics</div>
        <div id="registered">Inventory</div>
      </nav>
      <p>/analytics</p><table><tr><td>/analytics</td><td>/inventory</td></tr></table>
      <div tabindex="0" style="cursor:pointer">Focusable ordinary text</div>
      <button>Save</button>
      <script>document.getElementById('registered').addEventListener('click',
        () => { document.title='clicked'; });</script>'''
    profile = tmp_path / "chrome-profile"
    process = subprocess.Popen([
        chrome, "--headless", "--no-sandbox", "--disable-dev-shm-usage", "--no-first-run",
        "--disable-background-networking", "--disable-component-update", "--remote-debugging-port=0",
        f"--user-data-dir={profile}", "data:text/html," + urllib.parse.quote(html),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        port, page = _wait_for_cdp_page(opener, process, profile / "DevToolsActivePort")
        namespace = {"__name__": "cdp_smoke"}
        exec(VM_SCRIPT, namespace)
        snapshot = namespace["evaluate"](port, page)
        controls = {e["name"]: e for e in snapshot["elements"]}
        assert set(controls) == {"Metrics", "Inventory", "Save"}
        assert controls["Inventory"]["parent_context"] == ["navigation", "Workspace"]
        assert controls["Metrics"]["role"] == "push_button"
        assert snapshot["title"] == "Synthetic target audit"  # Discovery never invokes handlers.
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
