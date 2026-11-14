"""Reviews and lessons: narrative plus verified trace references.

A review is caller-supplied narrative and claims. The caller is responsible
for redacting that narrative before calling :func:`save_review`; this module
never reads or copies raw events, observations, screenshots or task outputs.

Facts must cite at least one trace reference and every reference is checked by
``trace.verify_reference``. Hypotheses are stored as hypotheses and are never
promoted. Reviews are append-only, numbered files under
``runs/<run_id>/reviews/``. Lessons are immutable files under ``lessons/`` and
point back to the review that produced them and carry a canonical SHA-256 of
that review's content. ``discover_lessons`` re-reads the review, re-verifies
its references and checks the digest, so a lesson whose review, cited events or
review content were deleted, replaced or corrupted is not returned as current
experience. Legacy lessons written before the digest existed are still returned
when their references verify, marked ``source_integrity='legacy_no_digest'``.
Neither reviews nor lessons are executable, and neither grants a Journey
verified status.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Any, Mapping

from .contracts import Result, failure, success
from .execution import file_lock, identifier
from .store import Store, StoreError, atomic_write_json, read_json

SCHEMA_VERSION = 1
QUESTIONS = ("completion", "cost", "repetition", "cause", "scope",
             "next_experiment")
CLAIM_KINDS = ("fact", "hypothesis")
REDACTION_NOTICE = "caller_responsible"

MAX_TASK_KEY = 200
MAX_ANSWER = 8000
MAX_CLAIM_TEXT = 4000
MAX_CLAIMS = 50
MAX_REFS_PER_CLAIM = 20
MAX_REFERENCE_CHARS = 20000
MAX_LESSON_TEXT = 4000
DEFAULT_LESSON_LIMIT = 20
MAX_LESSON_LIMIT = 50


class ReviewError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ReviewError("invalid_request", f"{name} must be a string")
    value = value.strip()
    if not value:
        raise ReviewError("invalid_request", f"{name} must not be empty")
    if len(value) > limit:
        raise ReviewError("invalid_request", f"{name} is longer than {limit} characters")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
        raise ReviewError("invalid_request", f"{name} contains control characters")
    return value


def _run_id(value: Any) -> str:
    try:
        return identifier(value, "run_id")
    except ValueError as exc:
        raise ReviewError("invalid_request", str(exc)) from None


def _task_key(value: Any) -> str:
    return _text(value, "task_key", MAX_TASK_KEY)


def _reference(store: Store, value: Any, where: str) -> dict[str, Any]:
    """Check the reference's own shape; the trace module decides validity."""
    if not isinstance(value, Mapping):
        raise ReviewError("invalid_request", f"{where} must be an object")
    reference = dict(value)
    try:
        identifier(reference.get("run_id"), f"{where}.run_id")
    except ValueError as exc:
        raise ReviewError("invalid_request", str(exc)) from None
    for key in ("seq_start", "seq_end"):
        seq = reference.get(key)
        if type(seq) is not int or seq < 1:
            raise ReviewError("invalid_request", f"{where}.{key} must be a positive integer")
    if reference["seq_start"] > reference["seq_end"]:
        raise ReviewError("invalid_request", f"{where} has seq_start after seq_end")
    try:
        size = len(json.dumps(reference, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError):
        raise ReviewError("invalid_request", f"{where} is not JSON serializable") from None
    if size > MAX_REFERENCE_CHARS:
        raise ReviewError("invalid_request", f"{where} is larger than {MAX_REFERENCE_CHARS} characters")
    return reference


def _verify_reference(store: Store, reference: Mapping[str, Any]) -> bool:
    """Indirection so the trace module is imported only when a review is saved."""
    try:
        from .trace import verify_reference
    except ImportError:
        raise ReviewError("capability_gap", "trace verification is unavailable") from None
    try:
        return bool(verify_reference(store, reference))
    except ReviewError:
        raise
    except Exception as exc:
        raise ReviewError("invalid_request",
                          f"reference verification failed: {type(exc).__name__}") from None


def _claims(store: Store, raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ReviewError("invalid_request", "claims must be a list")
    if len(raw) > MAX_CLAIMS:
        raise ReviewError("invalid_request", f"at most {MAX_CLAIMS} claims are accepted")
    claims: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        where = f"claims[{index}]"
        if not isinstance(item, Mapping):
            raise ReviewError("invalid_request", f"{where} must be an object")
        if set(item) - {"kind", "text", "references"}:
            raise ReviewError("invalid_request", f"{where} has unexpected fields")
        kind = item.get("kind")
        if kind not in CLAIM_KINDS:
            raise ReviewError("invalid_request", f"{where}.kind must be fact or hypothesis")
        text = _text(item.get("text"), f"{where}.text", MAX_CLAIM_TEXT)
        refs = item.get("references")
        if not isinstance(refs, list):
            raise ReviewError("invalid_request", f"{where}.references must be a list")
        if len(refs) > MAX_REFS_PER_CLAIM:
            raise ReviewError("invalid_request",
                              f"{where} has more than {MAX_REFS_PER_CLAIM} references")
        if kind == "fact" and not refs:
            raise ReviewError("invalid_request", f"{where} is a fact and needs a reference")
        checked: list[dict[str, Any]] = []
        for ref_index, ref in enumerate(refs):
            checked_ref = _reference(store, ref, f"{where}.references[{ref_index}]")
            if not _verify_reference(store, checked_ref):
                raise ReviewError("invalid_request",
                                  f"{where}.references[{ref_index}] does not verify")
            checked.append(checked_ref)
        claims.append({"kind": kind, "text": text, "references": checked})
    return claims


def _lesson(raw: Any) -> dict[str, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ReviewError("invalid_request", "lesson must be an object")
    if set(raw) != {"rule", "applies_when", "invalid_when"}:
        raise ReviewError("invalid_request",
                          "lesson needs exactly rule, applies_when and invalid_when")
    return {
        "rule": _text(raw.get("rule"), "lesson.rule", MAX_LESSON_TEXT),
        "applies_when": _text(raw.get("applies_when"), "lesson.applies_when",
                              MAX_LESSON_TEXT),
        "invalid_when": _text(raw.get("invalid_when"), "lesson.invalid_when",
                              MAX_LESSON_TEXT),
    }


def _validated(store: Store, request: Any) -> tuple[str, str, dict[str, str],
                                                   list[dict[str, Any]],
                                                   dict[str, str] | None]:
    if not isinstance(request, Mapping):
        raise ReviewError("invalid_request", "request must be an object")
    extra = set(request) - {"run_id", "task_key", "questions", "claims", "lesson"}
    if extra:
        raise ReviewError("invalid_request", f"unexpected fields: {sorted(extra)}")
    run_id = _run_id(request.get("run_id"))
    if not store.run_dir(run_id).is_dir():
        raise ReviewError("invalid_request", f"unknown run {run_id!r}")
    task_key = _task_key(request.get("task_key"))
    questions = request.get("questions")
    if not isinstance(questions, Mapping):
        raise ReviewError("invalid_request", "questions must be an object")
    if set(questions) != set(QUESTIONS):
        raise ReviewError("invalid_request",
                          f"questions must contain exactly {', '.join(QUESTIONS)}")
    answers = {name: _text(questions.get(name), f"questions.{name}", MAX_ANSWER)
               for name in QUESTIONS}
    claims = _claims(store, request.get("claims"))
    return run_id, task_key, answers, claims, _lesson(request.get("lesson"))


def _review_dir(store: Store, run_id: str) -> Path:
    return store.run_dir(run_id) / "reviews"


def _content_digest(doc: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical JSON content, independent of file formatting."""
    canonical = json.dumps(doc, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _next_version(folder: Path) -> int:
    versions = [int(path.stem) for path in folder.glob("*.json") if path.stem.isdigit()]
    return max(versions, default=0) + 1


def _write_lesson(store: Store, review: Mapping[str, Any],
                  lesson: Mapping[str, str]) -> dict[str, Any]:
    folder = store.root / "lessons"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        folder.chmod(0o700)
    except OSError:  # pragma: no cover - exotic filesystems
        pass
    for _ in range(10):
        lesson_id = "lesson-" + secrets.token_hex(8)
        path = folder / f"{lesson_id}.json"
        if not path.exists():
            break
    else:  # pragma: no cover - astronomically unlikely
        raise ReviewError("outcome_unknown", "could not allocate a unique lesson id")
    doc = {
        "schema_version": SCHEMA_VERSION,
        "kind": "lesson",
        "lesson_id": lesson_id,
        "task_key": review["task_key"],
        "review": {
            "run_id": review["run_id"],
            "version": review["version"],
            "review_id": review["review_id"],
        },
        "review_digest": _content_digest(review),
        "rule": lesson["rule"],
        "applies_when": lesson["applies_when"],
        "invalid_when": lesson["invalid_when"],
        "executable": False,
        "runnable": False,
        "redaction": REDACTION_NOTICE,
        "created_at": time.time(),
    }
    atomic_write_json(path, doc)
    return doc


def save_review(store: Store, request: Mapping[str, Any]) -> Result:
    """Validate and append one immutable review, optionally with one lesson."""
    try:
        run_id, task_key, answers, claims, lesson = _validated(store, request)
        with file_lock(store.locks / f"review-{run_id}.lock") as acquired:
            if not acquired:
                return failure("device_busy", "another review is writing for this run")
            folder = _review_dir(store, run_id)
            folder.mkdir(parents=True, exist_ok=True)
            try:
                folder.chmod(0o700)
            except OSError:  # pragma: no cover - exotic filesystems
                pass
            version = _next_version(folder)
            path = folder / f"{version}.json"
            if path.exists():
                return failure("request_conflict", f"review version {version} already exists")
            review = {
                "schema_version": SCHEMA_VERSION,
                "kind": "review",
                "review_id": f"{run_id}:review:{version}",
                "run_id": run_id,
                "task_key": task_key,
                "version": version,
                "questions": answers,
                "claims": claims,
                "executable": False,
                "runnable": False,
                "redaction": REDACTION_NOTICE,
                "created_at": time.time(),
            }
            atomic_write_json(path, review)
            lesson_doc = _write_lesson(store, review, lesson) if lesson else None
            return success("recorded", {
                "run_id": run_id,
                "review_id": review["review_id"],
                "review_version": version,
                "review_path": f"runs/{run_id}/reviews/{version}.json",
                "review": review,
                "lesson": lesson_doc,
                "executable": False,
            }, run_id=run_id)
    except ReviewError as exc:
        return failure(exc.code, exc.message, run_id=request.get("run_id")
                       if isinstance(request, Mapping) else None)
    except (ValueError, TypeError) as exc:
        return failure("invalid_request", str(exc))
    except StoreError as exc:
        return failure("outcome_unknown", str(exc),
                       run_id=request.get("run_id")
                       if isinstance(request, Mapping) else None)
    except OSError as exc:
        return failure("outcome_unknown", f"review write failed: {type(exc).__name__}",
                       run_id=request.get("run_id")
                       if isinstance(request, Mapping) else None)


def _current_review(store: Store, lesson: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Load the review a lesson points to, or ``None`` if the pointer is stale."""
    pointer = lesson.get("review")
    if not isinstance(pointer, Mapping):
        return None
    run_id = pointer.get("run_id")
    version = pointer.get("version")
    review_id = pointer.get("review_id")
    if not isinstance(run_id, str) or not isinstance(review_id, str):
        return None
    if type(version) is not int or version < 1:
        return None
    try:
        doc = read_json(_review_dir(store, run_id) / f"{version}.json")
    except (StoreError, OSError, ValueError):
        return None
    if not isinstance(doc, Mapping):
        return None
    if (doc.get("kind") != "review" or doc.get("run_id") != run_id
            or doc.get("version") != version or doc.get("review_id") != review_id
            or doc.get("task_key") != lesson.get("task_key")):
        return None
    return doc


def _references_hold(store: Store, review_doc: Mapping[str, Any]) -> bool:
    """True only if every cited range still names the same events."""
    claims = review_doc.get("claims")
    if not isinstance(claims, list):
        return False
    for claim in claims:
        if not isinstance(claim, Mapping) or claim.get("kind") not in CLAIM_KINDS:
            return False
        refs = claim.get("references")
        if not isinstance(refs, list):
            return False
        if claim["kind"] == "fact" and not refs:
            return False
        for ref in refs:
            try:
                if not _verify_reference(store, ref):
                    return False
            except ReviewError:
                return False
    return True


def _source_integrity(lesson: Mapping[str, Any],
                      review_doc: Mapping[str, Any]) -> str | None:
    """``verified``, ``legacy_no_digest``, or ``None`` when the digest mismatches."""
    stored = lesson.get("review_digest")
    if stored is None:
        return "legacy_no_digest"
    if (not isinstance(stored, str) or len(stored) != 64
            or any(char not in "0123456789abcdef" for char in stored)):
        return None
    if not hmac.compare_digest(stored, _content_digest(review_doc)):
        return None
    return "verified"


def _lesson_status(store: Store, lesson: Mapping[str, Any]) -> str | None:
    review_doc = _current_review(store, lesson)
    if review_doc is None or not _references_hold(store, review_doc):
        return None
    return _source_integrity(lesson, review_doc)


def discover_lessons(store: Store, request: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return lessons whose ``task_key`` matches exactly, newest first.

    Each lesson is re-checked against its review and every cited trace range.
    Lessons whose review is missing or whose references no longer verify are
    omitted: stale evidence is not returned as current experience. Lessons
    saved with ``review_digest`` are omitted when the canonical review content
    no longer matches; legacy lessons without a digest are still returned when
    their references verify and carry ``source_integrity='legacy_no_digest'``.

    Results are reference material only: ``executable`` is always false and
    this function never runs a lesson. The caller supplies a bounded ``limit``
    (default 20, maximum 50).
    """
    if not isinstance(request, Mapping):
        return []
    task_key = request.get("task_key")
    if not isinstance(task_key, str):
        return []
    task_key = task_key.strip()
    if not task_key or len(task_key) > MAX_TASK_KEY:
        return []
    limit = request.get("limit", DEFAULT_LESSON_LIMIT)
    if type(limit) is not int or not 1 <= limit <= MAX_LESSON_LIMIT:
        return []
    folder = store.root / "lessons"
    if not folder.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in folder.glob("*.json"):
        try:
            doc = read_json(path)
        except (StoreError, OSError):
            continue
        if not isinstance(doc, Mapping) or doc.get("kind") != "lesson":
            continue
        if doc.get("task_key") != task_key:
            continue
        integrity = _lesson_status(store, doc)
        if integrity is None:
            continue
        row = dict(doc)
        row["source_integrity"] = integrity
        row["executable"] = False
        row["runnable"] = False
        rows.append(row)
    rows.sort(key=lambda row: (
        -(row["created_at"] if isinstance(row.get("created_at"), (int, float)) else 0),
        str(row.get("lesson_id") or ""),
    ))
    return rows[:limit]
