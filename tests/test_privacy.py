import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from journey_pilot import adapter, session
from journey_pilot.contracts import success
from journey_pilot.execution import request_once
from journey_pilot.store import Store


class RuntimePrivacy(unittest.TestCase):
    def test_live_output_is_returned_but_retry_never_repeats_or_retains_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            operation = mock.Mock(return_value=success('completed', {'outputs': {'body': 'CANARY_SECRET'}}, run_id='r_test'))
            first = request_once(store, 'test', 'one', {'text': 'CANARY_SECRET'}, operation)
            second = request_once(store, 'test', 'one', {'text': 'CANARY_SECRET'}, operation)
            self.assertEqual(first.data['outputs']['body'], 'CANARY_SECRET')
            self.assertNotIn('outputs', second.data)
            self.assertFalse(second.data['content_retained'])
            operation.assert_called_once()
            self.assertNotIn('CANARY_SECRET', ''.join(p.read_text() for p in Path(tmp).rglob('*.json')))

    def test_runtime_events_drop_locator_content_and_failure_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            store.create_run('r_test', {})
            store.append_event('r_test', {'event': 'action_started', 'step_id': 'step',
                'locator': {'text': 'CANARY_SECRET', 'resource_id': 'public:id'},
                'execution': {'fallback': {'alternatives': [{'description': 'CANARY_SECRET'}]}}})
            store.append_event('r_test', {'event': 'action_finished', 'step_id': 'step',
                'outcome': 'assertion_failed', 'detail': 'CANARY_SECRET',
                'unexpected_payload': 'CANARY_SECRET',
                'metrics': {'tree_reads': 1, 'unexpected_payload': 'CANARY_SECRET'}})
            text = store.events_path('r_test').read_text()
            self.assertNotIn('CANARY_SECRET', text)
            self.assertIn('public:id', text)

    def test_invalid_content_key_blocks_without_dispatch_or_regeneration(self):
        for key in ({}, {'key': 123}, {'key': 'abc'}):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                store = Store(Path(tmp))
                path = store.root / 'content-key.json'
                path.write_text(json.dumps(key))
                operation = mock.Mock()
                result = request_once(store, 'test', 'one', {}, operation)
                self.assertEqual(result.error['code'], 'outcome_unknown')
                operation.assert_not_called()
                self.assertEqual(json.loads(path.read_text()), key)

    def test_screenshot_script_cleans_its_file_and_returns_live_image(self):
        import base64
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'capture.png'
            png = b'\x89PNG\r\n\x1a\nCANARY_SECRET'
            path.write_bytes(b'multi-display warning prefix\n' + png)
            output=io.StringIO()
            with redirect_stdout(output):
                exec(adapter.OBSERVE_SCREENSHOT, {'screenshot': lambda: str(path)})
            result=json.loads(output.getvalue())
            self.assertEqual(base64.b64decode(result['image_base64']), png)
            self.assertEqual(result['bytes'], len(png))
            self.assertFalse(path.exists())
