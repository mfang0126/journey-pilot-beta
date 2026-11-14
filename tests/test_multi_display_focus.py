"""On-device foreground reads display 0, not a rear display's null focus (#9).

Generated harness code runs against a scripted ``dumpsys window``; no device access.
"""
import contextlib
import io
import json
import re
import unittest
from unittest import mock

from journey_pilot import adapter

ROOT = {"class": "android.widget.FrameLayout", "id": "", "text": "", "desc": "",
        "clickable": False, "x": 540, "y": 1200, "w": 1080, "h": 2400}


def dual(activity):
    """Two displays; display 1 (rear, off) is listed first with a null focus."""
    return "\n".join([
        "  Display: mDisplayId=1 rootTasks=[] info=976x596",
        "  mCurrentFocus=null",
        "  Display: mDisplayId=0 rootTasks=[] info=1200x2608",
        f"  mCurrentFocus=Window{{dd03526 u0 com.android.settings/com.android.settings.{activity}}}",
    ])


def script(action, focuses):
    """Each shell() call returns the next focus dump; the last one repeats."""
    seen = []

    def shell(cmd):
        # Applies the command's grep like the device would, so -m1 sees only the first line.
        seen.append(cmd)
        if "mCurrentFocus" not in cmd:
            return ""
        grep = re.search(r"grep (-m1 )?(?:-E )?'?([^']+)'?$", cmd)
        lines = [l for l in focuses[min(len(seen), len(focuses)) - 1].splitlines()
                 if re.search(grep.group(2), l)]
        return "\n".join(lines[:1] if grep.group(1) else lines)

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(action if isinstance(action, str)
             else adapter.build_action_script({"settle_ms": 0, "poll_ms": 0, **action}),
             {"ui": lambda: [ROOT], "tap": lambda x, y: None, "shell": shell,
              "press": lambda key: None})
    return json.loads(out.getvalue().strip().splitlines()[-1])


def act(action, focuses):
    """Through run_action, which turns the raw before/after into ``effect``."""
    with mock.patch.object(adapter, "harness_read",
                           side_effect=lambda code, serial=None, timeout=60.0: script(code, focuses)):
        return adapter.run_action({"settle_ms": 0, **action})


class MultiDisplayFocus(unittest.TestCase):
    def test_expect_app_reads_display_zero(self):
        result = script({"type": "key", "key": "back", "expect_app": "com.android.settings"},
                     [dual("MainSettings")])
        assert result["ok"] is True and "unmet" not in result


    def test_expect_app_other_package_on_display_zero_still_fails(self):
        result = script({"type": "key", "key": "back", "expect_app": "com.example.app"},
                     [dual("MainSettings")])
        assert result["unmet"] == ["expect_app"]


    def test_effect_compares_display_zero_windows(self):
        # Same tree; only display 0's activity changes. Reading the rear null would say no_effect.
        result = act({"type": "tap_xy", "x": 540, "y": 2000, "report_effect": True},
                     [dual("MainSettings"), dual("SubSettings")])
        assert result["effect"] == "place_changed"


    def test_unattributed_null_first_falls_back_to_first_window(self):
        # The issue's raw grep output: no display headers.
        plain = "  mCurrentFocus=null\n  mCurrentFocus=Window{dd03526 u0 com.android.settings/.MainSettings}"
        result = script({"type": "key", "key": "back", "expect_app": "com.android.settings"}, [plain])
        assert result["ok"] is True


    def test_single_display_old_format(self):
        one = "  mCurrentFocus=Window{1 u0 com.example.app/com.example.app.Main}"
        assert script({"type": "key", "key": "back", "expect_app": "com.example.app"}, [one])["ok"] is True
        assert script({"type": "key", "key": "back", "expect_app": "com.other"}, [one])["unmet"] == ["expect_app"]

    def test_rear_display_with_its_own_window_is_ignored(self):
        both = dual("MainSettings").replace(
            "mCurrentFocus=null", "mCurrentFocus=Window{9 u0 com.miui.rear/com.miui.rear.Clock}")
        result = script({"type": "key", "key": "back", "expect_app": "com.android.settings"}, [both])
        assert result["ok"] is True


def main_null(rear_activity, rear_first):
    """Display 0 focus is null while the rear display shows its own window."""
    main = ["  Display: mDisplayId=0 rootTasks=[] info=1200x2608", "  mCurrentFocus=null"]
    rear = ["  Display: mDisplayId=1 rootTasks=[] info=976x596",
            f"  mCurrentFocus=Window{{9 u0 com.miui.rear/com.miui.rear.{rear_activity}}}"]
    return "\n".join(rear + main if rear_first else main + rear)


class MainDisplayNull(unittest.TestCase):
    """Codex review on #11: a null display 0 stays unknown, never the rear window."""

    def test_expect_app_does_not_pass_on_rear_window(self):
        for rear_first in (False, True):
            result = script({"type": "key", "key": "back", "expect_app": "com.miui.rear"},
                            [main_null("Clock", rear_first)])
            assert result["unmet"] == ["expect_app"], rear_first

    def test_rear_only_change_is_not_place_changed(self):
        for rear_first in (False, True):
            result = act({"type": "tap_xy", "x": 540, "y": 2000, "report_effect": True},
                         [main_null("Clock", rear_first), main_null("Other", rear_first)])
            assert result["effect"] == "no_effect", rear_first

    def test_headers_without_display_zero_are_unknown(self):
        rear = "  Display: mDisplayId=1 rootTasks=[]\n  mCurrentFocus=Window{9 u0 com.miui.rear/.Clock}"
        result = script({"type": "key", "key": "back", "expect_app": "com.miui.rear"}, [rear])
        assert result["unmet"] == ["expect_app"]

    def test_all_null_is_unknown(self):
        nulls = main_null("Clock", True).replace(
            "Window{9 u0 com.miui.rear/com.miui.rear.Clock}", "null")
        result = script({"type": "key", "key": "back", "expect_app": "com.android.settings"}, [nulls])
        assert result["unmet"] == ["expect_app"]
        effect = act({"type": "tap_xy", "x": 540, "y": 2000, "report_effect": True}, [nulls])
        assert effect["effect"] == "no_effect"
