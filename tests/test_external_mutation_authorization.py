"""external_mutation runs only under one explicit, mismatch-refusing grant."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from journey_pilot import adapter, session
from journey_pilot.privacy import private_digest
from journey_pilot.store import Store

TITLE = "示例帖子标题"
AUTHOR = "示例作者"
TEXT = "这是一段用于离线测试的中文评论文本，包含全角标点，但不含真实个人信息。"
SEND = {"step_id": "send", "type": "tap", "locator": {"text": "发送"},
        "effect_class": "external_mutation"}
INPUT = {"step_id": "comment", "type": "input_text", "effect_class": "local_input",
         "locator": {"class": "android.widget.EditText"}, "method": "paste", "text": TEXT}
NODES = [{"text": TITLE}, {"text": AUTHOR}, {"text": TEXT}, {"text": "发送"}, {"text": "评论"}]


class ExternalMutationAuthorization(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name))
        patches = [
            mock.patch.object(adapter, "status", return_value={
                "bound": "dev", "devices": [{"serial": "dev"}], "ready": True}),
            mock.patch.object(adapter, "harness_read",
                              return_value={"ok": True, "nodes": NODES}),
            mock.patch("journey_pilot.pacing.before_action", return_value={}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        opened = session.start(self.store, {"device_id": "dev", "intent": "xhs comment"})
        self.assertEqual(opened.status, "ok")
        self.base = {"run_id": opened.run_id,
                     "lease_token": opened.data["lease_token"], "device_id": "dev"}

    def authorize(self, **overrides):
        request = {**self.base, "post": {"title": TITLE, "author": AUTHOR},
                   "action": SEND, "text": TEXT, **overrides}
        return session.authorize(self.store, request)

    def test_unauthorized_external_mutation_stays_paused(self):
        with mock.patch.object(adapter, "run_action") as action:
            plain = session.act(self.store, {**self.base, "request_id": "s1", "action": SEND})
            legacy_flag = session.act(self.store, {**self.base, "request_id": "s2",
                                                   "action": SEND,
                                                   "authorization": {"approved": True}})
            self.assertEqual(plain.error["code"], "needs_authorization")
            self.assertEqual(legacy_flag.error["code"], "needs_authorization")
            action.assert_not_called()

    def test_authorize_requires_the_exact_live_post_identity(self):
        with mock.patch.object(adapter, "harness_read",
                               return_value={"ok": True, "nodes": [{"text": TITLE}]}):
            missing_author = self.authorize()
        self.assertEqual(missing_author.error["code"], "precondition_mismatch")
        self.assertNotIn("grant_id", missing_author.data)
        with mock.patch.object(adapter, "harness_read",
                               return_value={"ok": True, "nodes": [{"text": "别的帖子"},
                                                                   {"text": AUTHOR}]}):
            wrong_title = self.authorize()
        self.assertEqual(wrong_title.error["code"], "precondition_mismatch")

    def test_authorize_rejects_non_external_or_non_tap_action(self):
        nav = self.authorize(action={**SEND, "effect_class": "navigation"})
        self.assertEqual(nav.error["code"], "invalid_request")
        unsupported = self.authorize(action={**SEND, "type": "key"})
        self.assertEqual(unsupported.error["code"], "invalid_request")

    def test_matching_run_text_and_locator_execute_exactly_once(self):
        grant = self.authorize()
        self.assertEqual(grant.status, "recorded")
        grant_id = grant.data["grant_id"]
        auth = {"authorization": {"grant_id": grant_id, "text": TEXT}}

        def fake_action(action, serial=None, timeout=90):
            return {"ok": True, "value": TEXT if action.get("type") == "input_text" else None}

        with mock.patch.object(adapter, "run_action", side_effect=fake_action) as action:
            ungranted_input = session.act(self.store, {**self.base, "request_id": "input-ungranted",
                                                       "action": INPUT})
            self.assertEqual(ungranted_input.error["code"], "needs_authorization")
            action.assert_not_called()

            before_text = session.act(self.store, {**self.base, "request_id": "send-early",
                                                   "action": SEND, **auth})
            self.assertEqual(before_text.error["code"], "needs_authorization")
            action.assert_not_called()

            wrong_text = session.act(self.store, {**self.base, "request_id": "input-wrong",
                                                  "action": {**INPUT, "text": TEXT + "改"},
                                                  **auth})
            self.assertEqual(wrong_text.error["code"], "needs_authorization")

            typed = session.act(self.store, {**self.base, "request_id": "input-ok",
                                             "action": INPUT, **auth})
            self.assertEqual(typed.status, "recorded")
            self.assertEqual(action.call_count, 1)

            other_target = session.act(self.store, {**self.base, "request_id": "send-wrong-target",
                                                    "action": {**SEND, "locator": {"text": "发布"}},
                                                    **auth})
            self.assertEqual(other_target.error["code"], "needs_authorization")
            self.assertEqual(action.call_count, 1)

            sent = session.act(self.store, {**self.base, "request_id": "send-ok",
                                            "action": SEND, **auth})
            self.assertEqual(sent.status, "recorded")
            self.assertEqual(action.call_count, 2)
            self.assertEqual(action.call_args.args[0]["locator"], {"text": "发送"})

            replay = session.act(self.store, {**self.base, "request_id": "send-replay",
                                              "action": SEND, **auth})
            self.assertEqual(replay.error["code"], "needs_authorization")
            self.assertEqual(action.call_count, 2, "a grant is one-time")

    def _typed_grant(self):
        grant = self.authorize()
        auth = {"authorization": {"grant_id": grant.data["grant_id"], "text": TEXT}}
        with mock.patch.object(adapter, "run_action",
                               return_value={"ok": True, "value": TEXT}):
            self.assertEqual(session.act(self.store, {**self.base, "request_id": "input",
                                                      "action": INPUT, **auth}).status,
                             "recorded")
        return auth

    def test_send_requires_the_authorized_text_in_the_live_tree(self):
        auth = self._typed_grant()
        with mock.patch.object(adapter, "harness_read",
                               return_value={"ok": True, "nodes": [{"text": "发送"}]}), \
             mock.patch.object(adapter, "run_action") as action:
            send = session.act(self.store, {**self.base, "request_id": "send-no-text",
                                            "action": SEND, **auth})
        self.assertEqual(send.error["code"], "precondition_mismatch")
        action.assert_not_called()
        row = json.loads(next((self.store.root / "authorizations").glob("*.json")).read_text())
        self.assertEqual(row["state"], "prepared", "a rejected live check must not burn the grant")

    def test_send_requires_a_unique_live_target(self):
        auth = self._typed_grant()
        with mock.patch.object(adapter, "harness_read",
                               return_value={"ok": True, "nodes": [{"text": TEXT},
                                                                   {"text": "发送"},
                                                                   {"text": "发送"}]}), \
             mock.patch.object(adapter, "run_action") as action:
            send = session.act(self.store, {**self.base, "request_id": "send-ambiguous",
                                            "action": SEND, **auth})
        self.assertEqual(send.error["code"], "precondition_mismatch")
        action.assert_not_called()

    def test_bound_opener_is_the_only_navigation_allowed_before_input(self):
        opener = {"step_id": "open-editor", "type": "tap",
                  "locator": {"text": "评论"}, "effect_class": "navigation"}
        grant = self.authorize(opener=opener)
        auth = {"authorization": {"grant_id": grant.data["grant_id"], "text": TEXT}}
        self.assertEqual(grant.data["opener_digest"],
                         private_digest(self.store, opener))
        with mock.patch.object(adapter, "run_action",
                               side_effect=lambda action, serial=None, timeout=90: {
                                   "ok": True,
                                   "value": TEXT if action.get("type") == "input_text" else None}):
            early = session.act(self.store, {**self.base, "request_id": "input-early",
                                             "action": INPUT, **auth})
            self.assertEqual(early.error["code"], "needs_authorization")
            opened = session.act(self.store, {**self.base, "request_id": "open",
                                              "action": opener})
            self.assertEqual(opened.status, "recorded")
            typed = session.act(self.store, {**self.base, "request_id": "input-after-open",
                                             "action": INPUT, **auth})
            self.assertEqual(typed.status, "recorded")
            sent = session.act(self.store, {**self.base, "request_id": "send-after-open",
                                            "action": SEND, **auth})
            self.assertEqual(sent.status, "recorded")

    def test_any_other_action_after_prepare_invalidates_the_grant(self):
        grant = self.authorize()
        auth = {"authorization": {"grant_id": grant.data["grant_id"], "text": TEXT}}
        with mock.patch.object(adapter, "run_action", return_value={"ok": True}):
            left = session.act(self.store, {**self.base, "request_id": "leave",
                                            "action": {"step_id": "leave", "type": "key",
                                                       "key": "back"}})
        self.assertEqual(left.status, "recorded")
        with mock.patch.object(adapter, "run_action") as action:
            send = session.act(self.store, {**self.base, "request_id": "send-after-leave",
                                            "action": SEND, **auth})
        self.assertEqual(send.error["code"], "needs_authorization")
        action.assert_not_called()
        row = json.loads(next((self.store.root / "authorizations").glob("*.json")).read_text())
        self.assertEqual(row["state"], "invalidated")

    def test_failed_input_never_stamps_the_grant(self):
        grant = self.authorize()
        auth = {"authorization": {"grant_id": grant.data["grant_id"], "text": TEXT}}
        with mock.patch.object(adapter, "run_action",
                               return_value={"ok": False, "code": "action_failed"}):
            failed = session.act(self.store, {**self.base, "request_id": "input-fail",
                                               "action": INPUT, **auth})
        self.assertEqual(failed.error["code"], "outcome_unknown")
        row = json.loads(next((self.store.root / "authorizations").glob("*.json")).read_text())
        self.assertEqual(row["state"], "prepared")
        self.assertNotIn("text_entered_at", row)
        with mock.patch.object(adapter, "run_action") as action:
            send = session.act(self.store, {**self.base, "request_id": "send-after-fail",
                                            "action": SEND, **auth})
            self.assertEqual(send.error["code"], "outcome_unknown")
            action.assert_not_called()

    def test_grant_is_bound_to_its_run(self):
        grant = self.authorize()
        self.assertEqual(session.finish(self.store, self.base).status, "recorded")
        other = session.start(self.store, {"device_id": "dev", "intent": "other"})
        other_base = {"run_id": other.run_id,
                      "lease_token": other.data["lease_token"], "device_id": "dev"}
        with mock.patch.object(adapter, "run_action") as action:
            result = session.act(self.store, {**other_base, "request_id": "cross-run",
                                              "action": INPUT,
                                              "authorization": {"grant_id": grant.data["grant_id"]}})
            self.assertEqual(result.error["code"], "needs_authorization")
            action.assert_not_called()

    def test_unknown_send_outcome_is_not_redispatched(self):
        grant = self.authorize()
        auth = {"authorization": {"grant_id": grant.data["grant_id"], "text": TEXT}}
        with mock.patch.object(adapter, "run_action",
                               side_effect=[{"ok": True, "value": TEXT},
                                            {"code": "action_failed"}]) as action:
            self.assertEqual(session.act(self.store, {**self.base, "request_id": "input",
                                                      "action": INPUT, **auth}).status, "recorded")
            first = session.act(self.store, {**self.base, "request_id": "send",
                                             "action": SEND, **auth})
            retry = session.act(self.store, {**self.base, "request_id": "send",
                                             "action": SEND, **auth})
            new_id = session.act(self.store, {**self.base, "request_id": "send-new",
                                              "action": SEND, **auth})
        self.assertEqual(first.error["code"], "outcome_unknown")
        self.assertEqual(retry.error["code"], "outcome_unknown")
        # The open action effect blocks a new request before the burned grant is read.
        self.assertEqual(new_id.error["code"], "outcome_unknown")
        self.assertEqual(action.call_count, 2)


if __name__ == "__main__":
    unittest.main()
