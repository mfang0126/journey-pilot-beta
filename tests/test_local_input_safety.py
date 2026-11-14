"""Non-ASCII input and unknown local-input recovery must fail closed."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from journey_pilot import adapter, session
from journey_pilot.execution import reservation_path
from journey_pilot.store import Store

TEXT = "这是一段用于离线测试的中文评论文本，包含全角标点，但不含真实个人信息。"
FIELD = {"class": "android.widget.EditText"}
INPUT = {"step_id": "comment", "type": "input_text", "effect_class": "local_input",
         "locator": FIELD, "method": "paste", "text": TEXT}


class LocalInputSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))
        patches = [
            mock.patch.object(adapter, "status", return_value={
                "bound": "dev", "devices": [{"serial": "dev"}], "ready": True}),
            mock.patch("journey_pilot.pacing.before_action", return_value={}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        opened = session.start(self.store, {"device_id": "dev", "intent": "input"})
        self.base = {"run_id": opened.run_id,
                     "lease_token": opened.data["lease_token"], "device_id": "dev"}

    def test_non_ascii_adb_input_is_rejected_before_any_device_input(self):
        action = {"step_id": "search", "type": "input_text", "effect_class": "local_input",
                  "locator": FIELD, "text": "关于手机"}
        with mock.patch.object(adapter, "run_action") as run:
            result = session.act(self.store, {**self.base, "request_id": "cjk", "action": action})
            self.assertEqual(result.error["code"], "unsupported_input")
            run.assert_not_called()
        self.assertFalse(any(e.get("event") == "action_started"
                             for e in self.store.read_events(self.base["run_id"])))

    def test_launch_activity_accepts_only_one_ascii_http_url(self):
        script = adapter.build_action_script({
            "type": "launch_activity", "step_id": "open-page",
            "component": "com.android.chrome/com.google.android.apps.chrome.Main",
            "data": "http://192.0.2.10:8000/token"})
        self.assertIn("am start -n com.android.chrome/com.google.android.apps.chrome.Main "
                      "-d http://192.0.2.10:8000/token", script)
        for bad in ({"data": "file:///sdcard/x"}, {"data": "http://例子.test/"},
                    {"data": "http://x/\n"}, {}):
            with self.assertRaises(Exception):
                adapter.build_action_script({"type": "launch_activity", **bad})

    def test_paste_uses_clipboard_key_and_verifies_the_field_text(self):
        script = adapter.build_action_script(INPUT)
        self.assertIn("input keyevent 279", script)
        self.assertNotIn("type_text(", script)
        with mock.patch.object(adapter, "run_action",
                               return_value={"ok": True, "value": TEXT}):
            result = session.act(self.store, {**self.base, "request_id": "paste-ok",
                                              "action": INPUT})
        self.assertEqual(result.status, "recorded")
        self.assertEqual(store_value(self.store, self.base["run_id"]), "ok")

    def test_paste_that_does_not_match_the_authorized_text_is_not_recorded(self):
        with mock.patch.object(adapter, "run_action",
                               return_value={"ok": True, "value": "wrong field text"}):
            result = session.act(self.store, {**self.base, "request_id": "paste-bad",
                                              "action": INPUT})
        self.assertEqual(result.error["code"], "assertion_failed")
        self.assertNotEqual(store_value(self.store, self.base["run_id"]), "ok")

    def _record_failed_input(self, step="s_open", text="关于手机",
                             metrics=None, state="done", after_nodes=None):
        import json

        from journey_pilot.execution import request_record_path
        from journey_pilot.privacy import private_digest
        from journey_pilot.store import atomic_write_json
        nodes = [{"text": "搜索", "class": "android.widget.EditText"}]
        geometry = {"width": 1200, "height": 2608, "orientation": 0}
        digest = private_digest(self.store, nodes)
        after_digest = private_digest(self.store, after_nodes) if after_nodes is not None else digest
        self.store.append_event(self.base["run_id"], {
            "event": "observation", "observation_id": "o_before", "source": "tree",
            "digest": digest, "observed_at": 1.0, "device_id": "dev",
            "geometry": geometry, "count": len(nodes),
        })
        action = {"step_id": step, "type": "input_text", "effect_class": "local_input",
                  "locator": FIELD, "text": text}
        self.store.append_event(self.base["run_id"], {
            "event": "action_started", "step_id": step, "action": "input_text",
            "effect_class": "local_input",
            "execution": {"type": "input_text", "effect_class": "local_input"},
        })
        self.store.append_event(self.base["run_id"], {
            "event": "action_finished", "step_id": step,
            "outcome": "action_failed", "locator_index": 0,
            "metrics": metrics if metrics is not None else {"script_ms": 5835.18, "tree_reads": 1},
        })
        self.store.append_event(self.base["run_id"], {
            "event": "observation", "observation_id": "o_after", "source": "tree",
            "digest": after_digest, "observed_at": 2.0, "device_id": "dev",
            "geometry": geometry, "count": len(nodes),
        })
        payload = {"run_id": self.base["run_id"], "device_id": "dev", "action": action}
        path = request_record_path(self.store, f"act:{self.base['run_id']}", "cjk")
        atomic_write_json(path, {
            "digest": private_digest(self.store, payload), "state": state,
            "result": {"status": "paused", "run_id": self.base["run_id"],
                       "data": {"step_id": step, "content_retained": False},
                       "error": {"code": "outcome_unknown", "retryable": False,
                                 "exit_code": 3, "status": "paused"},
                       "next_action": None},
        })
        return ({"run_id": self.base["run_id"], "lease_token": self.base["lease_token"],
                 "device_id": "dev", "request_id": "cjk", "action": action},
                nodes, geometry)

    def test_resolve_input_closes_matching_terminal_failure_with_same_visible_tree(self):
        request, nodes, geometry = self._record_failed_input()
        with mock.patch.object(adapter, "harness_read") as read:
            result = session.resolve_input(self.store, request)
        self.assertEqual(result.status, "recorded")
        self.assertTrue(result.data["resolved"])
        self.assertEqual(result.data["before_observation_id"], "o_before")
        self.assertEqual(result.data["after_observation_id"], "o_after")
        read.assert_not_called()
        self.assertEqual(self.store.open_actions(self.base["run_id"]), [])
        resolved = [e for e in self.store.read_events(self.base["run_id"])
                    if e.get("event") == "action_resolved"]
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["step_id"], "s_open")

    def _expire_lease(self):
        path = self.store.locks / f"{self.base['run_id']}.json"
        lease = json.loads(path.read_text())
        lease["expires_at"] = 0
        path.write_text(json.dumps(lease), encoding="utf-8")
        return path.read_bytes()

    def test_resolve_input_accepts_the_same_expired_lease_without_touching_the_device(self):
        request, nodes, geometry = self._record_failed_input()
        before = self._expire_lease()
        with mock.patch.object(adapter, "harness_read") as read:
            result = session.resolve_input(self.store, request)
        self.assertEqual(result.status, "recorded")
        self.assertTrue(result.data["resolved"])
        read.assert_not_called()
        self.assertEqual((self.store.locks / f"{self.base['run_id']}.json").read_bytes(), before,
                         "resolution must not renew or rewrite the expired lease")

    def test_resolve_input_refuses_an_expired_token_that_does_not_match(self):
        request, nodes, geometry = self._record_failed_input()
        self._expire_lease()
        result = session.resolve_input(self.store, {**request, "lease_token": "l_wrong"})
        self.assertEqual(result.error["code"], "device_busy")
        self.assertEqual([e["step_id"] for e in self.store.open_actions(self.base["run_id"])],
                         ["s_open"])

    def test_resolve_input_never_preempts_another_runs_reservation(self):
        request, nodes, geometry = self._record_failed_input()
        self._expire_lease()
        reservation_path(self.store, "dev").write_text(
            json.dumps({"run_id": "r_other"}), encoding="utf-8")
        with mock.patch.object(adapter, "harness_read") as read:
            result = session.resolve_input(self.store, request)
        self.assertEqual(result.error["code"], "device_busy")
        read.assert_not_called()
        self.assertEqual([e["step_id"] for e in self.store.open_actions(self.base["run_id"])],
                         ["s_open"])

    def test_resolve_input_rejects_a_different_full_payload(self):
        request, nodes, geometry = self._record_failed_input()
        request = {**request, "action": {**request["action"], "text": "另一个词"}}
        events_path = self.store.events_path(self.base["run_id"])
        before = events_path.read_bytes()
        with mock.patch.object(adapter, "harness_read") as read:
            result = session.resolve_input(self.store, request)
        self.assertEqual(result.error["code"], "request_conflict")
        read.assert_not_called()
        self.assertEqual(events_path.read_bytes(), before)

    def test_resolve_input_refuses_when_the_post_failure_tree_changed(self):
        request, nodes, geometry = self._record_failed_input(
            after_nodes=[{"text": "已输入", "class": "android.widget.EditText"}])
        result = session.resolve_input(self.store, request)
        self.assertEqual(result.error["code"], "precondition_mismatch")
        self.assertEqual([e["step_id"] for e in self.store.open_actions(self.base["run_id"])],
                         ["s_open"])

    def test_resolve_input_refuses_without_normal_script_metrics(self):
        request, nodes, geometry = self._record_failed_input(metrics={})
        with mock.patch.object(adapter, "harness_read") as read:
            result = session.resolve_input(self.store, request)
        self.assertEqual(result.error["code"], "outcome_unknown")
        read.assert_not_called()
        self.assertEqual([e["step_id"] for e in self.store.open_actions(self.base["run_id"])],
                         ["s_open"])

    def test_resolve_input_refuses_a_nonterminal_request(self):
        request, nodes, geometry = self._record_failed_input(state="intent")
        with mock.patch.object(adapter, "harness_read") as read:
            result = session.resolve_input(self.store, request)
        self.assertEqual(result.error["code"], "outcome_unknown")
        read.assert_not_called()


def store_value(store, run_id):
    return next((e.get("outcome") for e in reversed(store.read_events(run_id))
                 if e.get("event") == "action_finished"), None)


if __name__ == "__main__":
    unittest.main()
