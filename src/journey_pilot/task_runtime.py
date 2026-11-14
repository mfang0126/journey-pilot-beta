"""Shared execution context for reviewed, registered Python task scripts.

Not a sandbox, code loader, or replacement for Journey verification/promotion.
Task modules own business checks; this context owns execution and evidence.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime

from . import adapter, session
from .contracts import failure, success
from .runner import _device_lock, _jev_choice, ProviderUnavailable
from .store import atomic_write_json


class RouteBlocked(RuntimeError):
    """A route cannot continue safely; optionally carries a structured handoff."""
    def __init__(self, message, handoff=None):
        super().__init__(message)
        self.handoff = handoff


def select(nodes, predicate):
    hits = [n for n in nodes if predicate(n)]
    if len(hits) != 1:
        raise RouteBlocked(f"expected one grounded target, found {len(hits)}")
    return hits[0]


class TaskContext:
    def __init__(self, store, request, definition, chooser=None):
        self.store, self.request, self.definition = store, request, definition
        self.chooser = chooser or _jev_choice
        self.started = time.monotonic_ns()
        self.spans = []
        self.counts = {"observations": 0, "pixel_observations": 0, "actions": 0, "jev_calls": 0}
        self.nodes, self.observation_id, self.at = [], None, 0.0
        self.source = "tree"
        self.run_id: str | None = None
        self.base, self.foreground = {}, ""

    def timed(self, stage, fn):
        begin = time.monotonic_ns()
        stamp = datetime.now().astimezone().isoformat()
        try:
            return fn()
        finally:
            end = time.monotonic_ns()
            self.spans.append({"stage": stage, "started_at": stamp, "start_ns": begin,
                               "end_ns": end, "duration_ms": (end-begin)/1e6})

    def budget(self):
        if (time.monotonic_ns()-self.started)/1e9 > self.definition.get("budget_seconds", 90):
            raise RouteBlocked("task step-boundary budget reached")

    def require(self, condition, message):
        if not condition:
            raise RouteBlocked(message)

    def handoff(self, failed_condition, question):
        """What the next agent needs to continue here instead of restarting."""
        return {"stage": self.spans[-1]["stage"] if self.spans else "start",
                "run_id": self.run_id,
                "observation_id": self.observation_id,
                "source": self.source,
                "actions_done": self.counts["actions"],
                "observations": self.counts["observations"],
                "failed_condition": failed_condition,
                "question_for_agent": question,
                "note": "screen state preserved; do not restart the whole task; "
                        "refresh the observation and verify target identity before continuing"}

    def decide(self, name, options, goal):
        """Jev picks one caller-supplied safe option; it never invents actions."""
        last = ("not asked", None)
        for attempt in range(2):
            self.counts["jev_calls"] += 1
            choice, confidence = self.timed("decision.jev", lambda: self.chooser(
                list(options.values()), {"instructions": goal, "criteria": options, "timeout": 5}))
            last = (choice, confidence)
            if choice in options and (confidence or 0) >= .65:
                return choice
            if attempt == 0:
                self.observe()
        raise RouteBlocked(f"Jev could not decide {name} confidently",
                           handoff=self.handoff(f"decision needed: {name}",
                                                f"{goal} (Jev answered {last})"))

    def decide_target(self, name, predicate, fallback=None, goal=None, source=None):
        """Jev-first disambiguation: unique matches never reach here."""
        question = goal or f"Choose the {name}"
        last = ("not asked", None)
        for attempt in range(2):
            matches = [n for n in self.nodes if (fallback or predicate)(n)]
            if not matches:
                raise RouteBlocked(f"no safe Jev candidates for {name}",
                                   handoff=self.handoff(f"no candidate matches {name}",
                                                        "supply the right entry or a fresh observation"))
            signatures = {(n.get("text"), n.get("desc"), n.get("class"), n.get("x"), n.get("y"))
                          for n in matches}
            if len(signatures) == 1 and len(matches) > 1:
                # Byte-identical candidates cannot be told apart even by Jev;
                # the layout knowledge behind them is stale, so stop instead
                # of letting a coin flip drive a click.
                raise RouteBlocked(f"identical candidates for {name}; target knowledge may be stale",
                                   handoff=self.handoff(f"identical candidates: {name}",
                                                        "re-verify this page layout before continuing"))
            def _label(n):
                s = (n.get("desc") or "").strip() or (n.get("text") or "").strip()
                return s or (f"(blank {str(n.get('class', 'node')).split('.')[-1]}"
                             f" at x={n.get('x')}, y={n.get('y')})")
            choices = {f"n{i}": _label(n) for i, n in enumerate(matches)}
            choices["abstain"] = "Cannot choose safely; do not act"
            self.counts["jev_calls"] += 1
            choice, confidence = self.timed("decision.jev", lambda: self.chooser(
                list(choices.values()),
                {"instructions": question + "; abstain on ambiguity",
                 "criteria": choices, "timeout": 5}))
            last = (choice, confidence)
            if (isinstance(choice, str) and choice.startswith("n") and choice in choices
                    and (confidence or 0) >= .65):
                return matches[int(choice[1:])]
            if attempt == 0:
                self.observe(source or "tree")
        raise RouteBlocked(f"Jev could not choose a confident {name}",
                           handoff=self.handoff(f"ambiguous target: {name}",
                                                f"Jev answered {last}; inspect the live screen "
                                                f"and pick the {name} or restate the goal"))

    def observe(self, source="tree"):
        self.budget()
        out = self.timed("observe."+source, lambda: session.observe(self.store, {
            **self.base, "source": source, "persist_tree": self.request.get("persist_evidence") is True}))
        self.counts["observations"] += 1
        self.counts["pixel_observations"] += int(source == "pixels")
        self.require(out.status == "recorded", str(out.error))
        self.require(bool(out.data.get("nodes")), "empty observation is not an empty page")
        self.nodes = out.data["nodes"]
        self.observation_id = out.data["observation_id"]
        self.source, self.at = source, time.monotonic()
        self.budget()
        return self.nodes

    def _dispatch(self, kind, **args):
        self.budget()
        self.counts["actions"] += 1
        step_id = f"s_{self.counts['actions']}"
        out = self.timed("action."+kind, lambda: session.act(self.store, {
            **self.base, "request_id": step_id,
            "action": {"type": kind, "step_id": step_id, "effect_class": "navigation", **args}}))
        self.at = 0.0
        self.require(out.status == "recorded", str(out.error))

    def act(self, kind, **args):
        self.require(kind in ("launch_app", "scroll", "key"), "task action is not allowed")
        if kind == "key":
            self.require(args.get("key") == "back", "only back navigation is allowed")
        self.require("effect_class" not in args, "effect override is not allowed")
        if kind == "scroll":
            args.setdefault("traffic_class", "page")
        self._dispatch(kind, **args)

    def tap(self, predicate, name="target", *, source=None, fallback=None, goal=None):
        source = source or self.source
        freshness = self.definition.get("freshness_seconds", 8)
        if (self.request.get("mode") == "baseline" or source != self.source or
                time.monotonic()-self.at > freshness):
            self.observe(source)
        from .pacing import remaining_delay
        device = self.base["device_id"]
        delay = remaining_delay(self.store, device, {"type": "tap_xy"},
                                foreground=lambda: adapter.foreground_package(device))
        if time.monotonic() - self.at + delay > freshness:
            # Sit out the cooldown first so the coordinates are still fresh when it ends.
            self.timed("wait", lambda: time.sleep(delay))
            self.observe(source)
        try:
            # Unique grounded match executes directly: Jev decides only when
            # the screen is ambiguous or the selector found nothing.
            node = select(self.nodes, predicate)
        except RouteBlocked:
            if self.request.get("jev") is False:
                raise
            node = self.decide_target(name, predicate, fallback, goal, source)
        self.require(time.monotonic()-self.at <= freshness, "selection expired")
        self.require(node.get("x", 0) > 0 and node.get("y", 0) > 0, "target has no visible coordinates")
        if source == "pixels":
            self.require(node.get("confidence", 0) >= .8, "pixel target confidence is too low")
        self._dispatch("tap_xy", x=node["x"], y=node["y"], observation_id=self.observation_id,
                       locator={"text": node.get("text") or "", "description": node.get("desc") or ""})

    def wait_for(self, predicate, *, timeout=20, interval=.4):
        self.require(0 < timeout <= 30, "wait timeout must be within 0..30 seconds")
        deadline = time.monotonic()+timeout
        while not predicate(self.nodes):
            self.budget()
            self.require(time.monotonic() < deadline, "result did not become ready within wait budget")
            self.timed("wait", lambda: time.sleep(min(interval, max(0, deadline-time.monotonic()))))
            self.observe()
        return self.nodes

    def timing(self):
        return {"total_ms": (time.monotonic_ns()-self.started)/1e6, **self.counts, "spans": self.spans}


def execute(store, request, definition, task, *, chooser=None):
    device = request.get("device_id")
    if not isinstance(device, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", device):
        return failure("invalid_request", "task needs an explicit valid device_id")
    if request.get("mode", "fast") not in ("fast", "baseline"):
        return failure("invalid_request", "mode must be fast or baseline")
    ctx = TaskContext(store, request, definition, chooser)
    try:
        with _device_lock(store, device) as busy:
            if busy:
                return failure("device_busy", f"device {busy} is held by another run")
            opened = ctx.timed("session.start", lambda: session.start(store, {
                "device_id": device, "intent": definition["id"], "task_key": definition["id"]}))
            if opened.status != "ok":
                return opened
            ctx.run_id = opened.run_id
            ctx.base = {"run_id": ctx.run_id, "lease_token": opened.data["lease_token"], "device_id": device}
            ctx.foreground = opened.data.get("foreground") or ""
            result = None
            try:
                ctx.require(opened.data.get("ready"), "phone is not ready; inspect or unlock before retry")
                digest = hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()
                atomic_write_json(store.run_dir(ctx.run_id)/"route.json", {"route": definition, "sha256": digest})
                data = task(ctx)
                data.update(route_id=definition["id"], route_sha256=digest)
                data["content_retained"] = False
                result = success("completed", data, run_id=ctx.run_id)
            except ProviderUnavailable as exc:
                result = failure("provider_unavailable", str(exc),
                                 {"handoff": ctx.handoff(str(exc),
                                  "Jev is unavailable; decide the next step without it or retry later")},
                                 run_id=ctx.run_id)
            except RouteBlocked as exc:
                result = failure("precondition_mismatch", str(exc),
                                 {"handoff": exc.handoff or ctx.handoff(str(exc),
                                  "inspect the live screen and choose the next safe step")},
                                 run_id=ctx.run_id)
            except Exception as exc:
                result = failure("outcome_unknown", str(exc)[:200], run_id=ctx.run_id)
            finally:
                closed = ctx.timed("session.finish", lambda: session.finish(store, ctx.base))
                if closed.status != "recorded":
                    code = (closed.error or {}).get("code")
                    if code in ("cancelled", "platform_blocked", "outcome_unknown"):
                        result = closed
                    else:
                        result = failure("outcome_unknown", "session close failed; inspect before retry", run_id=ctx.run_id)
            result.data["timing"] = ctx.timing()
            store.append_event(ctx.run_id, {"event": "task_finished", "status": result.status,
                "outcome": (result.error or {}).get("code"), "ok": result.status == "completed"})
            # Persist the result envelope without the session credential.
            from .privacy import result_record
            record = result_record(result)
            path = store.run_dir(ctx.run_id)/"task-result.json"
            atomic_write_json(path, record)
            path.chmod(0o600)
            return result
    except Exception as exc:
        return failure("outcome_unknown", str(exc)[:200], run_id=ctx.run_id)
