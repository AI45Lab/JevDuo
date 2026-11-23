"""Guest availability failures must remain unscored; genuine zero scores remain zero."""

import json
import sys
import tempfile
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import evaluation_transport
import vm


class EvaluationTransportTest(unittest.TestCase):
    def response(self):
        return Mock(content=b'ID=ubuntu', raise_for_status=Mock(), close=Mock())

    def invoke(self, responses, evaluate):
        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp)/'receipt.jsonl'
            session = Mock()
            session.post.side_effect = responses
            context = Mock(__enter__=Mock(return_value=session), __exit__=Mock(return_value=False))
            try:
                with patch.object(evaluation_transport.requests, 'Session', return_value=context):
                    result = evaluation_transport.evaluate_with_service_check(
                        evaluate, endpoint='http://vm:5000/file', receipt=receipt)
            except BaseException as error:
                result = error
            return result, json.loads(receipt.read_text()), session

    def test_healthy_service_allows_official_zero_without_changing_it(self):
        score = Mock(return_value=0.0)
        result, receipt, session = self.invoke([self.response(), self.response()], score)
        self.assertEqual(result, 0.0)
        self.assertEqual(receipt['status'], 'verified')
        self.assertFalse(session.trust_env)
        self.assertEqual(session.post.call_count, 2)
        self.assertTrue(all(c.kwargs['data']=={'file_path':'/etc/os-release'}
                            for c in session.post.call_args_list))

    def test_before_outage_does_not_invoke_evaluator(self):
        score = Mock(return_value=0.0)
        result, receipt, _ = self.invoke([requests.ConnectionError(), requests.ConnectionError()], score)
        self.assertIsInstance(result, RuntimeError)
        self.assertEqual(receipt['stage'], 'before')
        self.assertEqual(receipt['status'], 'error')
        score.assert_not_called()

    def test_after_outage_keeps_tentative_zero_without_accepting_it(self):
        score = Mock(return_value=0.0)
        result, receipt, _ = self.invoke(
            [self.response(), requests.ConnectionError(), requests.ConnectionError()], score)
        self.assertIsInstance(result, RuntimeError)
        score.assert_called_once()
        self.assertEqual(receipt['stage'], 'after')
        self.assertEqual(receipt['unverified_raw'], 0.0)
        self.assertEqual(receipt['status'], 'error')

    def test_metric_exception_is_preserved(self):
        result, receipt, _ = self.invoke([self.response()], Mock(side_effect=ValueError('metric failure')))
        self.assertIsInstance(result, ValueError)
        self.assertEqual(receipt['stage'], 'evaluate')
        self.assertEqual(receipt['error_type'], 'ValueError')

    def test_vm_reset_honors_disabled_builtin_ax_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = types.ModuleType('desktop_env')
            package.__file__ = str(root/'desktop_env/__init__.py')
            desktop = types.ModuleType('desktop_env.desktop_env')
            desktop.DesktopEnv = Mock()
            desktop.MAX_RETRIES = 3
            with (patch.dict(sys.modules, {'desktop_env':package, 'desktop_env.desktop_env':desktop}),
                  patch.object(vm, 'prepared_setup_transport', return_value=nullcontext()),
                  patch.object(vm, 'prepare_evaluator_dependencies'), patch.object(vm.time, 'sleep'),
                  patch.object(sys, 'path', list(sys.path))):
                instance = vm.Vm(task_root=root, vm_path='image', run_root=root, require_a11y_tree=False)
                instance.reset({'id':'test'})
                self.assertFalse(desktop.DesktopEnv.call_args.kwargs['require_a11y_tree'])
                self.assertEqual(desktop.MAX_RETRIES, 3)
