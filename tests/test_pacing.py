"""Pacing tests: shared state, fake clock, budgets, cancellation.

No test sleeps for real, touches a device, or installs anything.  The fake
sleep advances the fake clock, so the assertions are about pacing decisions,
not wall-clock timing.
"""
from __future__ import annotations

import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from journey_pilot import adapter, pacing, runner, session

from journey_pilot.pacing import (
    MAX_ACTIONS,
    MAX_NETWORK,
    MAX_SCROLL,
    NETWORK_INTERVAL,
    NORMAL_INTERVAL,
    PacingError,
    before_action,
    package_config_path,
    remaining_delay,
    state_path,
)
from journey_pilot.privacy import event_record
from journey_pilot.store import Store, atomic_write_json, read_json


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _child_action(root, queue, now):
    """Run one action in a separate process, as another CLI would."""
    state = {"now": now}

    def clock():
        return state["now"]

    def sleep(seconds):
        state["now"] += seconds

    try:
        result = before_action(Store(Path(root)), "dev", "run-1", {"type": "tap"},
                               clock=clock, sleep=sleep)
        queue.put(result)
    except PacingError as exc:
        queue.put({"error": exc.code})


class PacingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))
        self.clock = FakeClock(100.0)

    def act(self, action=None, run_id="run-1", clock=None, cancelled=None, foreground=None):
        clock = clock or self.clock
        kwargs = {"clock": clock.clock, "sleep": clock.sleep}
        if cancelled is not None:
            kwargs["cancelled"] = cancelled
        if foreground is not None:
            kwargs["foreground"] = lambda: foreground
        return before_action(self.store, "dev", run_id, action or {"type": "tap"}, **kwargs)

    def test_default_navigation_interval_and_elapsed_deduction(self):
        first = self.act()
        self.assertEqual(first["waited"], 0.0)
        self.assertEqual(first["interval"], NORMAL_INTERVAL)
        self.assertEqual(first["counters"], {"actions": 1, "network": 0, "scroll": 0})

        self.clock.now += 1.0
        second = self.act()
        self.assertEqual(second["waited"], 2.0)
        self.assertEqual(second["started_at"], 103.0)
        self.assertEqual(second["counters"]["actions"], 2)

    def test_declared_network_action_uses_network_interval(self):
        self.act({"type": "tap", "traffic_class": "search"})
        out = self.act({"type": "tap", "traffic_class": "refresh"})
        self.assertEqual(out["waited"], NETWORK_INTERVAL)
        self.assertTrue(out["network"])
        self.assertEqual(out["counters"]["network"], 2)

    def test_read_observation_is_not_paced_or_counted(self):
        self.act()
        self.clock.now += 1.0
        observed = self.act({"type": "read_field", "traffic_class": "read"})
        self.assertEqual(observed["waited"], 0.0)
        self.assertFalse(observed["counted"])
        self.assertEqual(observed["counters"]["actions"], 1)

        # The observation did not reset the last action start.
        after = self.act()
        self.assertEqual(after["waited"], 2.0)

    def test_unknown_traffic_class_is_rejected(self):
        with self.assertRaises(PacingError) as caught:
            self.act({"type": "tap", "traffic_class": "video"})
        self.assertEqual(caught.exception.code, "invalid_traffic_class")

    def test_read_class_is_only_valid_for_read_field(self):
        for kind in ("tap", "tap_xy", "key", "scroll", "input_text"):
            with self.assertRaises(PacingError) as caught:
                self.act({"type": kind, "traffic_class": "read"})
            self.assertEqual(caught.exception.code, "invalid_traffic_class")
            self.assertEqual(caught.exception.details["action_type"], kind)

    def test_non_finite_last_started_is_rejected(self):
        path = state_path(self.store, "dev")
        for value in (float("nan"), float("inf"), float("-inf")):
            atomic_write_json(path, {"version": 1, "last_started_at": value, "runs": {}})
            with self.assertRaises(PacingError) as caught:
                self.act()
            self.assertEqual(caught.exception.code, "pacing_state_error")

    def test_unsupported_state_version_is_rejected(self):
        atomic_write_json(state_path(self.store, "dev"),
                          {"version": 99, "last_started_at": None, "runs": {}})
        with self.assertRaises(PacingError) as caught:
            self.act()
        self.assertEqual(caught.exception.code, "pacing_state_error")

    def test_malformed_counters_are_rejected(self):
        for bad in ("1", -1, True):
            atomic_write_json(state_path(self.store, "dev"), {
                "version": 1, "last_started_at": None,
                "runs": {"run-1": {"actions": bad, "network": 0, "scroll": 0}},
            })
            with self.assertRaises(PacingError) as caught:
                self.act()
            self.assertEqual(caught.exception.code, "pacing_state_error")

    def test_unknown_action_type_cannot_bypass_budget(self):
        for _ in range(MAX_ACTIONS):
            self.act()
        with self.assertRaises(PacingError) as caught:
            self.act({"type": "teleport", "traffic_class": "read"})
        self.assertEqual(caught.exception.code, "invalid_action")

        with self.assertRaises(PacingError) as caught:
            self.act()
        self.assertEqual(caught.exception.code, "budget_exhausted")
        self.assertEqual(caught.exception.details["budget"], "actions")

    def test_network_budget_stops_run(self):
        for _ in range(MAX_NETWORK):
            self.act({"type": "tap", "traffic_class": "page"})
        with self.assertRaises(PacingError) as caught:
            self.act({"type": "tap", "traffic_class": "page"})
        self.assertEqual(caught.exception.code, "budget_exhausted")
        self.assertEqual(caught.exception.details["budget"], "network")

    def test_scroll_budget_stops_run(self):
        for _ in range(MAX_SCROLL):
            self.act({"type": "scroll"})
        with self.assertRaises(PacingError) as caught:
            self.act({"type": "scroll"})
        self.assertEqual(caught.exception.code, "budget_exhausted")
        self.assertEqual(caught.exception.details["budget"], "scroll")

    def test_counters_are_per_run(self):
        for _ in range(MAX_NETWORK):
            self.act({"type": "tap", "traffic_class": "search"}, run_id="run-a")
        out = self.act({"type": "tap", "traffic_class": "search"}, run_id="run-b")
        self.assertEqual(out["counters"]["network"], 1)
        self.assertEqual(out["counters"]["actions"], 1)

    def test_cancellation_is_checked_during_wait_without_consuming_budget(self):
        self.act()
        cancelled = {"value": False}
        self.clock.now += 0.4
        cancelled["value"] = True

        with self.assertRaises(PacingError) as caught:
            self.act(cancelled=lambda: cancelled["value"])
        self.assertEqual(caught.exception.code, "cancelled")

        state = read_json(state_path(self.store, "dev"))
        self.assertEqual(state["runs"]["run-1"]["actions"], 1)

    def test_cancel_poll_never_exceeds_two_tenths(self):
        self.act()
        self.clock.sleeps.clear()
        cancelled = {"value": False}

        def cancel_after_two_polls():
            return len(self.clock.sleeps) >= 2

        with self.assertRaises(PacingError):
            self.act(cancelled=cancel_after_two_polls)
        self.assertTrue(self.clock.sleeps)
        self.assertLessEqual(max(self.clock.sleeps), 0.2)

    def test_state_is_durable_before_the_action(self):
        self.act()
        state = read_json(state_path(self.store, "dev"))
        self.assertEqual(state["last_started_at"], 100.0)
        self.assertEqual(state["runs"]["run-1"], {"actions": 1, "network": 0, "scroll": 0})

    def test_remaining_delay_probe_is_read_only(self):
        self.act()
        self.clock.now += 1.0
        self.assertEqual(remaining_delay(self.store, "dev", {"type": "tap"},
                                         clock=self.clock.clock), 2.0)
        self.assertEqual(remaining_delay(self.store, "dev",
                                         {"type": "read_field", "traffic_class": "read"},
                                         clock=self.clock.clock), 0.0)
        state = read_json(state_path(self.store, "dev"))
        self.assertEqual(state["last_started_at"], 100.0)

    def test_shared_state_is_visible_to_another_process(self):
        self.act()
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        child = ctx.Process(target=_child_action, args=(str(self.store.root), queue, 101.0))
        child.start()
        try:
            out = queue.get(timeout=10)
            child.join(10)
            self.assertEqual(child.exitcode, 0)
        finally:
            if child.is_alive():
                child.terminate()
                child.join()
            queue.close()
        self.assertEqual(out["waited"], 2.0)
        self.assertEqual(out["counters"]["actions"], 2)

    def test_malformed_action_is_rejected(self):
        with self.assertRaises(PacingError) as caught:
            self.act({"traffic_class": "navigation"})
        self.assertEqual(caught.exception.code, "invalid_action")


class PackageIntervalTests(unittest.TestCase):
    setUp = PacingTests.setUp
    act = PacingTests.act

    def configure(self, payload):
        atomic_write_json(package_config_path(self.store), payload)

    def test_builtin_default_does_not_probe_foreground(self):
        def probe():
            raise AssertionError("foreground must not be read without overrides")
        before_action(self.store, "dev", "run-1", {"type": "tap"},
                      clock=self.clock.clock, sleep=self.clock.sleep, foreground=probe)
        self.clock.now += 1.0
        out = before_action(self.store, "dev", "run-1", {"type": "tap"},
                            clock=self.clock.clock, sleep=self.clock.sleep, foreground=probe)
        self.assertEqual(out["waited"], 2.0)
        self.assertIsNone(out["package"])

    def test_override_spaces_actions_in_the_same_package(self):
        self.configure({"packages": {"com.example.app": 8}})
        self.act(foreground="com.example.app/.Main")
        self.clock.now += 1.0
        out = self.act(foreground="com.example.app/.Detail")
        self.assertEqual(out["waited"], 7.0)
        self.assertEqual(out["package"], "com.example.app")
        self.assertEqual(out["package_interval"], 8.0)
        self.assertEqual(read_json(state_path(self.store, "dev"))["package_started_at"],
                         {"com.example.app": 108.0})

    def test_other_package_keeps_its_own_cooldown(self):
        self.configure({"packages": {"com.example.app": 8}})
        self.act(foreground="com.example.app")
        self.clock.now += 1.0
        # Unlisted package: only the device-wide class interval applies.
        other = self.act(foreground="com.other")
        self.assertEqual(other["waited"], 2.0)
        self.assertEqual(other["package_interval"], NORMAL_INTERVAL)
        # Back in the overridden app: 8 s since its own last action, not the device's.
        back = self.act(foreground="com.example.app")
        self.assertEqual(back["waited"], 5.0)

    def test_configured_default_applies_to_unlisted_packages(self):
        self.configure({"default": 6})
        self.act(foreground="com.any")
        out = self.act(foreground="com.any")
        self.assertEqual(out["waited"], 6.0)

    def test_network_interval_still_wins_when_longer(self):
        self.configure({"packages": {"com.example.app": 4}})
        self.act({"type": "tap", "traffic_class": "search"}, foreground="com.example.app")
        out = self.act({"type": "tap", "traffic_class": "page"}, foreground="com.example.app")
        self.assertEqual(out["waited"], NETWORK_INTERVAL)

    def test_unknown_foreground_uses_the_largest_interval(self):
        self.configure({"default": 4, "packages": {"com.slow": 10, "com.fast": 1}})
        self.act(foreground="com.fast")
        out = before_action(self.store, "dev", "run-1", {"type": "tap"},
                            clock=self.clock.clock, sleep=self.clock.sleep,
                            foreground=lambda: None)
        self.assertEqual(out["waited"], 10.0)
        self.assertIsNone(out["package"])

    def test_invalid_config_fails_closed_without_consuming_budget(self):
        for bad in ({"packages": {"com.x": -1}}, {"default": float("nan")},
                    {"packages": {"com.x": True}}, {"packages": ["com.x"]},
                    {"default": 3, "extra": 1}):
            self.configure(bad)
            with self.assertRaises(PacingError) as caught:
                self.act(foreground="com.x")
            self.assertEqual(caught.exception.code, "pacing_config_error")
        self.assertIsNone(read_json(state_path(self.store, "dev")))

    def test_unreadable_config_or_empty_package_name_fails_closed(self):
        package_config_path(self.store).write_text("{not json", encoding="utf-8")
        with self.assertRaises(PacingError) as caught:
            self.act(foreground="com.x")
        self.assertEqual(caught.exception.code, "pacing_config_error")
        self.configure({"packages": {"": 3}})
        with self.assertRaises(PacingError) as caught:
            self.act(foreground="com.x")
        self.assertEqual(caught.exception.code, "pacing_config_error")
        self.assertIsNone(read_json(state_path(self.store, "dev")))

    def test_malformed_package_started_at_fails_closed(self):
        self.configure({"packages": {"com.x": 30}})
        state = {"version": 1, "last_started_at": None, "runs": {}, "package_started_at": ["com.x"]}
        atomic_write_json(state_path(self.store, "dev"), state)
        with self.assertRaises(PacingError) as caught:
            self.act(foreground="com.x")
        self.assertEqual(caught.exception.code, "pacing_state_error")
        self.assertEqual(read_json(state_path(self.store, "dev")), state)
        self.assertEqual(self.clock.sleeps, [])

    def test_package_fields_survive_event_privacy_filter(self):
        self.configure({"packages": {"com.example.app": 8}})
        pace = self.act(foreground="com.example.app")
        record = event_record({"event": "action_started", "pacing": pace})
        self.assertEqual(record["pacing"]["package"], "com.example.app")
        self.assertEqual(record["pacing"]["package_interval"], 8.0)


class PackageIntervalBoundaryTests(unittest.TestCase):
    """Codex PR #8 review counterexamples, kept as regressions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))
        atomic_write_json(package_config_path(self.store), {"packages": {"com.slow": 10}})
        self.clock = FakeClock(100.0)

    def reserve(self, foreground, sleep=None):
        return before_action(self.store, "dev", "run-1", {"type": "key"},
                             clock=self.clock.clock, sleep=sleep or self.clock.sleep,
                             foreground=foreground)

    def test_foreground_switch_during_wait_rechecks_package(self):
        self.reserve(lambda: "com.slow")
        self.clock.now = 101.0
        current = ["com.fast"]

        def sleep(seconds):
            self.clock.sleep(seconds)
            if self.clock.now >= 102:
                current[0] = "com.slow"

        out = self.reserve(lambda: current[0], sleep=sleep)
        self.assertEqual(out["package"], "com.slow")
        self.assertGreaterEqual(out["started_at"], 110.0)

    def test_unknown_foreground_action_constrains_next_known_package(self):
        self.reserve(lambda: "com.slow")
        self.clock.now = 101.0
        unknown = self.reserve(lambda: None)  # device stays in com.slow; probe fails
        self.clock.now = unknown["started_at"] + 1
        known = self.reserve(lambda: "com.slow")
        self.assertGreaterEqual(known["started_at"] - unknown["started_at"], 10.0)

    def test_popup_focus_title_is_not_a_package(self):
        for focus in ("PopupWindow:abc", "StatusBar", "NotificationShade"):
            with mock.patch.object(adapter, "_adb",
                                   return_value=f"mCurrentFocus=Window{{abc u0 {focus}}}"):
                self.assertIsNone(adapter.foreground_package("dev"), focus)
        with mock.patch.object(adapter, "_adb", return_value=(
                "mCurrentFocus=Window{abc u0 com.slow/com.slow.MainActivity}")):
            self.assertEqual(adapter.foreground_package("dev"), "com.slow")

    def test_remaining_delay_includes_package_cooldown(self):
        self.reserve(lambda: "com.slow")
        self.clock.now = 101.0
        forecast = remaining_delay(self.store, "dev", {"type": "tap_xy"},
                                   clock=self.clock.clock, foreground=lambda: "com.slow")
        out = self.reserve(lambda: "com.slow")
        self.assertEqual(forecast, 9.0)
        self.assertEqual(out["waited"], forecast)

    def test_expired_coordinate_tap_is_not_starved_by_package_cooldown(self):
        clock = self.clock
        real_before = pacing.before_action

        def timed_before(*args, **kwargs):
            return real_before(*args, **kwargs, clock=clock.clock, sleep=clock.sleep)

        geometry = {"width": 1000, "height": 2000, "orientation": 0}
        raw = {"ok": True, "nodes": [{"text": "Target", "x": 100, "y": 200}],
               "geometry": geometry}
        attempts = []
        with mock.patch.object(session, "_now", side_effect=clock.clock), \
             mock.patch.object(adapter, "status", return_value={
                 "bound": "dev", "devices": [{"serial": "dev"}], "ready": True}), \
             mock.patch.object(adapter, "foreground_package", return_value="com.slow"), \
             mock.patch.object(adapter, "harness_read", return_value=raw), \
             mock.patch.object(adapter, "run_action", return_value={"ok": True}) as dispatch, \
             mock.patch.object(pacing, "before_action", side_effect=timed_before):
            opened = session.start(self.store, {"device_id": "dev", "intent": "offline review"})
            base = {"run_id": opened.run_id, "lease_token": opened.data["lease_token"],
                    "device_id": "dev"}
            seed = session.act(self.store, {**base, "request_id": "seed",
                                            "action": {"type": "key", "key": "back"}})
            self.assertEqual(seed.status, "recorded")
            clock.now += 1
            for i in range(4):
                observed = session.observe(self.store, base)
                action = {"type": "tap_xy", "x": 100, "y": 200,
                          "observation_id": observed.data["observation_id"]}
                result = session.act(self.store, {**base, "request_id": f"tap-{i}",
                                                  "action": action})
                attempts.append((result.status, (result.error or {}).get("code")))
            taps = [c for c in dispatch.call_args_list if c.args[0]["type"] == "tap_xy"]
        state = read_json(state_path(self.store, "dev"))
        # Each refusal leaves the cooldown it sat out in place, so the next
        # fresh observation goes through instead of re-expiring forever.
        blocked, recorded = ("blocked", "precondition_mismatch"), ("recorded", None)
        self.assertEqual(attempts, [blocked, recorded, blocked, recorded])
        self.assertEqual(len(taps), 2)
        # Seed + two dispatched taps; expired attempts consumed no budget.
        self.assertEqual(state["runs"][opened.run_id]["actions"], 3)

    def test_cancel_during_foreground_probe_prevents_dispatch_and_reservation(self):
        # Codex PR #8 re-review: a zero remaining wait must still re-check
        # cancellation after the last foreground probe, before reserving.
        with mock.patch.object(adapter, "status", return_value={
                "bound": "dev", "devices": [{"serial": "dev"}], "ready": True}):
            opened = session.start(self.store, {"device_id": "dev", "intent": "cancel probe"})
        base = {"run_id": opened.run_id, "lease_token": opened.data["lease_token"],
                "device_id": "dev"}

        def foreground(serial):
            self.assertEqual(runner.cancel(self.store, {"run_id": opened.run_id}).status, "ok")
            return "com.slow"

        with mock.patch.object(adapter, "foreground_package", side_effect=foreground), \
             mock.patch.object(adapter, "run_action", return_value={"ok": True}) as dispatch:
            out = session.act(self.store, {**base, "request_id": "first",
                                           "action": {"type": "key", "key": "back"}})
        state = read_json(state_path(self.store, "dev")) or {}
        self.assertEqual((out.error or {}).get("code"), "cancelled")
        dispatch.assert_not_called()
        self.assertEqual(state.get("runs", {}).get(opened.run_id, {}).get("actions", 0), 0)

    def test_task_tap_sits_out_long_cooldown_before_final_observation(self):
        import time
        from journey_pilot.task_runtime import TaskContext
        ctx = TaskContext(self.store, {}, {})
        ctx.base = {"device_id": "dev"}
        calls = []

        def observe(source="tree"):
            calls.append("observe")
            ctx.nodes, ctx.observation_id = [{"text": "T", "x": 1, "y": 2}], "o"
            ctx.at = time.monotonic()

        ctx.observe = observe
        ctx._dispatch = lambda kind, **args: calls.append(kind)
        with mock.patch("journey_pilot.pacing.remaining_delay", return_value=9.0) as forecast, \
             mock.patch("journey_pilot.task_runtime.time.sleep",
                        side_effect=lambda s: calls.append(("sleep", s))):
            ctx.tap(lambda n: n["text"] == "T")
        # A cooldown longer than freshness is waited out before the coordinates are read.
        self.assertEqual(calls, ["observe", ("sleep", 9.0), "observe", "tap_xy"])
        self.assertIsNotNone(forecast.call_args.kwargs["foreground"])


if __name__ == "__main__":
    unittest.main()
