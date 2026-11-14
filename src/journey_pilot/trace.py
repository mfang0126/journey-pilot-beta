"""Trace references and cost summaries over the append-only event log.

A reference names an inclusive ``seq`` range of one run and carries a SHA-256
digest of the canonical JSON of exactly those events.  Appending later events
therefore leaves an earlier reference valid; mutating, deleting or losing a
selected event invalidates it.

``summarize_run`` reports only counts, timings and references.  It never copies
UI text, labels, failure details or other event payloads, so its output can be
consumed without copying raw UI content (sharing still needs review).  Missing metrics stay ``None`` instead of
being reported as zero.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .execution import identifier
from .store import Store, StoreError

_REFERENCE_KEYS = ("run_id", "seq_start", "seq_end", "digest")
_METRIC_KEYS = ("tree_reads", "tree_ms", "explicit_wait_ms")


def _is_int(value: Any) -> bool:
    return type(value) is int


def _is_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _canonical(events: Iterable[Mapping[str, Any]]) -> bytes:
    return json.dumps(list(events), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _digest(events: Iterable[Mapping[str, Any]]) -> str:
    return hashlib.sha256(_canonical(events)).hexdigest()


def _reference(run_id: str, events: list[dict[str, Any]],
               seq_start: int, seq_end: int) -> dict[str, Any]:
    selected = events[seq_start - 1:seq_end]
    if (len(selected) != seq_end - seq_start + 1
            or selected[0]["seq"] != seq_start
            or selected[-1]["seq"] != seq_end):
        raise ValueError(f"events {seq_start}..{seq_end} are not available for {run_id!r}")
    return {"run_id": run_id, "seq_start": seq_start, "seq_end": seq_end,
            "digest": _digest(selected)}


def build_reference(store: Store, run_id: str, seq_start: int,
                    seq_end: int) -> dict[str, Any]:
    """Reference events ``seq_start``..``seq_end`` (inclusive) of ``run_id``.

    Raises ``ValueError`` for an invalid id or range, or when the events are
    not all present.
    """
    identifier(run_id, "run_id")
    if not (_is_int(seq_start) and _is_int(seq_end)
            and 1 <= seq_start <= seq_end):
        raise ValueError("invalid seq range")
    return _reference(run_id, store.read_events(run_id), seq_start, seq_end)


def verify_reference(store: Store, reference: Mapping[str, Any]) -> bool:
    """True only if ``reference`` still names the same unmodified events."""
    if not isinstance(reference, Mapping):
        return False
    if any(key not in reference for key in _REFERENCE_KEYS):
        return False
    run_id = reference["run_id"]
    seq_start = reference["seq_start"]
    seq_end = reference["seq_end"]
    digest = reference["digest"]
    try:
        identifier(run_id, "run_id")
    except ValueError:
        return False
    if not (_is_int(seq_start) and _is_int(seq_end)
            and 1 <= seq_start <= seq_end):
        return False
    if (not isinstance(digest, str) or len(digest) != 64
            or any(ch not in "0123456789abcdef" for ch in digest)):
        return False
    try:
        events = store.read_events(run_id)
        expected = _reference(run_id, events, seq_start, seq_end)["digest"]
    except (StoreError, ValueError, OSError):
        return False
    return hmac.compare_digest(expected, digest)


def _metric_summary(values: list[Any]) -> dict[str, Any]:
    present = [v for v in values if _is_number(v)]
    total = sum(present) if present else None
    if isinstance(total, float):
        total = round(total, 6)
    return {"present": len(present), "missing": len(values) - len(present),
            "total": total}


def summarize_run(store: Store, run_id: str) -> dict[str, Any]:
    """Safe cost summary: counts, timings and a reference, never event payloads.

    ``counts.unknown`` is the number of actions the store still reports open
    (no conclusive finish, ``action_failed``/``outcome_unknown``, or a
    recovered corrupt log tail).  Each metric reports how many action events
    carried it (``present``), how many did not (``missing``) and the sum over
    present values; ``total`` is ``None`` when nothing was recorded.
    """
    identifier(run_id, "run_id")
    events = store.read_events(run_id)
    counts = {"observation": 0, "action": 0, "check": 0}
    action_metrics: dict[str, list[Any]] = {key: [] for key in _METRIC_KEYS}
    pacing_waits: list[Any] = []
    observation_times: list[Any] = []
    calls: list[Any] = []
    call_times: list[Any] = []

    for event in events:
        kind = event.get("event")
        if kind == "observation":
            counts["observation"] += 1
            observation_times.append(event.get("duration_ms"))
        elif kind == "action_started":
            counts["action"] += 1
            pacing = event.get("pacing")
            waited = pacing.get("waited") if isinstance(pacing, Mapping) else None
            pacing_waits.append(waited)
        elif kind == "cli_calls":
            calls.append(event.get("count"))
            call_times.append(event.get("duration_ms"))
        elif kind == "check":
            counts["check"] += 1
        elif kind == "action_finished":
            metrics = event.get("metrics")
            for key in _METRIC_KEYS:
                action_metrics[key].append(
                    metrics.get(key) if isinstance(metrics, Mapping) else None)

    counts["unknown"] = len(store.open_actions(run_id))
    reference = (_reference(run_id, events, 1, events[-1]["seq"])
                 if events else None)
    return {
        "run_id": run_id,
        "events": {
            "count": len(events),
            "seq_start": events[0]["seq"] if events else None,
            "seq_end": events[-1]["seq"] if events else None,
        },
        "reference": reference,
        "counts": counts,
        "metrics": {key: _metric_summary(action_metrics[key])
                    for key in _METRIC_KEYS},
        "pacing": {"waited": _metric_summary(pacing_waits)},
        "observation_ms": _metric_summary(observation_times),
        "total_harness_calls": _metric_summary(calls)["total"],
        "harness_ms": _metric_summary(call_times),
        "measurement_limits": ["harness totals cover recorded CLI invocations, including readiness and checkpoint probes; direct Python calls, killed invocations and adb calls are not included; missing historical metrics stay null"],
    }


def code_digest(paths: Iterable[str | Path]) -> dict[str, str]:
    """SHA-256 of each file's current content, keyed by the path as given."""
    return {str(path): hashlib.sha256(Path(path).read_bytes()).hexdigest()
            for path in paths}
