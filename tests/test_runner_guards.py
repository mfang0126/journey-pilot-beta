"""The four guards that decide whether a result can be trusted.

Each test here pins a bug that was found by review, not a style preference:

1. no frozen postconditions -> recorded, never completed, never promoted;
2. verification is per environment, so another device is still candidate;
3. one writer per device;
4. the same request_id never executes twice.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from journey_pilot import adapter, runner
from journey_pilot.journey import propose
from journey_pilot.store import Store

NODES = [{"text": "我的设备", "desc": "", "id": "a", "class": "V",
          "clickable": True, "x": 0, "y": 0, "w": 10, "h": 10},
         {"text": "WLAN", "desc": "", "id": "b", "class": "V",
          "clickable": True, "x": 0, "y": 0, "w": 10, "h": 10}]


def steps(with_post: bool):
    step = {"step_id": "open_wlan", "action": "tap", "locator": {"text": "WLAN"},
            "effect_class": "navigation", "settle_ms": 10}
    if with_post:
        step["postcondition"] = {"element_exists": {"text": "WLAN"}}
    return [step]


class Guards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "jp")
        self.executions = 0
        patches = [
            mock.patch.object(adapter, "status",
                              lambda device_id=None: {"bound": device_id or "dev1",
                                                      "model": "m1", "ready": True, "foreground": "com.android.settings/.Main",
                                                      "devices": [{"serial": "dev1"}]}),
            mock.patch.object(adapter, "harness_read",
                              lambda script, serial=None, timeout=60: {"ok": True, "nodes": NODES}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        def fake_action(action, serial=None, timeout=90):
            self.executions += 1
            return {"ok": True, "action": action.get("type"), "value": "WLAN"}

        patcher = mock.patch.object(adapter, "run_action", fake_action)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _journey(self, with_post: bool, task="t") -> str:
        out = propose(self.store, {
            "task_key": task, "intent": "test",
            "steps": steps(with_post),
            "environment_key": {"platform": "android", "app": "com.android.settings"},
            "postconditions": {"element_exists": {"text": "WLAN"}} if with_post else {},
        })
        return out.data["journey_id"]

    # 1 -----------------------------------------------------------------
    def test_no_postconditions_can_never_complete_or_promote(self):
        jid = self._journey(with_post=False)
        for i in range(3):
            out = runner.validate(self.store, {"journey_id": jid, "request_id": f"r{i}"})
            self.assertEqual(out.status, "recorded")
            self.assertEqual(out.data["promotion"]["consecutive"], 0)
            self.assertEqual(out.data["state"], "candidate")

    def test_with_postconditions_promotes_after_three(self):
        jid = self._journey(with_post=True)
        last = None
        for i in range(3):
            last = runner.validate(self.store, {"journey_id": jid, "request_id": f"p{i}"})
            self.assertEqual(last.status, "completed")
        self.assertEqual(last.data["state"], "verified")

    # 2 -----------------------------------------------------------------
    def test_verification_does_not_leak_to_another_device(self):
        jid = self._journey(with_post=True)
        for i in range(3):
            runner.validate(self.store, {"journey_id": jid, "request_id": f"e{i}"})
        other = runner.run(self.store, {"journey_id": jid, "request_id": "other-dev",
                                        "device_id": "dev2"})
        self.assertEqual(other.status, "blocked")
        self.assertEqual(other.error["code"], "precondition_mismatch")
        self.assertEqual(self.executions, 3, "the other device must not execute")

    def test_request_cannot_forge_verified_device(self):
        jid = self._journey(with_post=True)
        for i in range(3):
            runner.validate(self.store, {"journey_id": jid, "request_id": f"forge-{i}"})
        out = runner.run(self.store, {"journey_id": jid, "request_id": "forged",
                                     "device_id": "dev2", "environment_key": {"device": "dev1"}})
        self.assertEqual(out.error["code"], "version_mismatch")
        self.assertEqual(self.executions, 3)

    def test_unready_and_wrong_entry_never_act(self):
        jid = self._journey(with_post=True)
        with mock.patch.object(adapter, "status", return_value={"bound": "dev1", "devices": [{}], "ready": False}):
            out = runner.validate(self.store, {"journey_id": jid, "request_id": "locked"})
            self.assertEqual(out.error["code"], "locked")
        from journey_pilot.journey import load_version
        doc = load_version(self.store, jid, 1)
        summary, _ = runner._execute(self.store, {**doc, "entry_state": {"text_equals": "wrong screen"}}, "dev1", "validate")
        self.assertEqual(summary["failure"]["code"], "precondition_mismatch")
        self.assertEqual(self.executions, 0)

    def test_environment_is_rechecked_under_lock_before_any_action(self):
        jid = self._journey(with_post=True)
        ready = {"bound": "dev1", "devices": [{}], "ready": True, "foreground": "com.android.settings/.Main"}
        with mock.patch.object(adapter, "status", side_effect=[ready, {**ready, "ready": False}]):
            result = runner.validate(self.store, {"journey_id": jid, "request_id": "lock-race"})
        self.assertEqual(result.error["code"], "locked")
        self.assertEqual(self.executions, 0)

    def test_explicit_device_needs_only_one_locked_environment_probe(self):
        jid = self._journey(with_post=True)
        def locked_status(device_id=None):
            with runner._device_lock(self.store, "dev1") as busy:
                self.assertEqual(busy, "dev1")
            return {"bound": "dev1", "devices": [{}], "ready": True, "foreground": "com.android.settings/.Main"}
        with mock.patch.object(adapter, "status", side_effect=locked_status) as status:
            result = runner.validate(self.store, {"journey_id": jid, "device_id": "dev1", "request_id": "one-probe"})
        self.assertEqual(result.status, "completed")
        self.assertEqual(status.call_count, 1)

    def test_validation_record_is_written_while_device_remains_locked(self):
        jid = self._journey(with_post=True)
        record = runner._record_validation
        def assert_lock(store, row):
            with runner._device_lock(store, "dev1") as busy:
                self.assertEqual(busy, "dev1")
            record(store, row)
        with mock.patch.object(runner, "_record_validation", side_effect=assert_lock):
            result = runner.validate(self.store, {"journey_id": jid, "request_id": "record-lock"})
        self.assertEqual(result.status, "completed")

    def test_logical_failure_suspends_verified_version(self):
        jid = self._journey(with_post=True)
        for i in range(3):
            runner.validate(self.store, {"journey_id": jid, "request_id": f"suspend-{i}"})
        with mock.patch.object(adapter, "run_action", return_value={"ok": False, "code": "locator_missing"}):
            failed = runner.run(self.store, {"journey_id": jid, "request_id": "failure"})
        self.assertEqual(failed.error["code"], "locator_missing")
        refused = runner.run(self.store, {"journey_id": jid, "request_id": "after-failure"})
        self.assertEqual(refused.error["code"], "precondition_mismatch")
        self.assertEqual(refused.data["state"], "suspended")
        self.assertEqual(self.executions, 3)

    # 3 -----------------------------------------------------------------
    def test_one_writer_per_device(self):
        jid = self._journey(with_post=True)
        for i in range(3):
            runner.validate(self.store, {"journey_id": jid, "request_id": f"d{i}"})
        lock = self.store.locks / "device-dev1.json"
        lock.write_text(json.dumps({"token": "other", "expires_at": time.time() + 600}))
        out = runner.run(self.store, {"journey_id": jid, "request_id": "busy"})
        self.assertEqual(out.status, "blocked")
        self.assertEqual(out.error["code"], "device_busy")
        self.assertEqual(self.executions, 3)

    def test_stale_lock_is_taken_over(self):
        jid = self._journey(with_post=True)
        for i in range(3):
            runner.validate(self.store, {"journey_id": jid, "request_id": f"s{i}"})
        lock = self.store.locks / "device-dev1.json"
        lock.write_text(json.dumps({"token": "gone", "expires_at": time.time() - 1}))
        out = runner.run(self.store, {"journey_id": jid, "request_id": "stale"})
        self.assertEqual(out.status, "completed")
        self.assertFalse(lock.exists(), "the lock is released after the run")

    # 4 -----------------------------------------------------------------
    def test_same_request_id_runs_once(self):
        jid = self._journey(with_post=True)
        for i in range(3):
            runner.validate(self.store, {"journey_id": jid, "request_id": f"q{i}"})
        before = self.executions
        first = runner.run(self.store, {"journey_id": jid, "request_id": "same"})
        second = runner.run(self.store, {"journey_id": jid, "request_id": "same"})
        self.assertEqual(first.run_id, second.run_id)
        self.assertEqual(self.executions, before + 1, "the second call must not execute")

    def test_unknown_journey_action_blocks_new_request_and_session(self):
        from journey_pilot import session
        jid = self._journey(with_post=True)
        with mock.patch.object(adapter, "run_action", return_value={"ok": False, "code": "action_failed"}) as action:
            first = runner.validate(self.store, {"journey_id": jid, "device_id": "dev1", "request_id": "unknown-first"})
            second = runner.validate(self.store, {"journey_id": jid, "device_id": "dev1", "request_id": "unknown-other"})
            opened = session.start(self.store, {"device_id": "dev1", "intent": "another task"})
        self.assertEqual(first.error["code"], "outcome_unknown")
        self.assertEqual(second.error["code"], "device_busy")
        self.assertEqual(opened.error["code"], "device_busy")
        self.assertEqual(action.call_count, 1)

    def test_same_request_id_different_payload_is_a_conflict(self):
        jid = self._journey(with_post=True)
        for i in range(3):
            runner.validate(self.store, {"journey_id": jid, "request_id": f"c{i}"})
        runner.run(self.store, {"journey_id": jid, "request_id": "dup"})
        clash = runner.run(self.store, {"journey_id": jid, "request_id": "dup",
                                        "version": 1, "parameters": {"x": 1}})
        self.assertEqual(clash.status, "failed")
        self.assertEqual(clash.error["code"], "request_conflict")


if __name__ == "__main__":
    unittest.main()
