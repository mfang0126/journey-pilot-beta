"""T3: reviews keep narrative separate from verified trace references.

These tests use a temporary Store and a stubbed trace verifier. They never
touch a device, a real run log, or the user's store.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from journey_pilot import review
from journey_pilot.store import Store, atomic_write_json


class ReviewStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "jp")
        self.store.create_run("r1", {
            "run_id": "r1", "kind": "exploration", "intent": "Read a title",
            "task_key": "read-title", "status": "recorded",
            "started_at": 1.0, "finished_at": 2.0,
        })
        self.valid_refs: list[dict] = []
        self.patcher = mock.patch.object(
            review, "_verify_reference", side_effect=self._verify)
        self.verify = self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _verify(self, store, reference):
        return reference in self.valid_refs

    def ref(self, seq_start=1, seq_end=1, run_id="r1"):
        return {"run_id": run_id, "seq_start": seq_start, "seq_end": seq_end,
                "digest": "a" * 64}

    def request(self, **overrides):
        base = {
            "run_id": "r1",
            "task_key": "read-title",
            "questions": {name: f"answer for {name}" for name in review.QUESTIONS},
            "claims": [{"kind": "fact", "text": "the run completed",
                        "references": [self.ref()]}],
            "lesson": {
                "rule": "read the visible title before proposing a recipe",
                "applies_when": "the title is exposed in the accessibility tree",
                "invalid_when": "the page is not the target post",
            },
        }
        base.update(overrides)
        return base

    def review_doc(self, version=1):
        path = self.store.run_dir("r1") / "reviews" / f"{version}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def lesson_docs(self):
        folder = self.store.root / "lessons"
        if not folder.is_dir():
            return []
        return [json.loads(path.read_text(encoding="utf-8"))
                for path in sorted(folder.glob("*.json"))]

    def lesson_paths(self):
        folder = self.store.root / "lessons"
        return sorted(folder.glob("*.json")) if folder.is_dir() else []

    def rewrite_lesson(self, **changes):
        path = self.lesson_paths()[0]
        doc = json.loads(path.read_text(encoding="utf-8"))
        for key, value in changes.items():
            if value is None:
                doc.pop(key, None)
            else:
                doc[key] = value
        path.write_text(json.dumps(doc), encoding="utf-8")
        return doc


class SaveReview(ReviewStore):
    def test_valid_references_save_review_and_lesson(self):
        ref = self.ref()
        self.valid_refs.append(ref)
        result = review.save_review(self.store, self.request())

        self.assertIsNone(result.error)
        self.assertEqual(result.status, "recorded")
        self.assertEqual(result.run_id, "r1")
        self.assertEqual(result.data["review_version"], 1)
        self.assertEqual(result.data["review_path"], "runs/r1/reviews/1.json")
        self.assertFalse(result.data["executable"])

        doc = self.review_doc()
        self.assertEqual(doc["kind"], "review")
        self.assertEqual(doc["review_id"], "r1:review:1")
        self.assertEqual(doc["version"], 1)
        self.assertEqual(set(doc["questions"]), set(review.QUESTIONS))
        self.assertEqual(doc["claims"][0]["kind"], "fact")
        self.assertEqual(doc["claims"][0]["references"], [ref])
        self.assertFalse(doc["executable"])
        self.assertEqual(doc["redaction"], "caller_responsible")
        self.assertEqual(self.verify.call_count, 1)

        lessons = self.lesson_docs()
        self.assertEqual(len(lessons), 1)
        lesson = lessons[0]
        self.assertEqual(lesson["review"], {
            "run_id": "r1", "version": 1, "review_id": "r1:review:1"})
        self.assertEqual(lesson["task_key"], "read-title")
        canonical = json.dumps(doc, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode("utf-8")
        self.assertEqual(lesson["review_digest"],
                         hashlib.sha256(canonical).hexdigest())
        self.assertFalse(lesson["executable"])
        self.assertFalse(lesson["runnable"])

    def test_invalid_reference_is_rejected_without_writing(self):
        result = review.save_review(self.store, self.request())

        self.assertIsNotNone(result.error)
        self.assertEqual(result.error["code"], "invalid_request")
        self.assertEqual(self.verify.call_count, 1)
        self.assertFalse((self.store.run_dir("r1") / "reviews").exists())
        self.assertEqual(self.lesson_docs(), [])

    def test_malformed_reference_is_rejected_before_verification(self):
        malformed = {"run_id": "r1", "seq_start": 0, "seq_end": 1}
        request = self.request(claims=[{
            "kind": "fact", "text": "bad range", "references": [malformed]}])
        result = review.save_review(self.store, request)

        self.assertEqual(result.error["code"], "invalid_request")
        self.assertFalse(self.verify.called)
        self.assertFalse((self.store.run_dir("r1") / "reviews").exists())

    def test_fact_needs_a_reference_but_hypothesis_does_not(self):
        fact = self.request(claims=[{"kind": "fact", "text": "no evidence",
                                     "references": []}])
        self.assertEqual(review.save_review(self.store, fact).error["code"],
                         "invalid_request")

        hypothesis = self.request(claims=[{"kind": "hypothesis",
                                           "text": "maybe the tree changed",
                                           "references": []}])
        result = review.save_review(self.store, hypothesis)
        self.assertIsNone(result.error)
        doc = self.review_doc()
        self.assertEqual(doc["claims"][0]["kind"], "hypothesis")
        self.assertEqual(doc["claims"][0]["references"], [])

    def test_hypothesis_with_a_reference_is_not_promoted(self):
        ref = self.ref(seq_start=1, seq_end=2)
        self.valid_refs.append(ref)
        result = review.save_review(self.store, self.request(claims=[{
            "kind": "hypothesis", "text": "the title may be off-screen",
            "references": [ref]}]))

        self.assertIsNone(result.error)
        doc = self.review_doc()
        self.assertEqual(doc["claims"][0]["kind"], "hypothesis")
        self.assertNotIn("facts", doc)
        self.assertNotIn("claims", self.lesson_docs()[0])

    def test_review_versions_are_immutable_and_numbered(self):
        self.valid_refs.append(self.ref())
        first = review.save_review(self.store, self.request())
        self.assertEqual(first.data["review_version"], 1)
        before = (self.store.run_dir("r1") / "reviews" / "1.json").read_bytes()

        second = review.save_review(self.store, self.request(
            claims=[{"kind": "hypothesis", "text": "second pass",
                     "references": []}]))
        self.assertEqual(second.data["review_version"], 2)
        after = (self.store.run_dir("r1") / "reviews" / "1.json").read_bytes()
        self.assertEqual(before, after)
        self.assertTrue((self.store.run_dir("r1") / "reviews" / "2.json").is_file())
        self.assertEqual(self.review_doc(1)["claims"][0]["kind"], "fact")
        self.assertEqual(self.review_doc(2)["claims"][0]["kind"], "hypothesis")

    def test_lesson_ids_are_unique_and_reference_their_review(self):
        self.valid_refs.append(self.ref())
        first = review.save_review(self.store, self.request())
        first_lesson = first.data["lesson"]
        first_bytes = (self.store.root / "lessons" /
                       f"{first_lesson['lesson_id']}.json").read_bytes()

        second = review.save_review(self.store, self.request())
        second_lesson = second.data["lesson"]

        self.assertNotEqual(first_lesson["lesson_id"], second_lesson["lesson_id"])
        self.assertEqual(first_lesson["review"]["version"], 1)
        self.assertEqual(second_lesson["review"]["version"], 2)
        self.assertEqual((self.store.root / "lessons" /
                          f"{first_lesson['lesson_id']}.json").read_bytes(),
                         first_bytes)
        self.assertEqual(len(self.lesson_docs()), 2)

    def test_narrative_does_not_copy_raw_events_or_outputs(self):
        self.store.append_event("r1", {"event": "observation",
                                       "text": "SECRET EVENT TEXT"})
        atomic_write_json(self.store.run_dir("r1") / "task-result.json",
                          {"status": "completed", "data": {"post_text": "SECRET OUTPUT"}})
        self.valid_refs.append(self.ref())
        result = review.save_review(self.store, self.request(
            questions={name: "redacted narrative" for name in review.QUESTIONS}))
        self.assertIsNone(result.error)

        blobs = [path.read_text(encoding="utf-8")
                 for path in (self.store.run_dir("r1") / "reviews").glob("*.json")]
        blobs += [path.read_text(encoding="utf-8")
                  for path in (self.store.root / "lessons").glob("*.json")]
        joined = "\n".join(blobs)
        self.assertNotIn("SECRET EVENT TEXT", joined)
        self.assertNotIn("SECRET OUTPUT", joined)

    def test_rejects_bad_types_lengths_and_paths(self):
        cases = {
            "missing question": {"questions": {name: "x" for name in review.QUESTIONS[:-1]}},
            "extra question": {"questions": {**{name: "x" for name in review.QUESTIONS},
                                             "extra": "x"}},
            "long answer": {"questions": {**{name: "x" for name in review.QUESTIONS},
                                          "cost": "x" * (review.MAX_ANSWER + 1)}},
            "bad claim kind": {"claims": [{"kind": "guess", "text": "x",
                                           "references": []}]},
            "extra claim field": {"claims": [{"kind": "hypothesis", "text": "x",
                                              "references": [], "id": "x"}]},
            "bad lesson": {"lesson": {"rule": "x", "applies_when": "y"}},
            "unknown request field": {"extra": True},
            "path run id": {"run_id": "../escape"},
            "unknown run": {"run_id": "r_missing"},
        }
        for name, override in cases.items():
            with self.subTest(name=name):
                result = review.save_review(self.store, self.request(**override))
                self.assertIsNotNone(result.error, name)
                self.assertEqual(result.error["code"], "invalid_request", name)

    def test_lesson_is_optional(self):
        self.valid_refs.append(self.ref())
        result = review.save_review(self.store, self.request(lesson=None))
        self.assertIsNone(result.error)
        self.assertIsNone(result.data["lesson"])
        self.assertEqual(self.lesson_docs(), [])


class DiscoverLessons(ReviewStore):
    def test_exact_task_key_bounded_and_never_executable(self):
        self.valid_refs.append(self.ref())
        review.save_review(self.store, self.request())
        review.save_review(self.store, self.request())
        review.save_review(self.store, self.request(task_key="other-task"))

        rows = review.discover_lessons(self.store, {"task_key": "read-title"})
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["task_key"] == "read-title" for row in rows))
        self.assertTrue(all(row["source_integrity"] == "verified" for row in rows))
        self.assertTrue(all(row["executable"] is False for row in rows))
        self.assertTrue(all(row["runnable"] is False for row in rows))

        self.assertEqual(review.discover_lessons(
            self.store, {"task_key": "READ-TITLE"}), [])
        self.assertEqual(review.discover_lessons(self.store, {"query": "read-title"}), [])
        self.assertEqual(review.discover_lessons(
            self.store, {"task_key": "read-title", "limit": 1}), rows[:1])
        self.assertEqual(review.discover_lessons(
            self.store, {"task_key": "read-title", "limit": 0}), [])
        self.assertEqual(review.discover_lessons(
            self.store, {"task_key": "read-title", "limit": 51}), [])

    def test_corrupt_lesson_files_are_skipped(self):
        self.valid_refs.append(self.ref())
        review.save_review(self.store, self.request())
        (self.store.root / "lessons" / "broken.json").write_text(
            "{not json", encoding="utf-8")
        rows = review.discover_lessons(self.store, {"task_key": "read-title"})
        self.assertEqual(len(rows), 1)

    def test_lesson_whose_reference_no_longer_verifies_is_not_returned(self):
        self.valid_refs.append(self.ref())
        review.save_review(self.store, self.request())
        self.assertEqual(len(review.discover_lessons(
            self.store, {"task_key": "read-title"})), 1)

        self.valid_refs.clear()
        self.assertEqual(review.discover_lessons(
            self.store, {"task_key": "read-title"}), [])

    def test_lesson_whose_review_file_is_missing_is_not_returned(self):
        self.valid_refs.append(self.ref())
        review.save_review(self.store, self.request())
        (self.store.run_dir("r1") / "reviews" / "1.json").unlink()

        self.assertEqual(review.discover_lessons(
            self.store, {"task_key": "read-title"}), [])

    def test_review_with_an_unreferenced_fact_is_not_returned(self):
        self.valid_refs.append(self.ref())
        review.save_review(self.store, self.request())
        path = self.store.run_dir("r1") / "reviews" / "1.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["claims"] = [{"kind": "fact", "text": "no evidence",
                          "references": []}]
        path.write_text(json.dumps(doc), encoding="utf-8")

        self.assertEqual(review.discover_lessons(
            self.store, {"task_key": "read-title"}), [])

    def test_legacy_lesson_without_digest_is_returned_when_references_hold(self):
        self.valid_refs.append(self.ref())
        review.save_review(self.store, self.request())
        self.rewrite_lesson(review_digest=None)

        rows = review.discover_lessons(self.store, {"task_key": "read-title"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_integrity"], "legacy_no_digest")

    def test_legacy_lesson_without_digest_still_needs_valid_references(self):
        self.valid_refs.append(self.ref())
        review.save_review(self.store, self.request())
        self.rewrite_lesson(review_digest=None)

        self.valid_refs.clear()
        self.assertEqual(review.discover_lessons(
            self.store, {"task_key": "read-title"}), [])

    def test_new_lesson_whose_review_content_changed_is_not_returned(self):
        self.valid_refs.append(self.ref())
        review.save_review(self.store, self.request())
        path = self.store.run_dir("r1") / "reviews" / "1.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["questions"]["completion"] = "rewritten after the fact"
        path.write_text(json.dumps(doc), encoding="utf-8")

        self.assertEqual(review.discover_lessons(
            self.store, {"task_key": "read-title"}), [])

    def test_reformatted_review_file_keeps_the_digest_valid(self):
        self.valid_refs.append(self.ref())
        review.save_review(self.store, self.request())
        path = self.store.run_dir("r1") / "reviews" / "1.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(doc, indent=2, sort_keys=False),
                        encoding="utf-8")

        rows = review.discover_lessons(self.store, {"task_key": "read-title"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_integrity"], "verified")

    def test_new_lesson_with_a_malformed_digest_is_not_returned(self):
        self.valid_refs.append(self.ref())
        review.save_review(self.store, self.request())
        self.rewrite_lesson(review_digest="not-a-digest")

        self.assertEqual(review.discover_lessons(
            self.store, {"task_key": "read-title"}), [])

    def test_planted_lesson_without_a_review_pointer_is_not_returned(self):
        atomic_write_json(self.store.root / "lessons" / "planted.json", {
            "kind": "lesson", "lesson_id": "lesson-planted",
            "task_key": "read-title", "rule": "run this",
            "executable": True, "runnable": True, "created_at": 1.0,
        })

        self.assertEqual(review.discover_lessons(
            self.store, {"task_key": "read-title"}), [])


class RealTraceIntegration(unittest.TestCase):
    """The production verifier accepts a built reference and rejects tampering."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "jp")
        self.store.create_run("r1", {"run_id": "r1", "task_key": "read-title",
                                     "status": "recorded"})
        self.store.append_event("r1", {"event": "observation", "step_id": "s1"})
        self.store.append_event("r1", {"event": "check", "step_id": "s1"})

    def tearDown(self):
        self.tmp.cleanup()

    def request(self, references):
        return {
            "run_id": "r1",
            "task_key": "read-title",
            "questions": {name: "answer" for name in review.QUESTIONS},
            "claims": [{"kind": "fact", "text": "the check ran",
                        "references": references}],
            "lesson": {"rule": "check after the observation",
                       "applies_when": "the run recorded a check",
                       "invalid_when": "the check is missing"},
        }

    def test_built_reference_is_accepted_and_tampered_reference_is_rejected(self):
        from journey_pilot.trace import build_reference

        reference = build_reference(self.store, "r1", 1, 2)
        result = review.save_review(self.store, self.request([reference]))
        self.assertIsNone(result.error)
        self.assertEqual(result.data["review_version"], 1)

        tampered = dict(reference)
        tampered["digest"] = "0" * 64
        bad = review.save_review(self.store, self.request([tampered]))
        self.assertEqual(bad.error["code"], "invalid_request")
        self.assertEqual(len(list((self.store.run_dir("r1") / "reviews").glob("*.json"))), 1)

    def test_discover_drops_lesson_after_a_cited_event_is_removed(self):
        from journey_pilot.trace import build_reference

        reference = build_reference(self.store, "r1", 1, 2)
        self.assertIsNone(review.save_review(self.store, self.request([reference])).error)
        self.assertEqual(len(review.discover_lessons(
            self.store, {"task_key": "read-title"})), 1)

        events = self.store.events_path("r1")
        first = events.read_text(encoding="utf-8").splitlines(keepends=True)[0]
        events.write_text(first, encoding="utf-8")
        self.assertEqual(review.discover_lessons(
            self.store, {"task_key": "read-title"}), [])

    def test_discover_drops_lesson_after_a_cited_event_is_replaced(self):
        from journey_pilot.trace import build_reference

        reference = build_reference(self.store, "r1", 1, 2)
        self.assertIsNone(review.save_review(self.store, self.request([reference])).error)

        events = self.store.events_path("r1")
        lines = events.read_text(encoding="utf-8").splitlines(keepends=True)
        events.write_text(lines[0] + lines[1].replace('"check"', '"observation"'),
                          encoding="utf-8")
        self.assertEqual(review.discover_lessons(
            self.store, {"task_key": "read-title"}), [])


if __name__ == "__main__":
    unittest.main()
