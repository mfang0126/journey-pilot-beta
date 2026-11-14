"""expect_gone / expect_app local waits, and limit notices that count only when new.

Generated harness code runs against scripted screens; no device access.
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from journey_pilot import adapter, journey, session
from journey_pilot.journey import JourneyError
from journey_pilot.store import Store

FAST = {"settle_ms": 0, "poll_ms": 400}
BUTTON = {"text": "提交", "x": 10, "y": 20}


def focus(package):
    return f"  mCurrentFocus=Window{{1a2b3c u0 {package}/{package}.Main}}"


def execute(action, screens, foreground="com.example.app"):
    """Each ui() call returns the next screen; the last one repeats."""
    calls = []

    def ui():
        calls.append(1)
        return screens[min(len(calls), len(screens)) - 1]

    def shell(cmd):
        return focus(foreground() if callable(foreground) else foreground)

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(adapter.build_action_script({**FAST, **action}),
             {"ui": ui, "tap": lambda x, y: None, "shell": shell, "press": lambda key: None,
              "wait_stable": lambda: None, "type_text": lambda text: None})
    return json.loads(output.getvalue())


def tap(**extra):
    return {"type": "tap", "locator": {"text": "提交"}, **extra}


class Validation(unittest.TestCase):
    def step(self, **extra):
        return journey.validate_steps([{"step_id": "s", "action": "key", **extra}])[0]

    def test_accepts_new_fields(self):
        step = self.step(expect_gone={"element_exists": {"text": "加载中"}}, expect_app="com.example.app")
        self.assertEqual(step["expect_app"], "com.example.app")
        self.step(expect_gone={"text_equals": "加载中"})

    def test_rejects_bad_values(self):
        for extra in ({"expect_app": "not a package"}, {"expect_app": ""}, {"expect_app": 3},
                      {"expect_gone": {}}, {"expect_gone": {"element_count": {"text": "x"}}},
                      {"expect_gone": {"text_equals": {"css": "#a", "text": "x"}}}):
            with self.subTest(extra=extra), self.assertRaises(JourneyError):
                self.step(**extra)

    def test_web_steps_cannot_use_native_waits(self):
        with self.assertRaises(JourneyError):
            journey.validate_steps([{"step_id": "s", "action": "tap", "locator": {"css": "#go"},
                                     "expect_app": "com.android.chrome"}])
        with self.assertRaises(JourneyError):
            self.step(expect={"element_exists": {"css": "#done"}}, expect_gone={"text_equals": "x"})

    def test_build_rejects_before_any_device_code(self):
        with self.assertRaises(JourneyError):
            adapter.build_action_script({"type": "key", "expect_app": "bad pkg"})


class ExpectGone(unittest.TestCase):
    def test_waits_until_text_disappears(self):
        spinner = {"text": "加载中"}
        result = execute(tap(expect_gone={"element_exists": {"text": "加载中"}}),
                         [[BUTTON, spinner], [BUTTON, spinner], [BUTTON]])
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["settle_escalated"], 1)

    def test_still_present_is_assertion_failed(self):
        result = execute(tap(expect_gone={"text_equals": "加载中"}), [[BUTTON, {"text": "加载中"}]])
        self.assertEqual(result["code"], "assertion_failed")
        self.assertEqual(result["unmet"], ["expect_gone"])

    def test_combined_with_expect_reports_each_unmet(self):
        result = execute(tap(expect={"text_equals": "完成"}, expect_gone={"text_equals": "加载中"}),
                         [[BUTTON, {"text": "加载中"}]])
        self.assertEqual(result["unmet"], ["expect", "expect_gone"])


class ExpectApp(unittest.TestCase):
    def test_foreground_package_matches(self):
        apps = iter(["com.launcher", "com.example.app"])
        current = {"pkg": "com.launcher"}

        def foreground():
            current["pkg"] = next(apps, current["pkg"])
            return current["pkg"]

        result = execute({"type": "key", "key": "home", "expect_app": "com.example.app"},
                         [[BUTTON]], foreground)
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["settle_escalated"], 1)

    def test_other_foreground_is_assertion_failed(self):
        result = execute({"type": "key", "key": "home", "expect_app": "com.example.app"},
                         [[BUTTON]], "com.other")
        self.assertEqual(result["code"], "assertion_failed")
        self.assertEqual(result["unmet"], ["expect_app"])

    def test_package_prefix_is_not_a_match(self):
        result = execute({"type": "key", "expect_app": "com.example"}, [[BUTTON]], "com.example.app")
        self.assertEqual(result["unmet"], ["expect_app"])


class LimitNotice(unittest.TestCase):
    def test_new_limit_notice_after_action_blocks(self):
        result = execute(tap(expect={"text_equals": "完成"}),
                         [[BUTTON], [BUTTON, {"text": "今日次数已用完，请明天再来"}]])
        self.assertEqual(result["code"], "platform_blocked")
        self.assertIs(result["limit_notice"], True)
        self.assertNotIn("locked", result["reason"].lower())  # session maps "locked" reasons

    def test_notice_already_on_screen_is_not_blocked(self):
        label = {"text": "每日上传已达上限"}
        result = execute(tap(expect={"text_equals": "完成"}),
                         [[BUTTON, label], [BUTTON, label, {"text": "完成"}]])
        self.assertTrue(result["ok"])
        self.assertNotIn("limit_notice", result)

    def test_notice_before_action_does_not_stop_the_tap(self):
        tapped = mock.Mock()
        screens = [[BUTTON, {"text": "Daily limit reached"}]]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exec(adapter.build_action_script(tap()), {"ui": lambda: screens[0], "tap": tapped})
        self.assertTrue(json.loads(output.getvalue())["ok"])
        tapped.assert_called_once_with(10, 20)

    def test_without_locator_takes_a_before_snapshot(self):
        label = {"text": "额度已用完"}
        result = execute({"type": "key", "key": "back", "expect_gone": {"text_equals": "加载中"}},
                         [[label], [label]])
        self.assertTrue(result["ok"])
        self.assertEqual(result["metrics"]["tree_reads"], 2)
        result = execute({"type": "key", "key": "back", "expect_gone": {"text_equals": "加载中"}},
                         [[], [label]])
        self.assertTrue(result.get("limit_notice"))

    def test_verification_still_blocks_even_if_seen_before(self):
        captcha = {"text": "请完成安全验证"}
        result = execute({"type": "key", "expect": {"text_equals": "完成"}}, [[captcha], [captcha]])
        self.assertEqual(result["code"], "platform_blocked")
        self.assertNotIn("limit_notice", result)

    def test_text_the_action_typed_is_not_a_limit_notice(self):
        # Issue #16: the edited field echoes the typed text as a new node.
        field = {"id": "android:id/input", "class": "android.widget.EditText", "text": "", "x": 5, "y": 5}
        for method in ("adb", "paste"):
            with self.subTest(method=method):
                typed = {**field, "text": " quota exceeded"}
                result = execute({"type": "input_text", "method": method, "text": " quota exceeded",
                                  "locator": {"resource_id": "android:id/input"},
                                  "expect": {"text_equals": "quota exceeded"}},
                                 [[field], [typed]])
                self.assertTrue(result["ok"], result)
                self.assertNotIn("limit_notice", result)

    def test_typed_text_also_ignored_when_echoed_elsewhere_or_appended(self):
        # input_text replaces the field (#15), so the appended echo sits in another row.
        field = {"id": "q", "text": "note:", "x": 5, "y": 5}
        result = execute({"type": "input_text", "text": "daily limit", "locator": {"resource_id": "q"},
                          "expect_gone": {"text_equals": "加载中"}},
                         [[field], [{**field, "text": "daily limit"}, {"text": "note:daily limit"},
                                    {"text": "Search for \"daily limit\""}]])
        self.assertTrue(result["ok"], result)

    def test_real_notice_after_typing_still_blocks(self):
        field = {"id": "q", "text": "", "x": 5, "y": 5}
        result = execute({"type": "input_text", "text": "daily limit", "locator": {"resource_id": "q"},
                          "expect_gone": {"text_equals": "加载中"}},
                         [[field], [{**field, "text": "daily limit"}, {"text": "今日次数已用完"}]])
        self.assertEqual(result["code"], "platform_blocked")
        self.assertIs(result["limit_notice"], True)

    def test_typing_part_of_a_notice_does_not_hide_it(self):
        # Codex review on PR #19: removing the typed substring broke real notices.
        field = {"id": "q", "text": "", "x": 5, "y": 5}
        for method, typed, notice in (("adb", "a", "quota exceeded"),
                                      ("adb", "limit", "daily limit"),
                                      ("adb", "Daily", "Daily limit reached"),
                                      ("paste", "次", "今日次数已用完")):
            with self.subTest(typed=typed, notice=notice):
                result = execute({"type": "input_text", "method": method, "text": typed,
                                  "locator": {"resource_id": "q"}, "expect_app": "com.example.app"},
                                 [[field], [{**field, "text": typed}, {"text": notice}]])
                self.assertEqual(result["code"], "platform_blocked", result)
                self.assertIs(result["limit_notice"], True)

    def test_typed_echo_next_to_a_notice_in_one_node_still_blocks(self):
        field = {"id": "q", "text": "", "x": 5, "y": 5}
        result = execute({"type": "input_text", "text": "daily limit", "locator": {"resource_id": "q"},
                          "expect_app": "com.example.app"},
                         [[field], [{**field, "text": "daily limit"},
                                    {"text": "daily limit: quota exceeded"}]])
        self.assertIs(result.get("limit_notice"), True, result)

    def test_typed_text_does_not_hide_other_actions_notices(self):
        # Only input_text / share_text carry typed text into the check.
        result = execute(tap(text="quota exceeded", expect={"text_equals": "完成"}),
                         [[BUTTON], [BUTTON, {"text": "quota exceeded"}]])
        self.assertIs(result.get("limit_notice"), True)

    def test_pre_existing_notice_stays_ignored_while_typing(self):
        label = {"text": "Daily limit reached"}
        field = {"id": "q", "text": "", "x": 5, "y": 5}
        result = execute({"type": "input_text", "text": "hello", "locator": {"resource_id": "q"},
                          "expect_gone": {"text_equals": "加载中"}},
                         [[field, label], [{**field, "text": "hello"}, label]])
        self.assertTrue(result["ok"], result)

    def test_plain_success_keeps_old_result_shape(self):
        result = execute(tap(expect={"text_equals": "完成"}), [[BUTTON], [{"text": "完成"}]])
        self.assertEqual(set(result), {"ok", "action", "value", "settle_escalated", "metrics",
                                       "locator_index", "dispatched"})


class SessionCheckEvent(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(Path(tmp.name))
        patch = mock.patch.object(adapter, "status", return_value={
            "bound": "dev", "devices": [{"serial": "dev"}], "ready": True})
        patch.start()
        self.addCleanup(patch.stop)
        opened = session.start(self.store, {"device_id": "dev", "intent": "test"})
        self.run_id = opened.run_id
        self.base = {"run_id": opened.run_id, "lease_token": opened.data["lease_token"],
                     "device_id": "dev", "request_id": "r1"}

    def act(self, result):
        action = {"type": "key", "key": "home", "expect_app": "com.example.app"}
        with mock.patch.object(adapter, "run_action", return_value=result):
            return session.act(self.store, {**self.base, "action": action})

    def checks(self):
        return [e["ok"] for e in self.store.read_events(self.run_id) if e.get("event") == "check"]

    def test_expect_app_alone_records_a_check(self):
        self.assertEqual(self.act({"ok": True}).status, "recorded")
        self.assertEqual(self.checks(), [True])

    def test_unmet_and_limit_notice_reach_the_caller(self):
        out = self.act({"ok": False, "code": "assertion_failed", "unmet": ["expect_app"]})
        self.assertEqual(out.error["code"], "assertion_failed")
        self.assertEqual(out.data["detail"]["unmet"], ["expect_app"])
        self.assertEqual(self.checks(), [False])

    def test_limit_notice_pauses_as_platform_blocked(self):
        out = self.act({"ok": False, "code": "platform_blocked", "limit_notice": True,
                        "reason": "a limit notice appeared after the action"})
        self.assertEqual(out.error["code"], "platform_blocked")
        self.assertTrue(out.data["detail"]["limit_notice"])

    def blocked_by(self):
        return [(e["event"], e.get("blocked_by")) for e in self.store.read_events(self.run_id)
                if e.get("event") == "platform_blocked" or e.get("outcome") == "platform_blocked"]

    def test_trace_records_limit_notice_as_the_stop_reason(self):
        self.act({"ok": False, "code": "platform_blocked", "limit_notice": True,
                  "reason": "a limit notice appeared after the action"})
        self.assertEqual(self.blocked_by(), [("platform_blocked", "limit_notice"),
                                             ("action_finished", "limit_notice")])

    def test_trace_records_verification_block_as_the_stop_reason(self):
        self.act({"ok": False, "code": "platform_blocked"})
        self.assertEqual(self.blocked_by(), [("platform_blocked", "verification_block"),
                                             ("action_finished", "verification_block")])

    def test_observe_block_is_a_verification_block(self):
        tree = {"ok": True, "nodes": [{"text": "请完成安全验证"}]}
        with mock.patch.object(adapter, "harness_read", return_value=tree):
            out = session.observe(self.store, {**self.base, "source": "tree"})
        self.assertEqual(out.error["code"], "platform_blocked")
        self.assertEqual(self.blocked_by(), [("platform_blocked", "verification_block")])

    def test_other_failures_carry_no_stop_reason(self):
        self.act({"ok": False, "code": "assertion_failed", "unmet": ["expect_app"]})
        finished = [e for e in self.store.read_events(self.run_id) if e["event"] == "action_finished"]
        self.assertNotIn("blocked_by", finished[0])

    def test_stop_reason_is_on_the_privacy_allowlist(self):
        from journey_pilot.privacy import event_record
        self.assertEqual(event_record({"event": "platform_blocked", "blocked_by": "limit_notice",
                                       "reason": "secret"}),
                         {"event": "platform_blocked", "blocked_by": "limit_notice"})


class WakeTemplate(unittest.TestCase):
    """wake runs its own template; waits it never evaluates must not pass as checks."""

    def test_wake_cannot_report_successful_check_without_evaluating_native_waits(self):
        # Codex offline counterexample on PR #7 (comment 6650010051).
        from test_wake_recovery import FakeLock
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            with mock.patch.object(adapter, "status", return_value={
                    "bound": "dev", "devices": [{"serial": "dev"}], "ready": True}):
                opened = session.start(store, {"device_id": "dev", "intent": "offline review"})
            action = {"type": "wake", "expect_app": "com.expected.app",
                      "expect_gone": {"text_equals": "Loading"}, "settle_ms": 0, "poll_ms": 0}
            device = FakeLock(showing=False, wakefulness="Awake")

            def harness(script, **kwargs):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    exec(script, {"shell": device, "ui": lambda: [{"text": "Loading"}]})
                return json.loads(output.getvalue())

            with mock.patch.object(adapter, "harness_read", side_effect=harness), \
                 mock.patch("journey_pilot.pacing.before_action", return_value={}):
                result = session.act(store, {"run_id": opened.run_id,
                                             "lease_token": opened.data["lease_token"],
                                             "device_id": "dev", "request_id": "wake-waits",
                                             "action": action})
            checks = [e["ok"] for e in store.read_events(opened.run_id) if e["event"] == "check"]
        self.assertNotEqual(result.status, "recorded")
        self.assertEqual(result.error["code"], "invalid_request")
        self.assertEqual(checks, [])

    def test_validation_rejects_new_waits_on_wake(self):
        for extra in ({"expect_gone": {"text_equals": "Loading"}}, {"expect_app": "com.expected.app"}):
            with self.subTest(extra=extra), self.assertRaises(JourneyError):
                journey.validate_steps([{"step_id": "s", "action": "wake", **extra}])

    def test_wake_expect_is_never_recorded_as_a_check(self):
        # Pre-existing gap: WAKE_TEMPLATE ignores expect too, so ok proves nothing about it.
        self.assertIsNone(adapter.expect_check("s", {"type": "wake", "expect": {"text_equals": "x"}},
                                               {"ok": True}))


class WithActionEffect(unittest.TestCase):
    """Merged with #5: report_effect reuses the wait's before/after reads."""

    def test_effect_and_expect_gone_share_tree_reads(self):
        spinner = {"text": "加载中"}
        result = execute({"type": "key", "key": "back", "report_effect": True,
                          "expect_gone": {"text_equals": "加载中"}}, [[spinner], []])
        self.assertTrue(result["ok"])
        self.assertEqual(result["metrics"]["tree_reads"], 2)
        self.assertEqual(result["_effect_input"]["before"], [spinner])
        self.assertEqual(result["_effect_input"]["after"], [])

    def test_limit_notice_still_blocks_with_effect(self):
        result = execute(tap(report_effect=True, expect_gone={"text_equals": "加载中"}),
                         [[BUTTON], [BUTTON, {"text": "Daily limit reached"}]])
        self.assertEqual(result["code"], "platform_blocked")
        self.assertIs(result["limit_notice"], True)
        self.assertEqual(result["metrics"]["tree_reads"], 2)


class FixedSegment(unittest.TestCase):
    def test_exit_stays_the_only_checkpoint(self):
        from journey_pilot.fixed import validate_fixed_steps
        with self.assertRaises(JourneyError):
            validate_fixed_steps([{"step_id": "s", "action": "tap_xy", "x": 1, "y": 1,
                                   "expect_app": "com.android.settings",
                                   "fixed_segment": {"entry": {"text_equals": "a"},
                                                     "exit": {"text_equals": "b"},
                                                     "geometry": {"width": 10, "height": 10, "orientation": 0},
                                                     "app": "com.android.settings",
                                                     "target": {"text": "a"}}}])


if __name__ == "__main__":
    unittest.main()
