"""tests/harness 共享夹具：合成 a11y XML、协议 Observation 工厂、mock 组件。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from PIL import Image

from tandem.components import Components
from tandem.gui.candidates import ActionCandidateBuilder
from tandem.gui.compile import gui_action
from tandem.gui.proposals import focused_candidates
from tandem.gui.semantics import enrich_targets
from tandem.gui.targets import A11yTargetBuilder
from tandem.models.ranking import Ranking
from tandem.protocol.models import (
    AccessibilityTree,
    Budget,
    Observation,
    Receipt,
    Screenshot,
)

SHA = "0123456789abcdef" * 4

A11Y_XML = (
    '<application xmlns:cp="https://accessibility.ubuntu.example.org/ns/component"'
    ' name="app" active="true">\n'
    '  <frame name="main-window" active="true">\n'
    '    <cp:panel cp:screencoord="(0,0)" cp:size="(1920,1080)" name="Format"/>\n'
    '    <tree name="file-list" screencoord-style="none"/>\n'
    '    <push_button name="确定" cp:screencoord="(100,200)" cp:size="(80,24)" enabled="true"/>\n'
    '    <push_button cp:screecoord-x="ignored" cp:screencoord="(200,200)" cp:size="(60,24)">\n'
    '      <label name="取消"/>\n'
    "    </push_button>\n"
    '    <textbox cp:screencoord="(300,120)" cp:size="(200,28)" editable="true" focused="true">'
    "当前文本</textbox>\n"
    '    <check_box name="启用" cp:screencoord="(300,160)" cp:size="(120,24)" checked="false"/>\n'
    '    <slider name="缩放" cp:screencoord="(300,200)" cp:size="(140,24)"/>\n'
    '    <icon name="chart.png" cp:screencoord="(400,240)" cp:size="(32,32)"/>\n'
    '    <menu name="File" click_kb="press;&lt;Alt&gt;f:i"/>\n'
    '    <tree name="documents" cp:screencoord="(600,80)" cp:size="(300,600)">\n'
    '      <table_cell name="A1" cp:screencoord="(610,90)" cp:size="(80,24)" editable="true"/>\n'
    "    </tree>\n"
    "  </frame>\n"
    "</application>"
)


def build_targets(xml: str = A11Y_XML):
    return A11yTargetBuilder().build(xml, "snap", 128, infer_button_labels=True, include_sliders=True)


def build_space(targets, context="打开文件"):
    return ActionCandidateBuilder().build(targets, 96, action_inputs=[], context=context)


def make_observation(tmp_path: Path, step_idx: int, *, receipt: Receipt | None = None,
                     tree_xml: str | None = A11Y_XML, steps: int = 50,
                     wall: float = 1800.0, screenshot_sha: str = SHA) -> Observation:
    shot = tmp_path / f"step_{step_idx:02d}.png"
    Image.new("RGB", (64, 64), "white").save(shot)
    a11y_path = None
    if tree_xml is not None:
        a11y_path = str(tmp_path / f"a11y_{step_idx:02d}.xml")
        Path(a11y_path).write_text(tree_xml, encoding="utf-8")
    return Observation(
        task_id="t-001",
        instruction="整理表格并保存",
        step_idx=step_idx,
        budget=Budget(steps_remaining=steps, wall_seconds_remaining=wall),
        screenshot=Screenshot(path=str(shot), sha256=screenshot_sha, width=1920, height=1080),
        accessibility_tree=AccessibilityTree(
            present=tree_xml is not None,
            source="vm" if tree_xml is not None else "none",
            digest="f" * 64 if tree_xml is not None else None,
            node_count=42 if tree_xml is not None else None,
            path=a11y_path,
        ),
        last_receipt=receipt,
    )


def gui_receipt(**overrides) -> Receipt:
    base = {"returncode": 0, "stdout": "", "stderr": "", "kind": "gui", "acknowledged": True,
            "full_output_ref": None}
    base.update(overrides)
    return Receipt(**base)


def cli_receipt(**overrides) -> Receipt:
    base = {"returncode": 0, "stdout": "", "stderr": "", "kind": "cli", "acknowledged": True,
            "full_output_ref": None}
    base.update(overrides)
    return Receipt(**base)


@dataclass
class ScriptedPlanner:
    plans: list[dict]
    calls: list[dict] = field(default_factory=list)

    def __call__(self, instruction, screenshot, targets, recent, tree, verified_focus,
                 requirements, remaining_steps):
        self.calls.append(
            {
                "instruction": instruction,
                "screenshot": str(screenshot),
                "n_targets": len(targets),
                "recent_tail": recent[-1:] if recent else [],
                "verified_focus": verified_focus,
                "remaining_steps": remaining_steps,
            }
        )
        return self.plans.pop(0)


@dataclass
class ScriptedVerifier:
    responses: list[dict]
    calls: list[dict] = field(default_factory=list)

    def __call__(self, instruction, screenshot, **kwargs):
        self.calls.append({"screenshot": str(screenshot), **kwargs})
        response = dict(self.responses.pop(0))
        for key in ("control_state_check",):
            if kwargs.get(key) is not None:
                response.setdefault(key, kwargs[key])
        return response


@dataclass
class FixedSelector:
    pick: str = "first"
    calls: list[dict] = field(default_factory=list)

    def rank(self, *, stage_goal, intent, active_window, targets, action_space, recent_actions,
             screenshot=None):
        self.calls.append({
            "intent": intent,
            "n_candidates": len(action_space.candidates()),
            "operations": [candidate.operation for candidate in action_space.candidates()],
        })
        candidates = action_space.candidates()
        winner = candidates[0] if candidates else None
        return Ranking(
            winner=winner,
            action_ranked=[{"id": winner.id, "operation": winner.operation, "score": 1.0}]
            if winner else [],
            operation_ranked=[{"operation": winner.operation, "score": 1.0}] if winner else [],
            operation_margin=0.0,
            target_margin=0.0,
            latency_ms=0.1,
            used_image=False,
        )


@dataclass
class NullSelector:
    calls: list[dict] = field(default_factory=list)

    def rank(self, **kwargs):
        self.calls.append(kwargs)
        return Ranking(
            winner=None, action_ranked=[], operation_ranked=[], operation_margin=0.0,
            target_margin=0.0, latency_ms=0.1, used_image=False,
        )


def make_components(planner, verifier, selector) -> Components:
    return Components(
        planner=planner,
        verifier=verifier,
        requirements=lambda instruction: [],
        selector=selector,
        target_builder=A11yTargetBuilder(),
        semantic_enricher=enrich_targets,
        candidate_builder=ActionCandidateBuilder(),
        focus_candidates=focused_candidates,
        grounder=None,
        compile_gui=gui_action,
    )


@pytest.fixture
def tmp_tree_file(tmp_path):
    path = tmp_path / "tree.xml"
    path.write_text(A11Y_XML, encoding="utf-8")
    return path


def valid_verifier_response(**overrides) -> dict:
    base = {
        "outcome": "achieved",
        "evidence": "可见变化符合预期",
        "task_status": "incomplete",
        "path_input_focused": False,
        "state_changed": True,
    }
    base.update(overrides)
    return base


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
