"""input_text replaces the field and is ok only if the re-read field text equals ``text`` (#15)."""
import contextlib
import io
import json
import tempfile
from pathlib import Path
from unittest import mock

from journey_pilot import adapter, journey, runner, session
from journey_pilot.store import Store

FOCUS = "mCurrentFocus=Window{1 u0 com.android.settings/.SearchActivity}"


class Field:
    """An EditText whose caret lands mid-text on tap, like the 2026-11-05 smoke."""

    def __init__(self, text, clipboard="", deletes=True):
        self.text, self.caret, self.clipboard, self.deletes = text, 0, clipboard, deletes

    def insert(self, chunk):
        self.text = self.text[:self.caret] + chunk + self.text[self.caret:]
        self.caret += len(chunk)

    def shell(self, cmd):
        if "mCurrentFocus" in cmd:
            return FOCUS
        if cmd.startswith("input keyevent"):
            for code in cmd.split()[2:]:
                if code == "279":
                    self.insert(self.clipboard)
                elif self.deletes and code == "67" and self.caret:
                    self.text = self.text[:self.caret - 1] + self.text[self.caret:]
                    self.caret -= 1
                elif self.deletes and code == "112":
                    self.text = self.text[:self.caret] + self.text[self.caret + 1:]
        return ""

    def tap(self, x, y):
        self.caret = len(self.text) // 2

    def harness_read(self, script, serial=None, timeout=60.0):
        node = {"class": "android.widget.EditText", "id": "android:id/input", "text": self.text,
                "clickable": True, "x": 540, "y": 200, "w": 900, "h": 120}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exec(script, {"ui": lambda: [dict(node, text=self.text)], "tap": self.tap,
                          "wait_stable": lambda: None, "shell": self.shell,
                          "type_text": self.insert})
        return json.loads(out.getvalue().strip().splitlines()[-1])


def act(field, **extra):
    action = {"type": "input_text", "locator": {"resource_id": "android:id/input"},
              "settle_ms": 0, **extra}
    with mock.patch.object(adapter, "harness_read", side_effect=field.harness_read):
        return adapter.run_action(action)


def test_adb_input_on_non_empty_field_replaces_instead_of_inserting_mid_field():
    field = Field("jp smoke daily limit reached")
    result = act(field, text=" quota exceeded")
    assert field.text == " quota exceeded"
    assert result["ok"] is True and result["value"] == " quota exceeded"


def test_paste_on_non_empty_field_also_replaces():
    field = Field("old draft", clipboard="新的文本")
    result = act(field, method="paste", text="新的文本")
    assert field.text == "新的文本" and result["ok"] is True


def test_field_text_mismatch_is_assertion_failed_not_success():
    field = Field("jp smoke daily limit reached", deletes=False)  # the clear did not land
    result = act(field, text=" quota exceeded")
    assert field.text == "jp smoke daily quota exceeded limit reached"
    assert result["ok"] is False and result["code"] == "assertion_failed"
    assert result["value"] == field.text


def session_act(field, **extra):
    action = {"step_id": "type", "type": "input_text", "effect_class": "local_input",
              "locator": {"resource_id": "android:id/input"}, "settle_ms": 0, **extra}
    with tempfile.TemporaryDirectory() as tmp, \
         mock.patch.object(adapter, "status", return_value={
             "bound": "dev", "devices": [{"serial": "dev"}], "ready": True}), \
         mock.patch("journey_pilot.pacing.before_action", return_value={}), \
         mock.patch.object(adapter, "harness_read", side_effect=field.harness_read) as harness:
        store = Store(Path(tmp))
        opened = session.start(store, {"device_id": "dev", "intent": "input"})
        result = session.act(store, {
            "run_id": opened.run_id, "lease_token": opened.data["lease_token"],
            "device_id": "dev", "request_id": "r1", "action": action})
        started = [e for e in store.read_events(opened.run_id) if e.get("event") == "action_started"]
        return result, harness, started


def test_session_act_does_not_record_a_mismatched_adb_input():
    result, _, _ = session_act(Field("abc", deletes=False), text="xyz")
    assert result.status != "recorded"
    assert result.error["code"] == "assertion_failed"


def test_missing_text_is_rejected_before_any_tap_or_deletion():
    # Codex review on #20: a missing text used to default to '' and now would clear the draft.
    field = Field("unsaved draft")
    result, harness, started = session_act(field)
    assert result.status != "recorded" and result.error["code"] == "invalid_request"
    harness.assert_not_called()
    assert started == [] and field.text == "unsaved draft"
    try:
        act(field)
    except journey.JourneyError as exc:
        assert exc.code == "invalid_request"
    else:
        raise AssertionError("adapter accepted input_text without text")
    assert field.text == "unsaved draft"


def test_missing_text_is_rejected_by_propose_and_replay_preflight():
    step = {"step_id": "type", "action": "input_text", "effect_class": "local_input",
            "locator": {"resource_id": "android:id/input"}}
    with tempfile.TemporaryDirectory() as tmp, \
         mock.patch.object(adapter, "run_action") as dispatch, \
         mock.patch.object(adapter, "harness_read") as observe:
        store = Store(Path(tmp))
        proposed = journey.propose(store, {"task_key": "type", "intent": "type", "steps": [step]})
        assert proposed.error["code"] == "invalid_request"
        summary, results = runner._execute(store, {"journey_id": "j_test", "version": 1,
                                                   "steps": [step]}, "dev", "validate")
        assert summary["failure"]["step_id"] == "preflight" and results == []
        dispatch.assert_not_called()
        observe.assert_not_called()


def test_explicit_empty_text_clears_the_field_and_is_verified():
    field = Field("old query")
    result = act(field, text="")
    assert field.text == "" and result["ok"] is True
    result, _, _ = session_act(Field("old query"), text="")
    assert result.status == "recorded"
    stuck = Field("old query", deletes=False)
    assert act(stuck, text="")["code"] == "assertion_failed" and stuck.text == "old query"
