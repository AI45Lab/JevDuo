"""Optional real-CDP smoke on a synthetic page; not a VM or benchmark acceptance test."""

import http.server
import io
import json
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from functools import partial
from unittest.mock import Mock

import pytest

from jevduo_eval.browser import VM_SCRIPT


def _wait_for_cdp_page(opener, process, active_port, page_url, evaluate):
    """Wait for the intended document, not just a listening socket or cached target title."""
    deadline = time.monotonic() + 30
    pages, last_error, document = [], "", None
    while time.monotonic() < deadline:
        if active_port.exists():
            port = int(active_port.read_text().splitlines()[0])
            try:
                with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
                    pages = json.load(response)
                page = next((p for p in pages if p.get("type") == "page" and p.get("url") == page_url), None)
                if page is not None:
                    document = evaluate(port, page)
                    if document.get("url") == page_url and document.get("ready"):
                        return port, page
            except OSError as error:
                last_error = str(error)
        assert process.poll() is None, "Synthetic Chromium exited before CDP was ready"
        time.sleep(0.05)
    seen = [{key: page.get(key) for key in ("type", "url", "title")} for page in pages]
    pytest.fail(f"Synthetic Chromium CDP page was not ready before the deadline; "
                f"expected_url={page_url!r}; active_port_exists={active_port.exists()}; "
                f"pages={seen!r}; document={document!r}; last_error={last_error!r}")


@pytest.mark.parametrize("error", [TimeoutError("startup timeout"), urllib.error.URLError("starting")])
def test_cdp_readiness_retries_temporary_connection_failure(tmp_path, monkeypatch, error):
    active_port = tmp_path / "DevToolsActivePort"
    active_port.write_text("9222\n")
    page = {"id": "synthetic", "type": "page", "url": "file:///synthetic.html", "title": ""}
    opener = Mock()
    opener.open.side_effect = [error, io.BytesIO(json.dumps([page]).encode())]
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    evaluate = Mock(return_value={"url": page["url"], "ready": True})
    assert _wait_for_cdp_page(opener, process, active_port, page["url"], evaluate) == (9222, page)
    assert opener.open.call_count == 2


def test_cdp_readiness_matches_page_url_not_a_cached_or_duplicate_title(tmp_path):
    active_port = tmp_path / "DevToolsActivePort"
    active_port.write_text("9222\n")
    page = {"id": "synthetic", "type": "page", "url": "file:///synthetic.html", "title": ""}
    unrelated = {**page, "url": "about:blank", "title": "Synthetic target audit"}
    opener = Mock()
    opener.open.return_value = io.BytesIO(json.dumps([unrelated, page]).encode())
    process = Mock()
    process.poll.return_value = None
    evaluate = Mock(return_value={"url": page["url"], "ready": True})
    assert _wait_for_cdp_page(opener, process, active_port, page["url"], evaluate) == (9222, page)
    evaluate.assert_called_once_with(9222, page)


def test_cdp_readiness_waits_for_the_actual_document_to_finish_loading(tmp_path, monkeypatch):
    active_port = tmp_path / "DevToolsActivePort"
    active_port.write_text("9222\n")
    page = {"type": "page", "url": "http://127.0.0.1/controls.html"}
    opener = Mock()
    opener.open.side_effect = [io.BytesIO(json.dumps([page]).encode()) for _ in range(3)]
    evaluate = Mock(side_effect=[{"url": "about:blank", "ready": True},
                                 {"url": page["url"], "ready": False},
                                 {"url": page["url"], "ready": True}])
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    assert _wait_for_cdp_page(opener, process, active_port, page["url"], evaluate) == (9222, page)
    assert evaluate.call_count == 3


def test_cdp_readiness_stops_at_deadline(tmp_path, monkeypatch):
    active_port = tmp_path / "DevToolsActivePort"
    active_port.write_text("9222\n")
    opener = Mock()
    opener.open.side_effect = TimeoutError("still starting")
    process = Mock()
    process.poll.return_value = None
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda seconds: clock.__setitem__(0, 30.0))
    with pytest.raises(pytest.fail.Exception, match="last_error='still starting'"):
        _wait_for_cdp_page(opener, process, active_port, "file:///synthetic.html", Mock())
    assert opener.open.call_count == 1


def test_real_cdp_discovers_click_handlers_without_promoting_plain_text(tmp_path):
    chrome = shutil.which("google-chrome") or shutil.which("chrome") or shutil.which("chromium")
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
    page_file = tmp_path / "controls.html"
    page_file.write_text(html, encoding="utf-8")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(
        http.server.SimpleHTTPRequestHandler, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    page_url = f"http://127.0.0.1:{server.server_port}/controls.html"
    startup_log = tmp_path / "chrome-startup.log"
    with server, startup_log.open("w") as log:
        process = subprocess.Popen([
            chrome, "--headless", "--no-sandbox", "--disable-dev-shm-usage", "--no-first-run",
            "--disable-background-networking", "--disable-component-update", "--remote-debugging-port=0",
            f"--user-data-dir={profile}", page_url,
        ], stdout=subprocess.DEVNULL, stderr=log)
        thread.start()
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            namespace = {"__name__": "cdp_smoke"}
            exec(VM_SCRIPT, namespace)
            expression = namespace["EXPRESSION"]
            namespace["EXPRESSION"] = "({url:location.href, ready:document.readyState === 'complete'})"
            port, page = _wait_for_cdp_page(opener, process, profile / "DevToolsActivePort",
                                          page_url, namespace["evaluate"])
            namespace["EXPRESSION"] = expression
            snapshot = namespace["evaluate"](port, page)
            controls = {e["name"]: e for e in snapshot["elements"]}
            assert set(controls) == {"Metrics", "Inventory", "Save"}, snapshot
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
            server.shutdown()
            thread.join(timeout=5)
            print("Chromium startup log:\n" + startup_log.read_text(errors="replace")[-4000:])
