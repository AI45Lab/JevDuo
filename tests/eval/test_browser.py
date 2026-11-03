"""CDP response handling stays read-only and degrades without breaking observation."""

import json

from tandem_eval.browser import VM_SCRIPT, capture_browser_snapshot


class Controller:
    def __init__(self, payload):
        self.payload = payload
        self.commands = []

    def execute_python_command(self, command):
        self.commands.append(command)
        return {"returncode": 0, "output": json.dumps(self.payload)}


def test_cdp_snapshot_response_and_public_diagnostic():
    snapshot = {"url": "https://example.test", "elements": [{"name": "Save"}], "omitted": 0}
    controller = Controller({"snapshot": snapshot, "port": 1337})
    value, diagnostic = capture_browser_snapshot(controller)
    assert value == snapshot
    assert diagnostic == {
        "available": True, "port": 1337, "url": "https://example.test",
        "element_count": 1, "omitted": 0,
    }
    assert controller.commands == [VM_SCRIPT]


def test_cdp_failure_returns_a11y_fallback_diagnostic():
    value, diagnostic = capture_browser_snapshot(Controller({"error": "connection refused"}))
    assert value is None
    assert diagnostic["available"] is False and "connection refused" in diagnostic["error"]


def test_readonly_javascript_collects_clickable_cards_but_not_their_children_or_occluded_controls():
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if node is None:
        pytest.skip("Optional Node runtime is unavailable")
    namespace = {"__name__": "cdp_test"}
    exec(VM_SCRIPT, namespace)
    setup = r'''
    const body = {tabIndex:-1, onclick:null, cursor:'auto', closest:()=>null,
                  matches:s=>s.includes('body')};
    const make = (text, x, parent=body, native=false) => ({
      tagName:native?'BUTTON':'DIV', type:'', tabIndex:-1, onclick:native?null:()=>{},
      cursor:native?'auto':'pointer', parentElement:parent, childNodes:[{nodeType:3,textContent:text}],
      labels:[], innerText:text, disabled:false, checkVisibility:()=>true,
      getAttribute:()=>null, matches:s=>native&&s.includes('button'), closest:()=>null,
      querySelector:()=>null, querySelectorAll:()=>[],
      getBoundingClientRect:()=>({left:x,top:20,right:x+100,bottom:60,width:100,height:40}),
      contains(other){return other===this||other.parentElement===this;},
      cloneNode(){return {innerText:text,querySelectorAll:()=>[]};}
    });
    const card=make('Afternoon class',20);
    const child=make('Child label',20,card);
    const hidden=make('Covered button',200,body,true);
    const cover=make('Overlay',200);
    global.getComputedStyle=e=>({cursor:e.cursor,display:'block',visibility:'visible',opacity:'1'});
    global.innerWidth=800;global.innerHeight=600;
    global.window={screenX:0,screenY:0,outerWidth:800,innerWidth:800,outerHeight:600,innerHeight:600};
    global.location={href:'https://example.test/'};global.devicePixelRatio=1;
    global.document={title:'Synthetic cards',visibilityState:'visible',activeElement:null,
      getElementById:()=>null,
      querySelectorAll:s=>s.startsWith('a[href]')?[card,child,hidden]:[],
      elementFromPoint:x=>x<150?child:cover};
    '''
    run = subprocess.run([node, "-e", setup + "\nconsole.log(JSON.stringify(" + namespace["EXPRESSION"]
                          + "));"], capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stderr
    assert [e["name"] for e in json.loads(run.stdout)["elements"]] == ["Afternoon class"]


def test_vm_websocket_frame_parser_executes_with_stdlib_only():
    namespace = {"__name__": "cdp_test"}
    exec(VM_SCRIPT, namespace)

    class Socket:
        def __init__(self):
            self.data = b"\x81\x02{}"

        def recv(self, size):
            value, self.data = self.data[:size], self.data[size:]
            return value

    assert namespace["recv_frame"](Socket()) == (True, 1, b"{}")


def test_vm_evaluate_reads_fragmented_cdp_response():
    namespace = {"__name__": "cdp_test"}
    exec(VM_SCRIPT, namespace)

    class Socket:
        def settimeout(self, timeout):
            pass

        def sendall(self, data):
            pass

        def recv(self, size):
            return b"HTTP/1.1 101 Switching Protocols\r\n\r\n"

        def close(self):
            pass

    socket = Socket()
    namespace["socket"].create_connection = lambda address, timeout: socket
    namespace["send_frame"] = lambda *args: None
    frames = iter([
        (False, 1, b'{"id":1,"result":'),
        (True, 0, b'{"result":{"value":{"elements":[]}}}}'),
    ])
    namespace["recv_frame"] = lambda sock: next(frames)
    page = {"webSocketDebuggerUrl": "ws://127.0.0.1:1337/devtools/page/1"}
    assert namespace["evaluate"](1337, page) == {"elements": []}
