"""Journeys: the reusable artifact, its versions, and discovery.

A journey is data, never code. The caller proposes the steps; this module
refuses anything outside the whitelist, anything without a unique locator, and
any field that could smuggle executable content.

Lifecycle is append-only: every state change is an event, and the current state
is derived from those events per environment.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from .web import WEB_KEYS, has_web, is_web_locator
from .contracts import ACTIONS, CHECKS, STEP_CHECKS, EFFECT_CLASSES, failure, success
from .store import Store, StoreError, atomic_write_json, read_json

ACTION_WHITELIST = frozenset(ACTIONS)
LOCATOR_KEYS = ("resource_id", "text", "text_contains", "description", "class")
FORBIDDEN_KEYS = frozenset({"code", "script", "python", "shell", "eval", "exec", "cmd"})
STATES = frozenset({"candidate", "verified", "suspended", "superseded"})
SHARE_TEXT_MAX = 4000


class JourneyError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _walk_forbidden(node: Any, path: str = "recipe") -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                raise JourneyError("invalid_request", f"{path}.{key} is not allowed in a recipe")
            _walk_forbidden(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _walk_forbidden(value, f"{path}[{i}]")


def validate_locator(locator: Any) -> dict[str, str]:
    if (not isinstance(locator, Mapping) or not locator
            or set(locator) - set(LOCATOR_KEYS) - set(WEB_KEYS)
            or any(not isinstance(v, str) or not v.strip() for v in locator.values())):
        raise JourneyError("invalid_request", f"locator needs nonempty strings from {LOCATOR_KEYS} or {WEB_KEYS}")
    if is_web_locator(locator) and set(locator) - set(WEB_KEYS):
        raise JourneyError("invalid_request", "a web locator cannot mix native and web keys")
    return dict(locator)


def validate_check(spec: Any, *, step: bool = False) -> None:
    """Reject unsupported or vacuous checks before any device action."""
    if not isinstance(spec, Mapping) or not spec:
        raise JourneyError("invalid_request", "a check must be a nonempty object")
    operators = set(spec) - {"equals"}
    allowed = STEP_CHECKS if step else CHECKS
    if len(operators) != 1 or not operators <= set(allowed):
        raise JourneyError("unsupported_input", f"supported checks: {allowed}")
    op = next(iter(operators))
    if set(spec) - {op} - ({"equals"} if op == "element_count" else set()):
        raise JourneyError("invalid_request", "unexpected check fields")
    value = spec[op]
    if op in ("all", "any"):
        if not isinstance(value, list) or not value:
            raise JourneyError("invalid_request", f"{op} needs a nonempty list")
        for sub in value:
            validate_check(sub)
    elif op == "not":
        validate_check(value)
    elif op in ("element_exists", "element_count"):
        validate_locator(value)
        if op == "element_count":
            count = spec.get("equals", 1)
            if type(count) is not int or count < 0:
                raise JourneyError("invalid_request", "element_count.equals needs a nonnegative integer")
    elif isinstance(value, Mapping):
        if (set(value) != {"css", "text"}
                or any(not isinstance(v, str) or not v.strip() for v in value.values())):
            raise JourneyError("invalid_request", "web text_equals needs nonempty css and text")
    elif not isinstance(value, str) or not value.strip():
        raise JourneyError("invalid_request", "text_equals needs a nonempty string")


def validate_steps(steps: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(steps, list):
        raise JourneyError("invalid_request", "steps must be a list")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, raw in enumerate(steps):
        if not isinstance(raw, Mapping):
            raise JourneyError("invalid_request", f"step {i} must be an object")
        step = dict(raw)
        step_id = step.get("step_id")
        if not step_id or not isinstance(step_id, str):
            raise JourneyError("invalid_request", f"step {i} has no step_id")
        if step_id in seen:
            raise JourneyError("invalid_request", f"duplicate step_id {step_id!r}")
        seen.add(step_id)

        action = step.get("action") or step.get("type")
        if not isinstance(action, str) or action not in ACTION_WHITELIST:
            raise JourneyError("unsupported_input",
                               f"step {step_id!r}: action {action!r} is not whitelisted")
        step["action"] = action

        locator = step.get("locator")
        if locator is not None:
            step["locator"] = validate_locator(locator)
        if is_web_locator(locator) and (action != "tap" or "fallback" in step):
            raise JourneyError("invalid_request", f"step {step_id!r}: web locators support tap only, without fallback")
        if "fallback" in step:
            fallback = step["fallback"]
            if (action not in ("tap", "read_field") or not locator
                    or not isinstance(fallback, Mapping)
                    or set(fallback) != {"alternatives"}
                    or not isinstance(fallback["alternatives"], list)
                    or not 1 <= len(fallback["alternatives"]) <= 8):
                raise JourneyError("invalid_request", "fallback needs 1..8 locator alternatives for tap/read_field")
            step["fallback"] = {"alternatives": [validate_locator(loc)
                                                  for loc in fallback["alternatives"]]}
        if ("expect" in step and "postcondition" in step
                and step["expect"] != step["postcondition"]):
            raise JourneyError("invalid_request", "expect and postcondition disagree")
        for key in ("expect", "postcondition"):
            if key in step and step[key] is not None:
                validate_check(step[key], step=True)
        if step.get("expect_gone") is not None:
            validate_check(step["expect_gone"], step=True)
        app = step.get("expect_app")
        if app is not None and (not isinstance(app, str)
                                or not re.fullmatch(r"[A-Za-z]\w*(\.[A-Za-z]\w*)+", app)):
            raise JourneyError("invalid_request", f"step {step_id!r}: expect_app needs an Android package name")
        if ((step.get("expect_gone") is not None or app is not None)
                and (action == "wake" or is_web_locator(locator) or has_web(step.get("expect"))
                     or has_web(step.get("postcondition")) or has_web(step.get("expect_gone")))):
            raise JourneyError("invalid_request", f"step {step_id!r}: expect_gone/expect_app are native-only and not on wake")
        if action in ("tap", "input_text", "read_field") and not step.get("locator"):
            raise JourneyError("invalid_request", f"step {step_id!r}: {action} needs a locator")
        # input_text replaces the field; a missing text must not default to clearing it.
        if action == "input_text" and "text" not in step:
            raise JourneyError("invalid_request", f"step {step_id!r}: input_text needs text ('' clears the field)")
        if action == "share_text":
            package, text = step.get("package"), step.get("text")
            if not isinstance(package, str) or not re.fullmatch(r"[A-Za-z]\w*(\.[A-Za-z]\w*)+", package):
                raise JourneyError("invalid_request", f"step {step_id!r}: share_text needs an Android package name")
            # ponytail: fixed cap keeps the am command line short; raise if a real share needs more.
            if not isinstance(text, str) or not text.strip() or len(text) > SHARE_TEXT_MAX or "\0" in text:
                raise JourneyError("invalid_request",
                                   f"step {step_id!r}: share_text needs nonempty text up to {SHARE_TEXT_MAX} chars")
            # Never defaulted: callers and the runner gate on the declared class.
            if step.get("effect_class") != "external_mutation":
                raise JourneyError("invalid_request",
                                   f"step {step_id!r}: share_text is an external write; declare effect_class=external_mutation")

        effect = step.get("effect_class", "navigation")
        if effect not in EFFECT_CLASSES:
            raise JourneyError("invalid_request", f"step {step_id!r}: bad effect_class")
        step["effect_class"] = effect
        if "output" in step and (action != "read_field" or not isinstance(step["output"], str)
                                 or not step["output"].isidentifier()):
            raise JourneyError("invalid_request", "output needs a field name on read_field")
        step.setdefault("settle_ms", 800)
        out.append(step)
    if not out:
        raise JourneyError("invalid_request", "a journey needs at least one step")
    return out


def _journey_dir(store: Store, journey_id: str) -> Path:
    from .execution import identifier
    identifier(journey_id, "journey_id")
    return store.journeys / journey_id


def _version_path(store: Store, journey_id: str, version: int) -> Path:
    return _journey_dir(store, journey_id) / "versions" / f"{version}.json"


def _append_lifecycle(store: Store, journey_id: str, event: Mapping[str, Any]) -> None:
    path = _journey_dir(store, journey_id) / "lifecycle.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"at": time.time(), **event}, ensure_ascii=False, sort_keys=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def lifecycle_of(store: Store, journey_id: str) -> dict[int, str]:
    """Derive the current state of every version from the append-only log."""
    path = _journey_dir(store, journey_id) / "lifecycle.jsonl"
    states: dict[int, str] = {}
    if not path.exists():
        return states
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        version = event.get("version")
        if isinstance(version, int) and event.get("state"):
            states[version] = event["state"]
    return states


def save_version(store: Store, doc: Mapping[str, Any]) -> Path:
    path = _version_path(store, doc["journey_id"], int(doc["version"]))
    if path.exists():
        raise StoreError(f"version {doc['version']} already exists")
    atomic_write_json(path, doc)
    return path


def recipe_digest(doc: Mapping[str, Any]) -> str:
    data = {k: v for k, v in doc.items() if k != "recipe_digest"}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_version(store: Store, journey_id: str, version: int) -> dict[str, Any] | None:
    doc = read_json(_version_path(store, journey_id, version))
    if doc and doc.get("recipe_digest") and doc["recipe_digest"] != recipe_digest(doc):
        raise JourneyError("version_mismatch", "recipe content differs from its saved digest")
    return doc


def latest_version(store: Store, journey_id: str) -> int:
    folder = _journey_dir(store, journey_id) / "versions"
    if not folder.is_dir():
        return 0
    versions = [int(p.stem) for p in folder.glob("*.json") if p.stem.isdigit()]
    return max(versions, default=0)


# ---------------------------------------------------------------- propose
def propose(store: Store, request: Mapping[str, Any]) -> Any:
    from .execution import file_lock
    try:
        with file_lock(store.locks / "journey-versions.lock") as acquired:
            if not acquired:
                return failure("device_busy", "another proposal is writing a version")
            return _propose(store, request)
    except JourneyError as exc:
        return failure(exc.code, exc.message)
    except (ValueError, TypeError) as exc:
        return failure("invalid_request", str(exc))
    except StoreError as exc:
        return failure("outcome_unknown", str(exc))


def _propose(store: Store, request: Mapping[str, Any]) -> Any:
    _walk_forbidden(request)
    if request.get("observation_strategy", "per_step") != "per_step":
        return failure("unsupported_input", "batch/adaptive strategies are not supported")
    task_key = (request.get("task_key") or "").strip()
    intent = (request.get("intent") or "").strip()
    if not task_key or not intent:
        return failure("invalid_request", "propose needs task_key and intent")
    try:
        from .parameters import validate_recipe_parameters
        from .fixed import validate_fixed_steps
        checked = validate_recipe_parameters(request)
        normalized = validate_steps(checked.get("steps") or [])
        validate_fixed_steps(normalized)
        steps = [{**original, "action": valid["action"], "effect_class": valid["effect_class"],
                  "settle_ms": valid["settle_ms"]}
                 for original, valid in zip(request["steps"], normalized)]
        if checked.get("postconditions") != {} and "postconditions" in checked:
            validate_check(checked["postconditions"])
        if checked.get("entry_state"):
            validate_check(checked["entry_state"])
    except JourneyError as exc:
        return failure(exc.code, exc.message)

    source_runs = list(request.get("source_run_ids") or [])
    for run_id in source_runs:
        if not store.run_dir(run_id).is_dir():
            return failure("invalid_request", f"unknown source run {run_id!r}")

    journey_id = request.get("journey_id")
    parent = None
    if journey_id:
        parent = int(request.get("base_version") or 0)
        if parent != latest_version(store, journey_id):
            return failure("version_mismatch", "base_version must match the latest version")
        if parent == 0:
            return failure("invalid_request", f"journey {journey_id!r} has no base version")
        version = latest_version(store, journey_id) + 1
    else:
        journey_id = "j_" + secrets.token_hex(4)
        version = 1

    environment = dict(request.get("environment_key") or {})
    doc = {
        "schema_version": 1,
        "journey_id": journey_id,
        "version": version,
        "parent_version": parent,
        "task_key": task_key,
        "intent": intent,
        "aliases": list(request.get("aliases") or []),
        "environment_key": environment,
        "parameters": request.get("parameters") or {"type": "object", "properties": {},
                                                    "required": []},
        "entry_state": request.get("entry_state") or {},
        "steps": steps,
        "postconditions": request.get("postconditions") or {},
        "source_runs": source_runs,
        "created_at": time.time(),
    }
    doc["recipe_digest"] = recipe_digest(doc)
    try:
        save_version(store, doc)
    except StoreError as exc:
        return failure("version_mismatch", str(exc))
    _append_lifecycle(store, journey_id, {"version": version, "state": "candidate",
                                          "reason": "proposed", "source_runs": source_runs})
    return success("ok", {"journey_id": journey_id, "version": version,
                          "parent_version": parent, "state": "candidate",
                          "steps": len(steps)}, run_id=None)


# ---------------------------------------------------------------- discover
def _normalise(text: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKC", text or "").strip().lower()


def discover(store: Store, request: Mapping[str, Any]) -> Any:
    query = _normalise(str(request.get("query") or ""))
    task_key = _normalise(str(request.get("task_key") or ""))
    environment = request.get("environment_key") or {}
    if not isinstance(environment, Mapping):
        environment = {}

    def aliases_of(doc: Mapping[str, Any]) -> list[str]:
        aliases = doc.get("aliases")
        return [a for a in aliases if isinstance(a, str)] \
            if isinstance(aliases, list) else []

    def match_score(doc: Mapping[str, Any]) -> int:
        doc_task_key = doc.get("task_key")
        doc_task_key = doc_task_key if isinstance(doc_task_key, str) else ""
        if task_key and _normalise(doc_task_key) == task_key:
            return 100
        if task_key and task_key in [_normalise(a) for a in aliases_of(doc)]:
            return 80
        if query:
            haystack = _normalise(" ".join([
                str(doc.get("intent") or ""), doc_task_key,
                " ".join(aliases_of(doc)),
            ]))
            if query in haystack:
                return 60
        return 0

    def environment_match(doc: Mapping[str, Any]) -> str:
        raw = doc.get("environment_key")
        declared = {k: v for k, v in (raw if isinstance(raw, Mapping) else {}).items()
                    if v}
        if not declared:
            return "undeclared"
        if not environment:
            return "unknown"
        if all(environment.get(k) == v for k, v in declared.items()):
            return "exact"
        if any(environment.get(k) == v for k, v in declared.items()):
            return "partial"
        return "mismatch"

    def compatible_with(doc: Mapping[str, Any]) -> bool:
        # A request key the recipe does not declare is a contradiction. A
        # declared key the request omits is unknown, not compatible: discover
        # must not claim an environment binding it cannot see.
        raw = doc.get("environment_key")
        declared = raw if isinstance(raw, Mapping) else {}
        if not all(not value or key not in declared or declared.get(key) == value
                   for key, value in environment.items()):
            return False
        return environment_match(doc) in ("exact", "undeclared")

    hits: list[dict[str, Any]] = []
    proposed_runs: set[str] = set()
    excluded = 0
    root = store.journeys
    if root.is_dir():
        for folder in sorted(root.iterdir()):
            if not folder.is_dir():
                continue
            journey_id = folder.name
            versions = sorted(
                int(p.stem) for p in (folder / "versions").glob("*.json")
                if p.stem.isdigit()
            )
            if not versions:
                continue
            states = lifecycle_of(store, journey_id)
            matched: list[tuple[int, dict[str, Any], int, bool, str, str]] = []
            for version in reversed(versions):
                try:
                    doc = load_version(store, journey_id, version)
                except (JourneyError, StoreError):
                    excluded += 1
                    continue
                if not doc:
                    excluded += 1
                    continue
                for run_id in doc.get("source_runs") or []:
                    if isinstance(run_id, str):
                        proposed_runs.add(run_id)
                score = match_score(doc)
                if not score:
                    continue
                matched.append((version, doc, score, compatible_with(doc),
                                states.get(version, "candidate"),
                                environment_match(doc)))
            if not matched:
                continue
            # The newest matching version is the default. An older verified
            # version is still surfaced so a fresh candidate cannot hide it.
            chosen = [matched[0]]
            if not (matched[0][4] == "verified" and matched[0][3]):
                older = next((m for m in matched[1:]
                              if m[4] == "verified" and m[3]), None)
                if older is not None:
                    chosen.append(older)
            from .runner import _state_in
            for version, doc, score, compatible, state, env_match in chosen:
                hits.append({
                    "kind": "journey",
                    "journey_id": journey_id,
                    "version": version,
                    "task_key": doc.get("task_key"),
                    "intent": doc.get("intent"),
                    "state": state,
                    "environment_match": env_match,
                    "compatible": compatible,
                    "executable": state == "verified" and compatible and bool(environment.get("device"))
                                  and _state_in(
                                      store, journey_id, version, {**environment, "guard_version": 2}) == "verified",
                    "steps": len(doc.get("steps")) if isinstance(doc.get("steps"), list) else 0,
                    "score": score,
                })

    hits.sort(key=lambda h: (-h["score"], h["journey_id"], h["version"]))
    executable = [h for h in hits if h["executable"]]
    reference = [h for h in hits if not h["executable"]]
    selection = "none" if not hits else (
        "unique" if len({h["journey_id"] for h in hits}) == 1 else "ambiguous")

    def content_summary(data: Any) -> dict[str, Any] | None:
        # Shape and size only: run text may be private and never belongs in a
        # discovery response.
        if not isinstance(data, Mapping):
            return None
        out: dict[str, Any] = {}
        if isinstance(data.get("coverage"), str):
            out["coverage"] = data["coverage"]
        for key in ("post_text", "comment_view_text", "summary_text"):
            value = data.get(key)
            if isinstance(value, list):
                out[key + "_lines"] = len(value)
                out[key + "_chars"] = sum(len(str(item)) for item in value)
            elif isinstance(value, str):
                out[key + "_chars"] = len(value)
        for key in ("title", "author"):
            if key in data:
                out["has_" + key] = bool(data.get(key))
        return out or None

    def run_reference(run_dir: Path) -> dict[str, Any] | None:
        run_id = run_dir.name
        try:
            summary = read_json(store.summary_path(run_id))
            result = read_json(run_dir / "task-result.json")
        except (StoreError, OSError):
            return None
        if summary is None and result is None:
            return None
        summary = summary if isinstance(summary, Mapping) else {}
        result = result if isinstance(result, Mapping) else None
        result_data = result.get("data") if result is not None else None
        result_data = result_data if isinstance(result_data, Mapping) else {}
        error = result.get("error") if result is not None else None
        error_code = error.get("code") if isinstance(error, Mapping) else None

        journey_id = summary.get("journey_id")
        journey_doc: Mapping[str, Any] = {}
        if isinstance(journey_id, str):
            try:
                journey_doc = load_version(
                    store, journey_id, int(summary.get("version") or 0)) or {}
            except (JourneyError, TypeError, ValueError):
                journey_doc = {}
        run_task_key = (result_data.get("route_id") or summary.get("task_key")
                        or journey_doc.get("task_key"))
        run_intent = summary.get("intent") or journey_doc.get("intent")
        aliases = aliases_of(journey_doc)

        score = 0
        if task_key and _normalise(str(run_task_key or "")) == task_key:
            score = 100
        elif task_key and task_key in [_normalise(str(a)) for a in aliases]:
            score = 80
        if not score and query:
            haystack = _normalise(" ".join(str(part) for part in (
                run_task_key, run_intent, journey_id,
                summary.get("status"),
                result.get("status") if result is not None else None,
                error_code,
            ) if part))
            if query in haystack:
                score = 60
        if not score:
            return None

        status = ((result.get("status") if result is not None else None)
                  or summary.get("status"))
        failure: dict[str, Any] | None = None
        if isinstance(error, Mapping):
            failure = {"code": error.get("code")}
        elif isinstance(summary.get("failure"), Mapping):
            failure = {"code": summary["failure"].get("code")}
            if summary["failure"].get("step_id"):
                failure["step_id"] = summary["failure"]["step_id"]

        if result is not None:
            source, purpose = "thin_task", "thin_task_result"
        else:
            source = str(summary.get("kind") or "run")
            purpose = {"exploration": "exploration_log",
                       "validate": "journey_run",
                       "replay": "journey_run"}.get(source, "run_summary")

        summary_env = summary.get("environment_key")
        summary_env = summary_env if isinstance(summary_env, Mapping) else {}
        journey_env = journey_doc.get("environment_key")
        journey_env = journey_env if isinstance(journey_env, Mapping) else {}
        app = (summary.get("app") or summary_env.get("app_package")
               or result_data.get("app") or journey_env.get("app_package"))
        evidence_dir = run_dir / "evidence"
        evidence_files = (sum(1 for _ in evidence_dir.iterdir())
                          if evidence_dir.is_dir() else 0)
        unknown = summary.get("unknown_steps")
        return {
            "kind": "run",
            "run_id": run_id,
            "source": source,
            "task_key": run_task_key,
            "intent": str(run_intent)[:200] if run_intent else None,
            "app": app,
            "status": status,
            "failure": failure,
            "evidence": "available" if evidence_files else "cleared",
            "reference": purpose,
            "unknown_steps": len(unknown) if isinstance(unknown, list) else 0,
            "started_at": summary.get("started_at"),
            "finished_at": summary.get("finished_at"),
            "content": content_summary(result_data),
            "executable": False,
            "runnable": False,
            "score": score,
        }

    runs: list[dict[str, Any]] = []
    if store.runs.is_dir() and (task_key or query):
        for run_dir in sorted(store.runs.iterdir()):
            if not run_dir.is_dir() or run_dir.name in proposed_runs:
                continue
            ref = run_reference(run_dir)
            if ref is not None:
                runs.append(ref)
        runs.sort(key=lambda r: (-r["score"],
                                 -(r["started_at"] if isinstance(r["started_at"],
                                                                 (int, float)) else 0),
                                 r["run_id"]))
    runs_total = len(runs)
    # ponytail: fixed cap; add paging only if run history makes it necessary.
    runs = runs[:20]

    return success("ok", {
        "executable": executable,
        "reference": reference,
        "excluded": excluded,
        "selection": selection,
        "query": {"task_key": task_key or None, "query": query or None},
        "runs": runs,
        "runs_total": runs_total,
    })


# ---------------------------------------------------------------- inspect
def inspect(store: Store, request: Mapping[str, Any]) -> Any:
    run_id = request.get("run_id")
    journey_id = request.get("journey_id")
    if bool(run_id) == bool(journey_id):
        return failure("invalid_request", "inspect takes exactly one of run_id or journey_id")

    if run_id:
        try:
            summary = read_json(store.summary_path(run_id))
            task_result = read_json(store.run_dir(run_id) / "task-result.json")
        except (StoreError, OSError) as exc:
            return failure("invalid_request", f"run {run_id!r} cannot be read: {exc}")
        if summary is None and task_result is None:
            return failure("invalid_request", f"unknown run {run_id!r}")
        events = store.read_events(run_id)
        evidence_dir = store.run_dir(run_id) / "evidence"
        evidence_files = (sum(1 for _ in evidence_dir.iterdir())
                          if evidence_dir.is_dir() else 0)
        failure_info = None
        if isinstance(task_result, Mapping) and isinstance(task_result.get("error"), Mapping):
            failure_info = dict(task_result["error"])
        elif isinstance(summary, Mapping) and isinstance(summary.get("failure"), Mapping):
            failure_info = dict(summary["failure"])
        return success("ok", {
            "kind": "run",
            "summary": summary,
            "task_result": task_result,
            "failure": failure_info,
            "evidence": {
                "summary": "present" if summary is not None else "missing",
                "task_result": "present" if task_result is not None else "missing",
                "files": evidence_files,
                "state": "available" if evidence_files else "cleared",
            },
            "events": events[-200:],
            "event_count": len(events),
            "unknown_steps": [e["step_id"] for e in store.open_actions(run_id)],
        })

    version = request.get("version")
    assert journey_id is not None
    try:
        version = int(version) if version else latest_version(store, journey_id)
    except (TypeError, ValueError):
        return failure("invalid_request", "version must be an integer")
    try:
        doc = load_version(store, journey_id, version)
    except (JourneyError, StoreError) as exc:
        code = getattr(exc, "code", "invalid_request")
        return failure(code, str(exc))
    if doc is None:
        return failure("invalid_request", f"unknown journey {journey_id!r} v{version}")
    states = lifecycle_of(store, journey_id)
    folder = store.journeys / journey_id / "versions"
    available = sorted(int(p.stem) for p in folder.glob("*.json") if p.stem.isdigit())
    return success("ok", {
        "kind": "journey",
        "journey": doc,
        "state": states.get(version, "candidate"),
        "versions": latest_version(store, journey_id),
        # A fresh candidate must not hide an older verified version.
        "verified_versions": [v for v in available if states.get(v) == "verified"],
    })
