"""act reports what the input visibly did: no_effect / done / place_changed, plus tree_blind."""
import contextlib
import io
import json
from unittest import mock

from journey_pilot import adapter, session
from journey_pilot.effect import action_effect, classify, tree_blind
from journey_pilot.store import Store

ROOT = {"class": "android.widget.FrameLayout", "id": "", "text": "", "desc": "",
        "clickable": False, "x": 540, "y": 1200, "w": 1080, "h": 2400}
BUTTON = {"class": "android.widget.Button", "id": "app:id/go", "text": "Go", "desc": "",
          "clickable": True, "x": 540, "y": 300, "w": 200, "h": 100}
LIST = [{"class": "android.widget.TextView", "id": "app:id/row", "text": f"row {i}", "desc": "",
         "clickable": False, "x": 540, "y": 600 + 100 * i, "w": 1080, "h": 100} for i in range(4)]
HOME = [ROOT, BUTTON, *LIST]
FOCUS_HOME = "mCurrentFocus=Window{1 u0 com.example/com.example.HomeActivity}"
FOCUS_DETAIL = "mCurrentFocus=Window{2 u0 com.example/com.example.DetailActivity}"


def test_identical_tree_and_window_is_no_effect():
    assert classify(HOME, [dict(n) for n in HOME], FOCUS_HOME, FOCUS_HOME) == "no_effect"
    assert classify(HOME, list(HOME)) == "no_effect"  # window unreadable on both sides


def test_local_change_on_same_screen_is_done():
    toggled = [ROOT, {**BUTTON, "text": "Stop"}, *LIST]
    assert classify(HOME, toggled, FOCUS_HOME, FOCUS_HOME) == "done"
    # Same tree, focus moved to a popup with no package/activity: something happened.
    assert classify(HOME, HOME, FOCUS_HOME, "mCurrentFocus=Window{3 u0 PopupWindow:1}") == "done"


def test_new_activity_or_new_structure_is_place_changed():
    assert classify(HOME, HOME, FOCUS_HOME, FOCUS_DETAIL) == "place_changed"
    detail = [ROOT] + [{"class": "android.widget.ImageView", "id": f"app:id/pic{i}", "x": 1, "y": 1}
                       for i in range(5)]
    assert classify(HOME, detail, FOCUS_HOME, FOCUS_HOME) == "place_changed"
    # A window change wins even when a tree side could not be read.
    assert classify(None, None, FOCUS_HOME, FOCUS_DETAIL) == "place_changed"


def test_unreadable_tree_is_unknown_not_no_effect():
    assert classify(None, HOME, FOCUS_HOME, FOCUS_HOME) is None
    assert classify(HOME, None) is None


def test_tree_blind_only_when_no_labelled_or_clickable_node_covers_point():
    assert tree_blind(HOME, (540, 2000)) is True       # only the bare root layout
    assert tree_blind(HOME, (560, 310)) is False       # inside the button
    assert tree_blind(HOME, (100, 700)) is False       # labelled row
    assert tree_blind(None, (540, 300)) is True        # the tree could not be read at all
    assert tree_blind(HOME, None) is False             # key / scroll / launch touch no point
    assert action_effect(HOME, HOME, point=(540, 2000)) == {"effect": "no_effect", "tree_blind": True}


class FakePhone:
    """Runs the generated action script; ``tap`` moves to ``screens[1]`` if it hits a node."""

    def __init__(self, screens, focuses, tree_fails=False):
        self.screens, self.focuses, self.at, self.tree_fails = screens, focuses, 0, tree_fails
        self.taps, self.tree_reads = [], 0

    def ui(self):
        self.tree_reads += 1
        if self.tree_fails:
            raise RuntimeError("uiautomator dump failed: could not get idle state")
        return [dict(n) for n in self.screens[self.at]]

    def tap(self, x, y):
        self.taps.append((x, y))
        if not tree_blind(self.screens[0], (x, y)):
            self.at = min(1, len(self.screens) - 1)

    def shell(self, cmd):
        return self.focuses[self.at] if "mCurrentFocus" in cmd else ""

    def harness_read(self, script, serial=None, timeout=60.0):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exec(compile(script, "<action-script>", "exec"),
                 {"ui": self.ui, "tap": self.tap, "shell": self.shell, "press": lambda key: None})
        return json.loads(out.getvalue().strip().splitlines()[-1])


def run(phone, action):
    with mock.patch.object(adapter, "harness_read", side_effect=phone.harness_read):
        return adapter.run_action(action)


def test_silent_tap_xy_on_blank_area_reports_no_effect_and_tree_blind():
    phone = FakePhone([HOME, HOME], [FOCUS_HOME, FOCUS_HOME])
    result = run(phone, {"type": "tap_xy", "x": 540, "y": 2000, "settle_ms": 0,
                         "report_effect": True})
    assert result["ok"] is True                         # the input itself still succeeded
    assert (result["effect"], result["tree_blind"]) == ("no_effect", True)
    assert phone.taps == [(540, 2000)]
    assert "_effect_input" not in result and "nodes" not in result  # raw trees stay inside
    assert result["metrics"]["tree_reads"] == 2


def test_tap_that_opens_another_activity_reports_place_changed():
    phone = FakePhone([HOME, [ROOT]], [FOCUS_HOME, FOCUS_DETAIL])
    result = run(phone, {"type": "tap", "locator": {"text": "Go"}, "settle_ms": 0,
                         "report_effect": True})
    assert (result["ok"], result["effect"], result["tree_blind"]) == (True, "place_changed", False)
    assert result["metrics"]["tree_reads"] == 2         # locator read reused as the before tree


def test_failed_expect_with_no_effect_shows_the_silent_tap():
    phone = FakePhone([HOME, HOME], [FOCUS_HOME, FOCUS_HOME])
    result = run(phone, {"type": "tap_xy", "x": 540, "y": 2000, "settle_ms": 0, "poll_ms": 0,
                         "expect": {"text_equals": "Done"}, "report_effect": True})
    assert result["code"] == "assertion_failed"
    assert (result["effect"], result["tree_blind"]) == ("no_effect", True)


def test_unreadable_tree_never_blocks_the_input():
    phone = FakePhone([HOME], [FOCUS_HOME], tree_fails=True)
    result = run(phone, {"type": "tap_xy", "x": 540, "y": 2000, "settle_ms": 0,
                         "report_effect": True})
    assert result["ok"] is True and phone.taps == [(540, 2000)]
    assert (result["effect"], result["tree_blind"]) == (None, True)


def test_without_report_effect_the_result_and_reads_are_unchanged():
    for action in ({"type": "tap_xy", "x": 540, "y": 2000, "settle_ms": 0},
                   {"type": "wait", "seconds": 0, "report_effect": True}):
        phone = FakePhone([HOME, HOME], [FOCUS_HOME, FOCUS_HOME])
        result = run(phone, action)
        assert result["ok"] is True
        assert "effect" not in result and "tree_blind" not in result
        assert phone.tree_reads == 0




def test_paste_tree_blind_uses_actual_tapped_point_before_target_moves():
    # Codex PR #5 counterexample: the field moves after paste (keyboard), the tap did not.
    before = [{"class": "android.widget.EditText", "id": "app:id/input", "text": "",
               "clickable": True, "x": 100, "y": 200, "w": 100, "h": 40}]
    after = [{**before[0], "text": "hello", "y": 800}]
    screens, taps = iter([before, after, after]), []

    def harness(script, serial=None, timeout=60.0):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exec(script, {"ui": lambda: next(screens), "tap": lambda x, y: taps.append((x, y)),
                          "wait_stable": lambda: None,
                          "shell": lambda cmd: FOCUS_HOME if "mCurrentFocus" in cmd else ""})
        return json.loads(out.getvalue().strip().splitlines()[-1])

    with mock.patch.object(adapter, "harness_read", side_effect=harness):
        result = adapter.run_action({"type": "input_text", "method": "paste", "text": "hello",
                                     "locator": {"resource_id": "app:id/input"},
                                     "report_effect": True, "settle_ms": 0})
    assert result["ok"] is True and result["value"] == "hello"
    assert taps == [(100, 200)]
    assert result["tree_blind"] is False


def test_native_tap_with_web_expect_never_returns_raw_trees():
    from journey_pilot import web
    phone = FakePhone([HOME, HOME], [FOCUS_HOME, FOCUS_HOME])
    action = {"type": "tap_xy", "x": 540, "y": 2000, "settle_ms": 0, "poll_ms": 0,
              "expect": {"element_exists": {"css": "#done"}}, "report_effect": True}
    with mock.patch.object(adapter, "harness_read", side_effect=phone.harness_read), \
         mock.patch.object(web, "page_text", return_value=""), \
         mock.patch("journey_pilot.runner.check", return_value=False):
        result = adapter.run_action(action)
    assert result["code"] == "assertion_failed" and phone.taps == [(540, 2000)]
    assert "_effect_input" not in result and "effect" not in result
    assert phone.tree_reads == 1                        # only the web expect's own read


def _session(tmp_path):
    store = Store(tmp_path)
    with mock.patch.object(adapter, "status", return_value={
            "bound": "dev", "devices": [{"serial": "dev"}], "ready": True}):
        opened = session.start(store, {"device_id": "dev", "intent": "test"})
    return store, {"run_id": opened.run_id, "lease_token": opened.data["lease_token"],
                   "device_id": "dev"}


def finished(store, run_id):
    return [e for e in store.read_events(run_id) if e.get("event") == "action_finished"][-1]


def test_act_returns_and_records_effect_fields(tmp_path):
    store, base = _session(tmp_path)
    reported = {"ok": True, "effect": "no_effect", "tree_blind": False}
    with mock.patch.object(adapter, "run_action", return_value=reported) as dispatch:
        result = session.act(store, {**base, "request_id": "r1",
                                     "action": {"type": "key", "key": "back"}})
    assert dispatch.call_args.args[0]["report_effect"] is True
    assert result.status == "recorded"
    assert result.data == {"step_id": result.data["step_id"], "action": "key",
                           "effect": "no_effect", "tree_blind": False}
    event = finished(store, base["run_id"])
    assert (event["effect"], event["tree_blind"]) == ("no_effect", False)


def test_failed_act_keeps_old_error_and_adds_effect(tmp_path):
    store, base = _session(tmp_path)
    reported = {"ok": False, "code": "assertion_failed", "effect": "no_effect", "tree_blind": True}
    with mock.patch.object(adapter, "run_action", return_value=reported):
        result = session.act(store, {**base, "request_id": "r1", "action": {
            "type": "key", "key": "back", "expect": {"text_equals": "x"}}})
    assert result.error["code"] == "assertion_failed"
    assert result.data["detail"]["effect"] == "no_effect"
    assert finished(store, base["run_id"])["tree_blind"] is True


def test_act_without_effect_from_adapter_keeps_old_shape(tmp_path):
    store, base = _session(tmp_path)
    with mock.patch.object(adapter, "run_action", return_value={"ok": True}):
        result = session.act(store, {**base, "request_id": "r1",
                                     "action": {"type": "key", "key": "back"}})
    assert set(result.data) == {"step_id", "action"}
    assert "effect" not in finished(store, base["run_id"])
