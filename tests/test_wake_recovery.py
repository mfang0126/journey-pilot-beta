"""Ordinary wake+swipe must clear a non-secure keyguard without asking a person.

The regression this file pins: keyguard showing=true, secure=false, input
restricted=false; a single wake+swipe returned recorded but the next tree read
was still locked. The old wake body swiped once with no verification.
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from journey_pilot import adapter, session
from journey_pilot.execution import reservation_path
from journey_pilot.store import Store

NODES = [{"text": "Home", "desc": "", "id": "home", "class": "V",
          "clickable": True, "x": 10, "y": 20, "w": 10, "h": 10}]


def run_wake(shell, ui=None):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(adapter.build_action_script({"type": "wake", "step_id": "wake"}),
             {"shell": shell, "ui": ui or mock.Mock(return_value=NODES)})
    return json.loads(output.getvalue())


class FakeLock:
    """Small dumpsys stand-in; a swipe clears the keyguard after N swipes."""

    def __init__(self, clear_after=1, *, secure=False, bouncer=False,
                 wakefulness="Dozing", showing=True,
                 size="Physical size: 1200x2608", focus_dump=None,
                 guard_text=None, policy_text=None):
        self.clear_after = clear_after
        self.secure = secure
        self.bouncer = bouncer
        self.wakefulness = wakefulness
        self.showing = showing
        self.size = size
        self.focus_dump = focus_dump  # raw display-header grep output, if set
        self.guard_text = guard_text  # raw isKeyguardShowing grep output, if set
        self.policy_text = policy_text  # raw window policy dump, if set
        self.swipes = 0
        self.keyevents = 0
        self.commands = []

    def __call__(self, command):
        self.commands.append(command)
        if command == "wm size":
            return self.size
        if command == "dumpsys input":
            return "SurfaceOrientation: 0"
        if "mWakefulness" in command:
            return f"mWakefulness={self.wakefulness}"
        if "isKeyguardShowing" in command and self.guard_text is not None:
            return self.guard_text
        if command == "dumpsys window policy" and self.policy_text is not None:
            return self.policy_text
        if "isKeyguardShowing" in command:
            # showing=None: the dump carries no keyguard flag at all.
            return "" if self.showing is None else f"isKeyguardShowing={str(self.showing).lower()}"
        if "mCurrentFocus" in command and self.focus_dump is not None:
            return self.focus_dump
        if "mCurrentFocus" in command:
            focus = ("com.android.systemui/.keyguard.KeyguardBouncer"
                     if self.bouncer else "NotificationShade")
            return f"mCurrentFocus=Window{{1 u0 {focus}}}"
        if command == "dumpsys window policy":
            if self.showing is None:
                return ""
            return (f"mKeyguardShowing={str(self.showing).lower()} "
                    f"isKeyguardSecure={str(self.secure).lower()} "
                    f"isKeyguardInputRestricted={str(self.showing_restricted()).lower()} "
                    f"mBouncerShowing={str(self.bouncer).lower()}")
        if command.startswith("input keyevent 224"):
            self.keyevents += 1
            self.wakefulness = "Awake"
            return ""
        if command.startswith("input swipe"):
            self.swipes += 1
            if self.swipes >= self.clear_after:
                self.showing = False
            return ""
        raise AssertionError(f"unexpected shell command: {command}")

    def showing_restricted(self):
        return self.secure and self.showing


class WakeRecovery(unittest.TestCase):
    def test_retries_until_non_secure_keyguard_clears(self):
        device = FakeLock(clear_after=2)
        ui = mock.Mock(return_value=NODES)
        result = run_wake(device, ui)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["wake"]["attempts"], 2)
        self.assertEqual(result["wake"]["verified_by"], "accessibility_tree")
        self.assertEqual(device.keyevents, 1)
        self.assertEqual(device.swipes, 2)
        ui.assert_called_once()
        self.assertEqual(result["metrics"]["tree_reads"], 1)

    def test_secure_bouncer_stops_and_asks_for_a_person(self):
        device = FakeLock(clear_after=99, secure=True, bouncer=True,
                          wakefulness="Awake")
        ui = mock.Mock(return_value=NODES)
        result = run_wake(device, ui)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "locked")
        self.assertTrue(result["wake"]["requires_human"])
        self.assertTrue(result["wake"]["credential_visible"])
        self.assertIn("PIN or password", result["reason"])
        self.assertEqual(device.swipes, 3)
        ui.assert_not_called()

    def test_secure_false_is_never_reported_as_unlocked(self):
        device = FakeLock(clear_after=99, secure=False, bouncer=False,
                          wakefulness="Awake")
        ui = mock.Mock(return_value=NODES)
        result = run_wake(device, ui)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "locked")
        self.assertFalse(result["wake"]["requires_human"])
        self.assertIn("non-secure keyguard", result["reason"])
        self.assertNotIn("PIN", result["reason"])
        ui.assert_not_called()

    def test_cleared_flags_still_need_a_real_tree_read(self):
        device = FakeLock(clear_after=1, showing=False, wakefulness="Awake")
        ui = mock.Mock(side_effect=RuntimeError("uiautomator dump failed"))
        result = run_wake(device, ui)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "locked")
        self.assertEqual(result["wake"]["verified_by"], "accessibility_tree_failed")
        self.assertEqual(device.swipes, 0, "an unlocked screen is never swiped")

    def test_swipe_uses_the_last_wm_size_not_the_physical_size(self):
        device = FakeLock(clear_after=1,
                          size="Physical size: 1200x2608\nOverride size: 1080x2400")
        result = run_wake(device)
        self.assertTrue(result["ok"], result)
        swipe = next(c for c in device.commands if c.startswith("input swipe"))
        self.assertEqual(swipe, "input swipe 540 1848 540 744 250")

    def test_lock_assessment_marks_only_a_real_credential_ui_for_humans(self):
        plain = adapter.lock_assessment({
            "keyguard": {"showing": True, "secure": False,
                         "input_restricted": False, "bouncer": False},
            "foreground": "NotificationShade"})
        self.assertFalse(plain["requires_human"])
        self.assertTrue(plain["ordinary_wake_allowed"])
        self.assertEqual(plain["kind"], "nonsecure")
        secure = adapter.lock_assessment({
            "keyguard": {"showing": True, "secure": True,
                         "input_restricted": True, "bouncer": True},
            "foreground": "com.android.systemui/.keyguard.KeyguardBouncer"})
        self.assertTrue(secure["requires_human"])
        self.assertFalse(secure["ordinary_wake_allowed"])
        unlocked = adapter.lock_assessment({
            "keyguard": {"showing": False, "secure": False}, "foreground": "Home"})
        self.assertEqual(unlocked["kind"], "none")
        self.assertFalse(unlocked["requires_human"])


def locked_status(**overrides):
    status = {"bound": "dev", "devices": [{"serial": "dev"}], "ready": False,
              "foreground": "NotificationShade",
              "keyguard": {"showing": True, "secure": False,
                           "input_restricted": False, "bouncer": False},
              "reason": "The Android phone is locked"}
    status["lock"] = adapter.lock_assessment(status)
    status.update(overrides)
    return status


class SessionWake(unittest.TestCase):
    def test_session_start_records_wake_and_returns_ready(self):
        ready = {"bound": "dev", "devices": [{"serial": "dev"}], "ready": True,
                 "foreground": "com.xingin.xhs/.MainActivity",
                 "keyguard": {"showing": False, "secure": False,
                              "input_restricted": False, "bouncer": False},
                 "reason": None}
        ready["lock"] = adapter.lock_assessment(ready)
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(adapter, "status",
                               side_effect=[locked_status(), ready]) as status, \
             mock.patch.object(adapter, "run_action", return_value={
                 "ok": True, "wake": {"attempts": 2,
                                      "verified_by": "accessibility_tree",
                                      "requires_human": False}}) as action:
            store = Store(Path(tmp))
            opened = session.start(store, {"device_id": "dev", "intent": "read comments"})
            self.assertEqual(opened.status, "ok")
            self.assertTrue(opened.data["ready"])
            self.assertEqual(opened.data["wake"]["attempts"], 2)
            self.assertEqual(status.call_count, 2)
            action.assert_called_once()
            self.assertEqual(action.call_args.args[0]["type"], "wake")
            events = store.read_events(opened.run_id)
            self.assertTrue(any(e.get("event") == "action_started" and e.get("action") == "wake"
                                for e in events))
            finished = [e for e in events if e.get("event") == "action_finished"]
            self.assertEqual(finished[0]["outcome"], "ok")
            self.assertEqual(finished[0]["wake"]["verified_by"], "accessibility_tree")

    def _ready_status(self):
        ready = {"bound": "dev", "devices": [{"serial": "dev"}], "ready": True,
                 "foreground": "com.xingin.xhs/.MainActivity",
                 "keyguard": {"showing": False, "secure": False,
                              "input_restricted": False, "bouncer": False},
                 "reason": None}
        ready["lock"] = adapter.lock_assessment(ready)
        return ready

    def test_handoff_wakes_nonsecure_keyguard_with_an_expired_lease(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(adapter, "status",
                               side_effect=[self._ready_status(), locked_status()]), \
             mock.patch.object(adapter, "run_action", return_value={
                 "ok": True, "wake": {"attempts": 1,
                                      "verified_by": "accessibility_tree",
                                      "requires_human": False}}) as action, \
             mock.patch.object(adapter, "harness_read",
                               return_value={"ok": True, "nodes": NODES,
                                             "geometry": {"width": 1200, "height": 2608,
                                                          "orientation": 0}}):
            store = Store(Path(tmp))
            opened = session.start(store, {"device_id": "dev", "intent": "handoff"})
            self.assertEqual(opened.status, "ok")
            lease_path = store.locks / f"{opened.run_id}.json"
            lease = json.loads(lease_path.read_text())
            lease["expires_at"] = 0
            lease_path.write_text(json.dumps(lease), encoding="utf-8")
            result = session.handoff(store, {"run_id": opened.run_id,
                                             "lease_token": opened.data["lease_token"]})
            self.assertEqual(result.status, "recorded", result.error)
            self.assertNotEqual(result.data["lease_token"], opened.data["lease_token"])
            self.assertEqual(result.data["wake"]["attempts"], 1)
            action.assert_called_once()
            self.assertEqual(action.call_args.args[0]["type"], "wake")
            events = store.read_events(opened.run_id)
            self.assertTrue(any(e.get("event") == "action_started" and e.get("action") == "wake"
                                for e in events))
            finished = [e for e in events if e.get("event") == "action_finished"]
            self.assertEqual(finished[0]["outcome"], "ok")

    def test_handoff_does_not_wake_a_secure_keyguard(self):
        secure = locked_status(foreground="com.android.systemui/.keyguard.KeyguardBouncer")
        secure["keyguard"] = {"showing": True, "secure": True,
                              "input_restricted": True, "bouncer": True}
        secure["lock"] = adapter.lock_assessment(secure)
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(adapter, "status",
                               side_effect=[self._ready_status(), secure]), \
             mock.patch.object(adapter, "run_action") as action, \
             mock.patch.object(adapter, "harness_read") as read:
            store = Store(Path(tmp))
            opened = session.start(store, {"device_id": "dev", "intent": "handoff"})
            lease_path = store.locks / f"{opened.run_id}.json"
            lease = json.loads(lease_path.read_text())
            lease["expires_at"] = 0
            lease_path.write_text(json.dumps(lease), encoding="utf-8")
            before = lease_path.read_bytes()
            result = session.handoff(store, {"run_id": opened.run_id})
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.error["code"], "locked")
            action.assert_not_called()
            read.assert_not_called()
            self.assertEqual(lease_path.read_bytes(), before)

    def test_session_start_reports_locked_when_wake_hits_a_credential_ui(self):
        secure = locked_status(foreground="com.android.systemui/.keyguard.KeyguardBouncer")
        secure["keyguard"] = {"showing": True, "secure": True,
                              "input_restricted": True, "bouncer": True}
        secure["lock"] = adapter.lock_assessment(secure)
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(adapter, "status",
                               side_effect=[locked_status(), secure]), \
             mock.patch.object(adapter, "run_action", return_value={
                 "ok": False, "code": "locked",
                 "reason": "secure lock screen is showing after ordinary wake and swipe",
                 "wake": {"requires_human": True, "credential_visible": True}}) as action:
            store = Store(Path(tmp))
            opened = session.start(store, {"device_id": "dev", "intent": "read comments"})
            self.assertEqual(opened.status, "blocked")
            self.assertEqual(opened.error["code"], "locked")
            self.assertIn("PIN or password", opened.error["message"])
            self.assertIsNotNone(opened.run_id)
            action.assert_called_once()
            finished = [e for e in store.read_events(opened.run_id)
                        if e.get("event") == "action_finished"]
            self.assertEqual(finished[0]["outcome"], "locked")
            self.assertTrue(finished[0]["wake"]["requires_human"])
            self.assertFalse((store.locks / f"{opened.run_id}.json").exists())
            self.assertFalse(reservation_path(store, "dev").exists(),
                             "a blocked start must not keep the device reserved")


LAUNCH = {"type": "launch_activity", "component": "com.x/.Main"}
LOCK_REFUSAL = ("The Android phone is locked. Unlock it (PIN / fingerprint), "
                "then retry — I won't enter a PIN.")


def locked_phone_run(action, serial=None, reads_before_lock=0, guard=None, **kwargs):
    """Run the real action script against a phone that locked mid-session.

    Mirrors phone-harness 88b3642: ui/tap/press/scroll/type_text/open_app pass
    the lock gate and raise; shell() does not pass it. The first
    ``reads_before_lock`` ui() calls still see the unlocked screen, and dumpsys
    reports the keyguard to match unless ``guard`` overrides that line.
    """
    def refuse(*args, **kw):
        raise RuntimeError(LOCK_REFUSAL)
    reads = iter([NODES] * reads_before_lock)

    def ui():
        return next(reads, None) or refuse()
    showing = "false" if reads_before_lock else "true"

    def dumpsys(command):
        if "isKeyguardShowing" in command:
            return f"    isKeyguardShowing={showing}" if guard is None else guard
        if "mDisplayId" in command:
            return "  Display: mDisplayId=0\n  mCurrentFocus=Window{1 u0 com.x/.Main}"
        return ""
    shell = mock.Mock(side_effect=dumpsys)
    namespace = {name: refuse for name in ("tap", "press", "scroll", "type_text", "open_app")}
    namespace.update(ui=ui, shell=shell, wait_stable=lambda: None)
    output = io.StringIO()
    with contextlib.redirect_stdout(output), mock.patch("time.sleep"):
        exec(adapter.build_action_script(action), namespace)
    result = json.loads(output.getvalue())
    result["shell_calls"] = [call.args[0] for call in shell.call_args_list]
    return result


class MidSessionLock(unittest.TestCase):
    def test_lock_on_the_effect_sample_refuses_before_input(self):
        result = locked_phone_run({"type": "key", "report_effect": True})
        self.assertEqual((result["code"], result["dispatched"]), ("locked", False))

    def test_refusal_before_input_is_locked_and_not_dispatched(self):
        result = locked_phone_run({"type": "tap", "locator": {"text": "Send"}})
        self.assertEqual((result["code"], result["dispatched"]), ("locked", False))

    def test_shell_input_before_refusal_stays_unknown(self):
        # The gate and pre-input reads pass; the phone locks before the expect read.
        result = locked_phone_run({"type": "launch_activity", "component": "com.x/.Main",
                                   "expect": {"text_equals": "Home"}}, reads_before_lock=2)
        self.assertIn("am start -n com.x/.Main", result["shell_calls"])
        self.assertEqual((result["code"], result["dispatched"]), ("action_failed", True))

    def _session(self, store):
        ready = SessionWake._ready_status(self)
        with mock.patch.object(adapter, "status", return_value=ready):
            opened = session.start(store, {"device_id": "dev", "intent": "probe"})
        return {"run_id": opened.run_id, "lease_token": opened.data["lease_token"],
                "device_id": "dev"}

    def test_session_releases_the_device_after_a_pre_input_refusal(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("journey_pilot.pacing.time.sleep"), \
             mock.patch.object(adapter, "run_action", side_effect=locked_phone_run):
            store = Store(Path(tmp))
            base = self._session(store)
            refused = session.act(store, {**base, "request_id": "a1",
                                          "action": {"type": "tap", "locator": {"text": "Send"}}})
            self.assertEqual(refused.error["code"], "locked")
            self.assertEqual(store.open_actions(base["run_id"]), [])
            finished = session.finish(store, base)
            self.assertEqual(finished.data["unknown_steps"], [])
            self.assertFalse(reservation_path(store, "dev").exists())
            with mock.patch.object(adapter, "status", return_value=SessionWake._ready_status(self)):
                self.assertEqual(session.start(store, {"device_id": "dev", "intent": "next"}).status, "ok")

    def test_session_does_not_claim_nothing_ran_after_shell_input(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("journey_pilot.pacing.time.sleep"), \
             mock.patch.object(adapter, "run_action", side_effect=lambda action, serial=None, **kw:
                               locked_phone_run(action, serial, reads_before_lock=2, **kw)):
            store = Store(Path(tmp))
            base = self._session(store)
            result = session.act(store, {**base, "request_id": "a1", "action": {
                "type": "launch_activity", "component": "com.x/.Main",
                "expect": {"text_equals": "Home"}}})
            self.assertEqual(result.error["code"], "outcome_unknown")
            self.assertEqual(len(store.open_actions(base["run_id"])), 1)

    def _act_launch(self, run, action=None):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch("journey_pilot.pacing.time.sleep"), \
             mock.patch.object(adapter, "run_action", side_effect=run):
            store = Store(Path(tmp))
            base = self._session(store)
            result = session.act(store, {**base, "request_id": "a1",
                                         "action": action or LAUNCH})
            return store, base, result, store.open_actions(base["run_id"]), session.finish(store, base)

    def test_session_refuses_locked_launch_without_expect_before_dispatch(self):
        _, _, result, still_open, finished = self._act_launch(locked_phone_run)
        self.assertEqual(result.error["code"], "locked")
        self.assertEqual(result.data["detail"]["dispatched"], False)
        self.assertFalse([c for c in result.data["detail"]["shell_calls"] if c.startswith("am start")])
        self.assertEqual(still_open, [])
        self.assertEqual(finished.data["unknown_steps"], [])

    def test_session_refuses_lock_on_the_effect_sample_without_expect(self):
        _, _, result, still_open, finished = self._act_launch(
            lambda action, serial=None, **kw: locked_phone_run(action, serial, reads_before_lock=1, **kw))
        self.assertEqual((result.status, result.error["code"]), ("blocked", "locked"))
        self.assertEqual(result.data["detail"]["dispatched"], False)
        self.assertFalse([c for c in result.data["detail"]["shell_calls"] if c.startswith("am start")])
        self.assertEqual(still_open, [])
        self.assertEqual(finished.data["unknown_steps"], [])

    def test_session_records_unlocked_launch_without_expect(self):
        calls = []

        def unlocked(action, serial=None, **kw):
            calls.append(action)
            result = locked_phone_run(action, serial, reads_before_lock=99, **kw)
            self.assertIn("am start -n com.x/.Main", result["shell_calls"])
            self.assertIs(result["dispatched"], True)
            return result
        _, _, result, still_open, _ = self._act_launch(unlocked)
        self.assertEqual(result.status, "recorded")
        self.assertIs(calls[0]["report_effect"], True)
        self.assertEqual(still_open, [])

    def _validate_launch(self, run, expect=None):
        from journey_pilot import journey, runner
        step = {"step_id": "open", "action": "launch_activity", "component": "com.x/.Main"}
        if expect:
            step["expect"] = expect
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(adapter, "status", return_value={"ready": True, "bound": "dev", "devices": [{}]}), \
             mock.patch.object(adapter, "harness_read", return_value={"ok": True, "nodes": NODES}), \
             mock.patch("journey_pilot.pacing.before_action", return_value={}), \
             mock.patch.object(adapter, "run_action", side_effect=run):
            store = Store(Path(tmp))
            proposed = journey.propose(store, {"task_key": "lock-launch", "intent": "test",
                "steps": [step], "postconditions": {"text_equals": "Home"}})
            result = runner.validate(store, {"journey_id": proposed.data["journey_id"], "request_id": "r1"})
            finished = [e for e in store.read_events(result.run_id)
                        if e.get("event") == "action_finished"]
            return result, finished, store.open_actions(result.run_id), reservation_path(store, "dev").exists()

    def test_journey_refuses_locked_launch_without_expect_before_dispatch(self):
        shell_calls = []

        def locked(action, serial=None, **kw):
            result = locked_phone_run(action, serial, **kw)
            shell_calls.extend(result["shell_calls"])
            return result
        result, finished, still_open, reserved = self._validate_launch(locked)
        self.assertEqual(result.error["code"], "locked")
        self.assertEqual([(e["outcome"], e["dispatched"]) for e in finished], [("locked", False)])
        self.assertFalse([c for c in shell_calls if c.startswith("am start")])
        self.assertEqual(still_open, [])
        self.assertFalse(reserved)

    def test_journey_dispatches_unlocked_launch_without_expect(self):
        result, finished, still_open, reserved = self._validate_launch(
            lambda action, serial=None, **kw: locked_phone_run(action, serial, reads_before_lock=99, **kw))
        self.assertIsNone(result.error)
        self.assertEqual([(e["outcome"], e["dispatched"]) for e in finished], [("ok", True)])
        self.assertEqual(still_open, [])
        self.assertFalse(reserved)

    def test_journey_keeps_unknown_when_the_lock_lands_after_dispatch(self):
        result, finished, still_open, reserved = self._validate_launch(
            lambda action, serial=None, **kw: locked_phone_run(action, serial, reads_before_lock=2, **kw),
            expect={"text_equals": "Home"})
        self.assertEqual(result.error["code"], "outcome_unknown")
        self.assertEqual([(e["outcome"], e["dispatched"]) for e in finished], [("action_failed", True)])
        self.assertEqual(len(still_open), 1)
        self.assertTrue(reserved)

    def test_journey_releases_the_device_after_a_pre_input_refusal(self):
        from journey_pilot import journey, runner
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(adapter, "status", return_value={"ready": True, "bound": "dev", "devices": [{}]}), \
             mock.patch.object(adapter, "harness_read", return_value={"ok": True, "nodes": NODES}), \
             mock.patch("journey_pilot.pacing.before_action", return_value={}), \
             mock.patch.object(adapter, "run_action", side_effect=locked_phone_run):
            store = Store(Path(tmp))
            proposed = journey.propose(store, {"task_key": "lock-test", "intent": "test",
                "steps": [{"step_id": "open", "action": "tap", "locator": {"text": "Home"}}],
                "postconditions": {"text_equals": "Home"}})
            result = runner.validate(store, {"journey_id": proposed.data["journey_id"], "request_id": "r1"})
            self.assertEqual(result.error["code"], "locked")
            self.assertEqual(store.open_actions(result.run_id), [])
            self.assertFalse(reservation_path(store, "dev").exists())
            finished = [e for e in store.read_events(result.run_id)
                        if e.get("event") == "action_finished"]
            self.assertEqual([e["dispatched"] for e in finished], [False])


def dual_focus(main):
    """Xiaomi dual screen: the rear display (1) is listed first with a null focus."""
    return ("  Display: mDisplayId=1 rootTasks=[] info=976x596\n  mCurrentFocus=null\n"
            "  Display: mDisplayId=0 rootTasks=[] info=1200x2608\n"
            f"  mCurrentFocus={main}")


class DualDisplayLock(unittest.TestCase):
    """The wake path reads display 0's focus, not the rear display's null."""

    def test_wake_sees_a_credential_window_on_display_zero(self):
        device = FakeLock(clear_after=99, secure=True, bouncer=False, wakefulness="Awake",
                          focus_dump=dual_focus(
                              "Window{1 u0 com.android.systemui/.keyguard.KeyguardBouncer}"))
        result = run_wake(device)
        self.assertEqual(result["code"], "locked")
        self.assertTrue(result["wake"]["credential_visible"])
        self.assertIn("KeyguardBouncer", result["wake"]["focus"])
        self.assertTrue(any("Display: mDisplayId=" in c for c in device.commands))

    def test_cleared_flags_with_null_main_focus_are_not_unlocked(self):
        rear = dual_focus("null").replace(
            "mCurrentFocus=null", "mCurrentFocus=Window{9 u0 com.miui.rear/.Clock}", 1)
        for dump in (dual_focus("null"), rear, ""):
            device = FakeLock(showing=False, wakefulness="Awake", focus_dump=dump)
            ui = mock.Mock(return_value=NODES)
            result = run_wake(device, ui)
            self.assertFalse(result["ok"], dump)
            self.assertEqual(result["code"], "locked")
            self.assertEqual(result["wake"]["verified_by"], "display0_focus_unknown")
            self.assertFalse(result["wake"]["focus_known"])
            self.assertEqual(device.swipes, 0, "an unknown screen is never swiped")
            ui.assert_not_called()

    def test_cleared_flags_with_a_main_window_still_unlock(self):
        device = FakeLock(showing=False, wakefulness="Awake",
                          focus_dump=dual_focus("Window{1 u0 com.miui.home/.launcher.Launcher}"))
        result = run_wake(device)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["wake"]["verified_by"], "accessibility_tree")

    def test_host_focus_reads_display_zero(self):
        cases = {
            dual_focus("Window{1 u0 com.android.systemui/.keyguard.KeyguardBouncer}"):
                "com.android.systemui/.keyguard.KeyguardBouncer",
            dual_focus("null").replace(
                "mCurrentFocus=null", "mCurrentFocus=Window{9 u0 com.miui.rear/.Clock}", 1): None,
            "": None,
        }
        for dump, want in cases.items():
            with mock.patch.object(adapter, "_adb", return_value=dump) as adb:
                self.assertEqual(adapter._focus("dev"), want, dump)
            self.assertEqual(adb.call_args.args, ("dev", "shell", adapter.FOCUS_GREP))

    def test_unknown_main_focus_is_not_an_unlocked_assessment(self):
        for focus in (None, ""):
            lock = adapter.lock_assessment({
                "keyguard": {"showing": False, "secure": False}, "foreground": focus})
            self.assertEqual(lock["kind"], "unknown")
            self.assertFalse(lock["ordinary_wake_allowed"])


BOUNCER = "Window{1 u0 com.android.systemui/.keyguard.KeyguardBouncer}"
PIN_TREE = [{"text": "Enter PIN", "desc": "", "id": "com.android.systemui:id/pinEntry",
             "class": "android.widget.EditText", "clickable": True,
             "x": 600, "y": 1300, "w": 800, "h": 120}]


class CredentialOverridesFlags(unittest.TestCase):
    """Codex #13 P1: a credential signal or unknown flags never become wake success."""

    def test_display_zero_bouncer_beats_global_showing_false(self):
        device = FakeLock(showing=False, wakefulness="Awake", focus_dump=dual_focus(BOUNCER))
        ui = mock.Mock(return_value=PIN_TREE)
        result = run_wake(device, ui)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "locked")
        self.assertEqual(result["wake"]["verified_by"], "credential_signal")
        self.assertTrue(result["wake"]["requires_human"])
        self.assertTrue(result["wake"]["credential_visible"])
        self.assertIn("PIN or password", result["reason"])
        self.assertEqual(device.swipes, 0)
        ui.assert_not_called()

    def test_bouncer_flag_beats_global_showing_false(self):
        device = FakeLock(showing=False, bouncer=True, wakefulness="Awake")
        result = run_wake(device, mock.Mock(return_value=PIN_TREE))
        self.assertEqual((result["ok"], result["wake"]["verified_by"]), (False, "credential_signal"))

    def test_readable_pin_tree_is_not_unlocked(self):
        device = FakeLock(showing=False, wakefulness="Awake",
                          focus_dump=dual_focus("Window{1 u0 NotificationShade}"))
        result = run_wake(device, mock.Mock(return_value=PIN_TREE))
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "locked")
        self.assertEqual(result["wake"]["verified_by"], "credential_tree")
        self.assertTrue(result["wake"]["requires_human"])

    def test_missing_flags_with_known_focus_and_empty_tree_are_unknown(self):
        for tree in ([], NODES):
            device = FakeLock(clear_after=99, showing=None, wakefulness="Awake",
                              focus_dump=dual_focus("Window{1 u0 com.miui.home/.launcher.Launcher}"))
            ui = mock.Mock(return_value=tree)
            result = run_wake(device, ui)
            self.assertFalse(result["ok"], tree)
            self.assertEqual(result["code"], "locked")
            self.assertIsNone(result["wake"]["keyguard_showing"])
            self.assertEqual(result["wake"]["verified_by"], "keyguard_state_unknown")
            ui.assert_not_called()

    def test_cleared_flags_with_an_empty_tree_are_not_unlocked(self):
        device = FakeLock(showing=False, wakefulness="Awake",
                          focus_dump=dual_focus("Window{1 u0 com.miui.home/.launcher.Launcher}"))
        result = run_wake(device, mock.Mock(return_value=[]))
        self.assertFalse(result["ok"])
        self.assertEqual(result["wake"]["verified_by"], "accessibility_tree_failed")

    def test_host_credential_focus_beats_showing_false(self):
        lock = adapter.lock_assessment({
            "keyguard": {"showing": False, "secure": False, "bouncer": False},
            "foreground": "com.android.systemui/.keyguard.KeyguardBouncer"})
        self.assertTrue(lock["requires_human"])
        self.assertTrue(lock["credential_visible"])
        self.assertFalse(lock["ordinary_wake_allowed"])
        self.assertEqual(lock["kind"], "secure")

    def test_status_is_not_ready_on_a_readable_credential_screen(self):
        screen = {"size": None, "wakefulness": "Awake",
                  "keyguard": {"showing": False, "secure": False, "bouncer": False},
                  "foreground": "com.android.systemui/.keyguard.KeyguardBouncer"}
        with mock.patch.object(adapter, "list_devices", return_value=[
                {"serial": "dev", "state": "device", "model": "m"}]), \
             mock.patch.object(adapter, "_screen", return_value=screen), \
             mock.patch.object(adapter, "harness_read",
                               return_value={"ready": True, "nodes": 1}) as probe:
            status = adapter.status("dev")
        self.assertFalse(status["ready"])
        self.assertTrue(status["lock"]["requires_human"])
        probe.assert_not_called()

    def test_session_start_blocks_on_a_locked_wake_even_if_a_probe_reads(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(adapter, "status",
                               side_effect=[locked_status(), SessionWake._ready_status(self)]), \
             mock.patch.object(adapter, "run_action", return_value={
                 "ok": False, "code": "locked",
                 "reason": "keyguard state could not be read after ordinary wake and swipe",
                 "wake": {"verified_by": "keyguard_state_unknown", "requires_human": False}}):
            store = Store(Path(tmp))
            opened = session.start(store, {"device_id": "dev", "intent": "read comments"})
            self.assertEqual(opened.status, "blocked")
            self.assertEqual(opened.error["code"], "locked")
            self.assertIn("could not be read", opened.error["message"])
            self.assertFalse(reservation_path(store, "dev").exists())


LAUNCHER = "Window{1 u0 com.miui.home/.launcher.Launcher}"


class StrictKeyguardFlags(unittest.TestCase):
    """Codex #13 round 2 P1: only a complete true/false is a keyguard value."""

    def test_flag_parser_accepts_only_true_or_false(self):
        cases = {"showing=true": True, "showing=false": False, "showing=TRUE": True,
                 "showing=false\n": False, "showing=unknown": None, "showing=": None,
                 "showing=tru": None, "showing=fals": None, "showing: true": None,
                 "showing=true1": None, "showing=null": None, "": None}
        for text, want in cases.items():
            self.assertIs(adapter._flag(text, "showing"), want, text)

    def test_odd_guard_values_with_a_main_window_and_tree_do_not_unlock(self):
        for guard in ("isKeyguardShowing=unknown", "isKeyguardShowing=",
                      "isKeyguardShowing: true", "isKeyguardShowing=fals"):
            device = FakeLock(clear_after=99, wakefulness="Awake", guard_text=guard,
                              policy_text="mKeyguardShowing=false", focus_dump=dual_focus(LAUNCHER))
            ui = mock.Mock(return_value=NODES)
            result = run_wake(device, ui)
            self.assertFalse(result["ok"], guard)
            self.assertEqual(result["code"], "locked", guard)
            self.assertIsNone(result["wake"]["keyguard_showing"], guard)
            self.assertEqual(result["wake"]["verified_by"], "keyguard_state_unknown", guard)
            ui.assert_not_called()

    def test_odd_policy_values_without_a_guard_line_do_not_unlock(self):
        for policy in ("mKeyguardShowing=unknown", "mKeyguardShowing=", "mKeyguardShowing=tru"):
            device = FakeLock(clear_after=99, wakefulness="Awake", guard_text="",
                              policy_text=policy, focus_dump=dual_focus(LAUNCHER))
            result = run_wake(device, mock.Mock(return_value=NODES))
            self.assertEqual((result["ok"], result["wake"]["verified_by"]),
                             (False, "keyguard_state_unknown"), policy)

    def test_explicit_true_and_false_still_work(self):
        cleared = FakeLock(wakefulness="Awake", guard_text="isKeyguardShowing=false",
                           focus_dump=dual_focus(LAUNCHER))
        self.assertTrue(run_wake(cleared)["ok"])
        stuck = FakeLock(clear_after=99, wakefulness="Awake", guard_text="isKeyguardShowing=true",
                         focus_dump=dual_focus(LAUNCHER))
        result = run_wake(stuck)
        self.assertEqual((result["ok"], result["wake"]["verified_by"]), (False, "raw_keyguard_state"))
        self.assertEqual(stuck.swipes, 3)

    def test_host_odd_policy_values_are_unknown(self):
        for value in ("unknown", "", "tru"):
            def adb(serial, *args, **kw):
                return f"showing={value} secure=false" if args[-1] == "policy" else ""
            with mock.patch.object(adapter, "_adb", side_effect=adb):
                screen = adapter._screen("dev")
            self.assertIsNone(screen["keyguard"]["showing"], value)
            self.assertEqual(adapter.lock_assessment(screen)["kind"], "unknown")


class UnknownWakeKeepsTheDevice(unittest.TestCase):
    """Codex #13 round 2 P1: a wake with unknown effect is never released."""

    def test_failed_wake_then_ready_keeps_reservation_and_blocks_a_new_start(self):
        ready = SessionWake._ready_status(self)
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(adapter, "status", side_effect=[locked_status(), ready, ready]), \
             mock.patch.object(adapter, "run_action", return_value={
                 "ok": False, "code": "action_failed", "reason": "harness timed out"}):
            store = Store(Path(tmp))
            opened = session.start(store, {"device_id": "dev", "intent": "read comments"})
            self.assertEqual(opened.error["code"], "outcome_unknown")
            self.assertEqual(opened.data["unknown_steps"], ["wake"])
            self.assertEqual([a["step_id"] for a in store.open_actions(opened.run_id)], ["wake"])
            self.assertEqual(json.loads(reservation_path(store, "dev").read_text())["run_id"],
                             opened.run_id)
            self.assertTrue((store.locks / f"{opened.run_id}.json").exists())
            events = store.read_events(opened.run_id)
            self.assertFalse([e for e in events if e.get("event") == "session_finished"],
                             "no unknown_steps=[] close for an unknown wake")
            second = session.start(store, {"device_id": "dev", "intent": "next"})
            self.assertEqual(second.error["code"], "device_busy")
            finished = session.finish(store, {"run_id": opened.run_id,
                                              "lease_token": opened.data["lease_token"]})
            self.assertEqual(finished.data["unknown_steps"], ["wake"])


if __name__ == "__main__":
    unittest.main()
