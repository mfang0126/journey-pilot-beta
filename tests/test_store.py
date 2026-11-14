"""Store tests: atomic JSON, durable append, truncation recovery, privacy mode.

Stage 2 acceptance (gate A) needs the store to prove:
  * a JSON document is never half-written;
  * an event line is on disk (fsynced) before the call returns;
  * a crash mid-append does not corrupt the run;
  * the directory is private to the user;
  * the location is shared across cwd and processes.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from journey_pilot.paths import store_root
from journey_pilot.store import Store, atomic_write_json, read_json


def concurrent_writer(root, start):
    store = Store(Path(root))
    start.wait(10)
    for i in range(20):
        store.append_event("run-1", {"event": "worker", "unknown_steps": ["step" + str(n) for n in range(1000)]})
        atomic_write_json(store.root / "shared.json", {"pid": os.getpid(), "i": i})


class StoreLayout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "jp"

    def tearDown(self):
        self.tmp.cleanup()

    def test_layout_is_created_private(self):
        store = Store(self.root)
        for path in (store.root, store.runs, store.journeys, store.locks):
            self.assertTrue(path.is_dir())
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o700, f"{path} is {oct(mode)}")

    def test_env_var_overrides_the_default(self):
        os.environ["JOURNEY_PILOT_HOME"] = str(self.root)
        try:
            self.assertEqual(store_root(), self.root.resolve())
        finally:
            del os.environ["JOURNEY_PILOT_HOME"]

    def test_default_root_is_under_local_share(self):
        os.environ.pop("JOURNEY_PILOT_HOME", None)
        self.assertEqual(
            store_root(),
            (Path.home() / ".local" / "share" / "journey-pilot").resolve(),
        )

    def test_no_create_leaves_the_filesystem_alone(self):
        store = Store(self.root, create=False)
        self.assertFalse(store.root.exists())


class AtomicJson(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "nested" / "doc.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_then_read_roundtrip(self):
        atomic_write_json(self.path, {"a": 1, "b": [1, 2]})
        self.assertEqual(read_json(self.path), {"a": 1, "b": [1, 2]})

    def test_overwrite_is_atomic_and_leaves_no_tmp(self):
        atomic_write_json(self.path, {"v": 1})
        atomic_write_json(self.path, {"v": 2})
        self.assertEqual(read_json(self.path), {"v": 2})
        leftovers = list(self.path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_read_missing_returns_none(self):
        self.assertIsNone(read_json(Path(self.tmp.name) / "absent.json"))


class DurableAppend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "jp")
        self.store.create_run("run-1", {"run_id": "run-1", "status": "running"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_seq_starts_at_one_and_increments(self):
        self.assertEqual(self.store.append_event("run-1", {"event": "start"}), 1)
        self.assertEqual(self.store.append_event("run-1", {"event": "x"}), 2)
        self.assertEqual([e["seq"] for e in self.store.read_events("run-1")], [1, 2])

    def test_event_is_on_disk_immediately(self):
        self.store.append_event("run-1", {"event": "action_started", "step_id": "s1"})
        raw = self.store.events_path("run-1").read_text(encoding="utf-8")
        self.assertTrue(raw.endswith("\n"))
        self.assertEqual(json.loads(raw.strip())["step_id"], "s1")

    def test_seq_survives_a_new_store_instance(self):
        self.store.append_event("run-1", {"event": "a"})
        other = Store(self.store.root)
        self.assertEqual(other.append_event("run-1", {"event": "b"}), 2)

    def test_long_event_sequence_and_concurrent_writers(self):
        self.store.append_event("run-1", {"event": "large", "unknown_steps": ["step" + str(n) for n in range(2000)]})
        self.assertGreater(self.store.events_path("run-1").stat().st_size, 20000)
        self.assertEqual(self.store.append_event("run-1", {"event": "next"}), 2)
        ctx = multiprocessing.get_context("spawn")
        start = ctx.Event()
        workers = [ctx.Process(target=concurrent_writer, args=(str(self.store.root), start))
                   for _ in range(3)]
        try:
            for worker in workers:
                worker.start()
            start.set()
            for worker in workers:
                worker.join(20)
                self.assertEqual(worker.exitcode, 0)
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join()
        events = self.store.read_events("run-1")
        self.assertEqual([e["seq"] for e in events], list(range(1, 63)))
        self.assertEqual(read_json(self.store.root / "shared.json")["i"], 19)
        self.assertEqual(list(self.store.root.glob("*.tmp")), [])

    def test_append_to_unknown_run_is_refused(self):
        from journey_pilot.store import StoreError
        with self.assertRaises(StoreError):
            self.store.append_event("nope", {"event": "a"})


class TruncationRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "jp")
        self.store.create_run("run-1", {"run_id": "run-1", "device_id": "dev"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_truncated_tail_is_ignored_not_corrupting(self):
        self.store.append_event("run-1", {"event": "a"})
        with open(self.store.events_path("run-1"), "a", encoding="utf-8") as fh:
            fh.write('{"seq": 2, "event": "b", "half')  # no newline: crash
        events = self.store.read_events("run-1")
        self.assertEqual([e["event"] for e in events], ["a"])

    def test_finish_does_not_hide_an_unrecovered_tail(self):
        from journey_pilot import session
        session._write_lease(self.store, "run-1", "token")
        with self.store.events_path("run-1").open("ab") as fh:
            fh.write(b'{"seq":1,"event":"partial')
        result = session.finish(self.store, {"run_id": "run-1", "lease_token": "token"})
        self.assertEqual(result.error["code"], "outcome_unknown")
        self.assertEqual(read_json(self.store.summary_path("run-1"))["status"], "unknown")

    def test_next_append_recovers_tail_and_preserves_unknown_effects(self):
        self.store.append_event("run-1", {"event": "a"})
        with open(self.store.events_path("run-1"), "a", encoding="utf-8") as fh:
            fh.write('{"seq": 9, "event": "b", "half')
        # Preserve the corrupt bytes separately; subsequent records stay readable.
        self.store.append_event("run-1", {"event": "c"})
        events = self.store.read_events("run-1")
        self.assertEqual([e["event"] for e in events], ["a", "log_recovered", "c"])
        saved = self.store.run_dir("run-1") / events[1]["evidence"]
        self.assertEqual(saved.read_bytes(), b'{"seq": 9, "event": "b", "half')
        self.assertEqual([e["seq"] for e in events], [1, 2, 3])
        self.assertTrue(self.store.open_actions("run-1"))
        from journey_pilot.store import StoreError
        with self.assertRaises(StoreError):
            self.store.append_event("run-1", {"event": "action_started", "step_id": "unsafe"})

    def test_archive_failure_preserves_original_log_and_blocks_device(self):
        from journey_pilot import adapter, session
        self.store.append_event("run-1", {"event": "a"})
        path = self.store.events_path("run-1")
        with path.open("ab") as fh:
            fh.write(b'{"seq":2,"event":"action_started"')
        original = path.read_bytes()
        session._write_lease(self.store, "run-1", "token")
        with mock.patch("journey_pilot.store.tempfile.NamedTemporaryFile", side_effect=OSError("disk full")), \
             mock.patch.object(adapter, "run_action") as action:
            result = session.act(self.store, {"run_id": "run-1", "lease_token": "token",
                "request_id": "request", "action": {"type": "key", "key": "back"}})
            self.assertEqual(result.error["code"], "outcome_unknown")
            action.assert_not_called()
        self.assertEqual(path.read_bytes(), original)

    def test_unfinished_action_is_reported_as_unknown(self):
        self.store.append_event("run-1", {"event": "action_started", "step_id": "s1"})
        self.store.append_event("run-1", {"event": "action_started", "step_id": "s2"})
        self.store.append_event("run-1", {"event": "action_finished", "step_id": "s2"})
        open_steps = [e["step_id"] for e in self.store.open_actions("run-1")]
        self.assertEqual(open_steps, ["s1"])


if __name__ == "__main__":
    unittest.main()
