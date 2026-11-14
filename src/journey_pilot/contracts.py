"""The unified result contract.

Every command returns exactly one JSON object. This module is the only place
that decides what a status means, which exit code goes with it, and what a
caller should do next. ``docs/experience-rules.md`` §7 and
``docs/agent-interface.md`` are the specification; this file implements it.

Nothing here touches a device, a store, or the network.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

SCHEMA_VERSION = 1

ACTIONS = ("launch_app", "launch_activity", "tap", "tap_xy", "input_text",
           "key", "scroll", "wait", "read_field", "wake", "share_text")
EFFECT_CLASSES = ("observation", "navigation", "local_input", "external_mutation")
STEP_CHECKS = ("element_exists", "text_equals")
CHECKS = ("all", "any", "not", "element_exists", "element_count", "text_equals")

# Command-level statuses (Journey lifecycle states are named separately).
STATUSES = (
    "ok",
    "completed",
    "recorded",
    "paused",
    "blocked",
    "failed",
    "cancelled",
)

# Fallback exit code per status. A specific error code can override this when
# the specification classifies the failure differently (e.g. a parameter error
# exits 2 even though the status is blocked).
STATUS_EXIT = {
    "ok": 0,
    "completed": 0,
    "recorded": 0,
    "paused": 3,
    "blocked": 4,
    "failed": 5,
    "cancelled": 130,
}

ALLOWED_EXITS = frozenset(STATUS_EXIT.values()) | {2}


@dataclass(frozen=True)
class ErrorSpec:
    """One row of the error catalogue.

    ``status`` is the command status this error produces, ``exit_code`` the
    process exit code, ``retryable`` whether the caller may repeat the same
    request, and ``next_action`` the command the caller should run instead of
    guessing.
    """

    code: str
    status: str
    exit_code: int
    retryable: bool
    next_action: str
    summary: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "status": self.status,
            "exit_code": self.exit_code,
            "retryable": self.retryable,
            "next_action": self.next_action,
            "summary": self.summary,
        }


def _spec(code, status, exit_code, retryable, next_action, summary) -> ErrorSpec:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r} for {code}")
    if exit_code not in ALLOWED_EXITS:
        raise ValueError(f"unknown exit code {exit_code} for {code}")
    return ErrorSpec(code, status, exit_code, retryable, next_action, summary)


# Ordered so the generated catalogue reads like the specification.
_ERROR_SPECS = (
    # --- request / caller errors -------------------------------------------
    _spec("invalid_request", "failed", 2, False, "capabilities",
          "The request object is malformed or missing a required field."),
    _spec("missing_parameter", "blocked", 2, False, "inspect",
          "A declared parameter has no value; inspect before supplying it."),
    _spec("request_conflict", "failed", 2, False, "inspect",
          "The same request_id was reused with a different payload."),
    _spec("ambiguous_device", "blocked", 2, False, "status",
          "More than one device is available and none was named."),
    _spec("unsupported_input", "blocked", 4, False, "capabilities",
          "The requested input or action is not supported by this adapter."),
    _spec("schema_unsupported", "failed", 5, False, "capabilities",
          "The payload's major schema_version is not supported."),
    # --- environment -------------------------------------------------------
    _spec("disconnected", "blocked", 4, False, "status",
          "No device is reachable; reconnect it before retrying."),
    _spec("locked", "blocked", 4, False, "status",
          "The device is locked; a person must unlock it."),
    _spec("device_busy", "blocked", 4, True, "inspect",
          "Another run holds the device lease."),
    _spec("capability_gap", "blocked", 4, False, "capabilities",
          "The underlying tool cannot meet the required contract."),
    _spec("precondition_mismatch", "blocked", 4, False, "session start",
          "The start state does not match what the journey declares."),
    _spec("version_mismatch", "blocked", 4, False, "discover",
          "The app or system version is outside the verified environment."),
    # --- execution ---------------------------------------------------------
    _spec("budget_exhausted", "paused", 3, False, "inspect",
          "The bounded task has consumed its action budget."),
    _spec("platform_blocked", "paused", 3, False, "inspect",
          "A verification or rate-limit signal requires the caller to inspect."),
    _spec("locator_missing", "paused", 3, False, "inspect",
          "The declared target is not on the current screen."),
    _spec("ambiguous_target", "paused", 3, False, "inspect",
          "More than one element matches the locator."),
    _spec("unexpected_page", "paused", 3, False, "inspect",
          "The page after an action is not the expected one."),
    _spec("assertion_failed", "paused", 3, False, "inspect",
          "A frozen postcondition did not hold."),
    _spec("outcome_unknown", "paused", 3, False, "inspect",
          "An action started but its result could not be confirmed."),
    _spec("needs_authorization", "paused", 3, False, "inspect",
          "The next step mutates external state and needs the caller's task grant."),
    _spec("needs_input", "paused", 3, False, "inspect",
          "A sensitive runtime value was not persisted; supply it again."),
    _spec("needs_replan", "paused", 3, False, "session start",
          "The run cannot be resumed; explore again and propose a new version."),
    _spec("provider_unavailable", "paused", 3, True, "inspect",
          "An optional visual check is unavailable; structure-only work continues."),
    _spec("visual_suspected", "paused", 3, False, "inspect",
          "A visual check disagreed; confirm the live screen before continuing."),
    _spec("cancelled", "cancelled", 130, False, "inspect",
          "The caller asked to stop and no further action was taken."),
)

ERRORS: Mapping[str, ErrorSpec] = {s.code: s for s in _ERROR_SPECS}

# Statuses that must carry a non-null error object.
ERROR_STATUSES = frozenset({"blocked", "failed", "cancelled", "paused"})


class ContractError(ValueError):
    """Raised when a result object violates the contract."""


@dataclass(frozen=True)
class Result:
    """A validated result envelope."""

    status: str
    data: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    run_id: str | None = None
    next_action: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": self.status,
            "run_id": self.run_id,
            "data": self.data,
            "error": self.error,
            "next_action": self.next_action,
        }

    @property
    def exit_code(self) -> int:
        if self.error:
            return int(self.error["exit_code"])
        return STATUS_EXIT[self.status]


def error_payload(code: str, message: str, **details: Any) -> dict[str, Any]:
    """Build the ``error`` object for a catalogue code."""
    try:
        spec = ERRORS[code]
    except KeyError:
        raise ContractError(f"unknown error code {code!r}") from None
    payload: dict[str, Any] = {
        "code": spec.code,
        "message": message,
        "retryable": spec.retryable,
        "exit_code": spec.exit_code,
        "status": spec.status,
    }
    if details:
        payload["details"] = details
    return payload


def success(status: str, data: Mapping[str, Any] | None = None,
            run_id: str | None = None) -> Result:
    """Build a successful result; ``status`` must be ok/completed/recorded."""
    if status not in {"ok", "completed", "recorded"}:
        raise ContractError(f"{status!r} needs an error object")
    return Result(status=status, data=dict(data or {}), run_id=run_id)


def failure(code: str, message: str, data: Mapping[str, Any] | None = None,
            run_id: str | None = None, **details: Any) -> Result:
    """Build a failing/paused/blocked result from a catalogue code."""
    payload = error_payload(code, message, **details)
    return Result(
        status=payload["status"],
        data=dict(data or {}),
        error=payload,
        run_id=run_id,
    )


def validate_result(payload: Mapping[str, Any]) -> None:
    """Raise :class:`ContractError` unless ``payload`` is a valid envelope."""
    required = {"schema_version", "status", "run_id", "data", "error", "next_action"}
    missing = required - set(payload)
    if missing:
        raise ContractError(f"missing keys: {sorted(missing)}")
    extra = set(payload) - required
    if extra:
        raise ContractError(f"unexpected keys: {sorted(extra)}")

    if payload["schema_version"] != SCHEMA_VERSION:
        raise ContractError("schema_version mismatch")
    status = payload["status"]
    if status not in STATUSES:
        raise ContractError(f"unknown status {status!r}")
    if not isinstance(payload["data"], dict):
        raise ContractError("data must be an object")

    err = payload["error"]
    if status in ERROR_STATUSES:
        if not isinstance(err, dict):
            raise ContractError(f"status {status!r} requires an error object")
        code = err.get("code")
        if code not in ERRORS:
            raise ContractError(f"unknown error code {code!r}")
        if err["status"] != status:
            raise ContractError(
                f"error {code!r} declares status {err['status']!r}, envelope says {status!r}")
        if err["exit_code"] not in ALLOWED_EXITS:
            raise ContractError("error exit_code outside the allowed set")
        if not err.get("message"):
            raise ContractError("error.message must be non-empty")
    else:
        if err is not None:
            raise ContractError(f"status {status!r} must not carry an error object")

    na = payload["next_action"]
    if na is not None:
        if not isinstance(na, dict) or "command" not in na or "request" not in na:
            raise ContractError("next_action needs 'command' and 'request'")


def error_catalog() -> list[dict[str, Any]]:
    """The catalogue as advertised by ``capabilities``."""
    return [spec.as_dict() for spec in _ERROR_SPECS]
