"""Choose one offered GUI action through the planner's multimodal API and key pool."""

import base64
import json
import time

from .ranking import Ranking, build_choice_requests


class PlannerChoicePolicy:
    def __init__(self, client):
        self.client = client

    def rank(self, *, stage_goal, intent, active_window, targets, action_space,
             recent_actions, screenshot=None):
        candidates = action_space.candidates()
        operation, requests = build_choice_requests(
            stage_goal=stage_goal, intent=intent, active_window=active_window, targets=targets,
            action_space=action_space, recent_actions=recent_actions)
        descriptions = {key: text for request in requests.values()
                        for key, text in zip(request["option_ids"], request["options"], strict=True)}
        request = {"state": operation["state"],
                   "question": "Which complete concrete action should be performed next "
                               "across ALL operations?",
                   "option_ids": [c.id for c in candidates],
                   "options": [f"{c.operation}: {descriptions[c.id]}" for c in candidates]}
        content = [{"type": "text", "text": json.dumps(request, ensure_ascii=False)}]
        if screenshot is not None:
            content.append({"type": "image_url", "image_url": {"url": "data:image/png;base64,"
                            + base64.b64encode(screenshot.read_bytes()).decode("ascii")}})
        messages = [{"role": "system", "content":
            "Choose exactly ONE offered action for the current local objective and screenshot. "
            "Honor the delegated goal and preservation conditions before next_action_hint; "
            "a field hint is not permission to discard an unsaved form or skip a navigation prerequisite. "
            "The exit condition is a desired state, NOT an observed fact. Use current evidence; "
            "do not repeat rejected completion or ineffective paths. Return only "
            '{"choice_id":"exact offered option_id"}. Do not invent actions, coordinates or text.'},
            {"role": "user", "content": content}]
        by_id = {c.id: c for c in candidates}

        def validate(value):
            okay = isinstance(value.get("choice_id"), str) and value["choice_id"] in by_id
            if not okay:
                messages.append({"role": "user", "content":
                    "Invalid choice_id. Return one exact offered ID; no action has been executed."})
            return okay

        started = time.perf_counter()
        value = self.client.json_request(messages, validate, role="gui_choice")
        winner = by_id[value["choice_id"]]
        return Ranking(winner=winner,
                       action_ranked=[{"id": winner.id, "operation": winner.operation}],
                       operation_ranked=[], operation_margin=0.0, target_margin=0.0,
                       latency_ms=round((time.perf_counter() - started) * 1000, 2),
                       used_image=screenshot is not None, action_request=request,
                       score_type="api_choice_id")
