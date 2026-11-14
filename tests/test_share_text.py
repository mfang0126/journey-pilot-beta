"""share_text: one quoted ACTION_SEND intent, never the clipboard, only under a one-time grant."""
from __future__ import annotations

import contextlib
import io
import json
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from journey_pilot import adapter, session
from journey_pilot.journey import JourneyError
from journey_pilot.store import Store

PACKAGE = "com.example.chat"
# Chinese, full-width punctuation, quotes, shell metacharacters and a newline.
TEXT = "离线测试：它's \"引号\" $HOME `id` ; rm -rf / && echo 完\n第二行 *"
SHARE = {"step_id": "share", "type": "share_text", "package": PACKAGE, "text": TEXT,
         "effect_class": "external_mutation"}


UNLOCKED_GUARD = "    isKeyguardShowing=false"
APP_FOCUS = ("  Display: mDisplayId=0\n  mCurrentFocus=Window{1 u0 com.example.home/.Main}\n"
             "  Display: mDisplayId=1\n  mCurrentFocus=Window{2 u0 com.miui.rear/.Clock}")
BOUNCER_FOCUS = ("  Display: mDisplayId=1\n  mCurrentFocus=Window{2 u0 com.miui.rear/.Clock}\n"
                 "  Display: mDisplayId=0\n"
                 "  mCurrentFocus=Window{3 u0 com.android.systemui/.keyguard.KeyguardBouncer}")
HOME_TREE = [{"id": "com.example.home:id/title", "text": "Home", "x": 1, "y": 1}]


class FakeAdb:
    """Runs the generated script; ``shell`` records the device command line."""

    def __init__(self, am_output="Starting: Intent { act=android.intent.action.SEND }",
                 guard=UNLOCKED_GUARD, policy="", focus=APP_FOCUS, tree=HOME_TREE):
        self.commands, self.am_output = [], am_output
        self.guard, self.policy, self.focus, self.tree = guard, policy, focus, tree

    def shell(self, cmd):
        self.commands.append(cmd)
        if cmd.startswith("am "):
            return self.am_output
        if "isKeyguardShowing" in cmd:
            return self.guard
        if cmd == "dumpsys window policy":
            return self.policy
        if "mDisplayId" in cmd:
            return self.focus
        return ""

    def ui(self):
        if isinstance(self.tree, Exception):
            raise self.tree
        return [dict(n) for n in self.tree]

    @property
    def sends(self):
        return [c for c in self.commands if c.startswith("am start -a android.intent.action.SEND")]

    def harness_read(self, script, serial=None, timeout=60.0):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("time.sleep"):
            exec(compile(script, "<action-script>", "exec"),
                 {"ui": self.ui, "tap": None, "shell": self.shell, "press": None})
        return json.loads(out.getvalue().strip().splitlines()[-1])


class ShareTextScript(unittest.TestCase):
    def run_share(self, adb, action=SHARE):
        with mock.patch.object(adapter, "harness_read", side_effect=adb.harness_read):
            return adapter.run_action(dict(action))

    def test_device_shell_sees_one_literal_text_extra_and_no_clipboard(self):
        adb = FakeAdb()
        result = self.run_share(adb)
        self.assertTrue(result["ok"])
        am = [c for c in adb.commands if c.startswith("am ")]
        self.assertEqual(len(am), 1)
        # adb shell hands the string to the device sh; POSIX splitting must restore TEXT exactly.
        self.assertEqual(shlex.split(am[0]), [
            "am", "start", "-a", "android.intent.action.SEND", "-t", "text/plain",
            "--es", "android.intent.extra.TEXT", TEXT, "-p", PACKAGE])
        script = adapter.build_action_script(dict(SHARE))
        self.assertNotIn("离线测试", script, "the text travels as escapes inside one literal")
        for cmd in adb.commands:
            self.assertNotIn("clip", cmd.lower())
            self.assertFalse(cmd.startswith("input"), "no paste keyevent, no adb typing")

    def test_locked_unknown_or_credential_never_sends(self):
        cases = {
            "keyguard showing": FakeAdb(guard="    isKeyguardShowing=true"),
            "flags missing": FakeAdb(guard="", policy=""),
            "policy says showing": FakeAdb(guard="", policy="  mKeyguardShowing=true"),
            # Present but unparsable values are unknown, never unlocked.
            "guard value unknown": FakeAdb(guard="    isKeyguardShowing=unknown"),
            "guard value empty": FakeAdb(guard="    isKeyguardShowing="),
            "guard value 1": FakeAdb(guard="    isKeyguardShowing=1"),
            "guard value falsey": FakeAdb(guard="    isKeyguardShowing=falsey"),
            "policy value unknown": FakeAdb(guard="", policy="  mKeyguardShowing=unknown"),
            "policy value 1": FakeAdb(guard="", policy="  mKeyguardShowing=1"),
            "policy value empty": FakeAdb(guard="", policy="  mKeyguardShowing="),
            "policy showing= truncated": FakeAdb(guard="", policy="  mShowing=0 showing="),
            # A display-0 credential window overrides the global showing=false.
            "display-0 bouncer, global false": FakeAdb(focus=BOUNCER_FOCUS),
            "display-0 focus null": FakeAdb(focus="  Display: mDisplayId=0\n  mCurrentFocus=null\n"
                                                  "  Display: mDisplayId=1\n"
                                                  "  mCurrentFocus=Window{2 u0 com.miui.rear/.Clock}"),
            "no display-0 entry": FakeAdb(focus="  Display: mDisplayId=1\n"
                                                "  mCurrentFocus=Window{2 u0 com.miui.rear/.Clock}"),
            "ui says locked": FakeAdb(tree=RuntimeError("The Android phone is locked; unlock it")),
            "ui unknown": FakeAdb(tree=RuntimeError("lock state unknown; tree unavailable")),
            "empty tree": FakeAdb(tree=[]),
            "pin entry in tree": FakeAdb(tree=[{"id": "com.android.systemui:id/pinEntry",
                                                "text": "Enter PIN"}]),
        }
        for name, adb in cases.items():
            for report_effect in (True, False):
                with self.subTest(case=name, report_effect=report_effect):
                    adb.commands.clear()
                    result = self.run_share(adb, {**SHARE, "report_effect": report_effect})
                    self.assertEqual(adb.sends, [], "SEND must not be dispatched")
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["code"], "locked")
                    self.assertFalse(any(c.startswith("input") for c in adb.commands),
                                     "no wake, swipe or PIN typing")
        self.assertTrue(self.run_share(FakeAdb(focus=BOUNCER_FOCUS))["requires_human"])
        self.assertTrue(self.run_share(cases["pin entry in tree"])["requires_human"])

    def test_keyguard_container_in_tree_blocks_send_despite_global_unlock(self):
        # PR #14 review on 883788f: global showing=false, display 0 on the
        # NotificationShade, and only a keyguard container (no entry view) in the
        # tree used to dispatch SEND.
        shade = "  Display: mDisplayId=0\n  mCurrentFocus=Window{1 u0 NotificationShade}"
        for view in ("keyguard_pin_view", "keyguard_password_view", "keyguard_status_view"):
            with self.subTest(view=view):
                adb = FakeAdb(guard="isKeyguardShowing=false", focus=shade,
                              tree=[{"id": "com.android.systemui:id/" + view,
                                     "text": "Enter PIN", "x": 600, "y": 1300}])
                result = self.run_share(adb)
                self.assertEqual(adb.sends, [])
                self.assertFalse(result["ok"])
                self.assertEqual(result["code"], "locked")
                self.assertFalse(result.get("dispatched"))

    def test_explicit_unlock_from_policy_flags_still_sends(self):
        for policy in ("  mKeyguardShowing=false", "  mKeyguardShowing=false mOccluded=false",
                       "  showing=false"):
            with self.subTest(policy=policy):
                adb = FakeAdb(guard="", policy=policy)
                self.assertTrue(self.run_share(adb, {**SHARE, "report_effect": True})["ok"])
                self.assertEqual(len(adb.sends), 1)

    def test_am_resolution_error_is_not_ok(self):
        adb = FakeAdb("Error: Activity not started, unable to resolve Intent")
        result = self.run_share(adb)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "action_failed")

    def test_invalid_share_text_is_rejected_before_any_script(self):
        for bad in ({"package": "not a package"}, {"package": "com.x;rm"}, {"text": ""},
                    {"text": "  "}, {"text": "x" * 4001}, {"text": "a\0b"}, {"text": 5},
                    {"effect_class": "navigation"}):
            with self.subTest(bad=bad), self.assertRaises(JourneyError) as caught:
                adapter.build_action_script({**SHARE, **bad})
            self.assertEqual(caught.exception.code, "invalid_request")
        without_class = {k: v for k, v in SHARE.items() if k != "effect_class"}
        with self.assertRaises(JourneyError):
            adapter.build_action_script(without_class)


class ShareTextAuthorization(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))
        self.read = mock.patch.object(adapter, "harness_read",
                                      return_value={"ok": True, "nodes": [{"text": "x"}]})
        patches = [
            mock.patch.object(adapter, "status", return_value={
                "bound": "dev", "devices": [{"serial": "dev"}], "ready": True}),
            self.read,
            mock.patch("journey_pilot.pacing.before_action", return_value={}),
        ]
        self.mocks = [patch.start() for patch in patches]
        for patch in patches:
            self.addCleanup(patch.stop)
        opened = session.start(self.store, {"device_id": "dev", "intent": "share offline test"})
        self.assertEqual(opened.status, "ok")
        self.base = {"run_id": opened.run_id,
                     "lease_token": opened.data["lease_token"], "device_id": "dev"}

    def authorize(self, **overrides):
        return session.authorize(self.store, {**self.base, "action": SHARE, "text": TEXT,
                                              **overrides})

    def act(self, request_id, action=SHARE, **extra):
        return session.act(self.store, {**self.base, "request_id": request_id,
                                        "action": action, **extra})

    def test_default_denied_without_a_grant(self):
        with mock.patch.object(adapter, "run_action") as dispatch:
            plain = self.act("s1")
            legacy = self.act("s2", authorization={"approved": True})
            unknown = self.act("s3", authorization={"grant_id": "g_0000000000000000", "text": TEXT})
        for result in (plain, legacy, unknown):
            self.assertEqual(result.error["code"], "needs_authorization")
        dispatch.assert_not_called()

    def test_authorize_binds_exact_text_without_post_or_screen_read(self):
        reads = self.mocks[1].call_count
        self.assertEqual(self.authorize(text=TEXT + "改").error["code"], "invalid_request")
        self.assertEqual(self.authorize(post={"title": "t", "author": "a"}).error["code"],
                         "invalid_request")
        self.assertEqual(self.authorize(action={**SHARE, "effect_class": "navigation"}).error["code"],
                         "invalid_request")
        grant = self.authorize()
        self.assertEqual(grant.status, "recorded")
        self.assertEqual(self.mocks[1].call_count, reads, "share grant reads nothing from the device")

    def test_matching_grant_sends_exactly_once_and_keeps_text_out_of_the_store(self):
        grant_id = self.authorize().data["grant_id"]
        auth = {"authorization": {"grant_id": grant_id, "text": TEXT}}
        with mock.patch.object(adapter, "run_action", return_value={"ok": True}) as dispatch:
            other_package = self.act("pkg", {**SHARE, "package": "com.example.other"}, **auth)
            other_text = self.act("txt", {**SHARE, "text": TEXT + "改"}, **auth)
            wrong_auth_text = self.act("auth", authorization={"grant_id": grant_id, "text": "别的"})
            for result in (other_package, other_text, wrong_auth_text):
                self.assertEqual(result.error["code"], "needs_authorization")
            dispatch.assert_not_called()

            sent = self.act("send", **auth)
            self.assertEqual(sent.status, "recorded")
            self.assertEqual(dispatch.call_count, 1)
            self.assertEqual(dispatch.call_args.args[0]["package"], PACKAGE)

            replay = self.act("again", **auth)
            self.assertEqual(replay.error["code"], "needs_authorization")
            self.assertEqual(dispatch.call_count, 1, "a grant is one-time")
        stored = "\n".join(p.read_text() for p in Path(self.tmp.name).rglob("*.json*"))
        self.assertNotIn("离线测试", stored)
        self.assertIn(PACKAGE, stored, "the target package is recorded as provenance")

    def test_phone_locks_after_a_normal_start_and_grant(self):
        grant_id = self.authorize().data["grant_id"]
        auth = {"authorization": {"grant_id": grant_id, "text": TEXT}}
        adb = FakeAdb(guard="    isKeyguardShowing=true",
                      tree=RuntimeError("The Android phone is locked; unlock it"))
        with mock.patch.object(adapter, "harness_read", side_effect=adb.harness_read):
            locked = self.act("send-locked", **auth)
        self.assertEqual(locked.error["code"], "locked")
        self.assertEqual(adb.sends, [])
        self.assertEqual(self.store.open_actions(self.base["run_id"]), [],
                         "a gate refusal is known-not-dispatched, not outcome_unknown")
        # The grant was burned before the attempt; a retry needs a fresh grant.
        with mock.patch.object(adapter, "run_action") as dispatch:
            self.assertEqual(self.act("send-retry", **auth).error["code"], "needs_authorization")
        dispatch.assert_not_called()

    def test_share_grant_and_tap_grant_do_not_cross(self):
        grant_id = self.authorize().data["grant_id"]
        auth = {"authorization": {"grant_id": grant_id, "text": TEXT}}
        typing = {"step_id": "t", "type": "input_text", "method": "paste", "text": TEXT,
                  "effect_class": "local_input", "locator": {"class": "android.widget.EditText"}}
        tap = {"step_id": "send", "type": "tap", "locator": {"text": "发送"},
               "effect_class": "external_mutation"}
        with mock.patch.object(adapter, "run_action", return_value={"ok": True}) as dispatch:
            self.assertEqual(self.act("type", typing, **auth).error["code"], "needs_authorization")
            self.assertEqual(self.act("tap", tap, **auth).error["code"], "needs_authorization")
        dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
