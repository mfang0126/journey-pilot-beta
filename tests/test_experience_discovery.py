"""S5: discovery exposes run summaries as reference-only experience.

Every test uses a temporary Store and never touches a device. The one
subprocess test proves the store is found from another process and cwd.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from journey_pilot import journey
from journey_pilot.store import Store, atomic_write_json

REPO = Path(__file__).resolve().parents[1]


def lifecycle(store: Store, journey_id: str, version: int, state: str) -> None:
    path = store.journeys / journey_id / "lifecycle.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": 1.0, "version": version, "state": state,
                             "environment_key": {"device": "dev", "guard_version": 2}}) + "\n")


def step() -> dict:
    return {"step_id": "read", "action": "read_field", "locator": {"text": "Title"}}


def propose(store: Store, task_key: str = "read-title", **extra) -> tuple[str, int]:
    request = {"task_key": task_key, "intent": f"Read {task_key}",
               "steps": [step()], "postconditions": {"text_equals": "Title"}}
    request.update(extra)
    result = journey.propose(store, request)
    assert result.error is None, result.error
    return result.data["journey_id"], result.data["version"]


def make_run(store: Store, run_id: str, summary: dict | None = None,
             task_result: dict | None = None, evidence: bool = False) -> None:
    store.create_run(run_id, summary or {
        "run_id": run_id, "kind": "exploration", "intent": "Read visible title",
        "task_key": "read-title", "status": "recorded",
        "started_at": 100.0, "finished_at": 110.0, "steps": [],
    })
    if task_result is not None:
        atomic_write_json(store.run_dir(run_id) / "task-result.json", task_result)
    if evidence:
        (store.run_dir(run_id) / "evidence" / "tree.json").write_text("{}")


class DiscoverRunSummaries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "jp"
        self.store = Store(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_process_from_other_cwd_finds_unproposed_summary(self):
        make_run(self.store, "r_explore")
        env = {**os.environ, "JOURNEY_PILOT_HOME": str(self.root),
               "PYTHONPATH": str(REPO / "src")}
        proc = subprocess.run(
            [sys.executable, "-m", "journey_pilot.cli", "discover", "--request-stdin"],
            input=json.dumps({"task_key": "read-title"}),
            text=True, capture_output=True, cwd=self.tmp.name, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["status"], "ok")
        runs = payload["data"]["runs"]
        self.assertEqual([r["run_id"] for r in runs], ["r_explore"])
        self.assertFalse(runs[0]["executable"])

    def test_run_summary_is_never_executable(self):
        make_run(self.store, "r_done", summary={
            "run_id": "r_done", "kind": "exploration", "intent": "Read visible title",
            "task_key": "read-title", "status": "completed",
            "started_at": 100.0, "finished_at": 110.0, "steps": []})
        data = journey.discover(self.store, {"task_key": "read-title"}).data
        self.assertEqual(data["executable"], [])
        self.assertFalse(data["runs"][0]["executable"])
        self.assertFalse(data["runs"][0]["runnable"])
        # A run summary is evidence, not a recipe: discovery creates nothing.
        self.assertEqual(list(self.store.journeys.iterdir()), [])

    def test_thin_task_success_and_failure_keep_text_out(self):
        make_run(self.store, "r_ok", task_result={
            "status": "completed", "run_id": "r_ok",
            "data": {"route_id": "xhs-own-post-read-v1", "coverage": "visible_only",
                     "post_text": ["TOP SECRET BODY"],
                     "comment_view_text": ["SECRET COMMENT"],
                     "title": "Secret Title", "author": "Secret Author"},
            "error": None})
        make_run(self.store, "r_bad", summary={
            "run_id": "r_bad", "kind": "exploration", "intent": "Read douyin summary",
            "task_key": "douyin-comment-ai-v1", "status": "recorded",
            "started_at": 200.0, "finished_at": 210.0, "steps": []},
            task_result={
                "status": "paused", "run_id": "r_bad",
                "data": {"route_id": "douyin-comment-ai-v1",
                         "summary_text": ["SECRET SUMMARY"],
                         "coverage": "pixel_ocr_lines"},
                "error": {"code": "precondition_mismatch", "message": "panel gone"}})

        ok = journey.discover(self.store, {"query": "xhs-own-post"}).data
        self.assertEqual([r["run_id"] for r in ok["runs"]], ["r_ok"])
        self.assertEqual(ok["runs"][0]["status"], "completed")
        self.assertEqual(ok["runs"][0]["content"]["post_text_lines"], 1)
        self.assertEqual(ok["runs"][0]["content"]["post_text_chars"],
                         len("TOP SECRET BODY"))
        self.assertTrue(ok["runs"][0]["content"]["has_title"])

        bad = journey.discover(self.store, {"task_key": "douyin-comment-ai-v1"}).data
        self.assertEqual([r["run_id"] for r in bad["runs"]], ["r_bad"])
        self.assertEqual(bad["runs"][0]["failure"],
                         {"code": "precondition_mismatch"})

        blob = json.dumps([ok, bad], ensure_ascii=False)
        self.assertNotIn("TOP SECRET BODY", blob)
        self.assertNotIn("SECRET COMMENT", blob)
        self.assertNotIn("SECRET SUMMARY", blob)

    def test_evidence_cleanup_keeps_summary_discoverable(self):
        make_run(self.store, "r_ev", evidence=True)
        first = journey.discover(self.store, {"task_key": "read-title"}).data
        self.assertEqual(first["runs"][0]["evidence"], "available")
        shutil.rmtree(self.store.run_dir("r_ev") / "evidence")
        second = journey.discover(self.store, {"task_key": "read-title"}).data
        self.assertEqual([r["run_id"] for r in second["runs"]], ["r_ev"])
        self.assertEqual(second["runs"][0]["evidence"], "cleared")

    def test_proposed_source_run_is_not_listed_as_unrefined(self):
        make_run(self.store, "r_src")
        journey_id, _ = propose(self.store, source_run_ids=["r_src"])
        data = journey.discover(self.store, {"task_key": "read-title"}).data
        self.assertNotIn("r_src", [r["run_id"] for r in data["runs"]])
        self.assertEqual([h["journey_id"] for h in data["reference"]], [journey_id])

    def test_latest_candidate_does_not_hide_older_verified(self):
        journey_id, v1 = propose(self.store)
        lifecycle(self.store, journey_id, v1, "verified")
        _, v2 = propose(self.store, journey_id=journey_id, base_version=v1)
        data = journey.discover(self.store, {"task_key": "read-title", "environment_key": {"device": "dev"}}).data
        self.assertEqual([(h["journey_id"], h["version"]) for h in data["executable"]],
                         [(journey_id, v1)])
        self.assertEqual([(h["journey_id"], h["version"]) for h in data["reference"]],
                         [(journey_id, v2)])
        self.assertEqual(data["selection"], "unique")
        self.assertTrue((self.store.journeys / journey_id / "versions" / "1.json").exists())
        self.assertTrue((self.store.journeys / journey_id / "versions" / "2.json").exists())

    def test_environment_mismatch_and_unknown_are_not_executable(self):
        journey_id, version = propose(self.store, environment_key={
            "platform": "android", "app_version": "1.0"})
        lifecycle(self.store, journey_id, version, "verified")

        exact = journey.discover(self.store, {
            "task_key": "read-title",
            "environment_key": {"platform": "android", "app_version": "1.0", "device": "dev"}}).data
        self.assertEqual(len(exact["executable"]), 1)
        self.assertEqual(exact["executable"][0]["environment_match"], "exact")

        mismatch = journey.discover(self.store, {
            "task_key": "read-title",
            "environment_key": {"platform": "android", "app_version": "2.0"}}).data
        self.assertEqual(mismatch["executable"], [])
        self.assertEqual(mismatch["reference"][0]["environment_match"], "partial")
        self.assertFalse(mismatch["reference"][0]["compatible"])

        unknown = journey.discover(self.store, {"task_key": "read-title"}).data
        self.assertEqual(unknown["executable"], [])
        self.assertEqual(unknown["reference"][0]["environment_match"], "unknown")
        self.assertFalse(unknown["reference"][0]["compatible"])

    def test_existing_journey_result_shape_is_preserved(self):
        journey_id, version = propose(self.store)
        data = journey.discover(self.store, {"task_key": "read-title"}).data
        for key in ("executable", "reference", "excluded", "selection", "query",
                    "runs", "runs_total"):
            self.assertIn(key, data)
        hit = data["reference"][0]
        for key in ("journey_id", "version", "task_key", "intent", "state",
                    "compatible", "executable", "steps", "score", "kind",
                    "environment_match"):
            self.assertIn(key, hit)
        self.assertEqual(hit["journey_id"], journey_id)
        self.assertEqual(hit["version"], version)
        self.assertEqual(data["runs"], [])

    def test_discovery_does_not_guess_a_recipe(self):
        make_run(self.store, "r_actions")
        self.store.append_event("r_actions", {
            "event": "action_started", "step_id": "s_1", "action": "tap"})
        self.store.append_event("r_actions", {
            "event": "action_finished", "step_id": "s_1", "outcome": "ok"})
        data = journey.discover(self.store, {"task_key": "read-title"}).data
        self.assertEqual([r["run_id"] for r in data["runs"]], ["r_actions"])
        self.assertEqual(list(self.store.journeys.iterdir()), [])

    def test_no_filter_does_not_dump_run_history(self):
        make_run(self.store, "r_explore")
        data = journey.discover(self.store, {}).data
        self.assertEqual(data["runs"], [])
        self.assertEqual(data["selection"], "none")


class InspectRuns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "jp"
        self.store = Store(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_inspect_run_returns_summary_task_result_and_evidence(self):
        make_run(self.store, "r_inspect", task_result={
            "status": "completed", "run_id": "r_inspect",
            "data": {"route_id": "xhs-own-post-read-v1", "coverage": "visible_only"},
            "error": None}, evidence=True)
        self.store.append_event("r_inspect", {
            "event": "observation", "observation_id": "o_1"})
        result = journey.inspect(self.store, {"run_id": "r_inspect"})
        self.assertIsNone(result.error)
        data = result.data
        self.assertEqual(data["kind"], "run")
        self.assertEqual(data["summary"]["run_id"], "r_inspect")
        self.assertEqual(data["task_result"]["status"], "completed")
        self.assertIsNone(data["failure"])
        self.assertEqual(data["event_count"], 1)
        self.assertEqual(data["events"][0]["event"], "observation")
        self.assertEqual(data["evidence"]["state"], "available")
        self.assertEqual(data["evidence"]["task_result"], "present")
        self.assertEqual(data["unknown_steps"], [])

    def test_inspect_failure_uses_task_result_error(self):
        make_run(self.store, "r_fail", task_result={
            "status": "paused", "run_id": "r_fail",
            "data": {"route_id": "xhs-own-post-read-v1"},
            "error": {"code": "locator_missing", "message": "post not found"}})
        data = journey.inspect(self.store, {"run_id": "r_fail"}).data
        self.assertEqual(data["failure"]["code"], "locator_missing")
        self.assertEqual(data["task_result"]["error"]["code"], "locator_missing")

    def test_inspect_survives_evidence_cleanup(self):
        make_run(self.store, "r_ev", task_result={
            "status": "completed", "run_id": "r_ev", "data": {}, "error": None},
            evidence=True)
        shutil.rmtree(self.store.run_dir("r_ev") / "evidence")
        result = journey.inspect(self.store, {"run_id": "r_ev"})
        self.assertIsNone(result.error)
        self.assertEqual(result.data["evidence"]["state"], "cleared")
        self.assertEqual(result.data["summary"]["run_id"], "r_ev")
        self.assertEqual(result.data["task_result"]["status"], "completed")

    def test_inspect_unknown_run_and_bad_selector(self):
        missing = journey.inspect(self.store, {"run_id": "r_missing"})
        self.assertEqual(missing.error["code"], "invalid_request")
        both = journey.inspect(self.store, {"run_id": "r", "journey_id": "j"})
        self.assertEqual(both.error["code"], "invalid_request")
        neither = journey.inspect(self.store, {})
        self.assertEqual(neither.error["code"], "invalid_request")

    def test_inspect_journey_lists_verified_versions(self):
        journey_id, v1 = propose(self.store)
        lifecycle(self.store, journey_id, v1, "verified")
        _, v2 = propose(self.store, journey_id=journey_id, base_version=v1)
        data = journey.inspect(self.store, {"journey_id": journey_id}).data
        self.assertEqual(data["kind"], "journey")
        self.assertEqual(data["journey"]["version"], v2)
        self.assertEqual(data["state"], "candidate")
        self.assertEqual(data["versions"], v2)
        self.assertEqual(data["verified_versions"], [v1])

    def test_inspect_can_read_older_version(self):
        journey_id, v1 = propose(self.store)
        lifecycle(self.store, journey_id, v1, "verified")
        propose(self.store, journey_id=journey_id, base_version=v1)
        data = journey.inspect(self.store, {
            "journey_id": journey_id, "version": v1}).data
        self.assertEqual(data["journey"]["version"], v1)
        self.assertEqual(data["state"], "verified")


if __name__ == "__main__":
    unittest.main()
