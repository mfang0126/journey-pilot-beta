"""Shared boundary failures must stop before input, including across processes."""
import multiprocessing
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from journey_pilot import adapter, runner, session
from journey_pilot.execution import device_lock, request_once
from journey_pilot.contracts import success
from journey_pilot.store import Store, atomic_write_json


def contend(root, queue):
    with device_lock(Store(Path(root)), "dev") as busy:
        queue.put(busy)


class ExecutionGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))
        patch = mock.patch.object(adapter, "status", return_value={
            "bound": "dev", "devices": [{"serial": "dev"}], "ready": True})
        patch.start()
        self.addCleanup(patch.stop)
        opened = session.start(self.store, {"device_id": "dev", "intent": "test"})
        self.base = {"run_id": opened.run_id, "lease_token": opened.data["lease_token"], "device_id": "dev"}

    def test_session_reserves_device_and_rejects_device_switch(self):
        with mock.patch.object(adapter, "harness_read") as read, mock.patch.object(adapter, "run_action") as action:
            other = session.start(self.store, {"device_id": "dev", "intent": "other"})
            self.assertEqual(other.error["code"], "device_busy")
            out = session.observe(self.store, {**self.base, "device_id": "other"})
            self.assertEqual(out.error["code"], "precondition_mismatch")
            with device_lock(self.store, "dev") as busy:
                self.assertEqual(busy, "dev")
            read.assert_not_called()
            action.assert_not_called()
        session.finish(self.store, self.base)
        with device_lock(self.store, "dev") as busy:
            self.assertIsNone(busy)

    def test_active_lock_cannot_be_stolen_even_with_zero_ttl(self):
        session.finish(self.store, self.base)
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        with device_lock(self.store, "dev", ttl=0):
            child = ctx.Process(target=contend, args=(str(self.store.root), queue))
            child.start()
            try:
                self.assertEqual(queue.get(timeout=10), "dev")
                child.join(10)
                self.assertEqual(child.exitcode, 0)
            finally:
                if child.is_alive():
                    child.terminate()
                    child.join()
        queue.close()

    def test_unknown_result_and_duplicate_request_never_repeat(self):
        request = {**self.base, "request_id": "one", "action": {"type": "key"}}
        with mock.patch.object(adapter, "run_action", return_value={"code": "action_failed"}) as action:
            first = session.act(self.store, request)
            second = session.act(self.store, request)
            third = session.act(self.store, {**request, "request_id": "two"})
            self.assertEqual(first.error["code"], "outcome_unknown")
            self.assertEqual(second.error["code"], "outcome_unknown")
            self.assertEqual(third.error["code"], "outcome_unknown")
            self.assertEqual(action.call_count, 1)

    def test_request_crash_window_does_not_redispatch(self):
        operation = mock.Mock(side_effect=RuntimeError("after input"))
        first = request_once(self.store, "test", "id", {}, operation)
        second = request_once(self.store, "test", "id", {}, operation)
        self.assertEqual(first.error["code"], "outcome_unknown")
        self.assertEqual(second.error["code"], "outcome_unknown")
        self.assertEqual(operation.call_count, 1)

    def test_coordinates_expire_after_action_and_cancel_stops_input(self):
        with mock.patch.object(adapter, "harness_read", return_value={"ok": True, "geometry": {"width":1440,"height":3000,"orientation":0}, "nodes": [{"text": "target"}]}):
            observed = session.observe(self.store, self.base)
        tap = {"type": "tap_xy", "x": 10, "y": 20, "observation_id": observed.data["observation_id"]}
        with mock.patch.object(adapter, "run_action", return_value={"ok": True}) as action:
            self.assertEqual(session.act(self.store, {**self.base, "request_id": "a", "action": tap}).status, "recorded")
            stale = session.act(self.store, {**self.base, "request_id": "b", "action": tap})
            self.assertEqual(stale.error["code"], "precondition_mismatch")
            runner.cancel(self.store, {"run_id": self.base["run_id"]})
            stopped = session.act(self.store, {**self.base, "request_id": "c", "action": {"type": "key"}})
            self.assertEqual(stopped.status, "cancelled")
            self.assertEqual(action.call_count, 1)

    def test_platform_signal_stops_all_following_input(self):
        with mock.patch.object(adapter, "harness_read", return_value={"ok": True, "nodes": [{"text": "操作频繁，请稍后再试"}]}):
            observed = session.observe(self.store, self.base)
        self.assertEqual(observed.error["code"], "platform_blocked")
        with mock.patch.object(adapter, "run_action") as action:
            result = session.act(self.store, {**self.base, "request_id": "after-block", "action": {"type": "key"}})
            self.assertEqual(result.error["code"], "platform_blocked")
            action.assert_not_called()

    def test_rotation_change_refuses_coordinates_inside_device_script(self):
        import io, json
        script = adapter.build_action_script({"type": "tap_xy", "x": 10, "y": 20,
            "geometry": {"width": 1440, "height": 3000, "orientation": 0}})
        tap = mock.Mock()
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            exec(script, {"tap": tap, "shell": lambda cmd: "1440x3000" if cmd == "wm size" else "SurfaceOrientation: 1"})
        self.assertEqual(json.loads(output.getvalue())["code"], "precondition_mismatch")
        tap.assert_not_called()

    def test_handoff_refreshes_scene_and_invalidates_old_token(self):
        with mock.patch.object(adapter, "harness_read", return_value={"ok": True, "nodes": [{"text": "Home"}]}):
            result = session.handoff(self.store, self.base)
        self.assertEqual(result.status, "recorded")
        self.assertNotEqual(result.data["lease_token"], self.base["lease_token"])
        with mock.patch.object(adapter, "run_action") as action:
            stale = session.act(self.store, {**self.base, "request_id": "old", "action": {"type": "key"}})
            self.assertEqual(stale.error["code"], "device_busy")
            action.assert_not_called()

    def test_handoff_preserves_cancel_and_cannot_reopen_finished_session(self):
        from journey_pilot import runner
        runner.cancel(self.store, {"run_id": self.base["run_id"]})
        with mock.patch.object(adapter, "harness_read", return_value={"ok": True, "nodes": []}):
            handed = session.handoff(self.store, self.base)
            self.assertFalse(handed.data["action_allowed"])
            new_base = {**self.base, "lease_token": handed.data["lease_token"]}
            session.finish(self.store, new_base)
            refused = session.handoff(self.store, new_base)
            self.assertEqual(refused.error["code"], "needs_replan")

    def test_same_action_id_is_scoped_to_its_session(self):
        with mock.patch.object(adapter, "run_action", return_value={"ok": True}) as action:
            first = session.act(self.store, {**self.base, "request_id": "s_1", "action": {"type": "key"}})
            self.assertEqual(first.status, "recorded")
            session.finish(self.store, self.base)
            second = session.start(self.store, {"device_id": "dev", "intent": "second"})
            with mock.patch("journey_pilot.pacing.before_action", return_value={}):
                out = session.act(self.store, {"run_id": second.run_id, "lease_token": second.data["lease_token"],
                    "request_id": "s_1", "action": {"type": "key"}})
            self.assertEqual(out.status, "recorded")
            self.assertEqual(action.call_count, 2)

    def test_path_escape_refused(self):
        with self.assertRaises(ValueError):
            self.store.run_dir("../escape")


class ReuseTab(unittest.TestCase):
    url = "http://127.0.0.1:8765/t.html"

    def test_reuse_tab_adds_the_chrome_application_id(self):
        base = {"type": "launch_activity", "data": self.url}
        self.assertNotIn("application_id", adapter.build_action_script(base))
        self.assertIn("--es com.android.browser.application_id journey-pilot",
                      adapter.build_action_script({**base, "reuse_tab": True}))

    def test_reuse_tab_needs_data_and_true(self):
        from journey_pilot.journey import JourneyError
        for bad in ({"component": "a/b", "reuse_tab": True},
                    {"data": self.url, "reuse_tab": "yes"},
                    {"data": self.url, "reuse_tab": False}):
            with self.assertRaises(JourneyError):
                adapter.build_action_script({"type": "launch_activity", **bad})
