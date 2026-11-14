"""Exploration sessions: lease, observe, act, finish.

The invariant this module exists for: an action is never started before its
``action_started`` event is durable. If the store cannot be written, the phone
is not touched.

Every call takes ``run_id`` and ``lease_token``. The lease is a file, not
memory, so a second CLI process sees it. Idle leases expire after 30 minutes;
a resumed run gets a new token and the old one stops working.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Mapping

from . import adapter
from .contracts import Result, failure, success
from .store import Store, StoreError, atomic_write_json, read_json
from .execution import (device_lock, file_lock, identifier, request_once,
                        request_record_path, reservation_path)

LEASE_IDLE_SECONDS = 30 * 60
GRANT_TTL_SECONDS = 10 * 60
AUTHORIZED_ACTION_TYPES = ("tap", "share_text")


class LeaseError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> float:
    return time.time()


def _lease_path(store: Store, run_id: str) -> Path:
    identifier(run_id, "run_id")
    return store.locks / f"{run_id}.json"


def _write_lease(store: Store, run_id: str, token: str) -> None:
    summary = read_json(store.summary_path(run_id)) or {}
    atomic_write_json(_lease_path(store, run_id),
                      {"run_id": run_id, "token": token, "device_id": summary.get("device_id"),
                       "expires_at": _now() + LEASE_IDLE_SECONDS, "pid": os.getpid()})


def _require_lease(store: Store, run_id: str, token: str) -> None:
    data = read_json(_lease_path(store, run_id))
    if not data:
        raise LeaseError("device_busy", "no lease for this run; start a session first")
    if data.get("token") != token:
        raise LeaseError("device_busy", "lease token does not match the active lease")
    if float(data.get("expires_at", 0)) < _now():
        raise LeaseError("device_busy", "lease expired; inspect and hand off before taking a new lease")
    return data


def _touch_lease(store: Store, run_id: str, token: str) -> None:
    _write_lease(store, run_id, token)


# ------------------------------------------------------- external-write grants
def _grant_path(store: Store, grant_id: str) -> Path:
    identifier(grant_id, "grant_id")
    return store.root / "authorizations" / f"{hashlib.sha256(grant_id.encode()).hexdigest()}.json"


def _grant_base_failure(row, run_id, device_id):
    if not row:
        return failure("needs_authorization", "authorization grant not found", run_id=run_id)
    if row.get("run_id") != run_id or row.get("device_id") != device_id:
        return failure("needs_authorization", "authorization grant belongs to another run or device", run_id=run_id)
    if row.get("state") != "prepared":
        return failure("needs_authorization",
                       "authorization grant was already used or invalidated", run_id=run_id)
    if float(row.get("expires_at", 0)) < _now():
        return failure("needs_authorization", "authorization grant expired; prepare a new one", run_id=run_id)
    return None


def _prepared_grants(store: Store, run_id: str) -> list[Path]:
    found = []
    for path in (store.root / "authorizations").glob("*.json"):
        row = read_json(path)
        if (isinstance(row, dict) and row.get("run_id") == run_id
                and row.get("state") == "prepared"
                and float(row.get("expires_at", 0)) >= _now()):
            found.append(path)
    return found


def _run_has_prepared_grant(store: Store, run_id: str) -> bool:
    return bool(_prepared_grants(store, run_id))


def _invalidate_prepared_grants(store: Store, run_id: str, reason: str,
                                keep: str | None = None) -> bool:
    """A grant only covers a prepared opener, input and send sequence."""
    for path in _prepared_grants(store, run_id):
        with file_lock(path.with_suffix(".lock")) as acquired:
            if not acquired:
                return False
            row = read_json(path)
            if (not isinstance(row, dict) or row.get("run_id") != run_id
                    or row.get("state") != "prepared" or row.get("grant_id") == keep):
                continue
            row.update(state="invalidated", invalidated_at=_now(),
                       invalidated_reason=reason)
            atomic_write_json(path, row)
    return True


def _advance_prepared_grants(store: Store, run_id: str, action: Mapping[str, Any],
                             keep: str | None = None) -> bool:
    """Allow only the bound opener tap; every other unbound action burns grants."""
    from .privacy import private_digest
    digest = private_digest(store, dict(action))
    for path in _prepared_grants(store, run_id):
        with file_lock(path.with_suffix(".lock")) as acquired:
            if not acquired:
                return False
            row = read_json(path)
            if (not isinstance(row, dict) or row.get("run_id") != run_id
                    or row.get("state") != "prepared" or row.get("grant_id") == keep):
                continue
            opener = row.get("opener_digest")
            if (isinstance(opener, str) and row.get("opener_used_at") is None
                    and action.get("type") == "tap" and digest == opener):
                row.update(opener_used_at=_now())
                atomic_write_json(path, row)
                continue
            row.update(state="invalidated", invalidated_at=_now(),
                       invalidated_reason="action_after_prepare")
            atomic_write_json(path, row)
    return True


def _locator_hits(nodes, locator) -> list:
    """The same matching rules the action script applies to one tree read."""
    if not isinstance(locator, Mapping):
        return []
    hits = []
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        if "resource_id" in locator and node.get("id") != locator["resource_id"]:
            continue
        if "text" in locator and (node.get("text") or "").strip() != locator["text"]:
            continue
        if "text_contains" in locator and locator["text_contains"] not in (node.get("text") or ""):
            continue
        if "description" in locator and (node.get("desc") or "").strip() != locator["description"]:
            continue
        if "class" in locator and node.get("class") != locator["class"]:
            continue
        hits.append(node)
    return hits


def _prepare_grant(store: Store, request: Mapping[str, Any]) -> Result:
    """Bind one exact external tap to live post identity and one text.

    ``share_text`` has no on-screen target: its identity is the package and
    text inside the action itself, so the grant binds those digests instead.
    """
    from .privacy import private_digest
    run_id = request.get("run_id")
    post = request.get("post")
    action = request.get("action")
    text = request.get("text")
    if isinstance(action, Mapping) and action.get("type") == "share_text":
        return _prepare_share_grant(store, request)
    if not isinstance(post, Mapping):
        return failure("invalid_request", "authorize needs post.title and post.author", run_id=run_id)
    title, author = post.get("title"), post.get("author")
    if not (isinstance(title, str) and title.strip()
            and isinstance(author, str) and author.strip()):
        return failure("invalid_request", "post.title and post.author must be nonempty strings", run_id=run_id)
    if not isinstance(text, str) or not text.strip():
        return failure("invalid_request", "authorize needs the exact nonempty comment text", run_id=run_id)
    if not isinstance(action, Mapping) or action.get("type") not in AUTHORIZED_ACTION_TYPES:
        return failure("invalid_request", f"authorize supports only {AUTHORIZED_ACTION_TYPES}", run_id=run_id)
    if action.get("effect_class") != "external_mutation":
        return failure("invalid_request", "authorized action must declare effect_class=external_mutation", run_id=run_id)
    opener = request.get("opener")
    if opener is not None and (not isinstance(opener, Mapping) or opener.get("type") != "tap"
                               or opener.get("effect_class") == "external_mutation"):
        return failure("invalid_request", "opener must be the navigation tap that opens the editor", run_id=run_id)
    try:
        adapter.build_action_script(dict(action))
        if opener is not None:
            adapter.build_action_script(dict(opener))
    except Exception as exc:
        code = getattr(exc, "code", "invalid_request")
        return failure(code, str(exc), run_id=run_id)

    serial = request.get("device_id")
    raw = adapter.harness_read(adapter.OBSERVE_TREE, serial=serial)
    if not raw.get("ok"):
        return failure("precondition_mismatch", "cannot read the live post for authorization", run_id=run_id)
    nodes = raw.get("nodes") or []
    visible = {str(node.get("text") or "").strip() for node in nodes if isinstance(node, Mapping)}
    if title.strip() not in visible or author.strip() not in visible:
        return failure("precondition_mismatch",
                       "authorize needs the exact live post title and author on screen",
                       run_id=run_id)
    observation_id = "o_" + secrets.token_hex(4)
    observation_digest = private_digest(store, nodes)
    store.append_event(run_id, {
        "event": "observation", "observation_id": observation_id, "source": "tree",
        "digest": observation_digest, "observed_at": _now(), "device_id": serial,
        "geometry": raw.get("geometry"), "count": len(nodes),
    })
    grant_id = "g_" + secrets.token_hex(8)
    action_digest = private_digest(store, dict(action))
    text_digest = private_digest(store, {"text": text})
    post_digest = private_digest(store, {"title": title, "author": author})
    opener_digest = private_digest(store, dict(opener)) if opener is not None else None
    if not _invalidate_prepared_grants(store, run_id, "superseded_by_prepare"):
        return failure("device_busy", "cannot invalidate the previous grant; retry", run_id=run_id)
    grant = {
        "grant_id": grant_id, "run_id": run_id, "device_id": serial,
        "observation_id": observation_id, "post_digest": post_digest,
        "action_digest": action_digest, "text_digest": text_digest,
        "opener_digest": opener_digest, "opener_used_at": None,
        "state": "prepared", "created_at": _now(),
        "expires_at": _now() + GRANT_TTL_SECONDS,
    }
    atomic_write_json(_grant_path(store, grant_id), grant)
    store.append_event(run_id, {
        "event": "authorization_prepared", "grant_id": grant_id, "post_digest": post_digest,
        "action_digest": action_digest, "text_digest": text_digest,
        "opener_digest": opener_digest,
        "observation_id": observation_id, "device_id": serial,
    })
    return success("recorded", {
        "grant_id": grant_id, "expires_at": grant["expires_at"],
        "post_digest": post_digest, "action_digest": action_digest,
        "text_digest": text_digest, "opener_digest": opener_digest,
        "observation_id": observation_id,
    }, run_id=run_id)


def _prepare_share_grant(store: Store, request: Mapping[str, Any]) -> Result:
    from .privacy import private_digest
    run_id = request.get("run_id")
    action, text = request["action"], request.get("text")
    if action.get("effect_class") != "external_mutation":
        return failure("invalid_request", "authorized action must declare effect_class=external_mutation", run_id=run_id)
    if not isinstance(text, str) or text != action.get("text"):
        return failure("invalid_request", "authorize text must equal the share_text action text", run_id=run_id)
    if "post" in request or "opener" in request:
        return failure("invalid_request", "share_text authorization takes no post or opener", run_id=run_id)
    try:
        adapter.build_action_script(dict(action))
    except Exception as exc:
        return failure(getattr(exc, "code", "invalid_request"), str(exc), run_id=run_id)
    grant_id = "g_" + secrets.token_hex(8)
    action_digest = private_digest(store, dict(action))
    text_digest = private_digest(store, {"text": text})
    if not _invalidate_prepared_grants(store, run_id, "superseded_by_prepare"):
        return failure("device_busy", "cannot invalidate the previous grant; retry", run_id=run_id)
    grant = {
        "grant_id": grant_id, "run_id": run_id, "device_id": request.get("device_id"),
        "action_type": "share_text", "action_digest": action_digest, "text_digest": text_digest,
        "state": "prepared", "created_at": _now(), "expires_at": _now() + GRANT_TTL_SECONDS,
    }
    atomic_write_json(_grant_path(store, grant_id), grant)
    store.append_event(run_id, {
        "event": "authorization_prepared", "grant_id": grant_id, "action": "share_text",
        "action_digest": action_digest, "text_digest": text_digest,
        "device_id": request.get("device_id"),
    })
    return success("recorded", {
        "grant_id": grant_id, "expires_at": grant["expires_at"],
        "action_digest": action_digest, "text_digest": text_digest,
    }, run_id=run_id)


def _check_grant_text(store: Store, grant_id: str, request: Mapping[str, Any],
                      text: str) -> Result | None:
    """Read-only text check before any input; stamping happens after success."""
    from .privacy import private_digest
    row = read_json(_grant_path(store, grant_id))
    problem = _grant_base_failure(row, request.get("run_id"), request.get("device_id"))
    if problem:
        return problem
    if row.get("action_type") == "share_text":
        return failure("needs_authorization",
                       "a share_text grant does not cover input_text", run_id=request.get("run_id"))
    if row.get("text_digest") != private_digest(store, {"text": text}):
        return failure("needs_authorization",
                       "input text does not match the authorized text", run_id=request.get("run_id"))
    if row.get("opener_digest") and row.get("opener_used_at") is None:
        return failure("needs_authorization",
                       "the authorized editor-opening tap has not run in this grant's sequence",
                       run_id=request.get("run_id"))
    return None


def _stamp_grant_text(store: Store, grant_id: str, request: Mapping[str, Any],
                      text: str) -> Result | None:
    """Record that the authorized text (by keyed digest) was entered in this run."""
    from .privacy import private_digest
    path = _grant_path(store, grant_id)
    digest = private_digest(store, {"text": text})
    with file_lock(path.with_suffix(".lock")) as acquired:
        if not acquired:
            return failure("device_busy", "authorization grant is busy", run_id=request.get("run_id"))
        row = read_json(path)
        problem = _grant_base_failure(row, request.get("run_id"), request.get("device_id"))
        if problem:
            return problem
        if row.get("text_digest") != digest:
            return failure("needs_authorization",
                           "input text does not match the authorized text", run_id=request.get("run_id"))
        row.update(text_entered_at=_now(), input_request_id=request.get("request_id"))
        atomic_write_json(path, row)
    return None


def _consume_grant(store: Store, grant_id: str, request: Mapping[str, Any],
                   action: Mapping[str, Any], text: str | None) -> Result | None:
    """Burn the grant only after the live field and send target still match."""
    from .privacy import private_digest
    run_id = request.get("run_id")
    path = _grant_path(store, grant_id)
    digest = private_digest(store, dict(action))
    with file_lock(path.with_suffix(".lock")) as acquired:
        if not acquired:
            return failure("device_busy", "authorization grant is busy", run_id=run_id)
        row = read_json(path)
        problem = _grant_base_failure(row, run_id, request.get("device_id"))
        if problem:
            return problem
        if row.get("action_digest") != digest:
            return failure("needs_authorization",
                           "action or locator does not match the authorization grant",
                           run_id=run_id)
        if (row.get("action_type") == "share_text") != (action.get("type") == "share_text"):
            return failure("needs_authorization",
                           "action or locator does not match the authorization grant",
                           run_id=run_id)
        if action.get("type") == "share_text":
            if text != action.get("text") or row.get("text_digest") != private_digest(store, {"text": text}):
                return failure("needs_authorization",
                               "share_text needs the exact authorized text", run_id=run_id)
            row.update(state="consumed", consumed_at=_now(), request_id=request.get("request_id"))
            atomic_write_json(path, row)
            return None
        if not row.get("text_entered_at"):
            return failure("needs_authorization",
                           "authorized text was not entered in this run before send",
                           run_id=run_id)
        if not isinstance(text, str) or not text or row.get("text_digest") != private_digest(store, {"text": text}):
            return failure("needs_authorization",
                           "send needs the exact authorized comment text for a live check",
                           run_id=run_id)
        # Bind the send to the current screen, not only to the prepare-time tree.
        observed = _observe(store, {**request, "source": "tree"})
        if observed.status != "recorded":
            return observed
        nodes = (observed.data or {}).get("nodes") or []
        if not any((node.get("text") or "") == text for node in nodes
                   if isinstance(node, Mapping)):
            return failure("precondition_mismatch",
                           "authorized comment text is not visible; send not attempted",
                           run_id=run_id)
        if len(_locator_hits(nodes, action.get("locator"))) != 1:
            return failure("precondition_mismatch",
                           "send target does not resolve uniquely; send not attempted",
                           run_id=run_id)
        row.update(state="consumed", consumed_at=_now(), request_id=request.get("request_id"))
        atomic_write_json(path, row)
    return None


def _close_blocked_start(store: Store, run_id: str,
                         serial: str | None, reason: str) -> None:
    """Close a start that could not become ready, so no lease blocks a retry."""
    summary = read_json(store.summary_path(run_id)) or {}
    summary.update(status="blocked", finished_at=_now(),
                   failure={"code": "locked", "detail": reason})
    atomic_write_json(store.summary_path(run_id), summary)
    store.append_event(run_id, {"event": "session_finished", "outcome": "locked",
                                "unknown_steps": []})
    _lease_path(store, run_id).unlink(missing_ok=True)
    if serial:
        reservation = reservation_path(store, serial)
        if (read_json(reservation) or {}).get("run_id") == run_id:
            reservation.unlink(missing_ok=True)


# ---------------------------------------------------------------- start
def _start(store: Store, request: Mapping[str, Any]) -> Result:
    intent = request.get("intent")
    if not isinstance(intent, str):
        return failure("invalid_request", "session start needs a string intent")
    intent = intent.strip()
    if not intent:
        return failure("invalid_request", "session start needs an intent")
    device = adapter.status(request.get("device_id"))
    if device.get("ambiguous"):
        return failure("ambiguous_device", "several devices are attached; pass device_id",
                       data={"devices": device.get("devices")})
    if not device.get("devices"):
        return failure("disconnected", "no device is reachable")

    from .privacy import private_digest
    run_id = "r_" + secrets.token_hex(4)
    token = "l_" + secrets.token_hex(8)
    summary = {
        "run_id": run_id,
        "kind": "exploration",
        "intent": request.get("task_key") or "exploration",
        "intent_digest": private_digest(store, intent),
        "task_key": request.get("task_key"),
        "device_id": device.get("bound"),
        "model": device.get("model"),
        "status": "running",
        "started_at": _now(),
        "finished_at": None,
        # Exploration never evaluates these checks; retain only their keyed identity.
        "frozen_postconditions_digest": private_digest(store, request["frozen_postconditions"])
            if request.get("frozen_postconditions") is not None else None,
        "steps": [],
    }
    store.create_run(run_id, summary)
    _write_lease(store, run_id, token)
    atomic_write_json(reservation_path(store, device["bound"]), {"run_id": run_id})
    store.append_event(run_id, {"event": "session_started", "intent": intent,
                                "device_id": device.get("bound")})
    wake = None
    lock = device.get("lock") or {}
    if not device.get("ready") and lock.get("requires_human") is True:
        reason = "credential/PIN screen is visible; a person must unlock"
        _close_blocked_start(store, run_id, device.get("bound"), reason)
        return failure("locked", reason, data={
            "device_id": device.get("bound"), "ready": False,
            "foreground": device.get("foreground"),
            "lock": lock, "wake": None}, run_id=run_id)
    if not device.get("ready") and adapter.ordinary_wake_allowed(device):
        # Record the ordinary wake/swipe in this run before touching the phone.
        attempt = _act(store, {"run_id": run_id, "lease_token": token,
                               "device_id": device.get("bound"),
                               "request_id": "wake-" + run_id,
                               "action": {"type": "wake", "step_id": "wake"}})
        wake = (attempt.data or {}).get("wake")
        detail = (attempt.data or {}).get("detail") or {}
        wake = wake or detail.get("wake")
        device = adapter.status(device.get("bound"))
        # A locked/unknown wake blocks even if a later probe reads a tree.
        if attempt.status != "recorded" or not device.get("ready"):
            unknown = [a.get("step_id") for a in store.open_actions(run_id)]
            if unknown:
                # The wake's effect is unknown (e.g. harness timeout): it may still
                # be running, so keep the run, lease and reservation; no new
                # session may take the device. Inspect, resolve or finish this run.
                return failure("outcome_unknown",
                               "ordinary wake did not finish with a known result; "
                               "the device stays reserved by this run",
                               data={"device_id": device.get("bound"), "ready": False,
                                     "lease_token": token, "unknown_steps": unknown,
                                     "wake": wake, "wake_status": attempt.status},
                               run_id=run_id)
            reason = (detail.get("reason") or device.get("reason")
                      or (attempt.error or {}).get("message")
                      or "device is still locked after ordinary wake")
            if (wake or {}).get("requires_human"):
                reason = "secure lock screen is showing; a person must enter the PIN or password"
            _close_blocked_start(store, run_id, device.get("bound"), reason)
            return failure("locked", reason, data={
                "device_id": device.get("bound"), "ready": False,
                "foreground": device.get("foreground"),
                "lock": device.get("lock"), "wake": wake,
                "wake_status": attempt.status}, run_id=run_id)
    return success("ok", {"run_id": run_id, "lease_token": token,
                          "device_id": device.get("bound"),
                          "ready": device.get("ready"),
                          "foreground": device.get("foreground"),
                          "reason": device.get("reason"),
                          "lock": device.get("lock"), "wake": wake},
                   run_id=run_id)


# ---------------------------------------------------------------- observe
def _observe(store: Store, request: Mapping[str, Any]) -> Result:
    run_id = request.get("run_id")
    token = request.get("lease_token")
    if not run_id or not token:
        return failure("invalid_request", "observe needs run_id and lease_token")
    try:
        _require_lease(store, run_id, token)
    except LeaseError as exc:
        return failure(exc.code, exc.message, run_id=run_id)

    source = request.get("source", "tree")
    if source in ("tree", "pixels"):
        script = adapter.OBSERVE_TREE if source == "tree" else adapter.OBSERVE_PIXELS
        observe_started = time.monotonic()
        raw = adapter.harness_read(script, serial=request.get("device_id"))
        # Coordinates age from the tree read, not from any OCR that follows it.
        observed_at = _now()
        extra: dict[str, Any] = {}
        if source == "tree":
            nodes = (raw.get("nodes") or []) if raw.get("ok") else []
            quality = adapter.tree_quality(nodes, raw.get("geometry"))
            extra["tree_quality"] = quality
            if quality != "good":
                pixels = adapter.harness_read(adapter.OBSERVE_PIXELS, serial=request.get("device_id"))
                if pixels.get("ok"):
                    extra["ocr_nodes"] = adapter.ocr_supplement(nodes, pixels.get("nodes") or [])
                    if not raw.get("ok"):
                        # No tree was read: record OCR-only evidence as pixels so tree
                        # consumers (resolve_input) never treat it as a tree.
                        extra["tree_error"] = raw.get("reason") or raw.get("error") or "observation failed"
                        raw = {"ok": True, "nodes": [], "geometry": pixels.get("geometry")}
                        source, observed_at = "pixels", _now()
                elif raw.get("ok"):
                    extra["ocr_error"] = pixels.get("reason") or pixels.get("error") or "ocr failed"
        observe_ms = (time.monotonic() - observe_started) * 1000
        if not raw.get("ok"):
            return failure("locked", raw.get("reason") or "observation failed",
                           data=extra or None, run_id=run_id)
        nodes = raw.get("nodes") or []
        observation = {"source": source, "nodes": nodes, "geometry": raw.get("geometry"), **extra}
        from .privacy import private_digest
        digest = private_digest(store, extra["ocr_nodes"] if "tree_error" in extra else nodes)
    elif source == "screenshot":
        observe_started = time.monotonic()
        script = (adapter.OBSERVE_SCREENSHOT_NO_STATUS_BAR if request.get("exclude_status_bar") is True
                  else adapter.OBSERVE_SCREENSHOT)
        raw = adapter.harness_read(script, serial=request.get("device_id"))
        observed_at = _now()
        observe_ms = (time.monotonic() - observe_started) * 1000
        if not raw.get("ok"):
            return failure("locked", raw.get("reason") or "capture failed", run_id=run_id)
        observation = {"source": "screenshot", "bytes": raw.get("bytes"),
                       "image_base64": raw.get("image_base64")}
        digest = hashlib.sha256(str(raw.get("bytes")).encode()).hexdigest()[:16]
    else:
        return failure("invalid_request", f"unknown source {source!r}", run_id=run_id)

    observation_id = "o_" + secrets.token_hex(4)
    if source in ("tree", "pixels") and request.get("persist_tree") is True:
        path = store.run_dir(run_id) / "evidence" / f"{observation_id}.json"
        atomic_write_json(path, observation)
        path.chmod(0o600)
        observation["evidence_path"] = str(path)
    store.append_event(run_id, {
        "event": "observation", "observation_id": observation_id,
        "source": source, "digest": digest, "observed_at": observed_at,
        "duration_ms": observe_ms,
        "device_id": request.get("device_id"), "geometry": raw.get("geometry"),
        "count": len(observation.get("nodes") or []),
        **({"tree_quality": observation["tree_quality"],
            "ocr_count": len(observation.get("ocr_nodes") or [])}
           if "tree_quality" in observation else {}),
    })
    _touch_lease(store, run_id, token)
    data = {"observation_id": observation_id, **observation}
    signals = ("操作频繁", "请求过于频繁", "访问频繁", "滑块验证", "安全验证", "人机验证",
               "too many requests", "rate limit", "verify you are human", "captcha")
    if any(any(signal in str(n.get("text") or n.get("desc") or "").lower() for signal in signals)
           for n in observation.get("nodes", []) + observation.get("ocr_nodes", [])):
        store.append_event(run_id, {"event": "platform_blocked", "observation_id": observation_id,
                                    "blocked_by": "verification_block"})
        return failure("platform_blocked", "verification or rate limit visible; hand back to caller", data=data, run_id=run_id)
    return success("recorded", data, run_id=run_id)


# ---------------------------------------------------------------- act
def _authorize(store: Store, request: Mapping[str, Any]) -> Result:
    if not request.get("run_id") or not request.get("lease_token"):
        return failure("invalid_request", "authorize needs run_id and lease_token",
                       run_id=request.get("run_id"))
    result = _prepare_grant(store, request)
    if result.status == "recorded":
        _touch_lease(store, request["run_id"], request["lease_token"])
    return result


def _act(store: Store, request: Mapping[str, Any]) -> Result:
    run_id = request.get("run_id")
    token = request.get("lease_token")
    request_id = request.get("request_id")
    action = request.get("action") or {}
    if not run_id or not token or not request_id:
        return failure("invalid_request", "act needs run_id, lease_token and request_id")
    if not isinstance(action, Mapping) or not action.get("type"):
        return failure("invalid_request", "act needs action.type")
    from .journey import JourneyError
    try:
        adapter.build_action_script(dict(action))
    except JourneyError as exc:
        return failure(exc.code, exc.message, run_id=run_id)
    except (ValueError, TypeError, OverflowError) as exc:
        return failure("invalid_request", str(exc), run_id=run_id)
    authorization = request.get("authorization")
    grant_id = authorization.get("grant_id") if isinstance(authorization, Mapping) else None
    auth_text = authorization.get("text") if isinstance(authorization, Mapping) else None
    if action.get("effect_class") == "external_mutation" and not isinstance(grant_id, str):
        return failure("needs_authorization", "external writes need a one-time authorization grant",
                       run_id=run_id)
    if grant_id is not None:
        if not isinstance(grant_id, str):
            return failure("needs_authorization", "authorization needs a string grant_id", run_id=run_id)
        if not (action.get("effect_class") == "external_mutation"
                or action.get("type") == "input_text"):
            return failure("invalid_request",
                           "authorization applies only to input_text and external_mutation",
                           run_id=run_id)
    if (action.get("effect_class") == "external_mutation"
            and (not isinstance(auth_text, str) or not auth_text)):
        return failure("needs_authorization",
                       "external writes need the exact comment text for a live check",
                       run_id=run_id)

    try:
        _require_lease(store, run_id, token)
    except LeaseError as exc:
        return failure(exc.code, exc.message, run_id=run_id)
    if any(e.get("event") == "platform_blocked" for e in store.read_events(run_id)):
        return failure("platform_blocked", "session stopped on verification or rate-limit signal", run_id=run_id)
    if store.open_actions(run_id):
        return failure("outcome_unknown", "unresolved action effects; inspect before continuing", run_id=run_id)
    if (store.locks / f"cancel-{run_id}").exists():
        return failure("cancelled", "session was cancelled", run_id=run_id)
    if action["type"] == "tap_xy":
        events = store.read_events(run_id)
        current = next((e for e in reversed(events) if e.get("event") in ("observation", "action_started")), {})
        if (current.get("event") != "observation"
                or current.get("observation_id") != action.get("observation_id")
                or current.get("device_id") != request.get("device_id")
                or not 0 <= _now() - current.get("observed_at", 0) <= 8):
            return failure("precondition_mismatch", "coordinates need a current observation from this session", run_id=run_id)
        geometry = current.get("geometry")
        if (not isinstance(geometry, dict) or not {"width", "height", "orientation"} <= set(geometry)
                or not 0 <= action["x"] < geometry["width"] or not 0 <= action["y"] < geometry["height"]):
            return failure("precondition_mismatch", "coordinates need known screen geometry and in-bounds target", run_id=run_id)
        action = {**action, "geometry": geometry}
    if (action["type"] == "input_text" and grant_id is None
            and _run_has_prepared_grant(store, run_id)):
        return failure("needs_authorization",
                       "this run has a prepared grant; input_text must carry its grant_id",
                       run_id=run_id)
    if grant_id is None and action["type"] != "input_text" and _run_has_prepared_grant(store, run_id):
        # The only allowed unbound action is the bound editor-opening tap.
        # Anything else leaves the prepared post/overlay and burns the grant.
        if not _advance_prepared_grants(store, run_id, action):
            return failure("device_busy", "cannot advance the prepared grant; retry",
                           run_id=run_id)
    if grant_id is not None and action["type"] == "input_text":
        text = action.get("text")
        if not isinstance(text, str):
            return failure("invalid_request", "input_text needs a string text", run_id=run_id)
        problem = _check_grant_text(store, grant_id, request, text)
        if problem:
            return problem

    from .pacing import before_action, PacingError
    try:
        pace = before_action(store, request["device_id"], run_id, action,
                             cancelled=lambda: (store.locks / f"cancel-{run_id}").exists(),
                             foreground=lambda: adapter.foreground_package(request["device_id"]),
                             # An expired coordinate is refused before the budget
                             # and cooldown are reserved, so observing again can progress.
                             still_valid=(lambda: _now() - current["observed_at"] <= 8)
                             if action["type"] == "tap_xy" else None)
    except PacingError as exc:
        if exc.code == "precondition_mismatch":
            return failure("precondition_mismatch", "coordinate observation expired during cooldown; observe again", run_id=run_id)
        code = exc.code if exc.code in ("cancelled", "budget_exhausted") else "invalid_request"
        return failure(code, exc.message, run_id=run_id)
    if action.get("effect_class") == "external_mutation":
        problem = _consume_grant(store, grant_id, request, action, auth_text)
        if problem:
            return problem
    step_id = action.get("step_id") or ("s_" + secrets.token_hex(3))
    started_ns = time.monotonic_ns()
    # Preserve navigation provenance, never arbitrary input text or credentials.
    execution = {k: action[k] for k in (
        "type", "x", "y", "app", "component", "package", "key", "direction", "seconds", "method",
        "locator", "fallback", "observation_id", "settle_ms", "poll_ms", "effect_class") if k in action}
    try:
        store.append_event(run_id, {
            "event": "action_started", "step_id": step_id,
            "pacing": pace, "execution": execution,
            "started_at": _now(),
            "action": action.get("type"),
            "locator": action.get("locator"),
            "effect_class": action.get("effect_class", "navigation"),
        })
    except StoreError as exc:
        # The phone is deliberately not touched.
        return failure("outcome_unknown",
                       f"action log persistence failed; no input was issued by this call; inspect before retrying: {exc}",
                       run_id=run_id)

    result = adapter.run_action({**action, "step_id": step_id, "report_effect": True},
                                serial=request.get("device_id"))
    ok = bool(result.get("ok"))
    effect = {k: result[k] for k in ("effect", "tree_blind") if k in result}
    check = adapter.expect_check(step_id, action, result)
    if check:
        store.append_event(run_id, check)
    if ok and action["type"] == "input_text" and action.get("method") == "paste":
        # The script returns the field text after pasting; equality is the proof.
        if result.get("value") != action.get("text"):
            ok = False
            result = {**result, "code": "assertion_failed",
                      "reason": "pasted text did not match the expected field text"}
    if ok:
        if grant_id is not None and action["type"] == "input_text":
            problem = _stamp_grant_text(store, grant_id, request, action.get("text", ""))
            if problem:
                store.append_event(run_id, {"event": "action_finished", "step_id": step_id,
                                            "duration_ms": (time.monotonic_ns() - started_ns) / 1e6,
                                            "metrics": result.get("metrics"),
                                            "locator_index": result.get("locator_index"),
                                            "wake": result.get("wake"), "outcome": "ok", **effect})
                _touch_lease(store, run_id, token)
                return problem
        store.append_event(run_id, {"event": "action_finished", "step_id": step_id,
                                    "duration_ms": (time.monotonic_ns() - started_ns) / 1e6,
                                    "metrics": result.get("metrics"),
                                    "locator_index": result.get("locator_index"),
                                    "wake": result.get("wake"),
                                    "outcome": "ok", **effect})
        _touch_lease(store, run_id, token)
        data = {"step_id": step_id, "action": action.get("type"), **effect}
        if result.get("value") is not None:
            data["value"] = result["value"]
        if result.get("wake") is not None:
            data["wake"] = result["wake"]
        return success("recorded", data, run_id=run_id)

    code = result.get("code") or "action_failed"
    reason = str(result.get("reason") or "")
    blocked = {}
    if code == "platform_blocked":
        # Which stop it was must survive in the trace, not only in this result.
        blocked = {"blocked_by": "limit_notice" if result.get("limit_notice") else "verification_block"}
        store.append_event(run_id, {"event": "platform_blocked", "step_id": step_id, **blocked})
    store.append_event(run_id, {"event": "action_finished", "step_id": step_id,
                                "duration_ms": (time.monotonic_ns() - started_ns) / 1e6,
                                    "metrics": result.get("metrics"),
                                    "locator_index": result.get("locator_index"),
                                "wake": result.get("wake"),
                                "outcome": code, **effect, **blocked,
                                "detail": reason or result.get("labels")})
    if code in ("locator_missing", "ambiguous_target", "assertion_failed",
                "precondition_mismatch", "platform_blocked", "locked"):
        mapped = code
    else:
        mapped = "outcome_unknown"
    _touch_lease(store, run_id, token)
    return failure(mapped, reason or f"{code}",
                   data={"step_id": step_id, "detail": result}, run_id=run_id)


# ------------------------------------------------- narrow local-input recovery
def _resolve_input(store: Store, request: Mapping[str, Any], *,
                   refresh_lease: bool = True) -> Result:
    """Close one provably-finished failed non-ASCII adb input, narrowly.

    Evidence required, all durable or freshly read now:

    * the caller supplies the original full action; its keyed digest must equal
      the digest persisted by ``request_once`` for the same request_id;
    * the request row is terminal (``done``), not ``intent``/``retry``, and its
      stored outcome is ``outcome_unknown`` for this exact step;
    * the terminal event carries normal script-completion metrics. The adapter
      returns no metrics for start failure, timeout, kill or missing JSON, so
      this excludes those branches; it does not prove a remote side effect
      never occurred;
    * exactly one action is open in the run, and it is this input;
    * the durable tree observation immediately after the failure (before any
      later action) has the same keyed digest, node count and geometry as the
      last tree observation before the failed action. This proves the visible
      state did not change, not that no transient effect happened, and says
      nothing about the current screen.

    Success appends ``action_resolved``; historical events are not rewritten.
    The command does not touch the device.
    """
    from .privacy import private_digest
    from .journey import JourneyError, validate_locator

    run_id = request.get("run_id")
    request_id = request.get("request_id")
    action = request.get("action")
    if not isinstance(request_id, str) or not request_id:
        return failure("invalid_request", "resolve-input needs the original request_id", run_id=run_id)
    if not isinstance(action, Mapping) or action.get("type") != "input_text":
        return failure("invalid_request", "resolve-input needs the original input_text action", run_id=run_id)
    if action.get("effect_class") != "local_input":
        return failure("invalid_request", "resolve-input only covers local_input actions", run_id=run_id)
    if action.get("method", "adb") != "adb":
        return failure("invalid_request", "resolve-input only covers the default adb method", run_id=run_id)
    text = action.get("text")
    if not isinstance(text, str) or not text or not any(ord(char) > 127 for char in text):
        return failure("invalid_request",
                       "resolve-input is only for a failed non-ASCII adb input", run_id=run_id)
    try:
        validate_locator(action.get("locator"))
    except JourneyError as exc:
        return failure(exc.code, exc.message, run_id=run_id)

    payload = {"run_id": run_id, "device_id": request.get("device_id"), "action": dict(action)}
    if "authorization" in request:
        payload["authorization"] = request["authorization"]
    digest = private_digest(store, payload)
    row = read_json(request_record_path(store, f"act:{run_id}", request_id))
    if not row:
        return failure("precondition_mismatch", "no durable request record; unknown retained", run_id=run_id)
    if row.get("digest") != digest:
        return failure("request_conflict",
                       "request_id does not match this full original payload; unknown retained",
                       run_id=run_id)
    if row.get("state") != "done":
        return failure("outcome_unknown",
                       "request has no terminal result; unknown retained", run_id=run_id)

    stored = row.get("result") or {}
    stored_error = stored.get("error") or {}
    step_id = (stored.get("data") or {}).get("step_id")
    if (stored.get("run_id") != run_id or stored_error.get("code") != "outcome_unknown"
            or not isinstance(step_id, str) or not step_id):
        return failure("precondition_mismatch",
                       "request record is not the expected outcome_unknown; unknown retained",
                       run_id=run_id)

    events = store.read_events(run_id)
    resolved = [e for e in events if e.get("event") == "action_resolved"
                and e.get("step_id") == step_id and e.get("request_digest") == digest]
    open_steps = [e.get("step_id") for e in store.open_actions(run_id)]
    if step_id not in open_steps:
        if resolved:
            return success("recorded", {"resolved": True, "already_resolved": True,
                                        "step_id": step_id, "request_id": request_id},
                           run_id=run_id)
        return failure("precondition_mismatch",
                       "no matching unresolved action; unknown retained", run_id=run_id)
    if open_steps != [step_id]:
        return failure("precondition_mismatch",
                       "other unresolved actions exist; unknown retained", run_id=run_id)

    started = [e for e in events if e.get("event") == "action_started" and e.get("step_id") == step_id]
    finished = [e for e in events if e.get("event") == "action_finished" and e.get("step_id") == step_id]
    if len(started) != 1 or len(finished) != 1:
        return failure("precondition_mismatch",
                       "the step does not have exactly one start and one finish; unknown retained",
                       run_id=run_id)
    start_event, finish_event = started[0], finished[0]
    execution = start_event.get("execution") or {}
    if (start_event.get("action") != "input_text"
            or start_event.get("effect_class") != "local_input"
            or execution.get("type") != "input_text"
            or execution.get("effect_class") != "local_input"):
        return failure("precondition_mismatch",
                       "the unknown action is not this local input; unknown retained", run_id=run_id)
    if finish_event.get("outcome") not in ("action_failed", "outcome_unknown"):
        return failure("precondition_mismatch",
                       "the action did not finish as a failure; unknown retained", run_id=run_id)
    metrics = finish_event.get("metrics") or {}
    # A normal harness script always prints metrics and locator_index. All
    # adapter error branches (start failure, timeout/kill, non-JSON) omit them.
    if (not isinstance(metrics.get("script_ms"), (int, float))
            or metrics.get("script_ms") <= 0
            or not isinstance(metrics.get("tree_reads"), int)
            or metrics.get("tree_reads") < 1
            or finish_event.get("locator_index") is None):
        return failure("outcome_unknown",
                       "terminal event lacks normal script-completion metrics; cannot exclude "
                       "timeout/kill/start failure; unknown retained", run_id=run_id)

    start_index = events.index(start_event)
    prior = None
    for event in reversed(events[:start_index]):
        if event.get("event") == "observation" and event.get("source") == "tree":
            prior = event
            break
    if not prior or not prior.get("observation_id") or not prior.get("digest"):
        return failure("precondition_mismatch",
                       "no tree observation before the failed input; unknown retained", run_id=run_id)
    prior_index = events.index(prior)
    if any(e.get("event") == "action_started" for e in events[prior_index + 1:start_index]):
        return failure("precondition_mismatch",
                       "another action ran after the comparison observation; unknown retained",
                       run_id=run_id)

    # The durable tree read taken immediately after the failure, before any
    # later action, is the historical evidence. A fresh screen is irrelevant:
    # the question is what this action did, not what is on screen now.
    finish_index = events.index(finish_event)
    after = None
    for event in events[finish_index + 1:]:
        if event.get("event") == "action_started":
            break
        if event.get("event") == "observation" and event.get("source") == "tree":
            after = event
            break
    if not after:
        return failure("precondition_mismatch",
                       "no tree observation after the failure before any later action; "
                       "unknown retained", run_id=run_id)
    if (after.get("digest") != prior.get("digest")
            or after.get("count") != prior.get("count")
            or after.get("geometry") != prior.get("geometry")):
        return failure("precondition_mismatch",
                       "visible tree after the failure differs from before it; unknown retained",
                       run_id=run_id)

    store.append_event(run_id, {
        "event": "action_resolved", "step_id": step_id, "action": "input_text",
        "effect_class": "local_input", "outcome": "unchanged_visible_tree",
        "request_id": request_id, "request_digest": digest,
        "observation_id": after.get("observation_id"), "digest": after.get("digest"),
        "count": after.get("count"), "geometry": after.get("geometry"),
        "before_observation_id": prior.get("observation_id"),
    })
    if refresh_lease:
        _touch_lease(store, run_id, request["lease_token"])
    return success("recorded", {
        "resolved": True, "already_resolved": False, "step_id": step_id,
        "request_id": request_id,
        "before_observation_id": prior.get("observation_id"),
        "after_observation_id": after.get("observation_id"),
        "same_visible_tree": True,
        "scope": "synchronous request is terminal and the durable tree immediately "
                 "after it equals the tree before it; no claim about invisible "
                 "transient effects or the current screen",
    }, run_id=run_id)


def resolve_input(store, request):
    """Offline historical resolution; never reads the device or takes occupancy.

    The recorded lease token must match even if expired, the reservation must
    still belong to this run, and no other run may hold an active lease on the
    device. No new lease, token or reservation is written.
    """
    run_id = request.get("run_id")
    token = request.get("lease_token")
    if not isinstance(run_id, str) or not run_id:
        return failure("invalid_request", "resolve-input needs run_id")
    if not isinstance(token, str) or not token:
        return failure("invalid_request", "resolve-input needs the original lease_token",
                       run_id=run_id)
    try:
        summary = read_json(store.summary_path(run_id))
        if not summary or not summary.get("device_id"):
            return failure("invalid_request", "unknown run or device", run_id=run_id)
        serial = summary["device_id"]
        if request.get("device_id") not in (None, serial):
            return failure("precondition_mismatch", "cannot change the device", run_id=run_id)
        with device_lock(store, serial, owner=run_id) as busy:
            if busy:
                return failure("device_busy",
                               "another active call or reservation holds the device",
                               run_id=run_id)
            reservation = read_json(reservation_path(store, serial))
            if reservation and reservation.get("run_id") != run_id:
                return failure("device_busy",
                               "device reservation belongs to another run; not preempted",
                               run_id=run_id)
            for path in store.locks.glob("r_*.json"):
                if path.stem == run_id:
                    continue
                other = read_json(path)
                if (isinstance(other, dict) and other.get("device_id") == serial
                        and float(other.get("expires_at", 0)) > _now()):
                    return failure("device_busy",
                                   "another run holds an active lease on this device",
                                   run_id=run_id)
            lease = read_json(_lease_path(store, run_id))
            if not lease or lease.get("token") != token:
                return failure("device_busy",
                               "lease token does not match this run's recorded lease",
                               run_id=run_id)
            if lease.get("device_id") not in (None, serial):
                return failure("precondition_mismatch",
                               "run is bound to a different device", run_id=run_id)
            return _resolve_input(store, {**request, "device_id": serial},
                                  refresh_lease=False)
    except ValueError as exc:
        return failure("invalid_request", str(exc), run_id=run_id)
    except (OSError, StoreError) as exc:
        return failure("outcome_unknown", f"session persistence failed: {type(exc).__name__}",
                       run_id=run_id)


# ---------------------------------------------------------------- finish
def _finish(store: Store, request: Mapping[str, Any]) -> Result:
    run_id = request.get("run_id")
    token = request.get("lease_token")
    if not run_id or not token:
        return failure("invalid_request", "session finish needs run_id and lease_token")
    try:
        _require_lease(store, run_id, token)
    except LeaseError as exc:
        return failure(exc.code, exc.message, run_id=run_id)

    summary = read_json(store.summary_path(run_id)) or {}
    events = store.read_events(run_id)
    unknown = [e["step_id"] for e in store.open_actions(run_id)]
    frozen_digest = summary.get("frozen_postconditions_digest")

    cancelled = (store.locks / f"cancel-{run_id}").exists()
    platform_blocked = any(e.get("event") == "platform_blocked" for e in events)
    summary.update({
        "status": "unknown" if unknown else "cancelled" if cancelled else "paused" if platform_blocked else "recorded",
        "finished_at": _now(),
        "events": len(events),
        "unknown_steps": unknown,
    })
    atomic_write_json(store.summary_path(run_id), summary)
    store.append_event(run_id, {"event": "session_finished",
                                "unknown_steps": unknown,
                                "events": len(events)})
    _lease_path(store, run_id).unlink(missing_ok=True)
    reservation = reservation_path(store, request["device_id"])
    if (read_json(reservation) or {}).get("run_id") == run_id and not unknown:
        reservation.unlink(missing_ok=True)

    data = {"run_id": run_id, "events": len(events), "unknown_steps": unknown,
            "frozen_postconditions_digest": frozen_digest}
    if unknown:
        return failure("outcome_unknown",
                       "some actions never reported a result; inspect before retrying",
                       data=data, run_id=run_id)
    if cancelled:
        return failure("cancelled", "session was cancelled", data=data, run_id=run_id)
    if platform_blocked:
        return failure("platform_blocked", "session stopped on a platform signal", data=data, run_id=run_id)
    return success("recorded", data, run_id=run_id)


def start(store: Store, request: Mapping[str, Any]) -> Result:
    try:
        serial = request.get("device_id")
        if not serial:
            devices = adapter.list_devices()
            if len(devices) != 1:
                return failure("ambiguous_device" if devices else "disconnected", "select one reachable device")
            serial = devices[0]["serial"]
        with device_lock(store, serial, nested=True) as busy:
            if busy:
                return failure("device_busy", "device is held by another run")
            return _start(store, {**request, "device_id": serial})
    except ValueError as exc:
        return failure("invalid_request", str(exc))
    except (OSError, StoreError) as exc:
        return failure("outcome_unknown", f"session persistence failed: {type(exc).__name__}")


def _session_call(store, request, handler):
    run_id = request.get("run_id")
    try:
        lease = _require_lease(store, run_id, request.get("lease_token"))
        serial = lease.get("device_id")
        if not serial or (request.get("device_id") and request["device_id"] != serial):
            return failure("precondition_mismatch", "session is bound to a different or unknown device", run_id=run_id)
        with device_lock(store, serial, owner=run_id, nested=True) as busy:
            if busy:
                return failure("device_busy", "device is held by another run", run_id=run_id)
            _require_lease(store, run_id, request.get("lease_token"))
            return handler(store, {**request, "device_id": serial})
    except LeaseError as exc:
        return failure(exc.code, exc.message, run_id=run_id)
    except ValueError as exc:
        return failure("invalid_request", str(exc), run_id=run_id)
    except (OSError, StoreError) as exc:
        return failure("outcome_unknown", f"session persistence failed: {type(exc).__name__}", run_id=run_id)


def observe(store, request):
    return _session_call(store, request, _observe)


def authorize(store, request):
    return _session_call(store, request, _authorize)


def act(store, request):
    # Validate syntax before acquiring a lease, including callers with stale IDs.
    action = request.get("action")
    if not isinstance(action, Mapping):
        return failure("invalid_request", "act needs action")
    from .journey import JourneyError
    try:
        adapter.build_action_script(dict(action))
    except JourneyError as exc:
        return failure(exc.code, exc.message)
    except (ValueError, TypeError, OverflowError) as exc:
        return failure("invalid_request", str(exc))
    authorization = request.get("authorization")
    grant_id = authorization.get("grant_id") if isinstance(authorization, Mapping) else None
    if action.get("effect_class") == "external_mutation" and not isinstance(grant_id, str):
        return failure("needs_authorization", "external writes need a one-time authorization grant")
    def dispatch(bound_store, bound_request):
        payload = {"run_id": bound_request["run_id"], "device_id": bound_request["device_id"], "action": action}
        if authorization is not None:
            payload["authorization"] = authorization
        return request_once(store, f"act:{bound_request['run_id']}", request.get("request_id"), payload,
                            lambda: _act(bound_store, bound_request))
    return _session_call(store, request, dispatch)


def finish(store, request):
    return _session_call(store, request, _finish)


def _ordinary_wake(store: Store, run_id: str, serial: str) -> Result:
    """Record one ordinary wake for a nonsecure keyguard.

    This bypasses the open-action gate only for ``wake``: it changes no app
    content, and the authorized handoff path needs it to make the screen
    readable. A failed wake is recorded as ``locked`` (not a new unknown).
    """
    from .pacing import PacingError, before_action
    step_id = "wake-" + secrets.token_hex(3)
    action = {"type": "wake", "step_id": step_id}
    try:
        pace = before_action(store, serial, run_id, action,
                             cancelled=lambda: (store.locks / f"cancel-{run_id}").exists())
    except PacingError as exc:
        code = exc.code if exc.code in ("cancelled", "budget_exhausted") else "invalid_request"
        return failure(code, exc.message, run_id=run_id)
    started_ns = time.monotonic_ns()
    try:
        store.append_event(run_id, {
            "event": "action_started", "step_id": step_id, "action": "wake",
            "effect_class": "navigation", "execution": {"type": "wake"},
            "pacing": pace, "started_at": _now(),
        })
    except StoreError as exc:
        return failure("outcome_unknown",
                       f"wake log persistence failed; no wake was issued: {exc}",
                       run_id=run_id)
    result = adapter.run_action(action, serial=serial)
    ok = bool(result.get("ok"))
    try:
        store.append_event(run_id, {
            "event": "action_finished", "step_id": step_id, "outcome": "ok" if ok else "locked",
            "duration_ms": (time.monotonic_ns() - started_ns) / 1e6,
            "metrics": result.get("metrics"), "locator_index": result.get("locator_index"),
            "wake": result.get("wake"),
            "detail": None if ok else (result.get("reason") or result.get("code")),
        })
    except StoreError as exc:
        return failure("outcome_unknown",
                       f"wake result persistence failed; inspect before retry: {exc}",
                       run_id=run_id)
    if not ok:
        return failure("locked", result.get("reason") or "ordinary wake did not make the screen readable",
                       data={"wake": result.get("wake"), "wake_status": result.get("code")},
                       run_id=run_id)
    return success("recorded", {"wake": result.get("wake"), "value": result.get("value")},
                   run_id=run_id)


def handoff(store, request):
    """Transfer a paused exploration lease after a fresh, read-only observation."""
    run_id = request.get("run_id")
    try:
        summary = read_json(store.summary_path(run_id))
        if not summary or summary.get("kind") != "exploration":
            return failure("needs_replan", "handoff currently accepts exploration sessions only")
        serial = summary.get("device_id")
        if request.get("device_id") not in (None, serial):
            return failure("precondition_mismatch", "handoff cannot change the bound device")
        with device_lock(store, serial, owner=run_id) as busy:
            if busy:
                return failure("device_busy", "an active call still holds this device")
            if summary.get("finished_at") and not store.open_actions(run_id):
                return failure("needs_replan", "finished sessions need a new exploration", run_id=run_id)
            lease = read_json(_lease_path(store, run_id))
            if lease and lease.get("expires_at", 0) > _now() and request.get("lease_token") != lease.get("token"):
                return failure("device_busy", "active lease needs its current token for handoff")
            status = adapter.status(serial)
            wake = None
            if not status.get("ready"):
                lock = status.get("lock") or {}
                if (lock.get("requires_human") is True or lock.get("kind") == "secure"
                        or lock.get("secure") is True):
                    return failure("locked",
                                   "secure lock screen is showing; a person must unlock",
                                   data={"lock": lock}, run_id=run_id)
                if adapter.ordinary_wake_allowed(status):
                    attempt = _ordinary_wake(store, run_id, serial)
                    if attempt.status != "recorded":
                        return attempt
                    wake = (attempt.data or {}).get("wake")
            observed = adapter.harness_read(adapter.OBSERVE_TREE, serial=serial)
            if not observed.get("ok"):
                return failure("precondition_mismatch", "cannot read current scene for handoff",
                               data={"lock": (status.get("lock") or {}), "wake": wake},
                               run_id=run_id)
            unknown = [e["step_id"] for e in store.open_actions(run_id)]
            token = "l_" + secrets.token_hex(8)
            _write_lease(store, run_id, token)
            atomic_write_json(reservation_path(store, serial), {"run_id": run_id})
            store.append_event(run_id, {"event": "handoff", "unknown_steps": unknown})
            return success("recorded", {"lease_token": token, "device_id": serial,
                "observation": observed, "unknown_steps": unknown, "wake": wake,
                "action_allowed": not unknown and not (store.locks / f"cancel-{run_id}").exists()
                    and not any(e.get("event") == "platform_blocked" for e in store.read_events(run_id)),
                "note": "old token invalidated; unknown effects remain blocked"}, run_id=run_id)
    except (ValueError, StoreError, OSError) as exc:
        return failure("outcome_unknown", f"handoff failed: {type(exc).__name__}", run_id=run_id)
