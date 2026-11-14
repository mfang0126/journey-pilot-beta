import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from journey_pilot import adapter, cli
from journey_pilot.store import Store
from journey_pilot.trace import summarize_run


class CallMetrics(unittest.TestCase):
    def test_cli_counts_readiness_and_observation_without_content(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict('os.environ', {'JOURNEY_PILOT_HOME': tmp}), \
             mock.patch.object(adapter, 'list_devices', return_value=[{'serial': 'dev', 'state': 'device', 'model': 'test'}]), \
             mock.patch.object(adapter, '_screen', return_value={}), \
             mock.patch.object(adapter, '_harness_read', return_value={'ok': True, 'nodes': []}):
            output = io.StringIO()
            with mock.patch('sys.stdin', io.StringIO(json.dumps({'device_id': 'dev', 'intent': 'CANARY_SECRET',
                    'frozen_postconditions': {'text_equals': 'CANARY_SECRET'}}))):
                self.assertEqual(cli.main(['session', 'start', '--request-stdin'], stdout=output), 0)
            opened = json.loads(output.getvalue())
            run_id = opened['run_id']
            self.assertEqual(opened['data']['harness_calls']['calls'], 1)
            request = {'run_id': run_id, 'lease_token': opened['data']['lease_token']}
            with mock.patch('sys.stdin', io.StringIO(json.dumps(request))):
                self.assertEqual(cli.main(['observe', '--request-stdin'], stdout=io.StringIO()), 0)
            summary = summarize_run(Store(Path(tmp)), run_id)
            # readiness + tree read + OCR: an empty tree is unusable, so observe adds OCR.
            self.assertEqual(summary['total_harness_calls'], 3)
            self.assertNotIn('CANARY_SECRET', ''.join(p.read_text() for p in Path(tmp).rglob('*.json*')))
            self.assertIsNone(adapter.call_metrics.get())
