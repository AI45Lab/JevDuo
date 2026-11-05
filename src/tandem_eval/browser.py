"""Read-only Chrome DevTools snapshots, captured inside the task VM."""

from __future__ import annotations

import json
from typing import Any

VM_SCRIPT = r'''
import base64
import hashlib
import http.client
import json
import os
import re
import socket
import urllib.request

EXPRESSION = r"""
(() => {
  const norm = value => String(value || '').replace(/\s+/g, ' ').trim();
  const visible = element => {
    if (element.closest('[aria-hidden="true"],[inert]')) return false;
    if (element.checkVisibility) return element.checkVisibility({checkOpacity:true, checkVisibilityCSS:true});
    const style = getComputedStyle(element);
    return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
  };
  const name = (element, seen = new Set()) => {
    if (!element || seen.has(element)) return '';
    seen.add(element);
    const referenced = (element.getAttribute('aria-labelledby') || '').split(/\s+/)
      .map(id => name(document.getElementById(id), seen)).filter(Boolean).join(' ');
    return norm(referenced || element.getAttribute('aria-label') ||
      [...(element.labels || [])].map(label => name(label, seen)).filter(Boolean).join(' ') ||
      (['button','submit','reset'].includes(element.type) ? element.value : '') ||
      element.getAttribute('alt') ||
      (['INPUT','TEXTAREA'].includes(element.tagName) || element.isContentEditable ? '' :
        [...element.childNodes].map(node =>
        node.nodeType === 3 ? node.textContent : node.nodeType === 1 ? name(node, seen) : '').join(' ')) ||
      element.getAttribute('title') || element.getAttribute('placeholder') || element.getAttribute('name'));
  };
  const roles = new Set(['button','link','checkbox','radio','switch','tab','menuitem','option',
    'gridcell','combobox','textbox','searchbox','spinbutton','slider']);
  const selector = 'a[href],button,input,textarea,select,summary,[contenteditable="true"],' +
    [...roles].map(role => '[role="' + role + '"]').join(',');
  const clickable = element => typeof element.onclick === 'function' ||
    element.hasAttribute('onclick') || (typeof getEventListeners === 'function' &&
      (getEventListeners(element).click || []).length > 0);
  const secretAutocomplete = new RegExp(
    '(^|\\s)(current-password|new-password|one-time-code|cc-number|' +
    'cc-csc|cc-exp|cc-exp-month|cc-exp-year)(\\s|$)');
  const roleOf = element => {
    const explicit = element.getAttribute('role');
    if (roles.has(explicit)) return explicit;
    if (element.tagName === 'BUTTON' || element.tagName === 'SUMMARY') return 'button';
    if (element.tagName === 'A') return 'link';
    if (element.tagName === 'SELECT') return 'combobox';
    if (element.tagName === 'TEXTAREA' || element.isContentEditable) return 'textbox';
    if (element.tagName === 'INPUT') {
      if (['checkbox','radio'].includes(element.type)) return element.type;
      if (['button','submit','reset','image'].includes(element.type)) return 'button';
      if (element.type === 'search') return 'searchbox';
      if (element.type === 'number') return 'spinbutton';
      if (['text','email','url','tel','date'].includes(element.type)) return 'textbox';
    }
    return clickable(element) ? 'button' : null;
  };
  const roleNames = {button:'push_button', link:'link', checkbox:'check_box', radio:'radio_button',
    switch:'switch', tab:'page_tab', menuitem:'menu_item', option:'option', gridcell:'table_cell',
    combobox:'combo_box', textbox:'entry', searchbox:'searchbox', spinbutton:'spin_button', slider:'slider'};
  const originX = window.screenX + Math.max(0, (window.outerWidth - window.innerWidth) / 2);
  const originY = window.screenY + Math.max(0, window.outerHeight - window.innerHeight);
  const textCandidates = [];
  for (const element of document.querySelectorAll('label,th,td,legend,p,span,h1,h2,h3,h4,h5,h6')) {
    if (element.querySelector('input,textarea,select,button') || !visible(element)) continue;
    const text = norm(element.innerText);
    const rect = element.getBoundingClientRect();
    if (text && text.length <= 100 && rect.width > 0 && rect.height > 0) {
      textCandidates.push({text, left:rect.left, right:rect.right, top:rect.top, bottom:rect.bottom,
        cx:rect.left + rect.width / 2, cy:rect.top + rect.height / 2});
    }
    if (textCandidates.length >= 600) break;
  }
  const hintFor = rect => {
    const cx = rect.left + rect.width / 2, cy = rect.top + rect.height / 2;
    let best = '', score = Infinity;
    for (const item of textCandidates) {
      const left = item.right <= rect.left + 4 && Math.abs(item.cy - cy) <= Math.max(14, rect.height);
      const above = item.bottom <= rect.top + 4 && item.right >= rect.left && item.left <= rect.right;
      if (!left && !above) continue;
      const candidate = left ? rect.left - item.right + Math.abs(item.cy - cy) :
        1000 + rect.top - item.bottom + Math.abs(item.cx - cx) / 4;
      if (candidate < score) { best = item.text; score = candidate; }
    }
    return best;
  };
  const contextText = element => {
    if (!element) return '';
    const clone = element.cloneNode(true);
    clone.querySelectorAll('input,textarea,select,[contenteditable="true"]').forEach(node => node.remove());
    return norm(clone.innerText).slice(0, 240);
  };
  const itemContext = element => {
    const row = element.closest('li,tr,[role="row"]');
    if (row) return contextText(row);
    for (let node = element.parentElement, depth = 0; node && depth < 4;
         node = node.parentElement, depth++) {
      if (node.matches('body,html,form,dialog,[role="dialog"]')) break;
      const text = contextText(node);
      if (text.length > name(element).length + 8 && text.length < 240 &&
          node.querySelectorAll(selector).length <= 8) return text;
    }
    return contextText(element.closest('fieldset,form,dialog,[role="dialog"]'));
  };
  const elements = [];
  let rawCount = 0;
  for (const element of document.querySelectorAll(selector + ',div,span,li,td')) {
    rawCount++;
    if (!element.matches(selector)) {
      if (!clickable(element)) continue;
      const parent = element.parentElement;
      if (parent && (parent.closest(selector) || clickable(parent))) continue;
    }
    if (!visible(element) || element.disabled || element.getAttribute('aria-disabled') === 'true') continue;
    if (element.type === 'password' || element.type === 'hidden' || element.type === 'file' ||
        secretAutocomplete.test(element.getAttribute('autocomplete') || '')) continue;
    const rect = element.getBoundingClientRect();
    const cx = rect.left + rect.width / 2, cy = rect.top + rect.height / 2;
    if (rect.width < 2 || rect.height < 2 || cx < 0 || cy < 0 ||
        cx >= innerWidth || cy >= innerHeight) continue;
    const hit = document.elementFromPoint(cx, cy);
    if (!hit || !element.contains(hit)) continue;  // A visible background node may still be occluded.
    const domRole = roleOf(element);
    if (!domRole) continue;
    const hint = hintFor(rect);
    const label = name(element) || hint || domRole;
    const editable = !element.readOnly && element.getAttribute('aria-readonly') !== 'true' &&
      (['textbox','searchbox','spinbutton'].includes(domRole) ||
       (domRole === 'combobox' && ['INPUT','TEXTAREA'].includes(element.tagName)));
    const navigation = element.closest('nav,[role="navigation"]');
    const item = {
      index: elements.length + 1,
      role: roleNames[domRole] || domRole,
      name: label,
      description: norm(element.getAttribute('aria-description') || element.getAttribute('title') ||
        element.getAttribute('placeholder')),
      value: element.tagName === 'SELECT' ? norm(element.selectedOptions[0]?.label) :
        'value' in element ? String(element.value).slice(0, 200) : '',
      input_type: ['INPUT','SELECT'].includes(element.tagName) ? element.type : '',
      focused: document.activeElement === element,
      date_order: element.type === 'date' ? new Intl.DateTimeFormat(navigator.language)
        .formatToParts(new Date(2001,10,22)).filter(p => ['year','month','day'].includes(p.type))
        .map(p => p.type) : [],
      x: originX + rect.left, y: originY + rect.top, width: rect.width, height: rect.height,
      scope: 'web_content', label_source: name(element) ? 'dom' : 'dom_nearby',
      parent_context: navigation ? ['navigation', name(navigation) || 'Navigation'] : [],
      nearby_text: itemContext(element),
      operations: editable ? ['click','type_text'] : ['click'],
      source: 'dom'
    };
    for (const state of ['checked','selected','expanded']) {
      const value = element.getAttribute('aria-' + state);
      if (value !== null) item[state] = value === 'true';
    }
    if (['checkbox','radio'].includes(element.type)) item.checked = Boolean(element.checked);
    elements.push(item);
    if (elements.length >= 250) break;
  }
  return {url:location.href, title:document.title, visibility:document.visibilityState,
    focused:document.hasFocus(), elements,
    omitted:Math.max(0, rawCount - elements.length),
    viewport:{width:innerWidth,height:innerHeight,screen_x:originX,screen_y:originY,dpr:devicePixelRatio}};
})()
"""


def read_exact(sock, size):
    chunks = []
    while size:
        chunk = sock.recv(size)
        if not chunk:
            raise OSError('CDP websocket closed')
        chunks.append(chunk)
        size -= len(chunk)
    return b''.join(chunks)


def send_frame(sock, payload, opcode=1):
    data = payload.encode() if isinstance(payload, str) else payload
    mask = os.urandom(4)
    size = len(data)
    header = bytes([0x80 | opcode, 0x80 | (size if size < 126 else 126 if size < 65536 else 127)])
    if size >= 65536:
        header += size.to_bytes(8, 'big')
    elif size >= 126:
        header += size.to_bytes(2, 'big')
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(data))
    sock.sendall(header + mask + masked)


def recv_frame(sock):
    first, second = read_exact(sock, 2)
    size = second & 127
    if size == 126:
        size = int.from_bytes(read_exact(sock, 2), 'big')
    elif size == 127:
        size = int.from_bytes(read_exact(sock, 8), 'big')
    mask = read_exact(sock, 4) if second & 128 else None
    payload = read_exact(sock, size)
    if mask:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return bool(first & 128), first & 15, payload


def evaluate(port, page):
    ws = page['webSocketDebuggerUrl']
    assert ws.startswith('ws://')
    rest = ws[5:]
    hostport, path = rest.split('/', 1)
    host, port_text = hostport.rsplit(':', 1)
    sock = socket.create_connection((host, int(port_text)), timeout=5)
    sock.settimeout(5)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        request = ('GET /' + path + ' HTTP/1.1\r\nHost: ' + hostport +
                   '\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: ' + key +
                   '\r\nSec-WebSocket-Version: 13\r\n\r\n')
        sock.sendall(request.encode())
        response = b''
        while b'\r\n\r\n' not in response:
            response += sock.recv(4096)
        if b' 101 ' not in response.split(b'\r\n', 1)[0]:
            raise OSError('CDP websocket handshake failed')
        send_frame(sock, json.dumps({'id':1, 'method':'Runtime.evaluate',
          'params':{'expression':EXPRESSION, 'returnByValue':True, 'awaitPromise':True,
                    'includeCommandLineAPI':True}}))
        chunks = None
        for _ in range(100):
            finished, opcode, payload = recv_frame(sock)
            if opcode == 9:
                send_frame(sock, payload, 10)
                continue
            if opcode == 1:
                chunks = [payload]
            elif opcode == 0 and chunks is not None:
                chunks.append(payload)
            else:
                continue
            if not finished:
                continue
            message = json.loads(b''.join(chunks))
            chunks = None
            if message.get('id') != 1:
                continue
            if 'error' in message or 'exceptionDetails' in message.get('result', {}):
                raise OSError('CDP Runtime.evaluate failed')
            return message['result']['result']['value']
        raise OSError('CDP response limit reached')
    finally:
        sock.close()


def foreground_debug_port():
    from Xlib.display import Display
    display = Display()
    try:
        active = display.screen().root.get_full_property(display.intern_atom('_NET_ACTIVE_WINDOW'), 0)
        if active is None or not active.value[0]:
            raise OSError('No foreground X11 window')
        window = display.create_resource_object('window', int(active.value[0]))
        process = window.get_full_property(display.intern_atom('_NET_WM_PID'), 0)
        if process is None:
            raise OSError('Foreground window has no process identity')
        with open(f'/proc/{int(process.value[0])}/cmdline', 'rb') as stream:
            command = stream.read().decode('utf-8')
        # Chromium can rewrite argv into one space-separated process title.
        match = re.search(r'(?:^|[\s\x00])--remote-debugging-port(?:=|[\s\x00]+)([0-9]+)(?=$|[\s\x00])',
                          command)
        if match:
            return int(match.group(1))
        raise OSError('Foreground browser does not expose a debugging port')
    finally:
        display.close()


def main():
    errors = []
    try:
        port = foreground_debug_port()
    except Exception as error:
        print(json.dumps({'error':f'foreground_cdp_unavailable:{type(error).__name__}:{str(error)[:160]}'}))
        return
    for port in (port,):
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f'http://127.0.0.1:{port}/json/list', timeout=3) as response:
                pages = json.load(response)
            candidates = [item for item in pages if item.get('type') == 'page']
            snapshot = None
            for page in candidates:
                candidate = evaluate(port, page)
                if candidate.get('visibility') == 'visible' and candidate.get('focused'):
                    snapshot = candidate
                    break
            if snapshot is None:
                raise OSError('no focused CDP page target')
            print(json.dumps({'snapshot':snapshot, 'port':port}, ensure_ascii=False))
            break
        except Exception as error:
            errors.append(f'{port}:{type(error).__name__}:{str(error)[:160]}')
    else:
        print(json.dumps({'error':' | '.join(errors)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
'''


def capture_browser_snapshot(controller) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Capture the active page without changing it; failures degrade to a11y-only perception."""
    try:
        result = controller.execute_python_command(VM_SCRIPT) or {}
        payload = json.loads(result.get("output") or "{}")
    except (TypeError, ValueError, AttributeError) as error:
        return None, {"available": False, "error": f"{type(error).__name__}: {error}"[:240]}
    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("elements"), list):
        return None, {"available": False, "error": str(payload.get("error") or "invalid CDP response")[:240]}
    return snapshot, {
        "available": True,
        "port": payload.get("port"),
        "url": snapshot.get("url"),
        "element_count": len(snapshot["elements"]),
        "omitted": snapshot.get("omitted", 0),
    }
