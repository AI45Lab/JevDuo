"""Optional migration check against the preserved JavaScript policy."""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from jev_desktop.policy import (
    action_space,
    build_operation_request,
    build_request,
    launch_request,
    risk_request,
)
from support import answer, node

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(
    shutil.which("node") and (ROOT / "scripts/jev/policy.mjs").exists(), "optional JS parity check"
)
class ParityTests(unittest.TestCase):
    def test_legacy_operation_projection_matches_preserved_javascript(self):
        script = """
import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const p = await import(pathToFileURL(process.argv[1]).href);
const x = JSON.parse(readFileSync(0, 'utf8'));
const space = p.actionSpace(x.nodes, {values:x.values,typable:x.typable,drillable:x.drillable});
const requests = [p.buildRequest(x.goal,x.screen,space,x.history,{values:x.values}),
 p.buildOperationRequest(x.goal,x.screen,space,x.history,{target:x.target,values:x.values}),
 p.riskRequest(x.goal,x.screen,'CLICK',x.nodes[0],{values:x.values})];
for (const r of requests) delete r.model;
console.log(JSON.stringify(requests));
"""
        cases = [
            [node()],
            [
                node(
                    role="textfield",
                    name="試験",
                    value="private text",
                    states=["focused"],
                    available_actions=["SetValue", "Click"],
                ),
                node(role="checkbox", available_actions=["Toggle"]),
            ],
            [node(children_count=30)],
            [node(ref_id=f"@s1:e{i}") for i in range(300)],
        ]
        for nodes in cases:
            for values in (False, True):
                with self.subTest(count=len(nodes), values=values):
                    screen = {
                        "app": "Fixture",
                        "window": "Test",
                        "surface": "window",
                        "root": "@s1:e0",
                        "context": [{"role": "textfield", "value": "private text"}],
                        "complete": False,
                    }
                    target = answer("1", confidence=0)
                    data = {
                        "goal": "make progress",
                        "screen": screen,
                        "nodes": nodes,
                        "values": values,
                        "history": [],
                        "target": target,
                        "typable": True,
                        "drillable": True,
                    }
                    result = subprocess.run(
                        ["node", "--input-type=module", "-e", script, str(ROOT / "scripts/jev/policy.mjs")],
                        input=json.dumps(data),
                        capture_output=True,
                        text=True,
                        check=True,
                        timeout=10,
                    )
                    space = action_space(nodes, values=values)
                    legacy = {
                        "CLICK",
                        "TYPE_TEXT",
                        "CHECK",
                        "UNCHECK",
                        "EXPAND",
                        "COLLAPSE",
                        "SCROLL",
                        "DRILL",
                    }
                    for item in space["by_target"].values():
                        item["operations"] = [op for op in item["operations"] if op in legacy]
                    actual = [
                        build_request(data["goal"], screen, space, [], values=values),
                        build_operation_request(
                            data["goal"], screen, space, [], target=target, values=values
                        ),
                        risk_request(data["goal"], screen, "CLICK", nodes[0], values=values),
                    ]
                    self.assertEqual(actual, json.loads(result.stdout))

    def test_launch_requests_match_original(self):
        script = """
import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const {launchRequest} = await import(pathToFileURL(process.argv[1]).href);
const x = JSON.parse(readFileSync(0,'utf8'));
const result=launchRequest(x.goal,x.apps,x.page);
delete result.model;
console.log(JSON.stringify(result));
"""
        apps = [{"id": f"app-{i}.desktop", "name": f"Application {i}"} for i in range(300)]
        for page in (0, 1):
            result = subprocess.run(
                ["node", "--input-type=module", "-e", script, str(ROOT / "scripts/jev/bootstrap.mjs")],
                input=json.dumps({"goal": "open a document", "apps": apps, "page": page}),
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            self.assertEqual(launch_request("open a document", apps, page), json.loads(result.stdout))
