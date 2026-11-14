"""Execute generated harness code against changing screens; no device access."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from journey_pilot import adapter, journey, runner
from journey_pilot.store import Store


def execute_script(action, ui, tap=lambda x, y: None):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(adapter.build_action_script(action), {"ui": ui, "tap": tap})
    return json.loads(output.getvalue())


class LocatorFallback(unittest.TestCase):
    def test_fallback_uses_one_tree_and_records_chosen_key(self):
        ui = mock.Mock(return_value=[{"desc": "个人主页", "id": "profile", "x": 10, "y": 20}])
        tap = mock.Mock()
        result = execute_script({"type": "tap", "locator": {"text": "我"},
                                 "fallback": {"alternatives": [{"description": "个人主页"}]}}, ui, tap)
        self.assertTrue(result["ok"])
        self.assertEqual(result["locator_index"], 1)
        self.assertEqual(result["metrics"]["tree_reads"], 1)
        ui.assert_called_once()
        tap.assert_called_once_with(10, 20)
        self.assertTrue(runner.check(ui.return_value, {"element_exists": {"resource_id": "profile"}}))
        self.assertTrue(runner.check(ui.return_value, {"element_exists": {"description": "个人主页"}}))
        self.assertFalse(runner.check([{"text": None}], {"element_exists": {"text_contains": "x"}}))

    def test_ambiguity_never_falls_through_to_another_target(self):
        for primary in ({"text": "重复"}, {"text": "missing"}):
            with self.subTest(primary=primary):
                tap = mock.Mock()
                result = execute_script({"type": "tap", "locator": primary,
                    "fallback": {"alternatives": [{"text": "重复"}, {"text": "别的"}]}},
                    lambda: [{"text": "重复"}, {"text": "重复"}, {"text": "别的"}], tap)
                self.assertEqual(result["code"], "ambiguous_target")
                tap.assert_not_called()

    def test_propose_validate_keeps_fallback_and_refreshes_after_navigation(self):
        pages = [[{"desc": "主页", "x": 10, "y": 20}],
                 [{"id": "item", "x": 30, "y": 40}], [{"text": "完成"}]]
        taps = []
        def tap(x, y):
            taps.append((x, y))
        def harness(script, **kwargs):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exec(script, {"ui": lambda: pages[len(taps)], "tap": tap,
                              "shell": lambda cmd: "Physical size: 1440x3000" if cmd == "wm size" else "SurfaceOrientation: 0"})
            return json.loads(output.getvalue())
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            proposed = journey.propose(store, {"task_key": "test", "intent": "test",
                "steps": [{"step_id": "home", "action": "tap", "locator": {"text": "我"},
                           "fallback": {"alternatives": [{"description": "主页"}]}},
                          {"step_id": "item", "action": "tap", "locator": {"resource_id": "item"}}],
                "postconditions": {"text_equals": "完成"}})
            self.assertEqual(proposed.status, "ok")
            with mock.patch.object(adapter, "status", return_value={"bound": "dev", "devices": [{"serial": "dev"}], "ready": True}), \
                 mock.patch.object(adapter, "harness_read", side_effect=harness):
                result = runner.validate(store, {"journey_id": proposed.data["journey_id"],
                                                "request_id": "test", "device_id": "dev"})
            self.assertEqual(result.status, "completed")
            self.assertEqual(taps, [(10, 20), (30, 40)])
            events = store.read_events(result.run_id)
            actions = [e for e in events if e["event"] == "action_finished"]
            self.assertEqual([e["metrics"]["tree_reads"] for e in actions], [1, 1])
            self.assertEqual(actions[0]["locator_index"], 1)
            self.assertFalse(any(e["event"] == "learned_batch" for e in events))

    def test_invalid_configs_fail_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            request = {"task_key": "test", "intent": "test", "steps": [
                {"step_id": "s", "action": "tap", "locator": {"text": "我"}}]}
            for mode in ("batch", "adaptive", "typo"):
                result = journey.propose(store, {**request, "observation_strategy": mode})
                self.assertEqual(result.error["code"], "unsupported_input")
            for locator in ({"description_contains": "我"}, {"text": "我", "typo": "bad"}, {"text": ""}):
                with self.subTest(locator=locator), self.assertRaises(journey.JourneyError):
                    adapter.build_action_script({"type": "tap", "locator": locator})
            bad = {**request["steps"][0], "fallback": {"alternatives": [{"text": "主页"}],
                                                     "decision_method": "jev_first"}}
            self.assertEqual(journey.propose(store, {**request, "steps": [bad]}).error["code"],
                             "invalid_request")

    def test_failed_observation_cannot_satisfy_negative_postcondition(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(adapter, "harness_read", return_value={"ok": False, "reason": "locked"}):
            summary, _ = runner._execute(Store(Path(directory)), {
                "journey_id": "j_test", "version": 1, "steps": [],
                "postconditions": {"not": {"element_exists": {"text": "error"}}}}, "dev", "validate")
            self.assertEqual(summary["status"], "paused")

    def test_external_mutation_always_hands_back(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(adapter, "run_action") as action:
            summary, _ = runner._execute(Store(Path(directory)), {
                "journey_id": "j_test", "version": 1,
                "steps": [{"step_id": "send", "action": "tap", "effect_class": "external_mutation"}]},
                "dev", "validate", {"approved": True})
            self.assertEqual(summary["failure"]["code"], "needs_authorization")
            action.assert_not_called()


if __name__ == "__main__":
    unittest.main()
