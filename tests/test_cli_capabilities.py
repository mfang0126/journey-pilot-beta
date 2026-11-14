"""capabilities must tell an agent everything it needs without touching a device."""
from __future__ import annotations

import io
import json
import os
import tempfile
from pathlib import Path
import unittest
from unittest import mock

from journey_pilot.cli import COMMANDS, main
from journey_pilot.contracts import SCHEMA_VERSION, validate_result


def run(argv, stdin_text=""):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("sys.stdin", io.StringIO(stdin_text)):
        code = main(argv, stdout=out, stderr=err)
    payload = json.loads(out.getvalue())
    validate_result(payload)
    return code, payload, err.getvalue()


class Capabilities(unittest.TestCase):
    def test_bare_invocation_is_capabilities(self):
        code, payload, _ = run([])
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "ok")

    def test_required_fields(self):
        _, payload, _ = run(["capabilities"])
        data = payload["data"]
        for key in ("store", "store_env_var", "program_version", "adapter_version",
                    "schema_version", "commands", "actions", "effect_classes",
                    "checks", "param_types", "field_types", "request_schema",
                    "minimal_example", "errors", "notes"):
            self.assertIn(key, data)
        self.assertEqual(data["schema_version"], SCHEMA_VERSION)

    def test_store_path_is_absolute_and_does_not_create_anything(self):
        _, payload, _ = run(["capabilities"])
        root = payload["data"]["store"]["root"]
        self.assertTrue(os.path.isabs(root))
        self.assertFalse(os.path.exists(root) and not os.path.isdir(root))

    def test_store_env_var_is_reported(self):
        _, payload, _ = run(["capabilities"])
        self.assertEqual(payload["data"]["store_env_var"], "JOURNEY_PILOT_HOME")

    def test_every_documented_command_is_listed_once(self):
        _, payload, _ = run(["capabilities"])
        names = [c["name"] for c in payload["data"]["commands"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(names), len(COMMANDS))
        self.assertIn("capabilities", names)
        for expected in ("status", "discover", "session start", "observe", "act",
                         "session finish", "inspect", "propose", "validate", "run",
                         "resume", "cancel"):
            self.assertIn(expected, names)

    def test_shipped_flag_matches_dispatch(self):
        _, payload, _ = run(["capabilities"])
        flags = {c["name"]: c["implemented"] for c in payload["data"]["commands"]}
        self.assertTrue(flags["capabilities"])
        self.assertTrue(flags["status"])
        # Anything not marked shipped must answer capability_gap, not pretend.
        for name, shipped in flags.items():
            if shipped:
                continue
            parts = name.split()
            code, reply, _ = run(parts)
            self.assertEqual(code, 4, name)
            self.assertEqual(reply["error"]["code"], "capability_gap", name)

    def test_error_catalogue_is_advertised(self):
        _, payload, _ = run(["capabilities"])
        codes = {e["code"] for e in payload["data"]["errors"]}
        self.assertIn("locked", codes)
        self.assertIn("needs_authorization", codes)
        for entry in payload["data"]["errors"]:
            self.assertIn(entry["next_action"], {"status", "inspect", "resume",
                                                 "discover", "capabilities",
                                                 "session start"})

    def test_request_schema_and_example_are_present(self):
        _, payload, _ = run(["capabilities"])
        schema = payload["data"]["request_schema"]
        self.assertEqual(schema["type"], "object")
        example = payload["data"]["minimal_example"]
        self.assertEqual(set(example), {"command", "request"})


class ExecutableContract(unittest.TestCase):
    def test_advertised_actions_build_and_unsupported_actions_never_dispatch(self):
        from journey_pilot import adapter, journey
        _, reply, _ = run(["capabilities"])
        self.assertEqual(set(reply["data"]["actions"]), journey.ACTION_WHITELIST)
        for kind in reply["data"]["actions"]:
            action = {"type": kind, "locator": {"text": "target"}, "x": 10, "y": 20}
            if kind == "launch_activity":
                action["component"] = "com.android.settings/.Settings"
            if kind == "share_text":
                action.update(package="com.example.chat", text="hi",
                              effect_class="external_mutation")
            if kind == "input_text":
                action["text"] = "hi"  # required: a missing text would clear the field
            adapter.build_action_script(action)
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict(os.environ, {"JOURNEY_PILOT_HOME": directory}), \
             mock.patch.object(adapter, "run_action") as dispatch:
            for kind in ("assert", "find", "read_state", "screenshot", "unknown"):
                code, result, _ = run(["act", "--request-stdin"], json.dumps({
                    "run_id": "r_test", "lease_token": "lease", "request_id": "req",
                    "action": {"type": kind}}))
                self.assertEqual(result["error"]["code"], "unsupported_input")
                self.assertEqual(code, 4)
            _, external, _ = run(["act", "--request-stdin"], json.dumps({
                "run_id": "r_test", "lease_token": "lease", "request_id": "external",
                "action": {"type": "key", "effect_class": "external_mutation"},
                "authorization": {"approved": True}}))
            self.assertEqual(external["error"]["code"], "needs_authorization")
            dispatch.assert_not_called()

    def test_invalid_checks_reject_proposals_and_stored_recipes_before_actions(self):
        from journey_pilot import adapter, journey, runner
        from journey_pilot.store import Store
        step = {"step_id": "read", "action": "read_field", "locator": {"text": "target"}}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(adapter, "run_action") as dispatch, \
             mock.patch.object(adapter, "harness_read") as observe:
            store = Store(Path(directory))
            for spec in ({"all": []}, {"not": {}}, {"app_is": "app"},
                         {"field_equals": "x"}, {"jev": {"expect": "ok"}},
                         {"element_count": {"text": "x"}, "equals": True}):
                recipe = {"task_key": "read", "intent": "read target", "steps": [step],
                          "postconditions": spec}
                self.assertIsNotNone(journey.propose(store, recipe).error)
                summary, _ = runner._execute(store, {**recipe, "journey_id": "j_test",
                                                     "version": 1}, "dev", "validate")
                self.assertEqual(summary["failure"]["step_id"], "preflight")
            result = journey.propose(store, {"task_key": "read", "intent": "read target",
                "steps": [{**step, "postcondition": {"all": [{"text_equals": "x"}]}}]})
            self.assertEqual(result.error["code"], "unsupported_input")
            summary, _ = runner._execute(store, {"journey_id": "j_test", "version": 1,
                "steps": [step, {"step_id": "unsupported", "action": "assert"}],
                "postconditions": {"text_equals": "target"}}, "dev", "validate")
            self.assertEqual(summary["failure"]["code"], "unsupported_input")
            dispatch.assert_not_called()
            observe.assert_not_called()

    def test_expect_is_preserved_and_conflicts_reject_before_actions(self):
        from journey_pilot import adapter, runner
        from journey_pilot.store import Store
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(adapter, "run_action", return_value={"ok": True}) as dispatch:
            store = Store(Path(directory))
            step = {"step_id": "back", "action": "key", "expect": {"text_equals": "Home"}}
            doc = {"journey_id": "j_test", "version": 1, "steps": [step]}
            runner._execute(store, doc, "dev", "validate")
            self.assertEqual(dispatch.call_args.args[0]["expect"], step["expect"])
            dispatch.reset_mock()
            for bad in ({**step, "expect": {"all": []}},
                        {**step, "postcondition": {"text_equals": "Elsewhere"}}):
                summary, _ = runner._execute(store, {**doc, "steps": [bad]}, "dev", "validate")
                self.assertEqual(summary["failure"]["step_id"], "preflight")
            dispatch.assert_not_called()

    def test_action_annotations_cannot_inject_python(self):
        from journey_pilot import adapter
        payload = "label\nraise RuntimeError('injected')\r#"
        script = adapter.build_action_script({"type": "key", "task": payload, "step_id": payload})
        press = mock.Mock()
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            exec(script, {"press": press})
        press.assert_called_once_with("back")
        self.assertTrue(json.loads(out.getvalue())["ok"])

    def test_cli_propose_inspect_roundtrip_and_error_navigation(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict(os.environ, {"JOURNEY_PILOT_HOME": directory}):
            code, reply, _ = run(["propose", "--request-stdin"], json.dumps({
                "task_key": "read-title", "intent": "Read visible title",
                "steps": [{"step_id": "title", "action": "read_field",
                           "locator": {"text": "Title"}}],
                "postconditions": {"text_equals": "Title"}}))
            self.assertEqual(code, 0)
            code, inspected, _ = run(["inspect", "--request-stdin"], json.dumps({
                "journey_id": reply["data"]["journey_id"]}))
            self.assertEqual(code, 0)
            self.assertIn("Title", json.dumps(inspected))
        _, caps, _ = run(["capabilities"])
        supported = {c["name"] for c in caps["data"]["commands"] if c["implemented"]}
        self.assertTrue(all(e["next_action"] in supported for e in caps["data"]["errors"]))


class Dispatch(unittest.TestCase):
    def test_resume_requires_explicit_request(self):
        code, payload, _ = run(["resume"])
        self.assertEqual(code, 2)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_unknown_command_is_a_request_error(self):
        code, payload, stderr = run(["frobnicate"])
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertIn("unknown command", stderr)

    def test_bad_stdin_is_rejected_not_ignored(self):
        code, payload, _ = run(["capabilities", "--request-stdin"], stdin_text="{oops")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_valid_stdin_is_accepted(self):
        code, payload, _ = run(["capabilities", "--request-stdin"], stdin_text='{"device_id": "x"}')
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "ok")


if __name__ == "__main__":
    unittest.main()
