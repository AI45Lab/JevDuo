import copy
from contextlib import contextmanager

from jev_desktop import DesktopError, Observation


def node(**extra):
    return {
        "ref_id": "@s1:e1",
        "role": "button",
        "name": "Apply",
        "available_actions": ["Click"],
        "path": ["window"],
        **extra,
    }


def answer(choice, confidence=1):
    return {"type": "choice", "choice": choice, "confidence": confidence, "probabilities": {choice: 1}}


def success(data=None):
    return {
        "ok": True,
        "data": {"disposition": {"delivery": "delivered_verified", "retry": "unsafe"}, **(data or {})},
    }


def failure(code="ACTION_FAILED", *, retry="unsafe", delivery="delivery_uncertain"):
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": "fixture error",
            "disposition": {"retry": retry, "delivery": delivery},
        },
    }


class Model:
    def __init__(self, *choices, confidence=1):
        self.choices = iter(choices)
        self.requests = []
        self.confidence = confidence

    def ask(self, request):
        self.requests.append(copy.deepcopy(request))
        head = next(iter(request["questions"]))
        choice = next(self.choices)
        if isinstance(choice, dict):
            return choice
        return {"answers": {head: answer(choice, self.confidence)}}


class Desktop:
    def __init__(self, nodes=None):
        self.nodes = [node()] if nodes is None else nodes
        self.actions = []
        self.observations = []
        self.entered = self.cleaned = False
        self.result = {"ok": True, "delivery": "delivered_verified"}
        self.observe_error = None
        self.on_execute = None
        self.catalog = [{"id": "org.example.Editor.desktop", "name": "Editor"}]
        self.launched = []

    @contextmanager
    def run_context(self, goal, cursor):
        self.entered = True
        try:
            yield
        finally:
            self.cleaned = True

    def observe(self, scope):
        self.observations.append(scope)
        if self.observe_error:
            raise DesktopError("read failed", self.observe_error)
        return Observation(
            copy.deepcopy(self.nodes),
            {
                "app": scope.app or "Fixture",
                "window": "Test",
                "window_id": scope.window_id or "w1",
                "surface": "window",
                "root": scope.root,
                "complete": True,
                "context": [{"role": "textfield", "value": "PRIVATE-VALUE"}],
            },
        )

    def execute(self, scope, operation, node, text):
        self.actions.append((scope, operation, node, text))
        if self.on_execute:
            self.on_execute()
        if operation == "DRILL":
            return {"ok": True, "root": node["ref_id"]}
        if operation == "WIDEN":
            return {"ok": True, "root": None}
        return self.result

    def applications(self):
        return self.catalog

    def launch(self, application):
        self.launched.append(application)
        self.observe_error = None
        return self.result

    def wait_for_window(self, launched):
        return {"id": "launched-window", "app_name": "Editor"}


class Transport:
    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler or (lambda args: success())

    def invoke(self, executable, argv, timeout):
        self.calls.append((executable, argv, timeout))
        return self.handler(argv)
