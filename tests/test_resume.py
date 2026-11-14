import tempfile
import unittest
from pathlib import Path
from unittest import mock

from journey_pilot import adapter, journey, runner
from journey_pilot.store import Store, atomic_write_json, read_json


class Resume(unittest.TestCase):
    def test_cancel_after_checkpoint_resumes_without_repeating_action(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(adapter, "status", return_value={"ready": True, "bound": "dev", "devices": [{}]}), \
             mock.patch.object(adapter, "harness_read", return_value={"ok": True, "nodes": [{"text": "Checkpoint"}]}), \
             mock.patch("journey_pilot.pacing.before_action", return_value={}):
            store = Store(Path(tmp))
            proposed = journey.propose(store, {"task_key": "resume-test", "intent": "test",
                "steps": [{"step_id": "first", "action": "key", "postcondition": {"text_equals": "Checkpoint"}},
                          {"step_id": "second", "action": "read_field", "locator": {"text": "Checkpoint"}}],
                "postconditions": {"text_equals": "Checkpoint"}})
            done = []
            def action(spec, **kwargs):
                done.append(spec["step_id"])
                if spec["step_id"] == "first":
                    active = next(store.runs.iterdir()).name
                    runner.cancel(store, {"run_id": active})
                return {"ok": True}
            with mock.patch.object(adapter, "run_action", side_effect=action):
                stopped = runner.validate(store, {"journey_id": proposed.data["journey_id"], "request_id": "initial"})
                self.assertEqual(stopped.status, "cancelled")
                resumed = runner.resume(store, {"run_id": stopped.run_id, "request_id": "continue"})
                self.assertEqual(resumed.status, "completed")
                self.assertEqual(done, ["first", "second"])
                self.assertFalse(resumed.data["promotion_eligible"])
                duplicate = runner.resume(store, {"run_id": stopped.run_id, "request_id": "continue"})
                self.assertEqual(duplicate.run_id, resumed.run_id)
                self.assertEqual(done, ["first", "second"])

    def test_chained_resume_uses_absolute_checkpoint_and_original_budget(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(adapter, "status", return_value={"ready": True, "bound": "dev", "devices": [{}]}), \
             mock.patch.object(adapter, "harness_read", return_value={"ok": True, "nodes": [{"text": "Checkpoint"}]}), \
             mock.patch("journey_pilot.pacing.before_action", return_value={}) as pacing:
            store = Store(Path(tmp))
            proposed = journey.propose(store, {"task_key": "chain", "intent": "test",
                "steps": [{"step_id": str(i), "action": "key", "postcondition": {"text_equals": "Checkpoint"}} for i in range(3)],
                "postconditions": {"text_equals": "Checkpoint"}})
            done = []
            def action(spec, **kwargs):
                done.append(spec["step_id"])
                if len(done) < 3:
                    for folder in store.runs.iterdir():
                        if (read_json(folder / "summary.json") or {}).get("status") == "running":
                            runner.cancel(store, {"run_id": folder.name})
                return {"ok": True}
            with mock.patch.object(adapter, "run_action", side_effect=action):
                first = runner.validate(store, {"journey_id": proposed.data["journey_id"], "request_id": "first"})
                second = runner.resume(store, {"run_id": first.run_id, "request_id": "second"})
                third = runner.resume(store, {"run_id": second.run_id, "request_id": "third"})
            self.assertEqual(third.status, "completed")
            self.assertEqual(done, ["0", "1", "2"])
            self.assertEqual({call.args[2] for call in pacing.call_args_list}, {first.run_id})
            # Per-package pacing reads the foreground of the device being driven.
            with mock.patch.object(adapter, "foreground_package", return_value="com.x") as fg:
                self.assertEqual(pacing.call_args_list[0].kwargs["foreground"](), "com.x")
            fg.assert_called_once_with("dev")

    def test_action_after_checkpoint_cannot_be_replayed_from_stale_boundary(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(adapter, "status", return_value={"ready": True, "bound": "dev", "devices": [{}]}), \
             mock.patch.object(adapter, "harness_read", return_value={"ok": True, "nodes": [{"text": "Checkpoint"}]}), \
             mock.patch("journey_pilot.pacing.before_action", return_value={}):
            store = Store(Path(tmp))
            proposed = journey.propose(store, {"task_key": "stale-checkpoint", "intent": "test",
                "steps": [{"step_id": "first", "action": "key", "postcondition": {"text_equals": "Checkpoint"}},
                          {"step_id": "second", "action": "key"},
                          {"step_id": "third", "action": "key"}],
                "postconditions": {"text_equals": "Checkpoint"}})
            done = []
            def action(spec, **kwargs):
                done.append(spec["step_id"])
                if spec["step_id"] == "second":
                    runner.cancel(store, {"run_id": next(store.runs.iterdir()).name})
                return {"ok": True}
            with mock.patch.object(adapter, "run_action", side_effect=action):
                stopped = runner.validate(store, {"journey_id": proposed.data["journey_id"], "request_id": "initial"})
                resumed = runner.resume(store, {"run_id": stopped.run_id, "request_id": "continue"})
            self.assertEqual(resumed.error["code"], "needs_replan")
            self.assertEqual(done, ["first", "second"])

    def test_crash_before_execute_leaves_original_linked_to_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(adapter, "status", return_value={"ready": True, "bound": "dev", "devices": [{}]}), \
             mock.patch.object(adapter, "harness_read", return_value={"ok": True, "nodes": [{"text": "Checkpoint"}]}), \
             mock.patch("journey_pilot.pacing.before_action", return_value={}):
            store = Store(Path(tmp))
            proposed = journey.propose(store, {"task_key": "placeholder", "intent": "test",
                "steps": [{"step_id": "first", "action": "key", "postcondition": {"text_equals": "Checkpoint"}},
                          {"step_id": "second", "action": "read_field", "locator": {"text": "Checkpoint"}}],
                "postconditions": {"text_equals": "Checkpoint"}})
            def action(spec, **kwargs):
                if spec["step_id"] == "first":
                    runner.cancel(store, {"run_id": next(store.runs.iterdir()).name})
                return {"ok": True}
            with mock.patch.object(adapter, "run_action", side_effect=action):
                stopped = runner.validate(store, {"journey_id": proposed.data["journey_id"], "request_id": "initial"})
            self.assertEqual(stopped.status, "cancelled")

            with mock.patch.object(runner, "_execute", side_effect=RuntimeError("crash before execution")):
                crashed = runner.resume(store, {"run_id": stopped.run_id, "request_id": "crash"})
            self.assertEqual(crashed.status, "paused")
            self.assertEqual(crashed.error["code"], "outcome_unknown")

            original = read_json(store.summary_path(stopped.run_id))
            placeholder_id = original["resumed_by"]
            self.assertIsNotNone(placeholder_id)
            placeholder = read_json(store.summary_path(placeholder_id))
            self.assertEqual(placeholder["status"], "blocked")
            self.assertEqual(placeholder["continuation_of"], stopped.run_id)
            self.assertEqual(placeholder["failure"]["code"], "outcome_unknown")

            retry = runner.resume(store, {"run_id": stopped.run_id, "request_id": "retry"})
            self.assertEqual(retry.error["code"], "needs_replan")
            self.assertEqual(retry.data["continuation_run_id"], placeholder_id)

    def test_unknown_or_missing_checkpoint_refuses(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(adapter, "run_action") as action:
            store = Store(Path(tmp))
            store.create_run("r_test", {"status": "paused", "device_id": "dev"})
            result = runner.resume(store, {"run_id": "r_test", "request_id": "resume"})
            self.assertEqual(result.error["code"], "needs_replan")
            action.assert_not_called()
