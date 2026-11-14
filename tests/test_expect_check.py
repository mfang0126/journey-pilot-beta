"""An act with ``expect`` records a check event, pass or fail, so trace counts it."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from journey_pilot import adapter, session
from journey_pilot.store import Store
from journey_pilot.trace import summarize_run


class ExpectCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))
        patch = mock.patch.object(adapter, "status", return_value={
            "bound": "dev", "devices": [{"serial": "dev"}], "ready": True})
        patch.start()
        self.addCleanup(patch.stop)
        opened = session.start(self.store, {"device_id": "dev", "intent": "test"})
        self.run_id = opened.run_id
        self.base = {"run_id": opened.run_id, "lease_token": opened.data["lease_token"],
                     "device_id": "dev"}

    def act(self, n, result, expect=True):
        action = {"type": "key", "key": "home"}
        if expect:
            action["expect"] = {"text_equals": "x"}
        with mock.patch.object(adapter, "run_action", return_value=result):
            return session.act(self.store, {**self.base, "request_id": f"r{n}", "action": action})

    def checks(self):
        return [e for e in self.store.read_events(self.run_id) if e.get("event") == "check"]

    def test_passing_expect_is_a_check(self):
        self.act(1, {"ok": True})
        self.assertEqual([c["ok"] for c in self.checks()], [True])
        self.assertEqual(summarize_run(self.store, self.run_id)["counts"]["check"], 1)

    def test_failing_expect_is_a_failed_check(self):
        out = self.act(1, {"ok": False, "code": "assertion_failed"})
        self.assertEqual(out.error["code"], "assertion_failed")
        self.assertEqual([c["ok"] for c in self.checks()], [False])
        self.assertEqual(summarize_run(self.store, self.run_id)["counts"]["check"], 1)

    def test_no_expect_no_check(self):
        self.act(1, {"ok": True}, expect=False)
        self.assertEqual(self.checks(), [])

    def test_action_that_never_reached_expect_is_no_check(self):
        self.act(1, {"ok": False, "code": "locator_missing"})
        self.assertEqual(self.checks(), [])


if __name__ == "__main__":
    unittest.main()
