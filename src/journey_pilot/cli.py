"""Command line entry point and current, scoped capability descriptions."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import __version__
from .contracts import (
    SCHEMA_VERSION, ACTIONS, CHECKS, STEP_CHECKS, EFFECT_CLASSES,
    error_catalog,
    failure,
    success,
    validate_result,
)
from .paths import ENV_VAR, layout

ADAPTER_VERSION = "phone-harness/adapter-0.1.0"

# Shipped commands describe current behavior; future commands stay explicit.
COMMANDS: tuple[tuple[str, str, bool], ...] = (
    ("capabilities", "Report local capability, versions and the request schema", True),
    ("status", "Device list, lock state, foreground app and readiness probe", True),
    ("discover", "Search Journey versions and non-executable run references", True),
    ("session start", "Open an exploration session and take the lease", True),
    ("session authorize", "Prepare one one-time external-mutation grant from the live post identity, or for one exact share_text", True),
    ("session resolve-input", "Close one provably-finished failed non-ASCII adb input using durable request and post-failure tree evidence", True),
    ("observe", "Record a fresh observation", True),
    ("act", "Record and perform one whitelisted action", True),
    ("session finish", "Close the session as recorded; does not evaluate postconditions", True),
    ("session handoff", "Wake a nonsecure screen if allowed, refresh the scene and transfer an exploration lease; unknown effects stay blocked", True),
    ("inspect", "Read a run or journey", True),
    ("propose", "Store an explicit recipe as a candidate journey", True),
    ("validate", "Run a candidate once, in validate mode", True),
    ("run", "Execute a verified journey", True),
    ("resume", "Continue once from a verified checkpoint with identical recipe and parameters", True),
    ("cancel", "Request a stop and keep what already happened", True),
    ("reconcile", "Inspect or release idle occupancy only when no action effects are unknown", True),
    ("trace", "Summarize a run or create a verifiable event reference", True),
    ("review", "Store an evidence-linked six-question review", True),
    ("lessons", "Find non-executable lessons by task", True),
    ("read-post", "Read exact own XHS post and visible comments (bounded route)", True),
    ("task-list", "List registered thin task scripts and entry conditions", True),
    ("task", "Run one registered task script", True),
)

FIELD_TYPES = ("string", "number", "integer", "boolean")

PARAM_TYPES = FIELD_TYPES

REQUEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Read from --request-stdin. Values that may be sensitive never travel "
        "as process arguments."
    ),
    "properties": {
        "device_id": {"type": "string"},
        "task_key": {"type": "string"},
        "parameters": {"type": "object"},
        "request_id": {"type": "string"},
        "run_id": {"type": "string"},
        "lease_token": {"type": "string"},
        "intent": {"type": "string"},
        "task": {"type": "string"},
        "title": {"type": "string"},
        "author": {"type": "string"},
        "query": {"type": "string"},
        "environment_key": {"type": "object"},
        "journey_id": {"type": "string"},
        "version": {"type": "integer", "minimum": 1},
        "action": {"type": "object", "required": ["type"],
                   "properties": {"type": {"enum": list(ACTIONS)}}},
        "source": {"enum": ["tree", "screenshot", "pixels"]},
        "exclude_status_bar": {"type": "boolean"},
        "steps": {"type": "array", "minItems": 1},
        "postconditions": {"type": "object"},
        "post": {"type": "object", "required": ["title", "author"]},
        "opener": {"type": "object", "required": ["type"]},
        "text": {"type": "string"},
        "authorization": {"type": "object", "required": ["grant_id"]},
    },
    "additionalProperties": True,
}

# Minimum input keys; command-specific semantics are documented in agent-interface.
COMMAND_REQUESTS = {
    "capabilities": [], "status": [], "discover": [], "task-list": [],
    "session start": ["intent"],
    "session authorize": ["run_id", "lease_token", "post (tap only)", "action", "text"],
    "session resolve-input": ["run_id", "lease_token", "request_id", "action"],
    "observe": ["run_id", "lease_token"],
    "act": ["run_id", "lease_token", "request_id", "action"],
    "session finish": ["run_id", "lease_token"],
    "session handoff": ["run_id"],
    "inspect": ["run_id or journey_id"],
    "propose": ["task_key", "intent", "steps"],
    "validate": ["journey_id", "request_id"],
    "run": ["journey_id", "request_id"],
    "cancel": ["run_id"], "resume": ["run_id", "request_id"],
    "trace": ["run_id"], "review": ["run_id", "task_key", "questions", "claims"],
    "lessons": ["task_key"], "reconcile": ["run_id"],
    "read-post": ["device_id", "title", "author"], "task": ["task", "device_id"],
}


MINIMAL_EXAMPLE = {
    "command": "capabilities",
    "request": {},
}


def _payload() -> dict[str, Any]:
    return {
        "store": layout(),
        "store_env_var": ENV_VAR,
        "program_version": __version__,
        "adapter_version": ADAPTER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "commands": [
            {"name": name, "summary": summary, "implemented": done}
            for name, summary, done in COMMANDS
        ],
        "actions": list(ACTIONS),
        "effect_classes": list(EFFECT_CLASSES),
        "checks": list(CHECKS),
        "step_checks": list(STEP_CHECKS),
        "observation_sources": ["tree", "screenshot", "pixels"],
        "command_requests": COMMAND_REQUESTS,
        "defaults": {"settle_ms": 800, "poll_ms": 2000, "lease_idle_seconds": 1800,
                     "observe_source": "tree", "effect_class": "navigation",
                     "action_interval_seconds": 3, "network_interval_seconds": 5,
                     "package_interval_seconds": {"default": 3, "overrides": "<store>/package_intervals.json"},
                     "max_actions": 40, "max_network_actions": 3, "max_scrolls": 5},
        "pacing_note": "Experimental local limits; not a platform-permitted rate or server-load guarantee",
        "limitations": [
            "resume requires a trusted checkpoint, identical parameters and no unresolved action effects",
            "parameter references are supported only in declared text and locator slots",
            "runtime content is returned live but omitted from persisted retry results; content_retained=false on cached results",
            "trace references prove event integrity, not that an agent interpretation is correct",
            "reconcile cannot release occupancy with unresolved action effects",
            "fixed segments require a unique live target at the recorded position; not a general overlay detector",
            "entry checks are enforced; unsupported environment fields fail closed",
            "discover includes non-executable run references; run rechecks live eligibility",
            "fixed coordinates are limited to guarded local Android Settings navigation",
            "field_equals/app_is checks and the assert action are not supported",
            "checks lists Journey final checks; step_checks applies to action.expect/postcondition/expect_gone; expect_app is an exact foreground package; all three share one bounded wait and report unmet keys",
            "limit notices block (platform_blocked, limit_notice=true) only when they newly appear after an action with a local wait, ignoring the text the action typed itself; verification signals block whenever visible; the trace records blocked_by=limit_notice|verification_block",
            "Jev is an internal helper, not an accepted final success assertion",
            "wake retries ordinary up-swipe at most three times and requires a real tree read; secure=false is not treated as unlocked; PIN/password is never entered",
            "external_mutation needs 'session authorize' while the exact title and author are visible; the one-time grant binds run, device, exact tap action and comment-text digest",
            "share_text (am start SEND text/plain -p package, no clipboard) is always external_mutation and needs its own one-time grant binding run, device, package and text digest; no post identity is checked and the receiving app's own send is a separate step",
            "input_text replaces the field (clears existing text first) and is ok only if the re-read field text equals text exactly, on both methods; otherwise assertion_failed",
            "input_text rejects non-ASCII on the adb method before touching the device; use method=paste after preloading the exact text on the device clipboard",
            "session resolve-input closes only a failed non-ASCII adb input whose full original payload digest matches a terminal (done) request record with normal script metrics, is the only open action, and whose durable post-failure tree digest/count/geometry equal the observation before the failure; it does not read the device and accepts this run's recorded lease token even if expired, but never creates/renews occupancy or preempts another run; it proves the synchronous wrapper returned and the immediately visible state was unchanged, not that no transient effect happened",
        ],
        "param_types": list(PARAM_TYPES),
        "field_types": list(FIELD_TYPES),
        "request_schema": REQUEST_SCHEMA,
        "minimal_example": MINIMAL_EXAMPLE,
        "errors": error_catalog(),
        "notes": [
            "Exploration and replay are recorded only when they go through this CLI.",
            "recorded means the action and its evidence were saved; it is not task success.",
            "completed requires the frozen postconditions of the run to pass.",
            "external_mutation pauses unless an explicit, one-time 'session authorize' grant matches the run, device, exact tap (or share_text package) and text; declaring navigation does not grant anything.",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="journey-pilot",
        description="Shared, replayable Android journeys for any local agent.",
    )
    parser.add_argument("command", nargs="?", help="capabilities | status | discover | ...")
    parser.add_argument("subcommand", nargs="?", help="e.g. start/finish for 'session'")
    parser.add_argument("--json", action="store_true",
                        help="explicitly request JSON output (it is the default)")
    parser.add_argument("--request-stdin", action="store_true",
                        help="read one JSON request object from stdin")
    parser.add_argument("--version", action="version", version=f"journey-pilot {__version__}")
    return parser


def _emit(result, stream) -> int:
    from . import adapter
    from .store import Store, StoreError
    metrics = adapter.call_metrics.get()
    if metrics and metrics["calls"]:
        result.data["harness_calls"] = dict(metrics)
        if result.run_id:
            try:
                Store().append_event(result.run_id, {"event": "cli_calls",
                    "count": metrics["calls"], "duration_ms": metrics["duration_ms"]})
            except (OSError, StoreError):
                result = failure("outcome_unknown", "call metrics could not be saved; inspect before retry",
                                 run_id=result.run_id)
    payload = result.as_dict()
    validate_result(payload)
    json.dump(payload, stream, ensure_ascii=False, sort_keys=False)
    stream.write("\n")
    return result.exit_code


def _read_request(args) -> dict[str, Any]:
    """Return the JSON request from stdin, or an empty dict when there is none."""
    if not args.request_stdin:
        return {}
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"stdin is not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("request must be a JSON object")
    return parsed


def _cmd_status(request: dict[str, Any]):
    from . import adapter

    data = adapter.status(request.get("device_id"))
    if data.get("ambiguous"):
        return failure("ambiguous_device",
                       f"{len(data['devices'])} devices found; pass device_id",
                       data=data)
    if not data.get("devices"):
        return failure("disconnected", "no device is reachable", data=data)
    return success("ok", data)


def _cmd_runner(command: str, request: dict[str, Any]):
    from . import runner
    from .store import Store

    store = Store()
    handler = {"validate": runner.validate, "run": runner.run,
               "cancel": runner.cancel, "resume": runner.resume}[command]
    return handler(store, request)


def _cmd_journey(command: str, request: dict[str, Any]):
    from . import journey
    from .store import Store

    store = Store()
    handler = {"discover": journey.discover, "inspect": journey.inspect,
               "propose": journey.propose}[command]
    try:
        return handler(store, request)
    except journey.JourneyError as exc:
        return failure(exc.code, exc.message)
    except (ValueError, TypeError) as exc:
        return failure("invalid_request", str(exc))


def _cmd_session_like(command: str, subcommand: str | None, request: dict[str, Any]):
    from . import session
    from .store import Store

    store = Store()
    if command == "observe":
        return session.observe(store, request)
    if command == "act":
        return session.act(store, request)
    if subcommand == "start":
        return session.start(store, request)
    if subcommand == "authorize":
        return session.authorize(store, request)
    if subcommand == "resolve-input":
        return session.resolve_input(store, request)
    if subcommand == "finish":
        return session.finish(store, request)
    if subcommand == "handoff":
        return session.handoff(store, request)
    return failure("invalid_request",
                   f"unknown session subcommand {subcommand!r}; use start, authorize or finish")


def main(argv: list[str] | None = None, stdout=None, stderr=None) -> int:
    from . import adapter
    token = adapter.call_metrics.set({"calls": 0, "duration_ms": 0.0})
    try:
        return _main(argv, stdout, stderr)
    finally:
        adapter.call_metrics.reset(token)


def _main(argv: list[str] | None = None, stdout=None, stderr=None) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    args = build_parser().parse_args(argv)

    try:
        request = _read_request(args)
    except ValueError as exc:
        return _emit(failure("invalid_request", str(exc)), stdout)

    if args.command in (None, "capabilities"):
        return _emit(success("ok", _payload()), stdout)

    if args.command == "status":
        return _emit(_cmd_status(request), stdout)

    if args.command in ("trace", "review", "lessons", "reconcile"):
        from .store import Store, StoreError
        store = Store()
        try:
            if args.command == "reconcile":
                from .recovery import reconcile
                result = reconcile(store, request)
            elif args.command == "trace":
                from .trace import build_reference, summarize_run
                data = (build_reference(store, request.get("run_id"), request.get("seq_start"), request.get("seq_end"))
                        if "seq_start" in request else summarize_run(store, request.get("run_id")))
                result = success("ok", data)
            else:
                from .review import save_review, discover_lessons
                result = (save_review(store, request) if args.command == "review"
                          else success("ok", {"lessons": discover_lessons(store, request)}))
        except (ValueError, TypeError, KeyError) as exc:
            result = failure("invalid_request", str(exc))
        except (OSError, StoreError):
            result = failure("outcome_unknown", "store unavailable; inspect before retry", run_id=request.get("run_id"))
        return _emit(result, stdout)

    if args.command == "read-post":
        from .read_post import execute
        from .store import Store
        return _emit(execute(Store(), request), stdout)

    if args.command in ("task", "task-list"):
        from . import task_registry
        from .store import Store
        result = task_registry.catalog() if args.command == "task-list" else task_registry.execute(Store(), request)
        return _emit(result, stdout)


    if args.command in ("validate", "run", "cancel", "resume"):
        return _emit(_cmd_runner(args.command, request), stdout)

    if args.command in ("discover", "inspect", "propose"):
        return _emit(_cmd_journey(args.command, request), stdout)

    if args.command in ("session", "observe", "act"):
        return _emit(_cmd_session_like(args.command, args.subcommand, request), stdout)

    known = {name for name, _, _ in COMMANDS}
    name = args.command if args.subcommand is None else f"{args.command} {args.subcommand}"
    if name in known:
        return _emit(
            failure("capability_gap", f"'{name}' is not implemented yet"),
            stdout,
        )
    print(f"unknown command: {args.command}", file=stderr)
    return _emit(failure("invalid_request", f"unknown command {args.command!r}"), stdout)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
