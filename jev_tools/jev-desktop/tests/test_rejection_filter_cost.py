import copy
import unittest
from unittest.mock import patch

from jev_desktop import RunOptions, Scope
from jev_desktop.actions import extend_space
from jev_desktop.goal_scope import filter_rejections, state_key
from jev_desktop.policy import action_space
from support import Desktop, node


class RejectionFilterCostTests(unittest.TestCase):
    def observation(self):
        nodes = [node(ref_id='@s1:e'+str(i), name='Item '+str(i),
                      bounds={'x': i*20, 'y': 0, 'width': 15, 'height': 15}) for i in range(30)]
        seen = Desktop(nodes).observe(Scope())
        seen.screen['headed_text'] = True
        space = action_space(seen.nodes, headed=True)
        extend_space(space, seen.screen, RunOptions())
        return seen, space

    def test_keyboard_refusals_do_not_rebuild_state_for_unrelated_drag_pairs(self):
        seen, space = self.observation()
        window = space['by_target']['WINDOW']['node']
        rejected = [state_key(seen, 'PRESS_KEY:tab', window)]
        destinations = copy.deepcopy(space['by_target']['1']['node']['drag_candidates'])
        with patch('jev_desktop.goal_scope.state_key', wraps=state_key) as build:
            filter_rejections(space, seen, rejected)
        self.assertLessEqual(build.call_count, 1)
        self.assertNotIn('PRESS_KEY:tab', space['by_target']['WINDOW']['operations'])
        self.assertEqual(destinations, space['by_target']['1']['node']['drag_candidates'])

    def test_drag_refusal_removes_only_its_exact_destination_and_expires_with_context(self):
        seen, space = self.observation()
        rejected = [state_key(seen, 'DRAG_TO:2', space['by_target']['1']['node'])]
        original = copy.deepcopy(space)
        filter_rejections(space, seen, rejected)
        self.assertNotIn('2', space['by_target']['1']['node']['drag_candidates'])
        self.assertIn('3', space['by_target']['1']['node']['drag_candidates'])
        self.assertEqual(space['by_target']['2'], original['by_target']['2'])
        changed = copy.deepcopy(seen)
        changed.screen['context'].append({'role': 'statictext', 'name': 'New selection scope'})
        filter_rejections(original, changed, rejected)
        self.assertIn('2', original['by_target']['1']['node']['drag_candidates'])
