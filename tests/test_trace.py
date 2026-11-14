"""Trace references and summaries over a real temporary Store."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from journey_pilot.store import Store
from journey_pilot.trace import (
    build_reference,
    code_digest,
    summarize_run,
    verify_reference,
)


class TraceCase(unittest.TestCase):
    run_id = "r_trace"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "jp")
        self.store.create_run(self.run_id, {"run_id": self.run_id})

    def tearDown(self):
        self.tmp.cleanup()

    def append(self, *events):
        for event in events:
            self.store.append_event(self.run_id, event)

    def rewrite(self, mutate):
        path = self.store.events_path(self.run_id)
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        events = mutate(events)
        path.write_text("".join(json.dumps(event) + "\n" for event in events),
                        encoding="utf-8")


class Reference(TraceCase):
    def setUp(self):
        super().setUp()
        self.append(
            {"event": "run_started"},
            {"event": "observation", "observation_id": "o_1"},
            {"event": "action_started", "step_id": "s1"},
            {"event": "action_finished", "step_id": "s1", "outcome": "ok"},
        )

    def test_build_and_verify_full_and_inner_range(self):
        for start, end in ((1, 4), (2, 4), (3, 3)):
            reference = build_reference(self.store, self.run_id, start, end)
            self.assertEqual(set(reference), {"run_id", "seq_start", "seq_end", "digest"})
            self.assertEqual(len(reference["digest"]), 64)
            self.assertTrue(verify_reference(self.store, reference))

    def test_append_does_not_invalidate_an_earlier_reference(self):
        reference = build_reference(self.store, self.run_id, 1, 2)
        digest = reference["digest"]
        self.append({"event": "observation", "observation_id": "o_2"})
        self.assertEqual(build_reference(self.store, self.run_id, 1, 2)["digest"], digest)
        self.assertTrue(verify_reference(self.store, reference))

    def test_mutation_outside_the_range_stays_valid(self):
        reference = build_reference(self.store, self.run_id, 1, 2)
        self.rewrite(lambda events: [
            {**event, "tampered": True} if event["seq"] == 3 else event
            for event in events])
        self.assertTrue(verify_reference(self.store, reference))

    def test_mutation_inside_the_range_is_rejected(self):
        reference = build_reference(self.store, self.run_id, 2, 3)
        self.rewrite(lambda events: [
            {**event, "observation_id": "changed"} if event["seq"] == 2 else event
            for event in events])
        self.assertFalse(verify_reference(self.store, reference))

    def test_missing_event_is_rejected(self):
        reference = build_reference(self.store, self.run_id, 1, 4)
        self.rewrite(lambda events: [event for event in events if event["seq"] != 2])
        self.assertFalse(verify_reference(self.store, reference))

    def test_missing_run_is_rejected(self):
        reference = build_reference(self.store, self.run_id, 1, 1)
        reference["run_id"] = "r_absent"
        self.assertFalse(verify_reference(self.store, reference))

    def test_malformed_references_are_rejected(self):
        reference = build_reference(self.store, self.run_id, 1, 2)
        cases = [
            None,
            "r_trace",
            {},
            {"run_id": self.run_id},
            {**reference, "run_id": "../escape"},
            {**reference, "run_id": 7},
            {**reference, "seq_start": 0},
            {**reference, "seq_start": True},
            {**reference, "seq_start": 3, "seq_end": 2},
            {**reference, "seq_end": 99},
            {**reference, "digest": "0" * 64},
            {**reference, "digest": "z" * 64},
            {**reference, "digest": "short"},
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assertFalse(verify_reference(self.store, case))

    def test_build_rejects_invalid_ranges_and_unknown_events(self):
        for start, end in ((0, 1), (2, 1), (True, 2), (1, 5)):
            with self.subTest(start=start, end=end):
                with self.assertRaises(ValueError):
                    build_reference(self.store, self.run_id, start, end)


class Summary(TraceCase):
    def test_counts_and_unknown_actions(self):
        self.append(
            {"event": "run_started"},
            {"event": "observation", "observation_id": "o_1"},
            {"event": "observation", "observation_id": "o_2"},
            {"event": "action_started", "step_id": "s1"},
            {"event": "action_finished", "step_id": "s1", "outcome": "ok"},
            {"event": "action_started", "step_id": "s2"},
            {"event": "action_finished", "step_id": "s2", "outcome": "outcome_unknown"},
            {"event": "check", "name": "postconditions", "ok": True},
        )
        summary = summarize_run(self.store, self.run_id)
        self.assertEqual(summary["counts"],
                         {"observation": 2, "action": 2, "check": 1, "unknown": 1})
        self.assertEqual(summary["events"], {"count": 8, "seq_start": 1, "seq_end": 8})
        self.assertTrue(verify_reference(self.store, summary["reference"]))

    def test_metrics_distinguish_missing_from_zero(self):
        self.append(
            {"event": "action_started", "step_id": "s1", "pacing": {"waited": 0.0}},
            {"event": "action_finished", "step_id": "s1", "outcome": "ok",
             "metrics": {"tree_reads": 0, "tree_ms": 0.0, "explicit_wait_ms": 0.0}},
            {"event": "action_started", "step_id": "s2"},
            {"event": "action_finished", "step_id": "s2", "outcome": "action_failed"},
        )
        summary = summarize_run(self.store, self.run_id)
        for key in ("tree_reads", "tree_ms", "explicit_wait_ms"):
            self.assertEqual(summary["metrics"][key],
                             {"present": 1, "missing": 1, "total": 0})
        self.assertEqual(summary["pacing"]["waited"],
                         {"present": 1, "missing": 1, "total": 0.0})
        # The store treats a failed action as an unresolved effect, not a clean finish.
        self.assertEqual(summary["counts"]["unknown"], 1)

    def test_all_missing_metrics_are_not_reported_as_zero(self):
        self.append(
            {"event": "action_started", "step_id": "s1"},
            {"event": "action_finished", "step_id": "s1", "outcome": "ok"},
        )
        summary = summarize_run(self.store, self.run_id)
        for key in ("tree_reads", "tree_ms", "explicit_wait_ms"):
            self.assertEqual(summary["metrics"][key],
                             {"present": 0, "missing": 1, "total": None})
        self.assertEqual(summary["pacing"]["waited"],
                         {"present": 0, "missing": 1, "total": None})

    def test_empty_run_has_no_reference_and_no_invented_totals(self):
        summary = summarize_run(self.store, self.run_id)
        self.assertIsNone(summary["reference"])
        self.assertEqual(summary["events"],
                         {"count": 0, "seq_start": None, "seq_end": None})
        self.assertEqual(summary["counts"],
                         {"observation": 0, "action": 0, "check": 0, "unknown": 0})
        self.assertIsNone(summary["metrics"]["tree_ms"]["total"])
        self.assertIsNone(summary["pacing"]["waited"]["total"])

    def test_summary_contains_no_event_payload_text(self):
        self.append(
            {"event": "observation", "observation_id": "o_1", "count": 3},
            {"event": "action_started", "step_id": "s1",
             "execution": {"text": "SECRET-INPUT"}},
            {"event": "action_finished", "step_id": "s1", "outcome": "action_failed",
             "detail": "SECRET-UI-LABEL", "labels": ["SECRET-LABEL"],
             "metrics": {"tree_reads": 1, "tree_ms": 1.0, "explicit_wait_ms": 0.0}},
        )
        rendered = json.dumps(summarize_run(self.store, self.run_id))
        for secret in ("SECRET-INPUT", "SECRET-UI-LABEL", "SECRET-LABEL"):
            self.assertNotIn(secret, rendered)


class CodeDigest(unittest.TestCase):
    def test_digest_tracks_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.py"
            second = Path(tmp) / "b.py"
            first.write_text("a = 1\n", encoding="utf-8")
            second.write_text("b = 2\n", encoding="utf-8")
            digests = code_digest([first, second])
            self.assertEqual(digests[str(first)],
                             hashlib.sha256(b"a = 1\n").hexdigest())
            self.assertNotEqual(digests[str(first)], digests[str(second)])
            first.write_text("a = 3\n", encoding="utf-8")
            self.assertNotEqual(code_digest([first])[str(first)], digests[str(first)])


if __name__ == "__main__":
    unittest.main()
