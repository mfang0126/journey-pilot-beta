"""Fixed coordinates require explicit entry, exit and layout checks per segment."""
from collections.abc import Mapping

from .journey import JourneyError, validate_check


def validate_segment(segment):
    if not isinstance(segment, Mapping) or set(segment) != {"entry", "exit", "geometry", "app", "target"}:
        raise JourneyError("invalid_request", "fixed_segment requires entry, exit, geometry, app and target locator")
    from .journey import validate_locator
    validate_locator(segment["target"])
    validate_check(segment["entry"])
    validate_check(segment["exit"])
    geometry = segment["geometry"]
    if (not isinstance(geometry, Mapping) or set(geometry) != {"width", "height", "orientation"}
            or any(type(geometry[k]) is not int for k in geometry)
            or geometry["width"] <= 0 or geometry["height"] <= 0
            or geometry["orientation"] not in (0, 1, 2, 3)):
        raise JourneyError("invalid_request", "fixed_segment needs exact screen dimensions and orientation")
    if not isinstance(segment["app"], str) or not segment["app"]:
        raise JourneyError("invalid_request", "fixed_segment needs an app package")
    # First release is intentionally limited to a local, fixed navigation surface.
    if segment["app"] != "com.android.settings":
        raise JourneyError("unsupported_input", "fixed segments currently support local Android Settings only")


def validate_fixed_steps(steps):
    for step in steps:
        segment = step.get("fixed_segment")
        if step.get("action", step.get("type")) == "tap_xy":
            if not segment:
                raise JourneyError("unsupported_input", "replay coordinates require fixed_segment conditions")
            validate_segment(segment)
            if any(step.get(k) is not None for k in ("postcondition", "expect", "expect_gone", "expect_app")):
                raise JourneyError("invalid_request", "fixed_segment.exit is the sole step checkpoint")
            if step.get("effect_class", "navigation") != "navigation":
                raise JourneyError("unsupported_input", "fixed coordinates are navigation only")
            if not {"x", "y"} <= set(step):
                raise JourneyError("invalid_request", "fixed coordinates need x/y")
            if not (0 <= step["x"] < segment["geometry"]["width"]
                    and 0 <= step["y"] < segment["geometry"]["height"]):
                raise JourneyError("invalid_request", "fixed coordinates outside declared screen")
        elif segment is not None:
            raise JourneyError("unsupported_input", "fixed_segment belongs to tap_xy only")
