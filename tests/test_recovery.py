import tempfile
import unittest
from pathlib import Path
from unittest import mock

from journey_pilot import adapter, recovery, runner, session
from journey_pilot.execution import reservation_path
from journey_pilot.store import Store, atomic_write_json, read_json


class Recovery(unittest.TestCase):
    def test_release_no_action_reservation_and_refuse_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp))
            store.create_run('r_idle', {'device_id': 'dev', 'status': 'running'})
            atomic_write_json(reservation_path(store, 'dev'), {'run_id': 'r_idle'})
            with mock.patch.object(adapter, 'harness_read', return_value={'ok': True, 'nodes': []}):
                result = recovery.reconcile(store, {'run_id': 'r_idle', 'release': True})
            self.assertTrue(result.data['released'])
            self.assertFalse(reservation_path(store, 'dev').exists())
            self.assertEqual(read_json(store.summary_path('r_idle'))['status'], 'paused')
            store.append_event('r_idle', {'event': 'action_started', 'step_id': 'unknown'})
            atomic_write_json(reservation_path(store, 'dev'), {'run_id': 'r_idle'})
            with mock.patch.object(adapter, 'harness_read') as probe:
                result = recovery.reconcile(store, {'run_id': 'r_idle', 'release': True})
                probe.assert_not_called()
            self.assertEqual(result.error['code'], 'outcome_unknown')
            self.assertTrue(reservation_path(store, 'dev').exists())

    def test_session_input_and_frozen_condition_digest(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(adapter, 'status', return_value={
                'bound': 'dev', 'devices': [{}], 'ready': True}) as status:
            store = Store(Path(tmp))
            invalid = session.start(store, {'device_id': 'dev', 'intent': 123})
            self.assertEqual(invalid.error['code'], 'invalid_request')
            status.assert_not_called()
            opened = session.start(store, {'device_id': 'dev', 'intent': 'test',
                'frozen_postconditions': {'text_equals': 'private'}})
            result = session.finish(store, {'run_id': opened.run_id, 'lease_token': opened.data['lease_token']})
            self.assertEqual(len(result.data['frozen_postconditions_digest']), 64)
            self.assertNotIn('frozen_postconditions', result.data)

    def test_task_finish_preserves_stop_reason(self):
        from journey_pilot import task_runtime
        from journey_pilot.contracts import failure
        for code in ('cancelled', 'platform_blocked'):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as tmp, \
                 mock.patch.object(adapter, 'status', return_value={'bound': 'dev', 'devices': [{}], 'ready': True}):
                store = Store(Path(tmp))
                def task(ctx):
                    return {'coverage': 'visible_only'}
                with mock.patch.object(session, 'finish', return_value=failure(code, 'stopped')):
                    result = task_runtime.execute(store, {'device_id': 'dev'}, {'id': 'test'}, task)
                self.assertEqual(result.error['code'], code)

    def test_finish_preserves_cancelled_status(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(adapter,'status',return_value={'bound':'dev','devices':[{}],'ready':True}):
            store=Store(Path(tmp))
            opened=session.start(store,{'device_id':'dev','intent':'cancel test'})
            runner.cancel(store,{'run_id':opened.run_id})
            result=session.finish(store,{'run_id':opened.run_id,'lease_token':opened.data['lease_token']})
            self.assertEqual(result.status,'cancelled')
            self.assertEqual(read_json(store.summary_path(opened.run_id))['status'],'cancelled')
