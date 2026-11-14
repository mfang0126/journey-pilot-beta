"""#14-class regression guards: every lock/credential refusal branch, one test each.

Covers the marker tuples (_CREDENTIAL_FOCUS, _CREDENTIAL_TREE, _DISPATCH_TREE)
member by member, so a marker dropped in a merge or a copy that drifts from its
constant fails here, plus two runner preflight refusals with no other test.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from journey_pilot import adapter, runner
from journey_pilot.store import Store

from test_share_text import SHARE, FakeAdb
from test_wake_recovery import LOCK_REFUSAL, FakeLock, dual_focus, run_wake

# Real-device spellings of each credential entry view (AOSP systemui ids).
ENTRY_IDS = {
    "id/pinentry": "com.android.systemui:id/pinEntry",
    "id/passwordentry": "com.android.systemui:id/passwordEntry",
    "id/lockpatternview": "com.android.systemui:id/lockPatternView",
}
# One display-0 focus window per _CREDENTIAL_FOCUS marker.
FOCUS_WINDOWS = {
    "keyguardbouncer": "com.android.systemui/.keyguard.KeyguardBouncer",
    "confirmdevicecredential": "com.android.settings/.password.ConfirmDeviceCredentialActivity",
    "simunlock": "com.android.systemui/.SimUnlockScreen",
    "simpuk": "com.android.systemui/.keyguard.SimPukView",
    "keyguardsimpin": "com.android.systemui/.keyguard.KeyguardSimPinView",
}
HOME = "Window{1 u0 com.miui.home/.launcher.Launcher}"


def node(view_id):
    return {"id": view_id, "text": "", "desc": "", "class": "android.view.View",
            "clickable": True, "x": 600, "y": 1300, "w": 800, "h": 120}


class MarkerTables(unittest.TestCase):
    def test_every_marker_has_a_fixture(self):
        # A marker added without a guard case here fails instead of going untested.
        self.assertEqual(set(ENTRY_IDS), set(adapter._CREDENTIAL_TREE))
        self.assertEqual(set(FOCUS_WINDOWS), set(adapter._CREDENTIAL_FOCUS))

    def test_dispatch_tree_is_a_superset_of_the_credential_tree(self):
        for marker in adapter._CREDENTIAL_TREE:
            with self.subTest(marker=marker):
                self.assertTrue(any(d in marker for d in adapter._DISPATCH_TREE))


class WakeCredentialTree(unittest.TestCase):
    """Wake: flags cleared, display 0 known, then _CREDENTIAL_TREE decides."""

    def test_each_credential_entry_view_stops_wake_for_a_person(self):
        for marker, view_id in ENTRY_IDS.items():
            with self.subTest(marker=marker):
                device = FakeLock(showing=False, wakefulness="Awake", focus_dump=dual_focus(HOME))
                result = run_wake(device, mock.Mock(return_value=[node(view_id)]))
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], "locked")
                self.assertEqual(result["wake"]["verified_by"], "credential_tree")
                self.assertTrue(result["wake"]["requires_human"])
                self.assertTrue(result["wake"]["credential_visible"])
                self.assertEqual(device.swipes, 0)

    def test_unreadable_tree_after_cleared_flags_is_not_unlocked(self):
        device = FakeLock(showing=False, wakefulness="Awake", focus_dump=dual_focus(HOME))
        result = run_wake(device, mock.Mock(side_effect=RuntimeError("tree unavailable")))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "locked")
        self.assertEqual(result["wake"]["verified_by"], "accessibility_tree_failed")
        self.assertIn("tree unavailable", result["reason"])

    def test_each_credential_focus_stops_wake_despite_global_unlock(self):
        # _state() keeps its own copy of the focus markers; this pins it to the constant.
        for marker, window in FOCUS_WINDOWS.items():
            with self.subTest(marker=marker):
                device = FakeLock(showing=False, wakefulness="Awake",
                                  focus_dump=dual_focus("Window{1 u0 %s}" % window))
                ui = mock.Mock(return_value=[node("home")])
                result = run_wake(device, ui)
                self.assertFalse(result["ok"])
                self.assertEqual(result["wake"]["verified_by"], "credential_signal")
                self.assertTrue(result["wake"]["requires_human"])
                ui.assert_not_called()

    def test_each_credential_focus_marks_lock_assessment_for_a_person(self):
        for marker, window in FOCUS_WINDOWS.items():
            with self.subTest(marker=marker):
                lock = adapter.lock_assessment({
                    "keyguard": {"showing": False, "secure": False, "bouncer": False},
                    "foreground": window})
                self.assertEqual(lock["kind"], "secure")
                self.assertTrue(lock["requires_human"])
                self.assertFalse(lock["ordinary_wake_allowed"])


class DispatchGate(unittest.TestCase):
    """share_text: DISPATCH_GATE stops the SEND on every lock signal, never types."""

    def run_share(self, adb):
        with mock.patch.object(adapter, "harness_read", side_effect=adb.harness_read):
            return adapter.run_action(dict(SHARE))

    def assert_refused(self, adb, reason, human):
        result = self.run_share(adb)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "locked")
        self.assertIn(reason, result["reason"])
        self.assertIs(result["requires_human"], human)
        self.assertEqual(adb.sends, [])
        self.assertFalse(any(c.startswith("input") for c in adb.commands))

    def test_each_credential_focus_on_display_zero_stops_send(self):
        for marker, window in FOCUS_WINDOWS.items():
            with self.subTest(marker=marker):
                adb = FakeAdb(focus="  Display: mDisplayId=0\n  mCurrentFocus=Window{3 u0 %s}" % window)
                self.assert_refused(adb, "credential UI is focused on display 0", True)

    def test_keyguard_showing_stops_send(self):
        self.assert_refused(FakeAdb(guard="    isKeyguardShowing=true"), "keyguard is showing", False)

    def test_unknown_keyguard_state_stops_send(self):
        self.assert_refused(FakeAdb(guard="", policy=""), "keyguard state unknown", False)

    def test_unknown_display_zero_focus_stops_send(self):
        self.assert_refused(FakeAdb(focus="  Display: mDisplayId=0\n  mCurrentFocus=null"),
                            "display 0 focus unknown", False)

    def test_unreadable_tree_stops_send(self):
        self.assert_refused(FakeAdb(tree=RuntimeError("boom")), "tree unreadable before dispatch", False)

    def test_empty_tree_stops_send(self):
        self.assert_refused(FakeAdb(tree=[]), "tree is empty before dispatch", False)

    def test_each_dispatch_tree_marker_stops_send_for_a_person(self):
        views = {"pinentry": "com.android.systemui:id/pinEntry",
                 "passwordentry": "com.android.systemui:id/passwordEntry",
                 "lockpattern": "com.android.systemui:id/lockPatternView",
                 "keyguard": "com.android.systemui:id/keyguard_status_view"}
        self.assertEqual(set(views), set(adapter._DISPATCH_TREE))
        for marker, view_id in views.items():
            with self.subTest(marker=marker):
                self.assert_refused(FakeAdb(tree=[node(view_id)]),
                                    "lock or credential view is in the tree", True)


IDLE_TREE = ("the accessibility tree is not available on this screen "
             "(uiautomator: could not get idle state). Read it from screenshot() instead.")
AM_START = "am start -n com.x/.Main"
EXPECT_HOME = {"expect": {"text_equals": "Home"}}


class LockingAdb(FakeAdb):
    """FakeAdb whose ui() becomes the harness lock refusal after ``reads`` reads."""

    def __init__(self, reads, error=LOCK_REFUSAL, **kw):
        super().__init__(**kw)
        self.reads, self.error = reads, error

    def ui(self):
        if self.reads == 0:
            raise RuntimeError(self.error)
        self.reads -= 1
        return super().ui()


class LaunchActivityGate(unittest.TestCase):
    """launch_activity: no expect still stops before ``am start`` on a lock signal."""

    def launch(self, adb, **extra):
        action = {"type": "launch_activity", "component": "com.x/.Main",
                  "step_id": "open", "report_effect": True, **extra}
        with mock.patch.object(adapter, "harness_read", side_effect=adb.harness_read):
            return adapter.run_action(action)

    def assert_refused(self, adb, reason, human=False, **extra):
        result = self.launch(adb, **extra)
        self.assertEqual((result["ok"], result["code"], result["dispatched"]), (False, "locked", False))
        self.assertIn(reason, result["reason"])
        self.assertIs(result["requires_human"], human)
        self.assertEqual([c for c in adb.commands if c.startswith("am start")], [])

    def test_keyguard_showing_stops_launch(self):
        self.assert_refused(FakeAdb(guard="    isKeyguardShowing=true"), "keyguard is showing")

    def test_unknown_keyguard_state_stops_launch(self):
        self.assert_refused(FakeAdb(guard="", policy=""), "keyguard state unknown")

    def test_unknown_display_zero_focus_stops_launch(self):
        self.assert_refused(FakeAdb(focus="  Display: mDisplayId=0\n  mCurrentFocus=null"),
                            "display 0 focus unknown")

    def test_credential_focus_stops_launch_for_a_person(self):
        window = FOCUS_WINDOWS["keyguardbouncer"]
        self.assert_refused(FakeAdb(focus="  Display: mDisplayId=0\n  mCurrentFocus=Window{3 u0 %s}" % window),
                            "credential UI is focused on display 0", True)

    def test_credential_view_in_tree_stops_launch_for_a_person(self):
        self.assert_refused(FakeAdb(tree=[node(ENTRY_IDS["id/pinentry"])]),
                            "lock or credential view is in the tree", True)

    def test_harness_lock_refusal_stops_launch_even_with_cleared_flags(self):
        self.assert_refused(LockingAdb(0), "phone is locked")

    def test_unreadable_or_empty_tree_stops_launch(self):
        self.assert_refused(FakeAdb(tree=RuntimeError("boom")), "tree unreadable before dispatch")
        self.assert_refused(FakeAdb(tree=[]), "tree is empty before dispatch")

    def test_lock_with_expect_still_refuses_before_dispatch(self):
        self.assert_refused(FakeAdb(guard="    isKeyguardShowing=true"), "keyguard is showing", **EXPECT_HOME)

    def test_web_expect_path_goes_through_the_same_gate(self):
        self.assert_refused(FakeAdb(guard="    isKeyguardShowing=true"), "keyguard is showing",
                            expect={"element_exists": {"css": "#app"}})

    def test_unlocked_launch_without_expect_dispatches_and_reports_effect(self):
        adb = FakeAdb()
        result = self.launch(adb)
        self.assertEqual((result["ok"], result["dispatched"]), (True, True))
        self.assertIn(AM_START, adb.commands)
        self.assertIn("effect", result)

    def test_unlocked_launch_with_expect_dispatches(self):
        adb = FakeAdb()
        result = self.launch(adb, **EXPECT_HOME)
        self.assertEqual((result["ok"], result["dispatched"]), (True, True))
        self.assertIn(AM_START, adb.commands)

    def test_never_idle_screen_is_not_read_as_locked(self):
        # Real compass screen (docs/device-smoke-2026-11-05-gaps.md): unlocked, tree refused.
        adb = FakeAdb(tree=RuntimeError(IDLE_TREE))
        result = self.launch(adb)
        self.assertEqual((result["ok"], result["dispatched"]), (True, True))
        self.assertIn(AM_START, adb.commands)

    def test_never_idle_screen_does_not_skip_the_flags(self):
        self.assert_refused(FakeAdb(guard="    isKeyguardShowing=true", tree=RuntimeError(IDLE_TREE)),
                            "keyguard is showing")

    def test_never_idle_screen_needs_a_named_app_on_display_zero(self):
        for window in ("NotificationShade", "StatusBar", "com.android.systemui/.keyguard.KeyguardHost"):
            with self.subTest(window=window):
                self.assert_refused(FakeAdb(focus="  Display: mDisplayId=0\n  mCurrentFocus=Window{3 u0 %s}" % window,
                                            tree=RuntimeError(IDLE_TREE)),
                                    "tree unreadable before dispatch")

    def test_share_text_still_needs_a_readable_tree(self):
        adb = FakeAdb(tree=RuntimeError(IDLE_TREE))
        with mock.patch.object(adapter, "harness_read", side_effect=adb.harness_read):
            result = adapter.run_action(dict(SHARE))
        self.assertEqual((result["code"], result["dispatched"]), ("locked", False))
        self.assertEqual(adb.sends, [])

    def test_lock_after_dispatch_stays_unknown(self):
        adb = LockingAdb(2)  # gate read, pre-action read; the expect read is refused
        result = self.launch(adb, **EXPECT_HOME)
        self.assertEqual((result["code"], result["dispatched"]), ("action_failed", True))
        self.assertIn(AM_START, adb.commands)

    def test_lock_on_the_read_after_the_gate_stops_launch(self):
        for extra in ({}, EXPECT_HOME):  # effect sample / expect's pre-action read
            with self.subTest(extra=extra):
                adb = LockingAdb(1)  # the gate's read passed
                result = self.launch(adb, **extra)
                self.assertEqual((result["ok"], result["code"], result["dispatched"]), (False, "locked", False))
                self.assertIn("phone is locked", result["reason"])
                self.assertEqual([c for c in adb.commands if c.startswith("am start")], [])

    def test_lock_on_the_effect_sample_stops_share_text(self):
        adb = LockingAdb(1)
        with mock.patch.object(adapter, "harness_read", side_effect=adb.harness_read):
            result = adapter.run_action({**SHARE, "report_effect": True})
        self.assertEqual((result["code"], result["dispatched"]), ("locked", False))
        self.assertEqual(adb.sends, [])

    def test_other_effect_sample_failure_does_not_stop_launch(self):
        adb = LockingAdb(1, error=IDLE_TREE)
        result = self.launch(adb)
        self.assertEqual((result["ok"], result["dispatched"]), (True, True))
        self.assertIn(AM_START, adb.commands)

    def test_lock_on_the_sample_after_dispatch_keeps_the_dispatch(self):
        adb = LockingAdb(2)  # gate read, before sample; the after sample is refused
        result = self.launch(adb)
        self.assertEqual((result["ok"], result["dispatched"]), (True, True))
        self.assertIn(AM_START, adb.commands)


class RunnerPreflight(unittest.TestCase):
    """Refusals that end a run before any device call."""

    def execute(self, doc):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(adapter, "harness_read") as read, \
             mock.patch.object(adapter, "run_action") as action:
            summary, results = runner._execute(Store(Path(directory)), {
                "journey_id": "j_test", "version": 1,
                "steps": [{"step_id": "s", "action": "tap", "locator": {"text": "OK"}}], **doc},
                "dev", "validate")
        read.assert_not_called()
        action.assert_not_called()
        self.assertEqual(results, [])
        return summary

    def test_non_per_step_observation_strategy_pauses_at_preflight(self):
        for strategy in ("batch", "adaptive"):
            with self.subTest(strategy=strategy):
                summary = self.execute({"observation_strategy": strategy})
                self.assertEqual(summary["status"], "paused")
                self.assertEqual(summary["failure"], {
                    "step_id": "preflight", "code": "unsupported_input",
                    "detail": "batch/adaptive strategies are not supported"})

    def test_invalid_entry_state_blocks_without_probing_the_screen(self):
        for entry, code in (({"bogus": "x"}, "unsupported_input"),
                            ({"all": []}, "invalid_request")):
            with self.subTest(entry=entry):
                summary = self.execute({"entry_state": entry})
                self.assertEqual(summary["status"], "blocked")
                self.assertEqual(summary["failure"], {"step_id": "entry", "code": code})


if __name__ == "__main__":
    unittest.main()
