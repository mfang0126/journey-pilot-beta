"""The shared store: atomic JSON, append-only JSONL, no partial writes.

Rules this module enforces (docs/experience-rules.md §1):

* a JSON document is written to a temporary file in the same directory, fsynced,
  then renamed into place — a reader never sees half a file;
* an event line is appended, flushed and fsynced before the call returns, so an
  action is never started before its ``action_started`` event is durable;
* a truncated tail (crash mid-write) is ignored on read instead of corrupting
  the run;
* the store is private to the current user.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from .paths import store_root


class StoreError(RuntimeError):
    """A durable write could not be completed; callers must not act on it."""


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write ``payload`` so that ``path`` is either the old file or the new one."""
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".",
                                         suffix=".tmp", delete=False) as fh:
            tmp = Path(fh.name)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except OSError as exc:
        raise StoreError(f"could not write {path}: {exc}") from exc
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise StoreError(f"corrupt JSON at {path}: {exc}") from exc


class Store:
    """One shared directory, usable from any agent and any cwd."""

    def __init__(self, root: Path | None = None, create: bool = True) -> None:
        self.root = (root or store_root()).expanduser().resolve()
        self.runs = self.root / "runs"
        self.journeys = self.root / "journeys"
        self.locks = self.root / "locks"
        self.catalog = self.root / "catalog.json"
        if create:
            self.ensure_layout()

    # -- layout ---------------------------------------------------------
    def ensure_layout(self) -> None:
        for path in (self.root, self.runs, self.journeys, self.locks):
            path.mkdir(parents=True, exist_ok=True)
            try:
                path.chmod(0o700)
            except OSError:  # pragma: no cover - exotic filesystems
                pass

    def run_dir(self, run_id: str) -> Path:
        from .execution import identifier
        identifier(run_id, "run_id")
        return self.runs / run_id

    def events_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "events.jsonl"

    def summary_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "summary.json"

    # -- runs -----------------------------------------------------------
    def create_run(self, run_id: str, summary: Mapping[str, Any]) -> Path:
        directory = self.run_dir(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        (directory / "evidence").mkdir(exist_ok=True)
        import hashlib
        source = Path(__file__).parent
        code_digest = hashlib.sha256(b''.join(p.name.encode() + b'\0' + p.read_bytes()
            for p in sorted(source.glob('*.py')))).hexdigest()
        atomic_write_json(self.summary_path(run_id), {**summary, "code_digest": code_digest})
        # Touch the event log so a reader sees a run with zero events.
        events = self.events_path(run_id)
        if not events.exists():
            events.touch(mode=0o600)
        return directory

    # -- events ---------------------------------------------------------
    @staticmethod
    def _scan_events(fh):
        # ponytail: scan the complete bounded run; add an index only if profiling needs it.
        fh.seek(0)
        events, offset = [], 0
        for raw in fh:
            if not raw.endswith(b"\n"):
                break
            try:
                event = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                break
            if (not isinstance(event, dict) or type(event.get("seq")) is not int
                    or event["seq"] != len(events) + 1):
                break
            events.append(event)
            offset = fh.tell()
        return events, offset

    def last_seq(self, run_id: str) -> int:
        events = self.read_events(run_id)
        return events[-1]["seq"] if events else 0

    def append_event(self, run_id: str, event: Mapping[str, Any]) -> int:
        """Serialize sequence allocation, recovery and durable append across processes."""
        path = self.events_path(run_id)
        if not path.parent.is_dir():
            raise StoreError(f"run {run_id!r} does not exist")
        try:
            with open(path, "r+b") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                events, offset = self._scan_events(fh)
                seq = events[-1]["seq"] if events else 0
                fh.seek(offset)
                tail = fh.read()
                if tail:
                    # Archive before truncating; the file itself is a persistent unknown gate,
                    # including a crash between archiving and writing log_recovered.
                    with tempfile.NamedTemporaryFile(dir=path.parent, prefix="events-",
                                                     suffix=".corrupt", delete=False) as saved:
                        saved.write(tail)
                        saved.flush()
                        os.fsync(saved.fileno())
                        evidence = Path(saved.name).name
                    _fsync_dir(path.parent)
                    fh.seek(offset)
                    fh.truncate()
                    seq += 1
                    recovery = {"seq": seq, "event": "log_recovered", "evidence": evidence}
                    fh.write((json.dumps(recovery) + "\n").encode())
                    fh.flush()
                    os.fsync(fh.fileno())
                if event.get("event") == "action_started" and list(path.parent.glob("events-*.corrupt")):
                    raise StoreError("recovered log has unknown effects; inspect before further actions")
                seq += 1
                from .privacy import event_record
                from datetime import datetime, timezone
                record = {**event_record(dict(event)), "seq": seq, "schema_version": 1,
                          "run_id": run_id, "at": datetime.now(timezone.utc).isoformat()}
                fh.seek(0, os.SEEK_END)
                fh.write((json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode())
                fh.flush()
                os.fsync(fh.fileno())
                return seq
        except OSError as exc:
            raise StoreError(f"could not append event to {path}: {exc}") from exc

    def read_events(self, run_id: str) -> list[dict[str, Any]]:
        """Read the valid prefix without observing an in-progress repair."""
        try:
            with open(self.events_path(run_id), "rb") as fh:
                fcntl.flock(fh, fcntl.LOCK_SH)
                return self._scan_events(fh)[0]
        except FileNotFoundError:
            return []

    def open_actions(self, run_id: str) -> list[dict[str, Any]]:
        """Actions that started but never finished (outcome unknown)."""
        started: dict[str, dict[str, Any]] = {}
        for event in self.read_events(run_id):
            step = event.get("step_id")
            kind = event.get("event")
            if not step:
                continue
            if kind == "action_started":
                started[step] = event
            elif kind == "action_finished" and event.get("outcome") not in ("action_failed", "outcome_unknown"):
                started.pop(step, None)
            elif kind == "action_resolved":
                started.pop(step, None)
        try:
            with open(self.events_path(run_id), "rb") as fh:
                fcntl.flock(fh, fcntl.LOCK_SH)
                _, offset = self._scan_events(fh)
                fh.seek(offset)
                if fh.read(1):
                    started["log_tail"] = {"step_id": "log_tail", "event": "log_corrupt",
                                           "outcome": "outcome_unknown"}
        except FileNotFoundError:
            pass
        for path in sorted(self.run_dir(run_id).glob("events-*.corrupt")):
            started[path.name] = {"step_id": path.name, "event": "log_recovered",
                                  "outcome": "outcome_unknown", "evidence": path.name}
        return list(started.values())
