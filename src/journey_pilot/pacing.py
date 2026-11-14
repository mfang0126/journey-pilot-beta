"""Cross-CLI action pacing and per-run budgets for one device.

The defaults come from ``experiments/pacing-study-20261023``: 3 s between
ordinary actions, 5 s before a declared network action, at most 40 actions,
3 network actions and 5 scrolls per run.  They are local experiment limits,
not a platform rate and not a claim about server capacity.

The caller holds the device lock for ``serial`` around :func:`before_action`
and the device action it guards.  This module persists the action start time
and the run counters *before* returning, so a crash or a later CLI cannot
replay the action without consuming the same budget.

``traffic_class`` is supplied by the trusted recipe.  Accepted values are
``navigation`` (default), ``search``, ``refresh``, ``page`` and ``read``;
``search``/``refresh``/``page`` are network actions, ``read`` is an
observation and is neither delayed nor counted; ``read`` is only valid for
``read_field``, because observations do not call :func:`before_action`.
An unknown class or action type is rejected instead of silently falling back
to a cheaper bucket.

Per-package intervals add a second, app-scoped cooldown: consecutive actions
while the same package is in the foreground stay at least that package's
interval apart, on top of the device-wide class interval.  The built-in
default equals ``NORMAL_INTERVAL`` and has no package overrides, so it changes
nothing until ``<store>/package_intervals.json`` sets ``default`` and/or
``packages`` (``{"default": 3, "packages": {"com.example": 8}}``).  The
caller passes ``foreground``, a zero-argument callable returning the
foreground package; it is only called when the configuration can lengthen a
wait, and again after every wait, so a switch during the wait is paced by
the package actually in front.  An unknown foreground uses the largest
configured interval device-wide, because a failed probe must not shorten the
cooldown; its start time also counts toward every package's next cooldown,
since the unknown action may have hit any of them.
"""
from __future__ import annotations

import math
import time
from collections.abc import Mapping
from pathlib import Path

from .contracts import ACTIONS
from .execution import identifier
from .store import StoreError, atomic_write_json, read_json

NORMAL_INTERVAL = 3.0
NETWORK_INTERVAL = 5.0
MAX_ACTIONS = 40
MAX_NETWORK = 3
MAX_SCROLL = 5
CANCEL_POLL = 0.2
DEFAULT_PACKAGE_INTERVAL = NORMAL_INTERVAL

# traffic_class -> (is_network, interval)
_TRAFFIC = {
    "navigation": (False, NORMAL_INTERVAL),
    "search": (True, NETWORK_INTERVAL),
    "refresh": (True, NETWORK_INTERVAL),
    "page": (True, NETWORK_INTERVAL),
    "read": (False, 0.0),
}
_COUNTERS = ("actions", "network", "scroll")


class PacingError(RuntimeError):
    """A paced action must not start; ``code`` is stable for callers."""

    def __init__(self, code: str, message: str, **details) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict:
        payload = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


def state_path(store, serial: str) -> Path:
    """The per-device pacing state shared by every CLI."""
    identifier(serial, "device_id")
    return store.root / "pacing" / f"{serial}.json"


def package_config_path(store) -> Path:
    return store.root / "package_intervals.json"


def _interval_value(value, where: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        raise PacingError("pacing_config_error", f"{where} must be a finite number >= 0")
    return float(value)


def package_intervals(store) -> tuple[float, dict[str, float]]:
    """(default, per-package overrides) from ``<store>/package_intervals.json``."""
    try:
        row = read_json(package_config_path(store))
    except StoreError as exc:
        raise PacingError("pacing_config_error", str(exc)) from exc
    if row is None:
        return DEFAULT_PACKAGE_INTERVAL, {}
    if not isinstance(row, dict) or not set(row) <= {"default", "packages"}:
        raise PacingError("pacing_config_error",
                          "package_intervals.json needs only 'default' and 'packages'")
    default = _interval_value(row.get("default", DEFAULT_PACKAGE_INTERVAL), "default")
    packages = row.get("packages", {})
    if not isinstance(packages, dict):
        raise PacingError("pacing_config_error", "'packages' must be an object")
    overrides = {}
    for name, value in packages.items():
        if not isinstance(name, str) or not name:
            raise PacingError("pacing_config_error", "package names must be non-empty strings")
        overrides[name] = _interval_value(value, f"packages[{name!r}]")
    return default, overrides


def classify(action: Mapping) -> tuple[str, str, bool, float]:
    """Validate ``action`` and return (type, traffic_class, network, interval)."""
    if not isinstance(action, Mapping):
        raise PacingError("invalid_action", "action must be an object")
    kind = action.get("type")
    if not isinstance(kind, str) or kind not in ACTIONS:
        raise PacingError("invalid_action", f"unknown action type {kind!r}", action_type=kind)
    traffic = action.get("traffic_class", "navigation")
    if not isinstance(traffic, str) or traffic not in _TRAFFIC:
        raise PacingError("invalid_traffic_class", f"unknown traffic_class {traffic!r}",
                          traffic_class=traffic)
    if traffic == "read" and kind != "read_field":
        raise PacingError("invalid_traffic_class",
                          "traffic_class 'read' is only valid for read_field",
                          traffic_class=traffic, action_type=kind)
    network, interval = _TRAFFIC[traffic]
    return kind, traffic, network, interval


def _read_state(path: Path) -> dict:
    try:
        row = read_json(path)
    except StoreError as exc:
        raise PacingError("pacing_state_error", str(exc)) from exc
    if row is None:
        return {"version": 1, "last_started_at": None, "runs": {}}
    if not isinstance(row, dict):
        raise PacingError("pacing_state_error", "pacing state is not an object")
    version = row.get("version", 1)
    if type(version) is not int or version != 1:
        raise PacingError("pacing_state_error",
                          f"unsupported pacing state version {version!r}", version=version)
    return row


def _counts(state: dict, run_id: str) -> dict[str, int]:
    runs = state.get("runs") or {}
    if not isinstance(runs, dict):
        raise PacingError("pacing_state_error", "pacing state 'runs' is not an object")
    row = runs.get(run_id) or {}
    if not isinstance(row, dict):
        raise PacingError("pacing_state_error", f"pacing counters for {run_id!r} are not an object")
    counts = {}
    for key in _COUNTERS:
        value = row.get(key, 0)
        if type(value) is not int or value < 0:
            raise PacingError("pacing_state_error",
                              f"pacing counter {key!r} for {run_id!r} is invalid")
        counts[key] = value
    return counts


def _last_started(state: dict) -> float | None:
    value = state.get("last_started_at")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PacingError("pacing_state_error", "pacing state 'last_started_at' is invalid")
    return float(value)


def _owed(store, state, interval, now, foreground):
    """(package, package_interval, probed, seconds owed) before the next action.

    ``probed`` is False when per-package pacing cannot lengthen the wait, so
    the foreground was not read.
    """
    last = _last_started(state)
    owed = 0.0 if last is None else max(0.0, interval - max(0.0, now - last))
    default, overrides = package_intervals(store)
    if foreground is None or (default <= NORMAL_INTERVAL and not overrides):
        return None, 0.0, False, owed  # cannot exceed the device-wide interval
    package = foreground()
    package = package.split("/", 1)[0] if isinstance(package, str) and package else None
    starts = state.get("package_started_at") or {}
    if not isinstance(starts, dict):
        raise PacingError("pacing_state_error", "pacing state 'package_started_at' is not an object")
    if package is None:
        package_interval = max([default, *overrides.values()])
    else:
        package_interval = overrides.get(package, default)
        unknown = _last_started({"last_started_at": state.get("unknown_started_at")})
        last = _last_started({"last_started_at": starts.get(package)})
        last = max((t for t in (last, unknown) if t is not None), default=None)
    if last is not None:
        owed = max(owed, package_interval - max(0.0, now - last))
    return package, package_interval, True, owed


def _wait(seconds: float, clock, sleep, cancelled) -> float:
    """Sleep in <=CANCEL_POLL chunks and return the time actually waited."""
    waited = 0.0
    target = clock() + max(0.0, seconds)
    while True:
        if cancelled():
            raise PacingError("cancelled", "cancelled while waiting for pacing", waited=waited)
        remaining = target - clock()
        if remaining <= 0:
            return waited
        step = min(CANCEL_POLL, remaining)
        sleep(step)
        waited += step


def _metrics(serial, run_id, kind, traffic, network, interval, waited, started_at, counts,
             package=None, package_interval=0.0):
    return {
        "serial": serial,
        "run_id": run_id,
        "action_type": kind,
        "traffic_class": traffic,
        "network": network,
        "counted": traffic != "read",
        "interval": interval,
        "package": package,
        "package_interval": package_interval,
        "waited": round(waited, 6),
        "started_at": started_at,
        "counters": dict(counts),
        "limits": {"actions": MAX_ACTIONS, "network": MAX_NETWORK, "scroll": MAX_SCROLL},
    }


def before_action(store, serial: str, run_id: str, action: Mapping, *,
                  clock=time.time, sleep=time.sleep, cancelled=lambda: False,
                  foreground=None, still_valid=None) -> dict:
    """Reserve budget and wait out the cooldown before a device action.

    Returns the pacing measurements after the state is durable.  Raises
    :class:`PacingError` with a stable ``code`` when the action must not start.
    """
    serial = identifier(serial, "device_id")
    run_id = identifier(run_id, "run_id")
    kind, traffic, network, interval = classify(action)
    if cancelled():
        raise PacingError("cancelled", "cancelled before action")

    path = state_path(store, serial)
    state = _read_state(path)
    counts = _counts(state, run_id)

    if traffic == "read":
        # Observations are not paced and do not consume the action budget.
        return _metrics(serial, run_id, kind, traffic, network, interval, 0.0, None, counts)

    for name, limit, used in (
        ("actions", MAX_ACTIONS, counts["actions"]),
        ("network", MAX_NETWORK, counts["network"] if network else 0),
        ("scroll", MAX_SCROLL, counts["scroll"] if kind == "scroll" else 0),
    ):
        if used >= limit:
            raise PacingError("budget_exhausted", f"{name} budget exhausted for this run",
                              budget=name, used=used, limit=limit, run_id=run_id)

    # Re-read the foreground after each wait: the cooldown that counts is the
    # one for the package in front when the action starts.
    waited = 0.0
    while True:
        package, package_interval, probed, owed = _owed(store, state, interval, clock(),
                                                        foreground)
        if owed <= 0:
            break
        waited += _wait(owed, clock, sleep, cancelled)
    # The last foreground probe can outlast a cancel; re-check before reserving.
    if cancelled():
        raise PacingError("cancelled", "cancelled before action", waited=waited)
    if still_valid is not None and not still_valid():
        # Nothing is reserved, so the elapsed wait still counts for the retry.
        raise PacingError("precondition_mismatch",
                          "the action's precondition expired during cooldown", waited=waited)

    started_at = clock()
    counts = {
        "actions": counts["actions"] + 1,
        "network": counts["network"] + int(network),
        "scroll": counts["scroll"] + int(kind == "scroll"),
    }
    state["version"] = 1
    state["last_started_at"] = started_at
    if package is not None:
        state["package_started_at"] = {**(state.get("package_started_at") or {}),
                                       package: started_at}
    elif probed:
        state["unknown_started_at"] = started_at
    state["runs"] = {**(state.get("runs") or {}), run_id: counts}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:  # pragma: no cover - exotic filesystems
            pass
        atomic_write_json(path, state)
    except StoreError as exc:
        raise PacingError("pacing_state_error",
                          f"could not persist pacing state: {exc}") from exc

    return _metrics(serial, run_id, kind, traffic, network, interval, waited, started_at, counts,
                    package, package_interval)


def remaining_delay(store, serial: str, action: Mapping, *, clock=time.time,
                    foreground=None) -> float:
    """Seconds still owed before ``action``; 0 for observations and expired cooldowns.

    Read-only probe for a caller that has finished its own waiting and needs to
    know whether a previously observed coordinate is still fresh enough.  Pass
    the same ``foreground`` as :func:`before_action` so the forecast includes
    the per-package cooldown.
    """
    serial = identifier(serial, "device_id")
    _kind, traffic, _network, interval = classify(action)
    if traffic == "read":
        return 0.0
    state = _read_state(state_path(store, serial))
    return _owed(store, state, interval, clock(), foreground)[3]
