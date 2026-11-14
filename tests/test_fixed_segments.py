import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from journey_pilot import adapter, runner
from journey_pilot.store import Store


class FixedSegments(unittest.TestCase):
    GEOMETRY = {"width": 1080, "height": 2400, "orientation": 0}

    def _run_script(self, action, nodes):
        """Execute the generated action script against a fake harness screen."""
        tapped = []
        namespace = {
            "ui": lambda: nodes,
            "shell": lambda cmd: ("Physical size: 1080x2400" if "wm size" in cmd
                                  else "SurfaceOrientation: 0"),
            "tap": lambda x, y: tapped.append((x, y)),
        }
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exec(compile(adapter.build_action_script(action), "<action-script>", "exec"), namespace)
        return json.loads(out.getvalue().strip().splitlines()[-1]), tapped

    def test_moved_fixed_target_script_does_not_tap(self):
        segment = {"app": "com.android.settings", "geometry": self.GEOMETRY,
                   "target": {"text": "Network"}, "entry": {"text_equals": "Settings"},
                   "exit": {"text_equals": "Network"}}
        action = {"step_id": "network", "type": "tap_xy", "x": 100, "y": 200,
                  "geometry": self.GEOMETRY, "fixed_segment": segment,
                  "settle_ms": 0, "poll_ms": 0}

        moved, taps = self._run_script(action, [{"text": "Network", "x": 640, "y": 900}])
        self.assertFalse(moved["ok"])
        self.assertEqual(moved["code"], "precondition_mismatch")
        self.assertEqual(taps, [])

        stayed, taps = self._run_script(action, [{"text": "Network", "x": 100, "y": 200}])
        self.assertTrue(stayed["ok"], stayed)
        self.assertEqual(taps, [(100, 200)])

    def test_layout_or_entry_mismatch_never_clicks(self):
        geometry = {"width": 1080, "height": 2400, "orientation": 0}
        segment = {"app": "com.android.settings", "geometry": geometry,
                   "target": {"text": "Network"}, "entry": {"text_equals": "Settings"}, "exit": {"text_equals": "Network"}}
        doc = {"journey_id": "j_test", "version": 1,
               "steps": [{"step_id": "network", "action": "tap_xy", "x": 100, "y": 200,
                          "fixed_segment": segment}], "postconditions": {"text_equals": "Network"}}
        for observed in (
            {"geometry": {**geometry, "orientation": 1}, "nodes": [{"text": "Settings"}]},
            {"geometry": geometry, "nodes": [{"text": "Keyboard dialog"}]},
        ):
            with self.subTest(observed=observed), tempfile.TemporaryDirectory() as tmp, \
                 mock.patch.object(adapter, "status", return_value={"ready": True, "foreground": "com.android.settings/.Main"}), \
                 mock.patch.object(adapter, "harness_read", return_value={"ok": True, **observed}), \
                 mock.patch.object(adapter, "run_action") as action:
                summary, _ = runner._execute(Store(Path(tmp)), doc, "dev", "validate")
                self.assertEqual(summary["failure"]["code"], "precondition_mismatch")
                action.assert_not_called()
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(adapter, "status", return_value={"ready": True, "foreground": "com.android.settings/.Main"}), \
             mock.patch.object(adapter, "harness_read", side_effect=[
                 {"ok": True, "geometry": geometry, "nodes": [{"text": "Settings"}]},
                 {"ok": True, "nodes": [{"text": "Network"}]},
                 {"ok": True, "nodes": [{"text": "Network"}]}]), \
             mock.patch.object(adapter, "run_action", return_value={"ok": True}) as action:
            summary, _ = runner._execute(Store(Path(tmp)), doc, "dev", "validate")
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(action.call_args.args[0]["geometry"], geometry)
