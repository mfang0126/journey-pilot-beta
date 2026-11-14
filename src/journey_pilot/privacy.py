"""Runtime persistence excludes content; current callers still receive live values."""
import hashlib
import hmac
import json
import secrets

from .store import StoreError, atomic_write_json, read_json

# These are runtime content, not caller-authored recipe/lesson metadata.
_CONTENT = {"value", "outputs", "text", "text_contains", "description", "desc",
            "labels", "reason", "detail", "nodes", "post_text", "comment_view_text",
            "summary_text", "title", "author", "screenshot_path", "evidence_path",
            "expected_title", "expected_author", "target", "target_title", "target_author"}


_EVENT_FIELDS = frozenset({
    "event", "kind", "journey_id", "version", "status", "step_id", "before_step",
    "action", "effect_class", "outcome", "duration_ms", "metrics", "locator_index",
    "value_present", "name", "ok", "observation_id", "source", "digest",
    "observed_at", "device_id", "geometry", "count", "started_at", "unknown_steps",
    "events", "observation_count", "pacing", "execution", "locator", "wake",
    "grant_id", "post_digest", "action_digest", "text_digest", "opener_digest",
    "request_id", "request_digest", "before_observation_id", "tree_quality", "ocr_count",
    "effect", "tree_blind", "dispatched", "blocked_by",
})


def event_record(event):
    """Unknown event fields are not implicitly approved for persistence."""
    record = metadata({key: value for key, value in event.items() if key in _EVENT_FIELDS})
    for key, allowed in {
        "execution": {"type", "x", "y", "app", "component", "package", "key", "direction", "seconds",
                      "method", "observation_id", "settle_ms", "poll_ms", "effect_class"},
        "locator": {"resource_id", "class"},
        "geometry": {"width", "height", "orientation"},
        "metrics": {"tree_reads", "tree_ms", "explicit_wait_ms",
                    "wake_attempts", "swipes", "script_ms"},
        "wake": {"attempts", "wakefulness", "keyguard_showing", "secure",
                 "input_restricted", "bouncer", "credential_focus",
                 "credential_visible", "requires_human", "verified_by", "nodes",
                 "focus_known"},
        "pacing": {"serial", "run_id", "action_type", "traffic_class", "network", "counted",
                   "interval", "package", "package_interval", "waited", "started_at",
                   "counters", "limits"},
    }.items():
        if key in record:
            record[key] = {k: v for k, v in record[key].items() if k in allowed} if isinstance(record[key], dict) else None
    for key in ("counters", "limits"):
        if isinstance(record.get("pacing"), dict) and key in record["pacing"]:
            values = record["pacing"][key]
            record["pacing"][key] = {k: v for k, v in values.items()
                if k in ("actions", "network", "scroll") and type(v) is int} if isinstance(values, dict) else {}
    return record


def metadata(value):
    """Drop known runtime content fields; caller-authored identifiers remain metadata."""
    if isinstance(value, dict):
        return {k: metadata(v) for k, v in value.items() if k not in _CONTENT}
    if isinstance(value, list):
        return [metadata(v) for v in value]
    return value


def result_record(result):
    # An allowlist prevents newly added result fields silently becoming stored data.
    allowed = {"journey_id", "version", "run_id", "step_id", "action", "state",
               "environment_key", "promotion", "continuation_of", "agent_intervened",
               "promotion_eligible", "timing", "route_id", "route_sha256"}
    data = {k: metadata(v) for k, v in result.data.items() if k in allowed}
    data["content_retained"] = False
    error = None
    if result.error:
        error = {k: v for k, v in result.error.items() if k in ("code", "retryable", "exit_code", "status")}
        error["message"] = "Stored failure metadata; original details are not retained"
    return {"status": result.status, "run_id": result.run_id, "data": data,
            "error": error, "next_action": result.next_action}


def private_digest(store, value):
    """Local keyed digest avoids exposing low-entropy runtime inputs in hashes."""
    from .execution import file_lock
    path = store.root / "content-key.json"
    with file_lock(store.locks / "content-key.lock") as acquired:
        if not acquired:
            raise OSError("content digest key busy")
        key = read_json(path)
        if key is None:
            key = {"key": secrets.token_hex(32)}
            atomic_write_json(path, key)
    raw_key = key.get("key") if isinstance(key, dict) else None
    if (not isinstance(raw_key, str) or len(raw_key) != 64
            or any(char not in "0123456789abcdef" for char in raw_key)):
        raise StoreError("invalid content digest key; inspect the store without regenerating it")
    body = json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hmac.new(bytes.fromhex(raw_key), body, hashlib.sha256).hexdigest()
