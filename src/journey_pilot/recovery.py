"""Explicit reconciliation of an idle reservation; never replays an action."""
from . import adapter
from .contracts import failure, success
from .execution import device_lock, reservation_path
from .store import atomic_write_json, read_json


def reconcile(store, request):
    run_id = request.get('run_id')
    if not run_id:
        return failure('invalid_request', 'reconcile needs run_id')
    summary = read_json(store.summary_path(run_id))
    if not summary or not summary.get('device_id'):
        return failure('invalid_request', 'unknown run or device')
    serial = summary['device_id']
    if request.get('device_id') not in (None, serial):
        return failure('precondition_mismatch', 'cannot change the device')
    with device_lock(store, serial, owner=run_id) as busy:
        if busy:
            return failure('device_busy', 'active device call must finish first', run_id=run_id)
        lease = read_json(store.locks / f'{run_id}.json')
        if lease:
            import time
            if lease.get('expires_at', 0) > time.time() and request.get('lease_token') != lease.get('token'):
                return failure('device_busy', 'active exploration needs its lease token', run_id=run_id)
        unknown = store.open_actions(run_id)
        if unknown:
            # A fresh UI cannot prove an orphan subprocess stopped or an action
            # did not occur. Keep occupancy until that proof can be supplied.
            return failure('outcome_unknown', 'unresolved actions require operator investigation; reservation retained',
                data={'unknown_steps': [e['step_id'] for e in unknown], 'released': False,
                      'continuation_run_id': summary.get('resumed_by')}, run_id=run_id)
        if request.get('release') is not True:
            return success('ok', {'released': False, 'can_release': True,
                'continuation_run_id': summary.get('resumed_by')}, run_id=run_id)
        observed = adapter.harness_read(adapter.OBSERVE_TREE, serial=serial)
        if not observed.get('ok'):
            return failure('precondition_mismatch', 'fresh scene unavailable; occupancy retained', run_id=run_id)
        store.append_event(run_id, {'event': 'reservation_reconciled', 'action': 'release',
                                   'unknown_steps': [], 'observation_count': len(observed.get('nodes') or [])})
        # Releasing occupancy does not change the historical task outcome.
        summary.update(reconciliation='released_without_replay')
        if summary.get('status') == 'running':
            summary['status'] = 'paused'
        atomic_write_json(store.summary_path(run_id), summary)
        path = reservation_path(store, serial)
        if (read_json(path) or {}).get('run_id') == run_id:
            path.unlink(missing_ok=True)
        (store.locks / f'{run_id}.json').unlink(missing_ok=True)
        return success('recorded', {'released': True, 'replayed': False}, run_id=run_id)
