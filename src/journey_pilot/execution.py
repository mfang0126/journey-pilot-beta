"""Local execution boundaries shared by sessions, tasks and Journey replay."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .contracts import Result, failure
from .store import StoreError, atomic_write_json, read_json

_local = threading.local()


def identifier(value, name="id"):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,199}", value):
        raise ValueError(f"invalid {name}")
    return value


@contextmanager
def file_lock(path: Path):
    """Nonblocking flock; lock files remain so contenders use the same inode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(fd)


def reservation_path(store, serial):
    identifier(serial, "device_id")
    return store.locks / f"session-device-{serial}.json"


@contextmanager
def device_lock(store, serial, ttl=1800.0, *, owner=None, nested=False):
    """Kernel lock protects active calls; reservation protects idle sessions."""
    identifier(serial, "device_id")
    key = (os.getpid(), str(store.root), serial)
    held = getattr(_local, "held", set())
    reservation = reservation_path(store, serial)

    def busy_reservation():
        row = read_json(reservation)
        return row and row.get("run_id") != owner

    if key in held:
        yield serial if not nested or busy_reservation() else None
        return
    with file_lock(store.locks / f"device-{serial}.lock") as acquired:
        if not acquired:
            yield serial
            return
        # Honor existing pre-migration occupancy until its expiry. New calls use flock.
        legacy = store.locks / f"device-{serial}.json"
        row = read_json(legacy)
        if row and float(row.get("expires_at", 0)) > time.time():
            yield serial
            return
        if busy_reservation():
            yield serial
            return
        if row:
            legacy.unlink(missing_ok=True)
        _local.held = held | {key}
        try:
            yield None
        finally:
            _local.held = held


def request_record_path(store, namespace, request_id):
    key = hashlib.sha256(f"{namespace}:{request_id}".encode()).hexdigest()
    return store.root / "requests" / f"{key}.json"


def request_once(store, namespace, request_id, payload, operation):
    """Persist intent before dispatch; a crash never turns into a blind retry."""
    if not isinstance(request_id, str) or not request_id:
        return failure("invalid_request", "request_id must be a nonempty string")
    path = request_record_path(store, namespace, request_id)
    try:
        from .privacy import private_digest, result_record
        digest = private_digest(store, payload)
        with file_lock(path.with_suffix(".lock")) as acquired:
            if not acquired:
                return failure("device_busy", "request is in progress; inspect before retrying")
            row = read_json(path)
            if row:
                if row.get("digest") != digest:
                    return failure("request_conflict", "request_id was reused with a different payload")
                if row.get("state") == "intent":
                    return failure("outcome_unknown", "prior request has no durable result; inspect before retrying",
                                   run_id=row.get("run_id"))
                if row.get("state") == "done":
                    return Result(**row["result"])
            atomic_write_json(path, {"digest": digest, "state": "intent", "run_id": payload.get("run_id")})
            try:
                result = operation()
            except Exception as exc:
                # Keep intent: the exception may be after the device input.
                return failure("outcome_unknown", f"execution interrupted: {type(exc).__name__}",
                               run_id=payload.get("run_id"))
            retry = result.run_id is None and result.status == "blocked"
            atomic_write_json(path, {"digest": digest, "state": "retry" if retry else "done",
                                     "result": result_record(result)})
            return result
    except (OSError, StoreError) as exc:
        return failure("outcome_unknown", f"request persistence failed: {type(exc).__name__}",
                       run_id=payload.get("run_id"))
