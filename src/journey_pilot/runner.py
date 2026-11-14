"""Executing journeys: validate once, run only when verified.

The rules this implements (docs/experience-rules.md §2, §4):

* ``validate`` runs a candidate exactly once per call — no internal loop;
* promotion needs 3 consecutive successful validations in the same environment
  plus 2 distinct parameter digests when the journey is parameterised;
* ``run`` refuses a candidate outright; only a verified version in a compatible
  environment executes;
* a failing run pauses with the real reason and never reports success from an
  exit code alone.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import time
from contextlib import contextmanager
from typing import Any, Mapping

from . import adapter, web
from .contracts import failure, success
from .journey import (
    JourneyError,
    latest_version,
    lifecycle_of,
    load_version,
    validate_check,
    validate_steps,
    _append_lifecycle,
    _journey_dir,
)
from .store import Store, atomic_write_json, read_json
from .privacy import metadata
from .execution import device_lock as _device_lock, request_once, reservation_path

PROMOTION_SUCCESSES = 3
PROMOTION_PARAM_SETS = 2


class ProviderUnavailable(RuntimeError):
    """Jev could not answer; the caller must pause, not fail the journey."""


def _jev_choice(labels: list[str], spec: Mapping[str, Any]) -> tuple[str | None, float | None]:
    """Ask Jev one finite choice over the visible labels."""
    payload = {
        "state": "Visible elements:\n" + "\n".join(f"[{i+1}] {t}" for i, t in enumerate(labels[:40])),
        "questions": [{
            "id": "q", "type": "choice",
            "instructions": spec.get("instructions", "Which element matches?"),
            "criteria": spec.get("criteria", {}),
        }],
    }
    try:
        proc = subprocess.run(["jev", "ask"], input=json.dumps(payload, ensure_ascii=False),
                              text=True, capture_output=True, timeout=float(spec.get("timeout", 5)))
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProviderUnavailable(f"jev unavailable: {exc}") from exc
    if proc.returncode != 0:
        raise ProviderUnavailable((proc.stderr or proc.stdout).strip()[:200] or "jev failed")
    try:
        answer = json.loads(proc.stdout)["answers"]["q"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise ProviderUnavailable(f"unexpected jev output: {exc}") from exc
    return answer.get("choice"), answer.get("confidence")


# ---------------------------------------------------------------- checks
def check(nodes: list[Mapping[str, Any]], spec: Mapping[str, Any],
          serial: str | None = None) -> bool:
    """Evaluate a postcondition against an observed tree.

    Web locators are not in the tree; they query the foreground Chrome tab on
    device ``serial``.
    """
    if not spec:
        return True
    if "all" in spec:
        return all(check(nodes, sub, serial) for sub in spec["all"])
    if "any" in spec:
        return any(check(nodes, sub, serial) for sub in spec["any"])
    if "not" in spec:
        return not check(nodes, spec["not"], serial)
    if "element_exists" in spec:
        loc = spec["element_exists"]
        if web.is_web_locator(loc):
            return _web_count(loc, serial) > 0
        return any(_matches(n, loc) for n in nodes)
    if "text_equals" in spec:
        want = spec["text_equals"]
        if isinstance(want, Mapping):
            return _web_count(web.text_equals_locator(want), serial) > 0
        return any((n.get("text") or "").strip() == want for n in nodes)
    if "element_count" in spec:
        loc = spec["element_count"]
        want = int(spec.get("equals", 1))
        if web.is_web_locator(loc):
            return _web_count(loc, serial) == want
        return sum(1 for n in nodes if _matches(n, loc)) == want
    if "field_equals" in spec:
        field = spec["field_equals"]
        return any((n.get("text") or "").strip() == field for n in nodes)
    if "jev" in spec:
        labels = [n.get("text") or n.get("desc") or "" for n in nodes]
        labels = [t for t in labels if t]
        choice, _confidence = _jev_choice(labels, spec["jev"])
        # Same fuse as action selection: a matching answer below the confidence
        # floor is not accepted (it let 0.01-confidence answers through before).
        return (choice == spec["jev"].get("expect") and (_confidence or 0) >= 0.65)
    return False


def _web_count(locator, serial) -> int:
    try:
        return web.count(locator, serial)
    except web.WebUnavailable as exc:
        raise ProviderUnavailable(f"web page not readable: {exc}") from exc


def _matches(node: Mapping[str, Any], loc: Mapping[str, Any]) -> bool:
    if "resource_id" in loc and node.get("id") != loc["resource_id"]:
        return False
    if "text" in loc and (node.get("text") or "").strip() != loc["text"]:
        return False
    if "text_contains" in loc and loc["text_contains"] not in (node.get("text") or ""):
        return False
    if "description" in loc and (node.get("desc") or "").strip() != loc["description"]:
        return False
    if "class" in loc and node.get("class") != loc["class"]:
        return False
    return True


def _digest(parameters: Mapping[str, Any]) -> str:
    body = json.dumps(parameters or {}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _parameter_digest(store, doc):
    from .execution import file_lock
    path = store.root / "parameter-key.json"
    with file_lock(store.locks / "parameter-key.lock") as acquired:
        if not acquired:
            raise JourneyError("device_busy", "parameter digest key is busy")
        key = read_json(path)
        if key is None:
            key = {"key": secrets.token_hex(32)}
            atomic_write_json(path, key)
    body = json.dumps({k: doc.get(k) for k in ("steps", "entry_state", "postconditions")}, sort_keys=True).encode()
    return hmac.new(bytes.fromhex(key["key"]), body, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------- execute
def _execute(store: Store, doc: Mapping[str, Any], device_id: str | None,
             kind: str, authorization: Mapping[str, Any] | None = None
             ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run the steps once. Returns (summary, step results)."""
    run_id = doc.get("continuation_run_id") or "r_" + secrets.token_hex(4)
    summary = {
        "run_id": run_id,
        "kind": kind,
        "journey_id": doc["journey_id"],
        "version": doc["version"],
        "device_id": device_id,
        "status": "running",
        "authorization_supplied": bool(authorization),
        "started_at": time.time(),
        "finished_at": None,
        "steps": [],
        "recipe_digest": doc.get("recipe_digest"),
        "parameters_digest": doc.get("original_parameters_digest") or _parameter_digest(store, doc),
        "continuation_of": doc.get("continuation_of"),
        "budget_run_id": doc.get("budget_run_id", run_id),
        "step_offset": doc.get("step_offset", 0),
    }
    store.create_run(run_id, summary)
    summary["code_digest"] = read_json(store.summary_path(run_id))["code_digest"]
    store.append_event(run_id, {"event": "run_started", "kind": kind,
                                "journey_id": doc["journey_id"],
                                "version": doc["version"]})

    if doc.get("observation_strategy", "per_step") != "per_step":
        summary.update(status="paused", finished_at=time.time(),
                       failure={"step_id": "preflight", "code": "unsupported_input",
                                "detail": "batch/adaptive strategies are not supported"})
        atomic_write_json(store.summary_path(run_id), metadata(summary))
        return summary, []

    try:
        post = doc.get("postconditions", {})
        if post != {}:
            validate_check(post)
        for step in doc.get("steps", []):
            if step.get("effect_class") == "external_mutation":
                raise JourneyError("needs_authorization", "external writes return to the calling agent")
            from .fixed import validate_fixed_steps
            validate_fixed_steps([step])
            validate_steps([step])
            adapter.build_action_script({**step, "type": step.get("action"),
                                         "expect": step.get("postcondition", step.get("expect"))})
    except (JourneyError, ValueError, TypeError, OverflowError) as exc:
        summary.update(status="paused", finished_at=time.time(),
                       failure={"step_id": "preflight", "code": getattr(exc, "code", "invalid_request"),
                                "detail": str(exc)})
        atomic_write_json(store.summary_path(run_id), metadata(summary))
        return summary, []

    entry = doc.get("entry_state")
    if entry:
        try:
            validate_check(entry)
        except JourneyError as exc:
            summary.update(status="blocked", finished_at=time.time(),
                           failure={"step_id": "entry", "code": exc.code})
            atomic_write_json(store.summary_path(run_id), metadata(summary))
            return summary, []
        probe = adapter.harness_read(adapter.OBSERVE_TREE, serial=device_id)
        if not probe.get("ok") or not check(probe.get("nodes") or [], entry):
            summary.update(status="blocked", finished_at=time.time(),
                           failure={"step_id": "entry", "code": "precondition_mismatch"})
            atomic_write_json(store.summary_path(run_id), metadata(summary))
            return summary, []

    cancel_marker = store.locks / f"cancel-{run_id}"
    results: list[dict[str, Any]] = []

    for step in doc.get("steps", []):
        if cancel_marker.exists():
            store.append_event(run_id, {"event": "cancelled", "before_step": step["step_id"]})
            summary.update(status="cancelled", finished_at=time.time(),
                           failure={"step_id": step["step_id"], "code": "cancelled"})
            atomic_write_json(store.summary_path(run_id), metadata(summary))
            return summary, results
        step_id = step["step_id"]
        action = {**step, "type": step["action"],
                  "expect": step.get("postcondition", step.get("expect")),
                  "settle_ms": step.get("settle_ms", 800)}
        if step.get("effect_class") == "external_mutation":
            store.append_event(run_id, {"event": "needs_authorization", "step_id": step_id})
            summary.update(status="paused", finished_at=time.time(),
                           failure={"step_id": step_id, "code": "needs_authorization"})
            atomic_write_json(store.summary_path(run_id), metadata(summary))
            return summary, results
        from .pacing import before_action, PacingError
        try:
            pace = before_action(store, device_id, doc.get("budget_run_id", run_id), action, cancelled=cancel_marker.exists,
                                 foreground=lambda: adapter.foreground_package(device_id))
        except PacingError as exc:
            code = exc.code if exc.code in ("cancelled", "budget_exhausted") else "invalid_request"
            summary.update(status="cancelled" if code == "cancelled" else "paused", finished_at=time.time(), failure={"step_id": step_id, "code": code})
            atomic_write_json(store.summary_path(run_id), metadata(summary))
            return summary, results
        segment = step.get("fixed_segment")
        if segment:
            info = adapter.status(device_id)
            probe = adapter.harness_read(adapter.OBSERVE_TREE, serial=device_id)
            if (not info.get("ready") or (info.get("foreground") or "").split("/")[0] != segment["app"]
                    or not probe.get("ok") or probe.get("geometry") != segment["geometry"]
                    or not check(probe.get("nodes") or [], segment["entry"])):
                summary.update(status="paused", finished_at=time.time(),
                               failure={"step_id": step_id, "code": "precondition_mismatch"})
                atomic_write_json(store.summary_path(run_id), metadata(summary))
                return summary, results
            action["geometry"] = segment["geometry"]
        # A later action invalidates the old checkpoint before dispatch, even if
        # it succeeds without establishing a new checkpoint.
        summary.pop("checkpoint", None)
        atomic_write_json(store.summary_path(run_id), metadata(summary))
        store.append_event(run_id, {"event": "action_started", "step_id": step_id,
                                    "pacing": pace, "action": step["action"],
                                    "effect_class": step.get("effect_class")})
        # Keep occupancy across process death until the effect is durably known.
        reservation = reservation_path(store, device_id)
        atomic_write_json(reservation, {"run_id": run_id, "kind": "journey"})
        started = time.monotonic()
        outcome = adapter.run_action(action, serial=device_id)
        ok = bool(outcome.get("ok"))
        code = outcome.get("code") or ("ok" if ok else "action_failed")
        check_event = adapter.expect_check(step_id, action, outcome)
        if check_event:
            store.append_event(run_id, check_event)
        store.append_event(run_id, {"event": "action_finished", "step_id": step_id,
                                    "outcome": code,
                                    "duration_ms": (time.monotonic()-started)*1000,
                                    "metrics": outcome.get("metrics"),
                                    "locator_index": outcome.get("locator_index"),
                                    "wake": outcome.get("wake"),
                                    "dispatched": outcome.get("dispatched"),
                                    "value_present": outcome.get("value") is not None})
        if not store.open_actions(run_id):
            reservation.unlink(missing_ok=True)
        results.append({"step_id": step_id, "output": step.get("output", step_id), "ok": ok, "code": code,
                        "value": outcome.get("value"), "dispatched": outcome.get("dispatched")})
        if ok and segment:
            probe = adapter.harness_read(adapter.OBSERVE_TREE, serial=device_id)
            if not probe.get("ok") or not check(probe.get("nodes") or [], segment["exit"], device_id):
                ok, code = False, "assertion_failed"
                results[-1].update(ok=False, code=code)
        if ok and (step.get("postcondition") or step.get("expect") or segment):
            summary["checkpoint"] = {"next_step": doc.get("step_offset", 0) + doc["steps"].index(step) + 1,
                                     "step_id": step["step_id"]}
            atomic_write_json(store.summary_path(run_id), metadata(summary))
        if not ok:
            summary.update(status="paused", finished_at=time.time(),
                           failure={"step_id": step_id, "code": code,
                                    "detail": outcome.get("reason") or outcome.get("labels")})
            atomic_write_json(store.summary_path(run_id), metadata(summary))
            return summary, results

    if cancel_marker.exists():
        summary.update(status="cancelled", finished_at=time.time(),
                       failure={"step_id": "postconditions", "code": "cancelled"})
        atomic_write_json(store.summary_path(run_id), metadata(summary))
        return summary, results

    # Final postconditions, evaluated on one fresh observation.
    final_ok = True
    detail = None
    post = doc.get("postconditions") or {}
    if post:
        probe = adapter.harness_read(adapter.OBSERVE_TREE, serial=device_id)
        if not probe.get("ok"):
            summary.update(status="paused", finished_at=time.time(),
                           failure={"step_id": "postconditions", "code": "outcome_unknown",
                                    "detail": probe.get("reason") or probe.get("error")})
            atomic_write_json(store.summary_path(run_id), metadata(summary))
            return summary, results
        nodes = probe.get("nodes") or []
        try:
            final_ok = check(nodes, post, device_id)
        except ProviderUnavailable as exc:
            summary.update(status="paused", finished_at=time.time(),
                           failure={"step_id": "postconditions",
                                    "code": "provider_unavailable", "detail": str(exc)})
            atomic_write_json(store.summary_path(run_id), metadata(summary))
            return summary, results
        if not final_ok:
            detail = {"labels": [n.get("text") or n.get("desc") for n in nodes
                                 if (n.get("text") or n.get("desc"))][:12]}
        store.append_event(run_id, {"event": "check", "name": "postconditions",
                                    "ok": final_ok})

    if not post:
        summary.update(status="recorded", finished_at=time.time(), failure=None,
                       note="no frozen postconditions; a run cannot be completed")
    else:
        summary.update(status="completed" if final_ok else "paused",
                       finished_at=time.time(),
                       failure=None if final_ok else {"step_id": "postconditions",
                                                      "code": "assertion_failed",
                                                      "detail": detail})
    atomic_write_json(store.summary_path(run_id), metadata(summary))
    store.append_event(run_id, {"event": "run_finished", "status": summary["status"]})
    return summary, results


# ---------------------------------------------------------------- validate
def _validations_path(store: Store, journey_id: str):
    return _journey_dir(store, journey_id) / "validations.jsonl"


def _record_validation(store: Store, record: Mapping[str, Any]) -> None:
    path = _validations_path(store, record["journey_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _env_matches(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """True when every key present in both environments agrees."""
    for key in set(a) & set(b):
        if a[key] and b[key] and a[key] != b[key]:
            return False
    return True


def _promotion(store: Store, journey_id: str, version: int,
               environment: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """3 consecutive successes in one environment, 2 parameter digests if any."""
    path = _validations_path(store, journey_id)
    rows: list[dict[str, Any]] = []
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if row.get("version") != version:
                continue
            if environment and ((row.get("environment_key") or {}).get("guard_version") != environment.get("guard_version")
                                or not _env_matches(row.get("environment_key") or {}, environment)):
                continue
            rows.append(row)

    consecutive = 0
    for row in reversed(rows):
        if row.get("status") == "completed":
            consecutive += 1
        elif row.get("failure_code") in ("locked", "disconnected", "device_busy", "cancelled"):
            continue
        else:
            break
    successful = []
    for row in reversed(rows):
        if row.get("status") == "completed":
            successful.append(row)
        elif row.get("failure_code") not in ("locked", "disconnected", "device_busy", "cancelled"):
            break
    digests = {row.get("parameters_digest") for row in successful}
    doc = load_version(store, journey_id, version) or {}
    parameterised = bool((doc.get("parameters") or {}).get("properties"))
    enough_sets = len(digests) >= PROMOTION_PARAM_SETS
    verified = consecutive >= PROMOTION_SUCCESSES and (enough_sets or not parameterised)
    return {"consecutive": consecutive, "distinct_parameter_sets": len(digests),
            "parameterised": parameterised, "verified": verified}


def _current_environment(doc: Mapping[str, Any], device_id: str | None) -> dict[str, Any]:
    """The environment a run actually happens in, derived from the device."""
    info = adapter.status(device_id)
    if info.get("ambiguous"):
        raise JourneyError("ambiguous_device", "select a device explicitly")
    if not info.get("bound") or not info.get("devices"):
        raise JourneyError("disconnected", "no device is reachable")
    if info.get("ready") is not True:
        raise JourneyError("locked", "device is not ready")
    env = {"platform": "android", "device": info["bound"], "guard_version": 2}
    if info.get("model"):
        env["model"] = info["model"]
    declared = doc.get("environment_key") or {}
    if "app" in declared:
        foreground = info.get("foreground") or ""
        env["app"] = foreground.split("/")[0]
    for key, expected in declared.items():
        if key not in env or not env[key]:
            raise JourneyError("capability_gap", f"cannot verify environment field {key}")
        if env[key] != expected:
            raise JourneyError("version_mismatch", f"environment differs for {key}")
    return env


def _state_in(store: Store, journey_id: str, version: int,
              environment: Mapping[str, Any]) -> str:
    """Latest lifecycle state for this version in this environment."""
    path = _journey_dir(store, journey_id) / "lifecycle.jsonl"
    state = "candidate"
    if not path.exists():
        return state
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event.get("version") != version:
            continue
        env = event.get("environment_key")
        if event.get("state") == "verified" and (env or {}).get("guard_version") != environment.get("guard_version"):
            continue
        if env and not _env_matches(env, environment):
            continue
        if event.get("state"):
            state = event["state"]
    return state


def _validate_impl(store: Store, request: Mapping[str, Any]) -> Any:
    journey_id = request.get("journey_id")
    if not journey_id:
        return failure("invalid_request", "validate needs journey_id")
    version = int(request.get("version") or latest_version(store, journey_id))
    try:
        doc = load_version(store, journey_id, version)
    except JourneyError as exc:
        return failure(exc.code, exc.message)
    if doc is None:
        return failure("invalid_request", f"unknown journey {journey_id!r} v{version}")
    request_id = request.get("request_id")
    if not request_id:
        return failure("invalid_request", "validate needs request_id")

    try:
        from .parameters import bind_recipe
        doc = bind_recipe(doc, request.get("parameters"))
        # An explicit serial can be locked before probing; auto-selection still
        # needs discovery first and must be rechecked after acquiring the lock.
        environment = None if request.get("device_id") else _current_environment(doc, None)
        serial = request.get("device_id") or environment["device"]
    except JourneyError as exc:
        return failure(exc.code, exc.message)
    with _device_lock(store, serial) as busy:
        if busy:
            return failure("device_busy", f"device {busy} is held by another run")
        try:
            current = _current_environment(doc, serial)
        except JourneyError as exc:
            return failure(exc.code, exc.message)
        if environment is not None and current != environment:
            return failure("version_mismatch", "environment changed before execution")
        environment = current
        for key, value in (request.get("environment_key") or {}).items():
            if environment.get(key) != value:
                return failure("version_mismatch", f"request cannot override environment field {key}")
        state = _state_in(store, journey_id, version, environment)
        if state in ("superseded", "suspended"):
            return failure("version_mismatch", f"version {version} is {state}; propose a new version")

        summary, results = _execute(store, doc, environment["device"], "validate",
                                    request.get("authorization"))

        passed = summary["status"] == "completed"
        if (summary.get("failure") or {}).get("code") in ("locator_missing", "ambiguous_target", "assertion_failed", "unexpected_page"):
            _append_lifecycle(store, journey_id, {"version": version, "state": "suspended",
                              "environment_key": environment, "reason": "logical validation failure"})
            state = "suspended"
        _record_validation(store, {
            "journey_id": journey_id, "version": version,
            "run_id": summary["run_id"], "status": summary["status"],
            "failure_code": (summary.get("failure") or {}).get("code"),
            "environment_key": environment,
            "parameters_digest": _parameter_digest(store, doc),
            "at": time.time(), "assertions": [r["code"] for r in results],
        })
        promotion = _promotion(store, journey_id, version, environment)
        if promotion["verified"] and state != "verified":
            _append_lifecycle(store, journey_id, {"version": version, "state": "verified",
                                                  "environment_key": environment,
                                                  "reason": "promotion rule satisfied"})
    data = {"journey_id": journey_id, "version": version,
            "run_id": summary["run_id"], "steps": results,
            "environment_key": environment,
            "promotion": promotion,
            "outputs": {r.get("output", r["step_id"]): r["value"] for r in results if r.get("value") is not None},
            "state": "verified" if promotion["verified"] else state}
    if summary["status"] == "recorded":
        return success("recorded", {**data, "note": "no frozen postconditions; "
                                                  "this run cannot promote"}, run_id=summary["run_id"])
    if not passed:
        info = summary.get("failure") or {}
        code = info.get("code") or "assertion_failed"
        mapped = code if code in ("locator_missing", "ambiguous_target",
                                  "assertion_failed", "provider_unavailable",
                                  "needs_authorization", "unsupported_input", "invalid_request", "cancelled", "precondition_mismatch", "budget_exhausted", "platform_blocked", "locked") else "outcome_unknown"
        return failure(mapped, f"validate run {summary['run_id']} did not complete",
                       data=data, run_id=summary["run_id"])
    return success("completed", data, run_id=summary["run_id"])


# ---------------------------------------------------------------- run
def _run_impl(store: Store, request: Mapping[str, Any]) -> Any:
    journey_id = request.get("journey_id")
    if not journey_id:
        return failure("invalid_request", "run needs journey_id")
    version = int(request.get("version") or latest_version(store, journey_id))
    try:
        doc = load_version(store, journey_id, version)
    except JourneyError as exc:
        return failure(exc.code, exc.message)
    if doc is None:
        return failure("invalid_request", f"unknown journey {journey_id!r} v{version}")
    request_id = request.get("request_id")
    if not request_id:
        return failure("invalid_request", "run needs request_id")

    try:
        from .parameters import bind_recipe
        doc = bind_recipe(doc, request.get("parameters"))
        # An explicit serial can be locked before probing; auto-selection still
        # needs discovery first and must be rechecked after acquiring the lock.
        environment = None if request.get("device_id") else _current_environment(doc, None)
        serial = request.get("device_id") or environment["device"]
    except JourneyError as exc:
        return failure(exc.code, exc.message)
    with _device_lock(store, serial) as busy:
        if busy:
            return failure("device_busy", f"device {busy} is held by another run")
        try:
            current = _current_environment(doc, serial)
        except JourneyError as exc:
            return failure(exc.code, exc.message)
        if environment is not None and current != environment:
            return failure("version_mismatch", "environment changed before execution")
        environment = current
        for key, value in (request.get("environment_key") or {}).items():
            if environment.get(key) != value:
                return failure("version_mismatch", f"request cannot override environment field {key}")
        state = _state_in(store, journey_id, version, environment)
        if state != "verified":
            return failure("precondition_mismatch",
                           f"version {version} is {state} in this environment; "
                           "only a verified version may run",
                           data={"journey_id": journey_id, "version": version,
                                 "state": state, "environment_key": environment})

        summary, results = _execute(store, doc, environment["device"], "replay",
                                    request.get("authorization"))

        if (summary.get("failure") or {}).get("code") in ("locator_missing", "ambiguous_target", "assertion_failed", "unexpected_page"):
            _append_lifecycle(store, journey_id, {"version": version, "state": "suspended",
                              "environment_key": environment, "reason": "logical replay failure"})
    data = {"journey_id": journey_id, "version": version,
            "run_id": summary["run_id"], "steps": results,
            "environment_key": environment,
            "outputs": {r.get("output", r["step_id"]): r["value"] for r in results if r.get("value") is not None}}
    if summary["status"] == "recorded":
        return success("recorded", {**data, "note": "no frozen postconditions; "
                                                  "nothing was proven"}, run_id=summary["run_id"])
    if summary["status"] != "completed":
        info = summary.get("failure") or {}
        code = info.get("code") or "assertion_failed"
        mapped = code if code in ("locator_missing", "ambiguous_target",
                                  "assertion_failed", "provider_unavailable",
                                  "needs_authorization", "unsupported_input", "invalid_request", "cancelled", "precondition_mismatch", "budget_exhausted", "platform_blocked", "locked") else "outcome_unknown"
        return failure(mapped, f"run {summary['run_id']} paused at {info.get('step_id')}",
                       data=data, run_id=summary["run_id"])
    return success("completed", data, run_id=summary["run_id"])


# ---------------------------------------------------------------- request dedupe
def _dedupe(store: Store, request: Mapping[str, Any], handler) -> Any:
    return request_once(store, "journey", request.get("request_id"),
                        {**request, "command": handler.__name__}, lambda: handler(store, request))


def validate(store: Store, request: Mapping[str, Any]) -> Any:
    return _dedupe(store, request, _validate_impl)


def run(store: Store, request: Mapping[str, Any]) -> Any:
    return _dedupe(store, request, _run_impl)


def resume(store: Store, request: Mapping[str, Any]) -> Any:
    return _dedupe(store, request, _resume_impl)


def _resume_impl(store, request):
    from .parameters import bind_recipe
    original_id = request.get("run_id")
    if not original_id:
        return failure("invalid_request", "resume needs run_id")
    original = read_json(store.summary_path(original_id))
    if not original or original.get("status") not in ("paused", "cancelled", "blocked"):
        return failure("needs_replan", "resume needs a paused Journey run")
    checkpoint = original.get("checkpoint")
    if not checkpoint or store.open_actions(original_id):
        return failure("needs_replan", "no trusted checkpoint or action effects remain unknown", run_id=original_id)
    if original.get("resumed_by"):
        return failure("needs_replan", "this checkpoint already has a continuation",
                       data={"continuation_run_id": original["resumed_by"]}, run_id=original_id)
    serial = original.get("device_id")
    if request.get("device_id") not in (None, serial):
        return failure("version_mismatch", "resume cannot change devices", run_id=original_id)
    try:
        doc = load_version(store, original["journey_id"], original["version"])
        if not doc or not original.get("recipe_digest") or doc.get("recipe_digest") != original["recipe_digest"]:
            return failure("version_mismatch", "resume requires the original immutable recipe", run_id=original_id)
        doc = bind_recipe(doc, request.get("parameters"))
        if _parameter_digest(store, doc) != original.get("parameters_digest"):
            return failure("missing_parameter", "supply the same runtime parameters to resume", run_id=original_id)
        environment = _current_environment(doc, serial)
        if _state_in(store, doc["journey_id"], doc["version"], environment) in ("suspended", "superseded"):
            return failure("needs_replan", "invalidated recipes cannot resume", run_id=original_id)
        index = checkpoint["next_step"]
        previous = doc["steps"][index - 1]
        condition = (previous.get("fixed_segment") or {}).get("exit") or previous.get("postcondition", previous.get("expect"))
        validate_check(condition)
        with _device_lock(store, serial) as busy:
            if busy:
                return failure("device_busy", "device is busy")
            current = _current_environment(doc, serial)
            if current != environment or _state_in(store, doc["journey_id"], doc["version"], current) in ("suspended", "superseded"):
                return failure("needs_replan", "environment or qualification changed before continuation", run_id=original_id)
            original = read_json(store.summary_path(original_id)) or {}
            if original.get("resumed_by"):
                return failure("needs_replan", "checkpoint already claimed by another continuation", run_id=original_id)
            observed = adapter.harness_read(adapter.OBSERVE_TREE, serial=serial)
            fixed = previous.get("fixed_segment")
            if fixed:
                info = adapter.status(serial)
                if (observed.get("geometry") != fixed["geometry"]
                        or (info.get("foreground") or "").split("/")[0] != fixed["app"]):
                    return failure("precondition_mismatch", "fixed checkpoint layout or app changed", run_id=original_id)
            if not observed.get("ok") or not check(observed.get("nodes") or [], condition, serial):
                return failure("precondition_mismatch", "live scene does not match the saved checkpoint", run_id=original_id)
            # Persist the claim before input; a crash cannot launch another continuation.
            continuation_id = "r_" + secrets.token_hex(4)
            store.create_run(continuation_id, {"run_id": continuation_id, "kind": "continuation",
                "status": "blocked", "device_id": serial, "continuation_of": original_id,
                "failure": {"code": "outcome_unknown"}, "note": "continuation reserved before dispatch"})
            original["resumed_by"] = continuation_id
            atomic_write_json(store.summary_path(original_id), original)
            continuation = {**doc, "continuation_run_id": continuation_id,
                            "steps": doc["steps"][index:], "entry_state": condition,
                            "continuation_of": original_id, "budget_run_id": original.get("budget_run_id", original_id),
                            "step_offset": index, "original_parameters_digest": original["parameters_digest"]}
            summary, results = _execute(store, continuation, serial, "continuation")
            original["resumed_by"] = summary["run_id"]
            atomic_write_json(store.summary_path(original_id), original)
    except JourneyError as exc:
        return failure(exc.code, exc.message, run_id=original_id)
    data = {"continuation_of": original_id, "steps": results, "agent_intervened": True,
            "promotion_eligible": False}
    if summary["status"] in ("completed", "recorded"):
        return success(summary["status"], data, run_id=summary["run_id"])
    return failure("needs_replan", "continuation paused; inspect its new run", data=data, run_id=summary["run_id"])


# ---------------------------------------------------------------- cancel
def cancel(store: Store, request: Mapping[str, Any]) -> Any:
    """Ask a run to stop before its next step. Already-done steps stay recorded."""
    run_id = request.get("run_id")
    if not run_id:
        return failure("invalid_request", "cancel needs run_id")
    if not store.run_dir(run_id).is_dir():
        return failure("invalid_request", f"unknown run {run_id!r}")
    marker = store.locks / f"cancel-{run_id}"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(time.time()), encoding="utf-8")
    store.append_event(run_id, {"event": "cancel_requested"})
    return success("ok", {"run_id": run_id, "cancel_requested": True,
                          "note": "the executor stops before its next step"})
