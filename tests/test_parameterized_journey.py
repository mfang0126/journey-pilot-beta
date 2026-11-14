"""S3 parameter binding: real substitution, strict types, bounded positions.

The two-input test is the point of the module: different values must select
different real targets, checked with the runner's own matcher on fake nodes.
The rest pin the rejection paths that keep a malformed or under-specified
recipe away from the device.
"""
from __future__ import annotations

import copy
import unittest

from journey_pilot import runner
from journey_pilot.journey import JourneyError
from journey_pilot.parameters import (
    bind_recipe,
    validate_parameter_schema,
    validate_recipe_parameters,
)


def schema(*, required: list[str] | None = None, **properties: dict) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(required or [])}


def recipe(parameters: dict, steps: list | None = None, **extra) -> dict:
    return {"parameters": parameters, "steps": steps or [], **extra}


def text_param(**spec) -> dict:
    return {"type": "string", **spec}


class ParameterizedJourney(unittest.TestCase):
    def test_propose_validate_binds_real_targets_and_missing_values_do_not_touch_device(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from journey_pilot import adapter, journey
        from journey_pilot.store import Store
        seen = []
        current = [""]
        def action(spec, **kwargs):
            current[0] = spec["locator"]["text"]
            seen.append(current[0])
            return {"ok": True, "value": current[0]}
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(adapter, "status", return_value={"bound": "dev", "devices": [{}], "ready": True}) as status, \
             mock.patch.object(adapter, "run_action", side_effect=action), \
             mock.patch.object(adapter, "harness_read", side_effect=lambda *a, **kw: {"ok": True, "nodes": [{"text": current[0]}]}), \
             mock.patch("journey_pilot.pacing.before_action", return_value={}):
            store = Store(Path(tmp))
            out = journey.propose(store, {"task_key": "parameter-read", "intent": "read target",
                "parameters": schema(required=["target"], target=text_param()),
                "steps": [{"step_id": "read", "action": "read_field", "locator": {"text": {"param": "target"}}}],
                "postconditions": {"text_equals": {"param": "target"}}})
            self.assertEqual(out.status, "ok")
            missing = runner.validate(store, {"journey_id": out.data["journey_id"], "request_id": "missing"})
            self.assertEqual(missing.error["code"], "missing_parameter")
            status.assert_not_called()
            for i, target in enumerate(("First", "Second", "First")):
                result = runner.validate(store, {"journey_id": out.data["journey_id"], "request_id": str(i),
                                                "parameters": {"target": target}})
                self.assertEqual(result.status, "completed")
            self.assertEqual(seen, ["First", "Second", "First"])
            self.assertEqual(result.data["state"], "verified")

    # ------------------------------------------------------------ binding
    def test_two_values_select_different_targets(self):
        doc = {
            "parameters": {
                "type": "object",
                "properties": {"target": {"type": "string",
                                          "enum": ["Search", "Settings"]}},
                "required": ["target"],
            },
            "steps": [{
                "step_id": "open_target", "action": "tap",
                "locator": {"text": {"param": "target"}},
                "postcondition": {"element_exists": {"text": {"param": "target"}}},
            }],
            "postconditions": {"text_equals": {"param": "target"}},
        }
        search = bind_recipe(doc, {"target": "Search"})
        settings = bind_recipe(doc, {"target": "Settings"})

        self.assertEqual(search["steps"][0]["locator"], {"text": "Search"})
        self.assertEqual(settings["steps"][0]["locator"], {"text": "Settings"})
        self.assertEqual(search["steps"][0]["postcondition"],
                         {"element_exists": {"text": "Search"}})

        nodes = [{"text": "Search", "id": "search"},
                 {"text": "Settings", "id": "settings"}]
        self.assertTrue(runner.check(nodes, {"element_exists":
                                             search["steps"][0]["locator"]}))
        self.assertTrue(runner.check(nodes, {"element_exists":
                                             settings["steps"][0]["locator"]}))

        only_search = [{"text": "Search", "id": "search"}]
        self.assertTrue(runner.check(only_search, search["postconditions"]))
        self.assertFalse(runner.check(only_search, settings["postconditions"]))

    def test_binding_returns_a_deep_copy(self):
        doc = recipe(
            schema(target=text_param()),
            steps=[{"step_id": "s", "action": "tap",
                    "locator": {"text": {"param": "target"}},
                    "postcondition": {"text_equals": {"param": "target"}}}],
            postconditions={"all": [{"text_equals": {"param": "target"}}]},
        )
        original = copy.deepcopy(doc)
        bound = bind_recipe(doc, {"target": "Search"})

        self.assertEqual(doc, original)
        self.assertIsNot(bound, doc)
        self.assertIsNot(bound["steps"][0], doc["steps"][0])
        self.assertIsNot(bound["steps"][0]["locator"], doc["steps"][0]["locator"])
        bound["steps"][0]["locator"]["text"] = "Changed"
        bound["postconditions"]["all"][0]["text_equals"] = "Changed"
        self.assertEqual(doc, original)

    def test_defaults_fill_absent_values(self):
        doc = recipe(
            schema(target=text_param(default="Search", enum=["Search", "Settings"]),
                   count={"type": "integer", "default": 2},
                   enabled={"type": "boolean", "default": False},
                   ratio={"type": "number", "default": 1.5}),
            steps=[{"step_id": "s", "action": "input_text",
                    "locator": {"text": "Box"},
                    "text": {"param": "count"}}],
            postconditions={"text_equals": {"param": "target"}},
            entry_state={"element_exists": {"text": {"param": "target"}}},
        )
        bound = bind_recipe(doc, {})
        self.assertEqual(bound["postconditions"], {"text_equals": "Search"})
        self.assertEqual(bound["entry_state"], {"element_exists": {"text": "Search"}})
        self.assertEqual(bound["steps"][0]["text"], "2")
        # The schema itself is kept for promotion bookkeeping.
        self.assertEqual(bound["parameters"], doc["parameters"])

    def test_scalar_types_are_rendered_into_text_slots(self):
        doc = recipe(
            schema(text=text_param(), integer={"type": "integer"},
                   number={"type": "number"}, boolean={"type": "boolean"}),
            steps=[
                {"step_id": "a", "action": "input_text",
                 "locator": {"text": "Box"}, "text": {"param": "text"}},
                {"step_id": "b", "action": "input_text",
                 "locator": {"text": "Box"}, "text": {"param": "integer"}},
                {"step_id": "c", "action": "input_text",
                 "locator": {"text": "Box"}, "text": {"param": "number"}},
                {"step_id": "d", "action": "input_text",
                 "locator": {"text": "Box"}, "text": {"param": "boolean"}},
            ],
        )
        bound = bind_recipe(doc, {"text": "hello", "integer": 42,
                                  "number": 1.5, "boolean": True})
        self.assertEqual([s["text"] for s in bound["steps"]],
                         ["hello", "42", "1.5", "true"])

    def test_allowed_positions_cover_locator_fallback_entry_state_and_checks(self):
        doc = recipe(
            schema(target=text_param()),
            steps=[{
                "step_id": "s", "action": "tap",
                "locator": {"text": {"param": "target"}},
                "fallback": {"alternatives": [
                    {"text_contains": {"param": "target"}},
                    {"text": "Other"},
                ]},
                "expect": {"text_equals": {"param": "target"}},
                "postcondition": {"element_exists": {"text": {"param": "target"}}},
            }],
            entry_state={"all": [{"text_equals": {"param": "target"}},
                                 {"not": {"element_exists": {"text": {"param": "target"}}}}]},
            postconditions={"any": [
                {"element_count": {"text": {"param": "target"}}, "equals": 1},
                {"text_equals": {"param": "target"}},
            ]},
        )
        bound = bind_recipe(doc, {"target": "Search"})
        step = bound["steps"][0]
        self.assertEqual(step["locator"], {"text": "Search"})
        self.assertEqual(step["fallback"]["alternatives"][0],
                         {"text_contains": "Search"})
        self.assertEqual(step["expect"], {"text_equals": "Search"})
        self.assertEqual(step["postcondition"], {"element_exists": {"text": "Search"}})
        self.assertEqual(bound["entry_state"]["all"][0], {"text_equals": "Search"})
        self.assertEqual(bound["entry_state"]["all"][1],
                         {"not": {"element_exists": {"text": "Search"}}})
        self.assertEqual(bound["postconditions"]["any"][0],
                         {"element_count": {"text": "Search"}, "equals": 1})

    # ----------------------------------------------------------- rejection
    def assert_code(self, expected: str, doc: dict, values: dict | None = None):
        with self.assertRaises(JourneyError) as caught:
            bind_recipe(doc, values)
        self.assertEqual(caught.exception.code, expected)
        return caught.exception

    def test_missing_required_parameter(self):
        doc = recipe(schema(required=["target"], target=text_param()),
                     steps=[{"step_id": "s", "action": "tap",
                             "locator": {"text": "OK"}}])
        self.assert_code("missing_parameter", doc, {})

    def test_referenced_optional_parameter_without_value_is_missing(self):
        doc = recipe(schema(target=text_param()),
                     steps=[{"step_id": "s", "action": "tap",
                             "locator": {"text": {"param": "target"}}}])
        self.assert_code("missing_parameter", doc, {})

    def test_wrong_types_are_rejected(self):
        cases = [
            ("string", 1),
            ("string", None),
            ("integer", True),          # bool is not an integer
            ("integer", 1.5),
            ("integer", "1"),
            ("number", True),           # bool is not a number
            ("number", float("nan")),
            ("number", float("inf")),
            ("number", float("-inf")),
            ("boolean", 1),
            ("boolean", "true"),
        ]
        for kind, value in cases:
            with self.subTest(kind=kind, value=value):
                doc = recipe({"type": "object",
                              "properties": {"p": {"type": kind}},
                              "required": ["p"]})
                self.assert_code("invalid_request", doc, {"p": value})

    def test_enum_is_enforced(self):
        doc = recipe({"type": "object",
                      "properties": {"p": {"type": "string",
                                           "enum": ["A", "B"]}},
                      "required": ["p"]})
        self.assert_code("invalid_request", doc, {"p": "C"})
        self.assertEqual(bind_recipe(doc, {"p": "A"})["parameters"], doc["parameters"])

    def test_unknown_supplied_parameter_is_rejected(self):
        doc = recipe(schema(target=text_param()))
        self.assert_code("invalid_request", doc, {"target": "A", "extra": "x"})

    def test_unknown_placeholder_reference_is_rejected(self):
        doc = recipe({"type": "object", "properties": {}, "required": []},
                     steps=[{"step_id": "s", "action": "tap",
                             "locator": {"text": {"param": "ghost"}}}])
        self.assert_code("invalid_request", doc, {})

    def test_illegal_positions_are_rejected(self):
        cases = {
            "action": {"steps": [{"step_id": "s", "action": {"param": "p"}}]},
            "type": {"steps": [{"step_id": "s", "type": {"param": "p"}}]},
            "effect_class": {"steps": [{"step_id": "s", "action": "tap",
                                        "locator": {"text": "OK"},
                                        "effect_class": {"param": "p"}}]},
            "coordinates": {"steps": [{"step_id": "s", "action": "tap_xy",
                                       "x": {"param": "p"}, "y": 1}]},
            "settle_ms": {"steps": [{"step_id": "s", "action": "tap",
                                     "locator": {"text": "OK"},
                                     "settle_ms": {"param": "p"}}]},
            "step_id": {"steps": [{"step_id": {"param": "p"}, "action": "tap",
                                   "locator": {"text": "OK"}}]},
            "top_level": {"task_key": {"param": "p"},
                          "steps": []},
            "count_equals": {"steps": [{"step_id": "s", "action": "tap",
                                        "locator": {"text": "OK"},
                                        "postcondition": {
                                            "element_count": {"text": "OK"},
                                            "equals": {"param": "p"}}}]},
            "text_on_non_input": {"steps": [{"step_id": "s", "action": "tap",
                                             "locator": {"text": "OK"},
                                             "text": {"param": "p"}}]},
            "fallback_itself": {"steps": [{"step_id": "s", "action": "tap",
                                           "locator": {"text": "OK"},
                                           "fallback": {"param": "p"}}]},
            "fallback_alternative": {"steps": [{
                "step_id": "s", "action": "tap", "locator": {"text": "OK"},
                "fallback": {"alternatives": [{"param": "p"}]}}]},
            "locator_itself": {"steps": [{"step_id": "s", "action": "tap",
                                          "locator": {"param": "p"}}]},
            "check_itself": {"postconditions": {"param": "p"}, "steps": []},
            "entry_state_itself": {"entry_state": {"param": "p"}, "steps": []},
        }
        for name, body in cases.items():
            with self.subTest(position=name):
                doc = {"parameters": {"type": "object",
                                      "properties": {"p": {"type": "string"}},
                                      "required": ["p"]},
                       **body}
                self.assert_code("invalid_request", doc, {"p": "X"})

    def test_unknown_step_fields_are_preserved_but_their_parameters_rejected(self):
        fixed = {"entry": "home", "exit": "search",
                 "geometry": {"w": 1080, "h": 2400}, "app": "com.example"}
        doc = recipe(
            schema(target=text_param()),
            steps=[{"step_id": "s", "action": "tap",
                    "locator": {"text": {"param": "target"}},
                    "fixed_segment": fixed,
                    "future_field": {"anything": 1}}],
        )
        bound = bind_recipe(doc, {"target": "Search"})
        self.assertEqual(bound["steps"][0]["fixed_segment"], fixed)
        self.assertEqual(bound["steps"][0]["future_field"], {"anything": 1})
        self.assertEqual(bound["steps"][0]["locator"], {"text": "Search"})

        # fixed_segment may not be parameterised yet; that is a reference
        # rejection, not a rejection of the field itself.
        with_param = recipe(
            schema(target=text_param()),
            steps=[{"step_id": "s", "action": "tap",
                    "locator": {"text": "OK"},
                    "fixed_segment": {"entry": {"param": "target"}}}],
        )
        self.assert_code("invalid_request", with_param, {"target": "Search"})

    def test_lookalike_placeholder_is_not_bound(self):
        doc = recipe(schema(target=text_param()),
                     steps=[{"step_id": "s", "action": "tap",
                             "locator": {"text": {"param": "target",
                                                  "fallback": "Search"}}}])
        self.assert_code("invalid_request", doc, {"target": "Search"})

    def test_empty_bound_locator_is_rejected(self):
        doc = recipe(schema(target=text_param()),
                     steps=[{"step_id": "s", "action": "tap",
                             "locator": {"text": {"param": "target"}}}])
        self.assert_code("invalid_request", doc, {"target": "   "})

    def test_error_messages_do_not_echo_values(self):
        doc = recipe({"type": "object",
                      "properties": {"p": {"type": "integer"}},
                      "required": ["p"]})
        exc = self.assert_code("invalid_request", doc, {"p": "s3cr3t-value"})
        self.assertNotIn("s3cr3t-value", str(exc))
        self.assertNotIn("s3cr3t-value", exc.message)

    def test_binding_does_not_add_a_values_field(self):
        doc = recipe(schema(target=text_param()),
                     steps=[{"step_id": "s", "action": "tap",
                             "locator": {"text": {"param": "target"}}}])
        bound = bind_recipe(doc, {"target": "secret"})
        self.assertNotIn("values", bound)
        self.assertNotIn("bound_parameters", bound)
        self.assertEqual(bound["steps"][0]["locator"]["text"], "secret")

    # ------------------------------------------------------------- schema
    def test_schema_validation(self):
        validate_parameter_schema(None)
        validate_parameter_schema({})
        validate_parameter_schema({"type": "object", "properties": {
            "s": {"type": "string", "default": "x", "enum": ["x", "y"]},
            "n": {"type": "number", "default": 1},
            "i": {"type": "integer", "default": 1},
            "b": {"type": "boolean", "default": False},
        }, "required": ["s"]})

        bad = [
            ("invalid_request", {"type": "object",
                                 "properties": {"p": {}}}),
            ("unsupported_input", {"type": "object",
                                   "properties": {"p": {"type": "array"}}}),
            ("unsupported_input", {"type": "array"}),
            ("invalid_request", {"type": "object",
                                 "properties": {"p": {"type": "string",
                                                      "unknown": 1}}}),
            ("invalid_request", {"type": "object",
                                 "properties": {"p": {"type": "string"}},
                                 "required": ["ghost"]}),
            ("invalid_request", {"type": "object",
                                 "properties": {"p": {"type": "string",
                                                      "enum": "AB"}}}),
            ("invalid_request", {"type": "object",
                                 "properties": {"p": {"type": "integer",
                                                      "enum": [True]}}}),
            ("invalid_request", {"type": "object",
                                 "properties": {"p": {"type": "integer",
                                                      "default": True}}}),
            ("invalid_request", {"type": "object",
                                 "properties": {"p": {"type": "number",
                                                      "default": float("inf")}}}),
            ("invalid_request", {"type": "object",
                                 "properties": {"p": {"type": "string"}},
                                 "required": ["p", "p"]}),
        ]
        for expected, spec in bad:
            with self.subTest(spec=spec):
                with self.assertRaises(JourneyError) as caught:
                    validate_parameter_schema(spec)
                self.assertEqual(caught.exception.code, expected)

    def test_recipe_validation_needs_no_values(self):
        doc = recipe(
            {"type": "object",
             "properties": {"target": {"type": "string"}},
             "required": ["target"]},
            steps=[{"step_id": "s", "action": "tap",
                    "locator": {"text": {"param": "target"}}}],
        )
        validate_recipe_parameters(doc)          # required value may arrive later
        with self.assertRaises(JourneyError) as caught:
            validate_recipe_parameters(recipe(
                {"type": "object", "properties": {}, "required": []},
                steps=[{"step_id": "s", "action": "tap",
                        "locator": {"text": {"param": "ghost"}}}]))
        self.assertEqual(caught.exception.code, "invalid_request")

        with self.assertRaises(JourneyError) as caught:
            validate_recipe_parameters(recipe(
                {"type": "object",
                 "properties": {"p": {"type": "string"}},
                 "required": ["p"]},
                steps=[{"step_id": "s", "action": {"param": "p"}}]))
        self.assertEqual(caught.exception.code, "invalid_request")


if __name__ == "__main__":
    unittest.main()
