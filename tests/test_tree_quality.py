"""Tree quality gate: a good tree skips OCR, a partial tree adds it, no tree needs it."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from journey_pilot import adapter, session
from journey_pilot.execution import request_record_path
from journey_pilot.privacy import private_digest
from journey_pilot.store import Store, atomic_write_json

GEOMETRY = {"width": 1000, "height": 2000, "orientation": 0}


def spread(count, ys=(100, 600, 1100, 1600)):
    return [{"text": f"item {i}", "x": 500, "y": ys[i % len(ys)]} for i in range(count)]


class TreeQualityRule(unittest.TestCase):
    def test_thresholds_are_pinned(self):
        self.assertEqual((adapter.TREE_MIN_USEFUL, adapter.TREE_BANDS, adapter.TREE_MIN_BANDS), (5, 4, 3))

    def test_enough_spread_text_or_clickable_is_good(self):
        self.assertEqual(adapter.tree_quality(spread(5), GEOMETRY), "good")
        icons = [{"text": "", "desc": "", "clickable": True, "x": 1, "y": y} for y in (100, 600, 1100, 1600, 1700)]
        self.assertEqual(adapter.tree_quality(icons, GEOMETRY), "good")
        # Without geometry the band check cannot run; the count alone decides.
        self.assertEqual(adapter.tree_quality(spread(5), None), "good")

    def test_sparse_or_missing_region_is_partial(self):
        self.assertEqual(adapter.tree_quality(spread(4), GEOMETRY), "partial")
        blanks = spread(4) + [{"text": "  ", "desc": None, "x": 1, "y": 1}] * 3
        self.assertEqual(adapter.tree_quality(blanks, GEOMETRY), "partial")
        top_and_bottom = spread(6, ys=(100, 1900))
        self.assertEqual(adapter.tree_quality(top_and_bottom, GEOMETRY), "partial")

    def test_no_tree_is_unusable(self):
        self.assertEqual(adapter.tree_quality([], GEOMETRY), "unusable")

    def test_ocr_supplement_drops_lines_the_tree_already_has(self):
        tree = [{"text": "", "desc": "笔记,Test note,来自Owner"}, {"text": "Home"}]
        ocr = [{"text": "Test note", "x": 1, "y": 1}, {"text": "Ho me"}, {"text": "评论 12", "x": 5, "y": 9},
               {"text": "  "}]
        self.assertEqual(adapter.ocr_supplement(tree, ocr), [{"text": "评论 12", "x": 5, "y": 9, "source": "ocr"}])


class ObserveGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))
        with mock.patch.object(adapter, "status", return_value={
                "bound": "dev", "devices": [{"serial": "dev"}], "ready": True}):
            opened = session.start(self.store, {"device_id": "dev", "intent": "test"})
        self.base = {"run_id": opened.run_id, "lease_token": opened.data["lease_token"], "device_id": "dev"}

    def observe(self, tree, pixels=None, **request):
        scripts = []

        def read(script, **_):
            scripts.append(script)
            return tree if script is adapter.OBSERVE_TREE else pixels
        with mock.patch.object(adapter, "harness_read", side_effect=read):
            return session.observe(self.store, {**self.base, **request}), scripts

    def last_observation(self):
        return [e for e in self.store.read_events(self.base["run_id"]) if e["event"] == "observation"][-1]

    def test_good_tree_skips_ocr(self):
        out, scripts = self.observe({"ok": True, "nodes": spread(8), "geometry": GEOMETRY})
        self.assertEqual(out.status, "recorded")
        self.assertEqual(scripts, [adapter.OBSERVE_TREE])
        self.assertEqual(out.data["tree_quality"], "good")
        self.assertNotIn("ocr_nodes", out.data)
        # Existing fields keep their meaning.
        self.assertEqual(out.data["nodes"], spread(8))
        self.assertEqual(out.data["geometry"], GEOMETRY)
        self.assertEqual(self.last_observation()["tree_quality"], "good")

    def test_partial_tree_adds_marked_ocr_and_keeps_tree_nodes(self):
        tree = [{"text": "Home", "x": 10, "y": 100}]
        pixels = {"ok": True, "geometry": GEOMETRY,
                  "nodes": [{"text": "Home", "x": 10, "y": 100}, {"text": "正文第一行", "x": 300, "y": 900}]}
        out, scripts = self.observe({"ok": True, "nodes": tree, "geometry": GEOMETRY}, pixels)
        self.assertEqual(out.status, "recorded")
        self.assertEqual(scripts, [adapter.OBSERVE_TREE, adapter.OBSERVE_PIXELS])
        self.assertEqual(out.data["tree_quality"], "partial")
        self.assertEqual(out.data["nodes"], tree)
        self.assertEqual(out.data["ocr_nodes"], [{"text": "正文第一行", "x": 300, "y": 900, "source": "ocr"}])
        event = self.last_observation()
        self.assertEqual((event["count"], event["tree_quality"], event["ocr_count"]), (1, "partial", 1))

    def test_partial_tree_survives_ocr_failure(self):
        tree = [{"text": "Home", "x": 10, "y": 100}]
        out, _ = self.observe({"ok": True, "nodes": tree, "geometry": GEOMETRY},
                              {"ok": False, "reason": "Vision OCR failed"})
        self.assertEqual(out.status, "recorded")
        self.assertEqual(out.data["nodes"], tree)
        self.assertEqual(out.data["ocr_error"], "Vision OCR failed")
        self.assertNotIn("ocr_nodes", out.data)

    def test_unusable_tree_must_ocr(self):
        pixels = {"ok": True, "geometry": GEOMETRY, "nodes": [{"text": "视频标题", "x": 200, "y": 1500}]}
        out, scripts = self.observe({"ok": False, "reason": "could not get idle state"}, pixels)
        self.assertEqual(out.status, "recorded")
        self.assertEqual(scripts, [adapter.OBSERVE_TREE, adapter.OBSERVE_PIXELS])
        self.assertEqual(out.data["tree_quality"], "unusable")
        self.assertEqual(out.data["tree_error"], "could not get idle state")
        self.assertEqual(out.data["nodes"], [])
        self.assertEqual(out.data["ocr_nodes"], [{"text": "视频标题", "x": 200, "y": 1500, "source": "ocr"}])
        # The OCR geometry backs the observation so tap_xy can use it.
        self.assertEqual(self.last_observation()["geometry"], GEOMETRY)

    def test_unusable_tree_and_failed_ocr_keep_the_old_failure(self):
        out, _ = self.observe({"ok": False, "reason": "could not get idle state"},
                              {"ok": False, "reason": "Vision OCR failed"})
        self.assertEqual(out.error["code"], "locked")
        self.assertEqual(out.error["message"], "could not get idle state")
        self.assertEqual(out.data["tree_quality"], "unusable")

    def test_block_signal_seen_only_by_ocr_stops(self):
        pixels = {"ok": True, "geometry": GEOMETRY, "nodes": [{"text": "请完成安全验证", "x": 1, "y": 1}]}
        out, _ = self.observe({"ok": True, "nodes": [], "geometry": GEOMETRY}, pixels)
        self.assertEqual(out.error["code"], "platform_blocked")

    def test_pixels_source_is_not_gated(self):
        pixels = {"ok": True, "geometry": GEOMETRY, "nodes": [{"text": "a", "x": 1, "y": 1}]}
        with mock.patch.object(adapter, "harness_read", return_value=pixels) as read:
            out = session.observe(self.store, {**self.base, "source": "pixels"})
        self.assertEqual(read.call_count, 1)
        self.assertNotIn("tree_quality", out.data)
        self.assertNotIn("ocr_nodes", out.data)

    def test_screenshot_source_is_not_gated_and_records_capture_time(self):
        shot = {"ok": True, "bytes": 42, "image_base64": "aGk="}
        clock = [session._now()]

        def read(*_, **__):
            clock[0] += 1  # the capture time must be taken after this read
            return shot

        def observations():
            return [e for e in self.store.read_events(self.base["run_id"]) if e["event"] == "observation"]
        for exclude, script in ((None, adapter.OBSERVE_SCREENSHOT),
                                (True, adapter.OBSERVE_SCREENSHOT_NO_STATUS_BAR)):
            with self.subTest(exclude_status_bar=exclude):
                request = {"source": "screenshot", **({"exclude_status_bar": exclude} if exclude else {})}
                before = observations()
                with mock.patch.object(session, "_now", side_effect=lambda: clock[0]), \
                     mock.patch.object(adapter, "harness_read", side_effect=read) as harness:
                    out = session.observe(self.store, {**self.base, **request})
                self.assertEqual(out.status, "recorded")
                self.assertEqual([c.args[0] for c in harness.call_args_list], [script])
                self.assertEqual((out.data["bytes"], out.data["image_base64"]), (42, "aGk="))
                self.assertNotIn("tree_quality", out.data)
                # This call's own event, not one left by the previous subtest.
                after = observations()
                self.assertEqual(len(after), len(before) + 1)
                event = after[-1]
                self.assertEqual(event["observation_id"], out.data["observation_id"])
                self.assertEqual((event["source"], event["observed_at"]), ("screenshot", clock[0]))
                self.assertNotIn("tree_quality", event)
        with mock.patch.object(adapter, "harness_read", return_value={"ok": False}):
            failed = session.observe(self.store, {**self.base, "source": "screenshot"})
        self.assertEqual((failed.error["code"], failed.error["message"]), ("locked", "capture failed"))


    # Codex review of PR #6, offline counterexample 1: tree read failed, OCR found nothing.
    def test_ocr_only_observation_cannot_resolve_unknown_input(self):
        with mock.patch.object(adapter, "harness_read", return_value={"ok": True, "nodes": [], "geometry": GEOMETRY}):
            self.assertEqual(session.observe(self.store, self.base).status, "recorded")
        run_id = self.base["run_id"]
        action = {"step_id": "legacy-input", "type": "input_text", "effect_class": "local_input",
                  "locator": {"class": "android.widget.EditText"}, "text": "离线样本"}
        self.store.append_event(run_id, {"event": "action_started", "step_id": "legacy-input",
            "action": "input_text", "effect_class": "local_input",
            "execution": {"type": "input_text", "effect_class": "local_input"}})
        self.store.append_event(run_id, {"event": "action_finished", "step_id": "legacy-input",
            "outcome": "action_failed", "locator_index": 0,
            "metrics": {"script_ms": 10.0, "tree_reads": 1}})
        payload = {"run_id": run_id, "device_id": "dev", "action": action}
        atomic_write_json(request_record_path(self.store, f"act:{run_id}", "legacy-r"), {
            "digest": private_digest(self.store, payload), "state": "done",
            "result": {"status": "paused", "run_id": run_id, "data": {"step_id": "legacy-input"},
                       "error": {"code": "outcome_unknown"}}})
        out, _ = self.observe({"ok": False, "reason": "could not get idle state"},
                              {"ok": True, "nodes": [], "geometry": GEOMETRY})
        self.assertEqual(out.data["source"], "pixels")
        self.assertEqual(self.last_observation()["source"], "pixels")
        resolved = session.resolve_input(self.store, {**self.base, "request_id": "legacy-r", "action": action})
        self.assertIsNot(resolved.data.get("resolved"), True)
        self.assertEqual(len(self.store.open_actions(run_id)), 1)

    def test_partial_tree_digest_is_tree_only(self):
        tree = [{"text": "Home", "x": 10, "y": 100}]
        out, _ = self.observe({"ok": True, "nodes": tree, "geometry": GEOMETRY},
                              {"ok": True, "geometry": GEOMETRY, "nodes": [{"text": "噪声", "x": 1, "y": 1}]})
        self.assertEqual(out.data["ocr_nodes"][0]["text"], "噪声")
        self.assertEqual(self.last_observation()["digest"], private_digest(self.store, tree))

    # Codex review of PR #6, offline counterexample 2: slow OCR must not refresh tree age.
    def test_slow_ocr_keeps_tree_coordinate_age(self):
        for pixels in ({"ok": False, "reason": "OCR timed out"},
                       {"ok": True, "geometry": GEOMETRY, "nodes": [{"text": "late", "x": 1, "y": 1}]}):
            with self.subTest(ocr_ok=pixels["ok"]):
                clock = [1_000_000.0 + 100 * pixels["ok"]]
                tree_at = clock[0]

                def read(script, **_):
                    if script is adapter.OBSERVE_PIXELS:
                        clock[0] += 20
                        return pixels
                    return {"ok": True, "nodes": [{"text": "Old target", "x": 100, "y": 200}],
                            "geometry": GEOMETRY}
                with mock.patch.object(session, "_now", side_effect=lambda: clock[0]), \
                     mock.patch.object(adapter, "harness_read", side_effect=read):
                    observed = session.observe(self.store, self.base)
                    self.assertEqual(observed.status, "recorded")
                    self.assertEqual(self.last_observation()["observed_at"], tree_at)
                    with mock.patch("journey_pilot.pacing.before_action", return_value={}), \
                         mock.patch.object(adapter, "run_action", return_value={"ok": True}) as dispatch:
                        result = session.act(self.store, {**self.base, "request_id": f"old-{pixels['ok']}",
                            "action": {"type": "tap_xy", "x": 100, "y": 200,
                                       "observation_id": observed.data["observation_id"]}})
                self.assertEqual(result.error["code"], "precondition_mismatch")
                dispatch.assert_not_called()

if __name__ == "__main__":
    unittest.main()
