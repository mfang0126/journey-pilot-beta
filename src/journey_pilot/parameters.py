"""Compile-time parameter binding for Journey recipes.

A recipe references a declared parameter only through the exact object
``{"param": "name"}``.  Binding substitutes the value into a fixed set of
text slots; there is no string templating, ``eval``, or generated code:

* step ``locator`` and ``fallback.alternatives`` string values;
* ``input_text`` step ``text``;
* ``text_equals`` values and locator values under ``element_exists`` /
  ``element_count`` in ``entry_state``, ``postconditions``, and a step's
  ``expect`` / ``postcondition`` / ``expect_gone``.

Everywhere else (``action``, ``type``, ``effect_class``, coordinates,
``equals``, ``step_id``, environment fields, ``fixed_segment``, ...) a
placeholder is rejected.  Ordinary fields this module does not know about are
left untouched; only parameter references are policed.  Only the exact
single-key shape is a placeholder; lookalikes are invalid.

Schema subset: ``{"type": "object", "properties": {...}, "required": [...]}``
with property ``type`` in string/number/integer/boolean plus optional
``default`` and ``enum``.  ``bool`` is never accepted as ``integer`` or
``number``, and non-finite numbers are rejected.  Unknown supplied names and
unknown placeholder references are rejected.

Values stay in memory for the duration of the call; this module writes
nothing.  Error messages name the parameter and expected type, never the
supplied value.
"""
from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from typing import Any

__all__ = ["bind_recipe", "validate_parameter_schema", "validate_recipe_parameters"]

PARAM_TYPES = frozenset({"string", "number", "integer", "boolean"})
_PLACEHOLDER_KEY = "param"
_SCHEMA_KEYS = frozenset({"type", "properties", "required"})
_PROPERTY_KEYS = frozenset({"type", "default", "enum"})


def _error(code: str, message: str):
    # Local import keeps this module importable before journey.py finishes
    # loading (journey imports this module once binding is wired in).
    from .journey import JourneyError
    return JourneyError(code, message)


def _is_placeholder(node: Any) -> bool:
    return (isinstance(node, Mapping) and len(node) == 1
            and _PLACEHOLDER_KEY in node
            and isinstance(node[_PLACEHOLDER_KEY], str)
            and bool(node[_PLACEHOLDER_KEY].strip()))


def _check_type(name: str, kind: str, value: Any) -> None:
    if kind == "string":
        ok = isinstance(value, str)
    elif kind == "boolean":
        ok = isinstance(value, bool)
    elif kind == "integer":
        ok = type(value) is int          # bool is a subclass of int; reject it
    else:                                # number
        ok = ((type(value) is int)
              or (type(value) is float and math.isfinite(value)))
    if not ok:
        raise _error("invalid_request", f"parameter {name!r} must be {kind}")


def _parse_schema(schema: Any) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Validate the supported schema subset and return normalized properties."""
    if schema is None:
        schema = {}
    if not isinstance(schema, Mapping):
        raise _error("invalid_request", "parameters must be an object")
    extra = set(schema) - _SCHEMA_KEYS
    if extra:
        raise _error("invalid_request", f"unsupported parameters fields: {sorted(extra)}")
    if schema.get("type", "object") != "object":
        raise _error("unsupported_input", "only object parameter schemas are supported")

    properties = schema.get("properties", {})
    if properties is None:
        properties = {}
    if not isinstance(properties, Mapping):
        raise _error("invalid_request", "parameters.properties must be an object")

    required = schema.get("required", [])
    if required is None:
        required = []
    if (not isinstance(required, list)
            or any(not isinstance(n, str) or not n for n in required)):
        raise _error("invalid_request", "parameters.required must be a list of names")
    if len(set(required)) != len(required):
        raise _error("invalid_request", "parameters.required has duplicates")
    undeclared = set(required) - set(properties)
    if undeclared:
        raise _error("invalid_request", f"required names not declared: {sorted(undeclared)}")

    normalized: dict[str, dict[str, Any]] = {}
    for name, spec in properties.items():
        if not isinstance(name, str) or not name:
            raise _error("invalid_request", "parameter names must be nonempty strings")
        if not isinstance(spec, Mapping):
            raise _error("invalid_request", f"parameter {name!r} schema must be an object")
        extra = set(spec) - _PROPERTY_KEYS
        if extra:
            raise _error("invalid_request",
                         f"parameter {name!r} has unsupported fields: {sorted(extra)}")
        kind = spec.get("type")
        if not isinstance(kind, str) or kind not in PARAM_TYPES:
            if kind is None:
                raise _error("invalid_request", f"parameter {name!r} needs a type")
            raise _error("unsupported_input",
                         f"parameter {name!r} type {kind!r} is not supported")
        enum = spec.get("enum")
        if enum is not None:
            if not isinstance(enum, list) or not enum:
                raise _error("invalid_request",
                             f"parameter {name!r} enum must be a nonempty list")
            for item in enum:
                _check_type(name, kind, item)
        entry: dict[str, Any] = {"type": kind}
        if enum is not None:
            entry["enum"] = list(enum)
        if "default" in spec:
            _check_type(name, kind, spec["default"])
            if enum is not None and spec["default"] not in enum:
                raise _error("invalid_request",
                             f"parameter {name!r} default is outside its enum")
            entry["default"] = spec["default"]
        normalized[name] = entry
    return normalized, set(required)


def _as_text(value: Any) -> str:
    """Render a validated scalar into a text slot."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return repr(value)                   # finite float


def _text_slot(value: Any, path: str, resolve, *, nonempty: bool) -> str:
    if _is_placeholder(value):
        text = resolve(value[_PLACEHOLDER_KEY], path)
        if nonempty and not text.strip():
            raise _error("invalid_request", f"{path}: bound value is empty")
        return text
    if not isinstance(value, str):
        raise _error("invalid_request",
                     f"{path}: expected text or a parameter placeholder")
    return value


def _locator(loc: Any, path: str, resolve) -> Any:
    if _is_placeholder(loc):
        raise _error("invalid_request", f"{path}: a locator cannot be a parameter")
    if not isinstance(loc, Mapping):
        raise _error("invalid_request", f"{path}: locator must be an object")
    out: dict[str, Any] = {}
    for key, value in loc.items():
        out[key] = _text_slot(value, f"{path}.{key}", resolve, nonempty=True)
    return out


def _checks(node: Any, path: str, resolve, *, step: bool) -> Any:
    if _is_placeholder(node):
        raise _error("invalid_request", f"{path}: a check cannot be a parameter")
    if not isinstance(node, Mapping):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        child = f"{path}.{key}"
        if key == "text_equals":
            out[key] = _text_slot(value, child, resolve, nonempty=True)
        elif key == "element_exists" or (key == "element_count" and not step):
            out[key] = _locator(value, child, resolve)
        elif key in ("all", "any") and not step and isinstance(value, list):
            out[key] = [_checks(item, f"{child}[{i}]", resolve, step=False)
                        for i, item in enumerate(value)]
        elif key == "not" and not step:
            out[key] = _checks(value, child, resolve, step=False)
        else:
            out[key] = value
    return out


def _step(step: Any, path: str, resolve) -> Any:
    if not isinstance(step, Mapping):
        return step
    out: dict[str, Any] = dict(step)
    if "locator" in out:
        out["locator"] = _locator(out["locator"], f"{path}.locator", resolve)
    if "fallback" in out:
        fallback = out["fallback"]
        if _is_placeholder(fallback):
            raise _error("invalid_request",
                         f"{path}.fallback: a fallback cannot be a parameter")
        if isinstance(fallback, Mapping) and isinstance(fallback.get("alternatives"), list):
            bound = dict(fallback)
            bound["alternatives"] = [
                _locator(alt, f"{path}.fallback.alternatives[{i}]", resolve)
                for i, alt in enumerate(fallback["alternatives"])
            ]
            out["fallback"] = bound
    action = out.get("action") or out.get("type")
    if action == "input_text" and "text" in out:
        out["text"] = _text_slot(out["text"], f"{path}.text", resolve, nonempty=False)
    for key in ("expect", "postcondition", "expect_gone"):
        if out.get(key) is not None:
            out[key] = _checks(out[key], f"{path}.{key}", resolve, step=True)
    return out


def _reject_remaining(node: Any, path: str) -> None:
    if _is_placeholder(node):
        raise _error("invalid_request",
                     f"{path}: parameter placeholder is not allowed here")
    if isinstance(node, Mapping):
        for key, value in node.items():
            _reject_remaining(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _reject_remaining(value, f"{path}[{i}]")


def _walk(doc: Mapping[str, Any], resolve) -> dict[str, Any]:
    out: dict[str, Any] = dict(doc)
    if "entry_state" in out:
        out["entry_state"] = _checks(out["entry_state"], "entry_state",
                                     resolve, step=False)
    if "postconditions" in out:
        out["postconditions"] = _checks(out["postconditions"], "postconditions",
                                        resolve, step=False)
    steps = out.get("steps")
    if isinstance(steps, list):
        out["steps"] = [_step(step, f"steps[{i}]", resolve)
                        for i, step in enumerate(steps)]
    _reject_remaining(out, "recipe")
    return out


def validate_parameter_schema(schema: Any) -> None:
    """Raise JourneyError unless ``schema`` is in the supported subset."""
    _parse_schema(schema)


def validate_recipe_parameters(doc: Mapping[str, Any]) -> dict[str, Any]:
    """Check placeholders and schema without requiring runtime values.

    Intended for ``propose``: every placeholder must be in an allowed slot and
    reference a declared parameter, but no value needs to be supplied.
    """
    if not isinstance(doc, Mapping):
        raise _error("invalid_request", "recipe must be an object")
    properties, _required = _parse_schema(doc.get("parameters"))

    def resolve(name: str, path: str) -> str:
        if name not in properties:
            raise _error("invalid_request", f"{path}: unknown parameter {name!r}")
        return "parameter"              # discarded; only reference validity matters

    return _walk(copy.deepcopy(doc), resolve)


def bind_recipe(doc: Mapping[str, Any], values: Mapping[str, Any] | None = None
                ) -> dict[str, Any]:
    """Return a deep copy of ``doc`` with all placeholders bound to ``values``.

    Raises JourneyError with ``invalid_request`` for malformed input, unknown
    names, wrong types, enum misses, or illegal placeholder positions, and
    ``missing_parameter`` when a required or referenced value is absent.
    """
    if not isinstance(doc, Mapping):
        raise _error("invalid_request", "recipe must be an object")
    if values is None:
        values = {}
    if not isinstance(values, Mapping):
        raise _error("invalid_request", "parameters must be an object")

    properties, required = _parse_schema(doc.get("parameters"))
    unknown = set(values) - set(properties)
    if unknown:
        raise _error("invalid_request", f"unknown parameters: {sorted(unknown)}")

    resolved: dict[str, Any] = {}
    for name, spec in properties.items():
        if name in values:
            value = values[name]
        elif "default" in spec:
            value = spec["default"]
        elif name in required:
            raise _error("missing_parameter", f"missing required parameter {name!r}")
        else:
            continue
        _check_type(name, spec["type"], value)
        if "enum" in spec and value not in spec["enum"]:
            raise _error("invalid_request",
                         f"parameter {name!r} is not one of its allowed values")
        resolved[name] = value

    def resolve(name: str, path: str) -> str:
        if name not in properties:
            raise _error("invalid_request", f"{path}: unknown parameter {name!r}")
        if name not in resolved:
            raise _error("missing_parameter",
                         f"{path}: no value for parameter {name!r}")
        return _as_text(resolved[name])

    return _walk(copy.deepcopy(doc), resolve)
